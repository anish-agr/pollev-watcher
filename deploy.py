"""Copy your .env settings to this GitHub repository and set the class schedule.

    python deploy.py           # push settings, update the cron schedule
    python deploy.py --test    # same, then send a test alert from GitHub

Needs the GitHub CLI (https://cli.github.com), logged in with `gh auth login`.
Blank values in .env are skipped, so secrets you set some other way stay as they are.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from watcher import cron_lines, load_dotenv

# POLLEV_USER is a secret so GitHub masks the presenter's name in run logs.
SECRETS = ["POLLEV_USER", "NTFY_TOPIC", "PAGERDUTY_ROUTING_KEY", "ALERT_EMAIL_TO", "SMTP_USER", "SMTP_PASSWORD"]
VARIABLES = ["TIMEZONE", "CLASS_DAYS", "CLASS_START", "CLASS_END", "SKIP_DATES", "NTFY_SERVER", "SMTP_HOST", "SMTP_PORT"]
WORKFLOW = Path(".github/workflows/watch.yml")


def gh(*args, value=None):
    return subprocess.run(["gh", *args], input=value, text=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true", help="send a test alert from GitHub afterwards")
    a = ap.parse_args()

    os.chdir(Path(__file__).parent)
    if not Path(".env").exists():
        sys.exit("No .env file. Copy .env.example to .env and fill it in first.")
    load_dotenv()
    if gh("auth", "status").returncode != 0:
        sys.exit("The GitHub CLI isn't logged in. Run: gh auth login")
    repo = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").stdout.strip()
    if not repo:
        sys.exit("Couldn't find the GitHub repository for this folder.")
    print(f"Repository: {repo}")

    for name in ("POLLEV_USER", "NTFY_TOPIC"):
        if not os.environ.get(name):
            sys.exit(f"{name} is empty in .env.")

    for kind, names in (("secret", SECRETS), ("variable", VARIABLES)):
        for name in names:
            value = os.environ.get(name, "").strip()
            if not value:
                continue
            r = gh(kind, "set", name, "--body", value) if kind == "variable" else gh(kind, "set", name, value=value)
            print(f"  {kind:8} {name:22} {'set' if r.returncode == 0 else 'FAILED: ' + r.stderr.strip()}")

    days, start, tz = (os.environ.get(k, "").strip() for k in ("CLASS_DAYS", "CLASS_START", "TIMEZONE"))
    if not (days and start and tz):
        print("\nCLASS_DAYS, CLASS_START or TIMEZONE is empty, so the automatic schedule stays off.")
    else:
        lines = cron_lines(days, start, ZoneInfo(tz))
        text = WORKFLOW.read_text(encoding="utf-8")
        updated = re.sub(r"(  schedule:\n)(    - cron: .*\n)+", lambda m: m.group(1) + "\n".join(lines) + "\n", text)
        if updated == text:
            print("\nSchedule already up to date.")
        else:
            WORKFLOW.write_text(updated, encoding="utf-8", newline="\n")
            subprocess.run(["git", "add", str(WORKFLOW)], check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: update class schedule"], check=True)
            subprocess.run(["git", "push", "-q"], check=True)
            print(f"\nSchedule updated and pushed ({days} at {start} {tz}).")

    if a.test:
        r = gh("workflow", "run", "watch.yml", "-f", "test_alerts=true", "-f", "minutes=1")
        print("\nTest alert started; it should arrive in about 30 seconds." if r.returncode == 0
              else f"\nCouldn't start the test run: {r.stderr.strip()}")


if __name__ == "__main__":
    main()
