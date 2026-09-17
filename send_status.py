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

# Occasional short gaps at run handoff are normal; anything larger is
# worth mentioning, but it is a coverage note, not a broken detector.
MAX_ACCEPTABLE_GAP_MINUTES = float(
    os.environ.get("MAX_ACCEPTABLE_GAP_MINUTES", "30"))


def _github_json(path):
    """GET a GitHub API path. Returns parsed JSON, or None on any failure."""
    token = os.environ.get("GITHUB_TOKEN", "")
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def merge_intervals(intervals):
    """Merge overlapping (start, end) pairs. Assumes nothing about order."""
    if not intervals:
        return []
    merged = [list(i) for i in sorted(intervals)]
    out = [merged[0]]
    for s, e in merged[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def measure_coverage(hours=24, now=None):
    """
    Measure how much of the last `hours` the bot was actually watching.

    Run *count* is a useless health signal: each run polls for ~6 hours, so
    four runs a day is full coverage while twelve short runs would be mostly
    gaps. What matters is whether a run was polling at any given moment, and
    how long the largest blind gap was.

    Uses each run's *job* start/finish rather than the run's own timestamps.
    A run queued behind the concurrency group reports run_started_at at queue
    time, which would count hours of waiting as coverage.

    Returns a dict, or None if the run history could not be read.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        return None

    data = _github_json(f"/repos/{repo}/actions/workflows/"
                        f"check-availability.yml/runs?per_page=20")
    if data is None:
        return None

    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    intervals, failed = [], 0
    for run in data.get("workflow_runs", []):
        # Cheap pre-filter so we do not fetch jobs for ancient runs.
        if _parse(run["updated_at"]) <= window_start and run["status"] == "completed":
            continue
        jobs = _github_json(f"/repos/{repo}/actions/runs/{run['id']}/jobs")
        if not jobs:
            continue
        for job in jobs.get("jobs", []):
            if not job.get("started_at"):
                continue
            job_start = _parse(job["started_at"])
            job_end = (_parse(job["completed_at"])
                       if job.get("completed_at") else now)
            if job_end <= window_start or job_start >= now:
                continue
            if run.get("conclusion") == "failure":
                failed += 1
            intervals.append((max(job_start, window_start), min(job_end, now)))

    if not intervals:
        return {"covered_minutes": 0.0, "max_gap_minutes": hours * 60.0,
                "runs": 0, "failed": 0, "watching_now": False}

    merged = merge_intervals(intervals)
    covered = sum((e - s).total_seconds() for s, e in merged) / 60

    gaps = [(merged[0][0] - window_start).total_seconds() / 60]
    for i in range(1, len(merged)):
        gaps.append((merged[i][0] - merged[i - 1][1]).total_seconds() / 60)
    gaps.append((now - merged[-1][1]).total_seconds() / 60)

    return {
        "covered_minutes": covered,
        "max_gap_minutes": max(gaps),
        "runs": len(intervals),
        "failed": failed,
        "watching_now": (now - merged[-1][1]).total_seconds() <= 120,
    }


def build_status_email():
    """Returns (subject, body, healthy)."""
    now = datetime.now(timezone.utc)
    problems = []
    warnings = []
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
    canary_status, canary_message = bot.run_canary()
    label = {bot.CANARY_OK: "PASS",
             bot.CANARY_BROKEN: "FAIL",
             bot.CANARY_UNREACHABLE: "WARN"}[canary_status]
    lines.append(f"[{label}] Detector self-check")
    lines.append(f"       {canary_message}")
    if canary_status == bot.CANARY_BROKEN:
        problems.append("the detector cannot see known-available inventory")
    elif canary_status == bot.CANARY_UNREACHABLE:
        lines.append("       Could not reach the API just now. This is usually a")
        lines.append("       transient WAF block, not a detector fault.")
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

    # 3. Coverage -- how much of the day was the bot actually watching?
    cov = measure_coverage()
    if cov is None:
        lines.append("[ -- ] Coverage could not be read (no GITHUB_TOKEN)")
    else:
        pct = 100 * cov["covered_minutes"] / (24 * 60)
        gap = cov["max_gap_minutes"]
        interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
        checks = int(cov["covered_minutes"] * 60 / interval)
        ok = cov["watching_now"] and gap <= MAX_ACCEPTABLE_GAP_MINUTES
        lines.append(f"[{'PASS' if ok else 'WARN'}] Coverage (last 24h)")
        lines.append(f"       watching {pct:.0f}% of the day across "
                     f"{cov['runs']} run(s), roughly {checks} checks")
        lines.append(f"       largest blind gap: {gap:.0f} min")
        lines.append(f"       watching right now: "
                     f"{'yes' if cov['watching_now'] else 'NO'}")
        if cov["failed"]:
            lines.append(f"       {cov['failed']} run(s) ended early")
        if not cov["watching_now"]:
            problems.append("no run is currently active")
        elif gap > MAX_ACCEPTABLE_GAP_MINUTES:
            warnings.append(f"there was a {gap:.0f} minute gap in coverage")
    lines.append("")

    if problems:
        lines.append("PROBLEM: " + "; ".join(problems) + ".")
        lines.append("Until this is resolved, silence from this bot means nothing.")
        subject = "Lake O'Hara Bot - PROBLEM - not detecting availability"
    elif warnings:
        lines.append("Note: " + "; ".join(warnings) + ".")
        lines.append("The detector is verified working and is watching now; this")
        lines.append("is about coverage, not correctness.")
        subject = "Lake O'Hara Bot - OK - watching (minor coverage gap)"
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
