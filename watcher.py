"""Watch a Poll Everywhere participant page and alert when a poll goes live.

The participant page keeps a websocket open and Poll Everywhere pushes each new
activity into it, so the watcher keeps one headless browser tab open and waits
on DOM changes instead of reloading. A real browser is needed because plain
HTTP requests get stopped by the site's AWS WAF JavaScript check.
"""

import argparse
import json
import os
import platform
import smtplib
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

RELOAD_EVERY = 10 * 60   # seconds between safety reloads while waiting
MISSING_GRACE = 120      # seconds without a readable page before sending a warning
REPEAT_COOLDOWN = 45     # minimum seconds between alerts for back-to-back questions
MAX_RESTARTS = 10        # browser relaunches allowed per run
# GitHub can start scheduled runs anywhere from a few minutes to hours late. The
# schedule fires every 30 minutes from 10 hours to 35 minutes before class. The
# first run to land within MAX_EARLY minutes of the watch window waits for it,
# and the rest exit in under a minute.
CRON_OFFSETS = tuple(range(35, 10 * 60 + 1, 30))
MAX_EARLY = 40
SNAPSHOT_DIR = Path("snapshots")

# Installed on every page load. Any loaded PollEv page without the waiting
# screen counts as a live poll, whether the poll arrives inside the presenter
# frame or replaces the whole page layout.
STATE_JS = r"""
window.__pvState = (user) => {
  const main = document.querySelector('main');
  if (!main || !/pollev\.com|polleverywhere\.com/.test(location.host)) return {status: 'missing'};
  if (document.querySelector('img[src*="waiting-screen"]') || /Waiting for .*presentation to begin/i.test(main.innerText || ''))
    return {status: 'waiting'};
  const frame = document.getElementById('participate_presenter_' + user.toLowerCase())
             || document.querySelector('turbo-frame[id^="participate_presenter_"]');
  const scope = frame || main;
  const text = (scope.innerText || '').trim();
  if (!text) return {status: 'loading'};
  const skip = /^(Introduce yourself|Welcome to|Responding as|Skip to)/i;
  const heads = [...scope.querySelectorAll('h1,h2,h3,legend,[role=heading]')]
    .map(h => (h.innerText || '').trim()).filter(t => t && !skip.test(t));
  const sig = heads[0] || text.split('\n').map(s => s.trim()).filter(s => s && !skip.test(s))[0] || '';
  return {status: 'active', sig: sig.slice(0, 200), text: (heads[0] ? text.slice(text.indexOf(heads[0])) : text).slice(0, 400)};
};
"""

# Resolves when the page leaves `st` (or the live question changes), or after `ms`.
WAIT_JS = r"""
([u, st, sig, ms]) => new Promise(res => {
  const changed = () => {
    const s = window.__pvState ? window.__pvState(u) : {status: 'missing'};
    return s.status !== st || (st === 'active' && s.sig !== sig);
  };
  if (changed()) return res(true);
  const obs = new MutationObserver(() => { if (changed()) { obs.disconnect(); clearTimeout(t); res(true); } });
  obs.observe(document, {subtree: true, childList: true, characterData: true});
  const t = setTimeout(() => { obs.disconnect(); res(false); }, ms);
})
"""

# --simulate pushes fake polls through Turbo.renderStreamMessage, the handler the
# live websocket feeds, so the page changes the way it does for a real poll.
SIM_JS = r"""
([user, step]) => {
  const fid = 'participate_presenter_' + user.toLowerCase();
  const poll = (q, opts) => `<div class="flex flex-col px-4 py-4"><h1 class="text-xl font-semibold">${q}</h1>
    <form method="post"><ul role="list">${opts.map(o => `<li><button type="submit" class="w-full">${o}</button></li>`).join('')}</ul></form></div>`;
  const stream = (action, target, html) =>
    Turbo.renderStreamMessage(`<turbo-stream action="${action}" target="${target}"><template>${html}</template></turbo-stream>`);
  const tab = document.getElementById('active_tab_content');
  window.__pvSavedTab = window.__pvSavedTab || tab.innerHTML;
  if (step === 0) stream('replace', fid, `<turbo-frame id="${fid}">${poll('SIMULATED: Which is the derivative of sin(x)?', ['cos(x)', '-cos(x)', 'tan(x)'])}</turbo-frame>`);
  if (step === 1) stream('update', 'active_tab_content', window.__pvSavedTab);
  if (step === 2) stream('update', 'active_tab_content', poll('SIMULATED: Is the series sum 1/n convergent?', ['Yes', 'No']));
  if (step === 3) stream('update', 'active_tab_content', poll('SIMULATED: What is the limit of sin(x)/x as x goes to 0?', ['0', '1', 'Does not exist']));
  if (step === 4) stream('update', 'active_tab_content', window.__pvSavedTab);
}
"""
# (seconds after the previous step, what the step simulates)
SIM_STEPS = [
    (0, "poll arrives in the presenter frame"),
    (20, "poll closes, waiting screen returns"),
    (20, "poll arrives as a whole-page swap"),
    (REPEAT_COOLDOWN + 10, "next question replaces the current one directly"),
    (20, "poll closes, waiting screen returns"),
]

BLOCKED_HOSTS = ("plausible.io", "production-cutlass.polleverywhere.com", "googletagmanager", "google-analytics")

TZ = timezone.utc


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def first_line(err):
    return (getattr(err, "message", None) or str(err)).splitlines()[0]


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def send_ntfy(title, body, url, priority):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": str(priority)}
    if url:
        headers["Click"] = url
    post(f"{server}/{topic}", body.encode("utf-8"), headers)


def pagerduty_event(event):
    event["routing_key"] = os.environ["PAGERDUTY_ROUTING_KEY"]
    post("https://events.pagerduty.com/v2/enqueue", json.dumps(event).encode(), {"Content-Type": "application/json"})


def send_pagerduty(title, body, url, dedup_key):
    if not os.environ.get("PAGERDUTY_ROUTING_KEY"):
        return
    pagerduty_event({
        "event_action": "trigger",
        "dedup_key": dedup_key,
        "payload": {"summary": title, "source": "pollev-watcher", "severity": "critical",
                    "custom_details": {"poll": body, "url": url}},
        "links": [{"href": url, "text": "Open poll"}],
    })


def resolve_pagerduty(dedup_key):
    """Close the incident so PagerDuty stops calling once the poll is over."""
    if not (dedup_key and os.environ.get("PAGERDUTY_ROUTING_KEY")):
        return
    try:
        pagerduty_event({"event_action": "resolve", "dedup_key": dedup_key})
        log("PagerDuty incident resolved")
    except Exception as e:
        log(f"  pagerduty resolve failed: {e}")


def send_email(title, body, url):
    to, user, pw = os.environ.get("ALERT_EMAIL_TO"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not (to and user and pw):
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, user, to
    msg.set_content(f"{body}\n\n{url}")
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT") or 465), timeout=20) as s:
        s.login(user, pw)
        s.send_message(msg)


def play_sound():
    def run():
        for _ in range(6):
            if platform.system() == "Windows":
                import winsound
                winsound.Beep(1200, 300)
                winsound.Beep(800, 300)
            else:
                print("\a", end="", flush=True)
                time.sleep(0.6)
    threading.Thread(target=run, daemon=True).start()


def configured_channels():
    out = []
    if os.environ.get("NTFY_TOPIC"):
        out.append("ntfy")
    if os.environ.get("PAGERDUTY_ROUTING_KEY"):
        out.append("pagerduty")
    if os.environ.get("ALERT_EMAIL_TO") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        out.append("email")
    return out


def alert(title, body, url, level="urgent", sound=False):
    """Send to every configured channel in parallel and return the PagerDuty dedup key.

    level: "urgent" (push at max priority, PagerDuty, email), "test" (normal push
    only, so simulations never call you) or "info" (quiet push only).
    """
    log(f"ALERT: {title} | {body[:120]!r}")
    dedup_key = f"pollev-{int(time.time())}"
    if sound:
        play_sound()
    priority = {"urgent": 5, "test": 3, "info": 2}[level]
    senders = [("ntfy", lambda: send_ntfy(title, body, url, priority))]
    if level == "urgent":
        senders += [("pagerduty", lambda: send_pagerduty(title, body, url, dedup_key)),
                    ("email", lambda: send_email(title, body, url))]

    def run(name, fn):
        try:
            fn()
        except Exception as e:
            log(f"  {name} failed: {e}")
    threads = [threading.Thread(target=run, args=s) for s in senders]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    return dedup_key if level == "urgent" else None


def parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def parse_days(s):
    return [d.strip().lower()[:3] for d in s.split(",") if d.strip()]


def resolve_window(args):
    """Return (start, end) as aware datetimes, or None if this run should exit now."""
    now = datetime.now(TZ)
    if args.minutes:
        return now, now + timedelta(minutes=args.minutes)
    if not (args.start and args.end):
        sys.exit("Give --minutes, or --start and --end (CLASS_START / CLASS_END).")
    if args.days and now.strftime("%a").lower() not in parse_days(args.days):
        log(f"No class on {now.strftime('%A')}; exiting.")
        return None
    if args.skip and now.strftime("%Y-%m-%d") in [d.strip() for d in args.skip.split(",")]:
        log(f"{now.strftime('%Y-%m-%d')} is in SKIP_DATES; exiting.")
        return None
    start = now.replace(hour=parse_hhmm(args.start)[0], minute=parse_hhmm(args.start)[1], second=0, microsecond=0)
    end = now.replace(hour=parse_hhmm(args.end)[0], minute=parse_hhmm(args.end)[1], second=0, microsecond=0)
    start -= timedelta(minutes=args.lead)
    if now >= end:
        log("Class is already over; exiting.")
        return None
    if start - now > timedelta(minutes=args.max_early):
        log(f"Started {int((start - now).total_seconds() // 60)} min before the watch window; exiting.")
        return None
    return max(start, now), end


def cron_lines(days, start, tz):
    """GitHub cron is UTC. Return cron lines that fire at each CRON_OFFSETS time
    before class, covering both sides of daylight saving time."""
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    h, m = parse_hhmm(start)
    dows = {}
    for ref in (datetime(2026, 1, 5), datetime(2026, 7, 6)):  # a Monday in winter and in summer
        for d in parse_days(days):
            local = (ref + timedelta(days=names.index(d))).replace(hour=h, minute=m, tzinfo=tz)
            for offset in CRON_OFFSETS:
                fire = (local - timedelta(minutes=offset)).astimezone(timezone.utc)
                dows.setdefault((fire.minute, fire.hour), set()).add((fire.weekday() + 1) % 7)  # cron: 0 = Sunday
    hours = {}
    for (minute, hour), ds in dows.items():
        hours.setdefault((minute, tuple(sorted(ds))), []).append(hour)
    return [f'    - cron: "{mi} {",".join(map(str, sorted(hs)))} * * {",".join(map(str, ds))}"'
            for (mi, ds), hs in sorted(hours.items(), key=lambda kv: (min(kv[1]), kv[0][0]))]


def save_snapshot(page, label):
    try:
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        stem = SNAPSHOT_DIR / f"{datetime.now(TZ).strftime('%H%M%S')}-{label}"
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        log(f"saved snapshot {stem}")
    except Exception as e:
        log(f"  snapshot failed: {e}")


class Watcher:
    def __init__(self, args):
        self.args = args
        self.user = args.user
        self.url = f"https://pollev.com/{self.user}"
        self.open_sockets = 0
        self.pd_key = None

    def state(self, page):
        try:
            return page.evaluate("u => window.__pvState ? window.__pvState(u) : {status: 'missing'}", self.user)
        except PlaywrightError:
            return {"status": "missing"}

    def wait_change(self, page, from_status, sig, timeout_s):
        """Block until the page leaves `from_status` or the live question changes.
        A MutationObserver does the waiting, so this costs nothing while idle."""
        try:
            page.evaluate(WAIT_JS, [self.user, from_status, sig, int(max(1, timeout_s) * 1000)])
        except PlaywrightError as e:
            log(f"wait interrupted: {first_line(e)}")
            time.sleep(2)

    def track_socket(self, ws):
        if "anycable" not in ws.url:
            return
        self.open_sockets += 1
        log("live connection open")

        def closed(_=None):
            self.open_sockets -= 1
            log("live connection closed")
        ws.on("close", closed)

    def load(self, page):
        self.open_sockets = 0
        try:
            page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            # The first response is a WAF challenge that reloads the page by itself.
            page.wait_for_selector("main", timeout=45000)
        except PlaywrightError as e:
            log(f"load problem: {first_line(e)}")

    def run(self, end, first_run):
        a = self.args
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not a.headed, channel=a.channel or None)
            ctx = browser.new_context(viewport={"width": 1000, "height": 800})
            ctx.add_init_script(STATE_JS)

            def route(r):
                req = r.request
                if req.resource_type in ("image", "media", "font") or any(h in req.url for h in BLOCKED_HOSTS):
                    return r.abort()
                return r.continue_()
            ctx.route("**/*", route)

            page = ctx.new_page()
            page.on("websocket", self.track_socket)
            self.load(page)
            last_load = time.time()

            s = self.state(page)
            log(f"page state: {s['status']}")
            if first_run:
                alert("PollEv watcher running",
                      f"Watching {self.user} until {end.strftime('%I:%M %p').lstrip('0')}. "
                      f"Page: {s['status']}. Alerts: {', '.join(configured_channels()) or 'none'}.",
                      self.url, level="info")

            sim_at = time.time() + (a.simulate or 0)
            sim_step = 0
            alerted_sig = None
            last_alert = 0.0
            missing_since = None
            warned = False

            while datetime.now(TZ) < end:
                remaining = (end - datetime.now(TZ)).total_seconds()
                simulating = a.simulate is not None and sim_step < len(SIM_STEPS)
                s = self.state(page)
                st = s["status"]

                if simulating and time.time() >= sim_at and st in ("waiting", "active"):
                    log(f"simulate {sim_step + 1}/{len(SIM_STEPS)}: {SIM_STEPS[sim_step][1]}")
                    page.evaluate(SIM_JS, [self.user, sim_step])
                    sim_step += 1
                    if sim_step < len(SIM_STEPS):
                        sim_at = time.time() + SIM_STEPS[sim_step][0]
                    continue

                if st == "active":
                    missing_since = None
                    if s["sig"] != alerted_sig and (alerted_sig is None or time.time() - last_alert > REPEAT_COOLDOWN):
                        page.wait_for_timeout(1500)  # ignore a page that only flickers mid-update
                        if self.state(page)["status"] != "active":
                            continue
                        resolve_pagerduty(self.pd_key)
                        test = a.simulate is not None
                        self.pd_key = alert(("TEST " if test else "") + "POLL IS LIVE", s["text"], self.url,
                                            level="test" if test else "urgent", sound=a.sound)
                        alerted_sig, last_alert = s["sig"], time.time()
                        save_snapshot(page, "poll")
                    timeout = min(remaining, 60)
                    if simulating:
                        timeout = min(timeout, max(1, sim_at - time.time()))
                    self.wait_change(page, "active", s["sig"], timeout)

                elif st == "waiting":
                    missing_since = None
                    if alerted_sig:
                        log("poll closed; re-armed")
                        resolve_pagerduty(self.pd_key)
                        self.pd_key = None
                    alerted_sig = None
                    connection_down = self.open_sockets <= 0 and time.time() - last_load > 60
                    if time.time() - last_load > RELOAD_EVERY or connection_down:
                        log("reloading" + (" (live connection down)" if connection_down else ""))
                        self.load(page)
                        last_load = time.time()
                        continue
                    timeout = min(remaining, RELOAD_EVERY - (time.time() - last_load), 60)
                    if simulating:
                        timeout = min(timeout, max(1, sim_at - time.time()))
                    self.wait_change(page, "waiting", None, timeout)

                else:  # missing or loading
                    missing_since = missing_since or time.time()
                    gone = time.time() - missing_since
                    if gone > MISSING_GRACE and not warned:
                        save_snapshot(page, "missing")
                        alert("PollEv watcher can't see the page",
                              f"No readable page for {int(gone)} s. Still retrying. Last URL: {page.url}",
                              self.url, level="info")
                        warned = True
                    if gone > 20 and time.time() - last_load > 20:
                        log(f"page not ready ({st}); reloading")
                        self.load(page)
                        last_load = time.time()
                    else:
                        page.wait_for_timeout(3000)
                    if warned and self.state(page)["status"] in ("waiting", "active"):
                        alert("PollEv watcher recovered", "The page is readable again.", self.url, level="info")
                        warned = False

            browser.close()


def main():
    global TZ
    load_dotenv()
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", default=env("POLLEV_USER"), help="presenter username, the part after pollev.com/")
    ap.add_argument("--minutes", type=float, help="watch for this many minutes starting now")
    ap.add_argument("--start", default=env("CLASS_START"), help="class start, HH:MM (24-hour) in --tz")
    ap.add_argument("--end", default=env("CLASS_END"), help="class end, HH:MM (24-hour) in --tz")
    ap.add_argument("--days", default=env("CLASS_DAYS"), help="class days, e.g. mon,wed,fri")
    ap.add_argument("--skip", default=env("SKIP_DATES"), help="dates with no class, e.g. 2026-11-11,2026-11-26")
    ap.add_argument("--tz", default=env("TIMEZONE"), help="IANA time zone, e.g. America/New_York (default: this computer's)")
    ap.add_argument("--lead", type=int, default=5, help="start watching this many minutes before class (default 5)")
    ap.add_argument("--max-early", type=int, default=MAX_EARLY,
                    help=f"exit if launched more than this many minutes early (default {MAX_EARLY})")
    ap.add_argument("--channel", default=env("BROWSER_CHANNEL") or ("msedge" if platform.system() == "Windows" else ""),
                    help="browser to use: msedge, chrome, or blank for Playwright's own Chromium")
    ap.add_argument("--sound", action="store_true", help="also beep on this computer")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--simulate", type=int, metavar="SECONDS",
                    help="after SECONDS, push five fake poll events through the real page (no PagerDuty or email)")
    ap.add_argument("--test-alerts", action="store_true", help="send one urgent test alert to every channel and exit")
    ap.add_argument("--print-cron", action="store_true", help="print the GitHub Actions cron lines for your schedule")
    a = ap.parse_args()
    a.user = (a.user or "").strip().strip("/").removeprefix("https://").removeprefix("pollev.com/")
    if a.tz:
        TZ = ZoneInfo(a.tz)
    else:
        TZ = datetime.now().astimezone().tzinfo

    if a.print_cron:
        if not (a.days and a.start and a.tz):
            sys.exit("--print-cron needs --days, --start and --tz (or CLASS_DAYS, CLASS_START, TIMEZONE).")
        print("  schedule:")
        print("\n".join(cron_lines(a.days, a.start, TZ)))
        return

    if not a.user:
        sys.exit("Set --user or POLLEV_USER to the presenter's username.")
    log(f"alert channels: {', '.join(configured_channels()) or 'none (console only)'}")

    if a.test_alerts:
        key = alert("PollEv watcher test", f"Test alert for pollev.com/{a.user}. If you got this, alerts work.",
                    f"https://pollev.com/{a.user}", sound=a.sound)
        if env("PAGERDUTY_ROUTING_KEY"):
            log("resolving the test incident in 60 s")
            time.sleep(60)
            resolve_pagerduty(key)
        return

    window = resolve_window(a)
    if not window:
        return
    start, end = window
    wait = (start - datetime.now(TZ)).total_seconds()
    if wait > 0:
        log(f"sleeping {int(wait // 60)} min until {start.strftime('%H:%M')}")
        time.sleep(wait)
    log(f"watching pollev.com/{a.user} until {end.strftime('%H:%M %Z')}")
    if env("WATCH_END_FILE"):  # lets the workflow restart a failed job for the time that's left
        Path(env("WATCH_END_FILE")).write_text(str(int(end.timestamp())))

    watcher = Watcher(a)
    for attempt in range(MAX_RESTARTS + 1):
        try:
            watcher.run(end, first_run=attempt == 0)
            break
        except Exception as e:
            log(f"watcher crashed: {first_line(e)}")
            if datetime.now(TZ) >= end or attempt == MAX_RESTARTS:
                resolve_pagerduty(watcher.pd_key)
                raise
            if attempt == 0:
                alert("PollEv watcher restarting", f"The browser crashed ({first_line(e)}). Relaunching.",
                      watcher.url, level="info")
            time.sleep(5)
    resolve_pagerduty(watcher.pd_key)
    log("watch window over")


if __name__ == "__main__":
    main()
