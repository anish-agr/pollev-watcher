# pollev-watcher

Get a phone call or push notification when a poll opens on a Poll Everywhere presenter page, so you don't miss in-class polls on days you can't be there. It runs on GitHub Actions, so your computer can be off.

Check that your course allows this before you use it.

## Detection

The participant page at `pollev.com/<presenter>` shows a "Waiting for ... to begin" screen until the presenter opens an activity. Poll Everywhere then pushes the activity to the page over a websocket. The watcher keeps that page open in headless Chrome and alerts when the waiting screen goes away. Between polls it sits idle and reloads only every 10 minutes or when the websocket drops.

It needs a real browser because Poll Everywhere is behind an AWS bot check that blocks plain HTTP requests.

When a poll opens, it sends at once:

- an urgent push through [ntfy](https://ntfy.sh)
- a PagerDuty incident, which can phone you (optional)
- an email (optional)

When the poll closes, it resolves the PagerDuty incident so the calls stop, and waits for the next poll. Each alert also saves the page's HTML and a screenshot as a run artifact for 14 days, which helps if Poll Everywhere changes its page.

## Setup

This takes about 10 minutes and needs a GitHub account and a phone.

1. Click **Use this template**, then **Create a new repository**. Private is fine.
2. Install the ntfy app ([iOS](https://apps.apple.com/app/ntfy/id1625396347), [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)). Tap **+**, enter a topic name nobody would guess (for example `pollev-` plus 16 random letters and digits) and subscribe. Anyone who knows the name can read your alerts.
3. Put your settings into GitHub, either from the command line or on the website (below).
4. Test it. Open the **Actions** tab (enable workflows if it asks), pick **Watch PollEv** and click **Run workflow** twice:
   - once with **test_alerts** checked. You should get a push within about 30 seconds, plus a call if PagerDuty is set up.
   - once with **simulate** set to `15` and **minutes** set to `3`. This pushes fake polls through the real class page, and you should get three "TEST POLL IS LIVE" pushes over about two minutes. Simulated polls never trigger PagerDuty or email.

### From the command line

Needs Python 3.10+ and the [GitHub CLI](https://cli.github.com) (`gh auth login`).

```
git clone https://github.com/<you>/<your-repo>.git
cd <your-repo>
pip install -r requirements.txt
cp .env.example .env        # fill it in
python deploy.py --test
```

`deploy.py` copies `.env` into the repository's secrets and variables, writes the cron schedule into the workflow and pushes it, then sends a test alert. Run it again whenever you change `.env`.

### On the website

Open **Settings > Secrets and variables > Actions** in your repository.

Under **Secrets**, add:

| Secret | Value |
|---|---|
| `POLLEV_USER` | the presenter's username, the part after `pollev.com/` |
| `NTFY_TOPIC` | your ntfy topic |
| `PAGERDUTY_ROUTING_KEY` | optional, see [Alert options](#alert-options) |
| `ALERT_EMAIL_TO`, `SMTP_USER`, `SMTP_PASSWORD` | optional, see [Alert options](#alert-options) |

`POLLEV_USER` is a secret so GitHub hides the presenter's name in run logs.

Under **Variables**, add:

| Variable | Example | Meaning |
|---|---|---|
| `TIMEZONE` | `America/New_York` | your class's [time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) |
| `CLASS_DAYS` | `mon,wed,fri` | days the class meets |
| `CLASS_START` | `13:30` | start time, 24-hour |
| `CLASS_END` | `14:20` | end time, 24-hour |
| `SKIP_DATES` | `2026-11-11,2026-11-26` | optional, days with no class |

Then run the workflow once by hand. Its summary page lists cron lines for your schedule. Edit `.github/workflows/watch.yml` on GitHub and replace the `- cron:` lines under `schedule:` with them.

## Daily use

Once the schedule is set, there is nothing to do. A quiet "PollEv watcher running" push tells you a watch began, and it runs from 5 minutes before class until `CLASS_END`.

GitHub can start a scheduled run anywhere from a few minutes to hours late, so the schedule fires every 30 minutes from 10 hours to 35 minutes before class. The first run to land within 40 minutes of class waits for it, and the rest exit in under a minute. Runs you start by hand begin right away.

To watch at another time, run the workflow by hand and set **minutes**. This works from the GitHub mobile app. With the [GitHub CLI](https://cli.github.com), run `gh workflow run watch.yml -f minutes=60` in your clone, or on Windows double-click `pollev-watcher.bat`.

If the browser crashes, the watcher relaunches it. If the whole run fails, it starts a replacement run for the time that's left, up to three times in a row. You get a push for each of these, and also if the watcher can't read the page for two minutes. If none of those arrive, it is watching.

The **user** field on a manual run is for testing with your own presenter account. It shows on the run page, so leave it blank for your real class.

To stop a run, open it in the Actions tab and click **Cancel workflow**.

## Alert options

| Channel | Setup |
|---|---|
| ntfy push | Free, no account. Set the `NTFY_TOPIC` secret. To use your own ntfy server, set the `NTFY_SERVER` variable. |
| PagerDuty call | In PagerDuty, open a service, add an **Events API V2** integration and save its Integration Key as `PAGERDUTY_ROUTING_KEY`. Set your notification rules to phone you. The watcher resolves the incident when the poll closes, so the calls stop then. |
| Email | Set `ALERT_EMAIL_TO`, `SMTP_USER` and `SMTP_PASSWORD`. For Gmail, `SMTP_PASSWORD` is an [app password](https://myaccount.google.com/apppasswords), which needs 2-Step Verification. Other providers also need `SMTP_HOST` and `SMTP_PORT` (SSL) variables. |

## Running on your own computer

```
pip install -r requirements.txt
cp .env.example .env        # then fill it in
python watcher.py --minutes 60 --sound
```

On Windows it uses the installed Microsoft Edge. On macOS or Linux, add `--channel chrome` if Chrome is installed, or run `python -m playwright install chromium` once.

| Command | Does |
|---|---|
| `python watcher.py` | watches on the `CLASS_*` schedule |
| `python watcher.py --minutes 60` | watches for 60 minutes from now |
| `python watcher.py --test-alerts` | sends one alert to every channel |
| `python watcher.py --minutes 3 --simulate 15` | runs the fake-poll test |
| `python watcher.py --print-cron` | prints cron lines for your schedule |
| `python watcher.py --headed` | shows the browser window |

## Limits

- Detection depends on Poll Everywhere's waiting screen. If they redesign the participant page, it can break. The saved snapshots show what the page looked like.
- It joins as a guest, so presentations that require participants to sign in won't work.
- If GitHub starts every scheduled run more than 9.5 hours late, the watch begins partway through class. Start a run by hand on days it matters.
- GitHub's terms for Actions expect it to be used for work on the repository's software. A watcher like this falls outside that, so GitHub could disable the workflow.

## License

MIT
