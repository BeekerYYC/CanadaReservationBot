"""
Watchdog: is anything actually polling Lake O'Hara right now?

The bot has gone blind twice, both times for the same shape of reason: a run
ended early and nothing started another until the next cron fire, which on
GitHub's scheduler can be hours. Both times a human had to notice and ask for
a restart. This removes the human from that loop.

It answers one question -- is a polling job in progress anywhere -- and the
answer has to span BOTH workflows. When the watchdog restarts the checker via
`workflow_call`, the resulting job runs under the *watchdog's* run, not under
check-availability.yml. Looking only at the checker's own runs would miss it,
conclude nothing is running, and start another poller every few minutes
forever.

So: scan every in-progress run, look for a job whose name contains the polling
job, and match on substring because a called workflow's job is reported as
"<caller job> / <called job>".

If the API cannot be read we report "covered" rather than "stale". An unknown
answer must never trigger a restart; a brief gap is cheap, a runaway loop of
six-hour runners is not.
"""

import os
import sys

from send_status import _github_json

POLLING_JOB_NAME = "check-campsites"


def polling_now():
    """True / False, or None when the run history could not be read."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        return None

    runs = _github_json(f"/repos/{repo}/actions/runs?status=in_progress&per_page=20")
    if runs is None:
        return None

    for run in runs.get("workflow_runs", []):
        jobs = _github_json(f"/repos/{repo}/actions/runs/{run['id']}/jobs")
        if not jobs:
            continue
        for job in jobs.get("jobs", []):
            if (POLLING_JOB_NAME in (job.get("name") or "")
                    and job.get("status") == "in_progress"):
                print(f"  polling job live in run {run['id']} "
                      f"({run['path']}, job '{job['name']}')")
                return True
    return False


def main():
    live = polling_now()

    if live is None:
        print("Could not read run history; assuming covered and standing down.")
        stale = False
    elif live:
        print("A polling job is in progress. Nothing to do.")
        stale = False
    else:
        print("NOTHING IS POLLING. Restarting the checker.")
        stale = True

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"stale={'true' if stale else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
