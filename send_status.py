"""
Daily status email for the Lake O'Hara reservation bot.

The old version of this script only checked whether the workflow exited 0.
That is why it mailed "All checks passed. The bot is running normally." every
morning for seven months while the checker was querying the wrong resources
with a condition that could never be true.

This version proves the bot can actually see availability before it claims to
be healthy:

  1. Canary  -- read a backcountry zone that is known to be open and confirm
                the detector reports it as open. This exercises the exact code
                path Lake O'Hara uses.
  2. Live read -- read Lake O'Hara itself and report how many nights are open
                out of how many, so "0 openings" is visibly a *measurement*
                rather than an absence of output.
  3. Cadence -- how many workflow runs actually fired in the last 24h, since
                GitHub silently throttles scheduled workflows.

If any of those fail, the subject line says PROBLEM and the script exits
non-zero so the Actions run goes red.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import check_availability as bot


def count_recent_runs():
    """(count, note) for check-availability runs created in the last 24 hours."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        return None, "not available (no GITHUB_TOKEN)"

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"check-availability.yml/runs?per_page=100&created=%3E{since}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return None, f"could not be read ({e})"

    runs = data.get("workflow_runs", [])
    failed = sum(1 for r in runs if r.get("conclusion") == "failure")
    note = f"{len(runs)} run(s)"
    if failed:
        note += f", {failed} failed"
    return len(runs), note


def build_status_email():
    """Returns (subject, body, healthy)."""
    now = datetime.now(timezone.utc)
    problems = []
    lines = [
        "Lake O'Hara Backcountry Bot - Daily Status",
        "=" * 44,
        "",
        f"Report time: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Watching:    {bot.CAMPGROUND_NAME}",
        f"Nights:      {bot.FIRST_NIGHT} .. {bot.LAST_NIGHT}",
        "",
    ]

    # 1. Canary
    canary_ok, canary_message = bot.run_canary()
    lines.append(f"[{'PASS' if canary_ok else 'FAIL'}] Detector self-check")
    lines.append(f"       {canary_message}")
    if not canary_ok:
        problems.append("the detector cannot see known-available inventory")
    lines.append("")

    # 2. Live read of Lake O'Hara
    try:
        nights = bot.fetch_nights(
            bot.RESOURCE_LOCATION_ID, bot.ROOT_MAP_ID, bot.CAMPGROUND_RESOURCE_ID,
            bot.FIRST_NIGHT, bot.LAST_NIGHT)
        open_nights = sorted(d for d, s in nights.items() if bot.night_is_open(s))
        windows = bot.find_windows(nights, bot.MIN_NIGHTS, bot.ALLOWED_DAYS)
        lines.append("[PASS] Live read of Lake O'Hara")
        lines.append(f"       {len(open_nights)} of {len(nights)} nights currently open")
        if open_nights:
            lines.append(f"       Open nights: {', '.join(open_nights)}")
            for w in windows:
                lines.append(f"       -> {w['check_in']} to {w['check_out']} "
                             f"({w['nights']} night(s))")
        else:
            lines.append("       Campground is full. This is a measurement, not a guess.")
    except bot.ApiError as e:
        lines.append("[FAIL] Live read of Lake O'Hara")
        lines.append(f"       {e}")
        problems.append("the Lake O'Hara read failed")
    lines.append("")

    # 3. Cadence
    count, note = count_recent_runs()
    if count is None:
        lines.append(f"[ -- ] Run history {note}")
    else:
        checks = count * 35  # ~70 minutes of polling at 2-minute intervals
        healthy_cadence = count >= 12
        lines.append(f"[{'PASS' if healthy_cadence else 'WARN'}] Run cadence (last 24h)")
        lines.append(f"       {note}, roughly {checks} availability checks")
        if not healthy_cadence:
            lines.append("       Fewer runs than expected -- GitHub may be throttling "
                         "the schedule.")
            problems.append("the workflow is not running as often as expected")
    lines.append("")

    if problems:
        lines.append("PROBLEM: " + "; ".join(problems) + ".")
        lines.append("Until this is resolved, silence from this bot means nothing.")
        subject = "Lake O'Hara Bot - PROBLEM - not detecting availability"
    else:
        lines.append("The bot is working. It has been verified end to end against")
        lines.append("live inventory, not just checked for a zero exit code.")
        lines.append("You will get a separate email the moment a spot opens.")
        subject = "Lake O'Hara Bot - OK - watching for cancellations"

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        lines += ["", f"Actions: https://github.com/{repo}/actions"]
    lines += ["", "-- ", "Lake O'Hara Reservation Bot"]

    return subject, "\n".join(lines), not problems


def main():
    subject, body, healthy = build_status_email()
    print(body)
    print()
    bot.send_email(subject, body)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
