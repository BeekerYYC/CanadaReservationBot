"""
Daily status email for the Lake O'Hara reservation bot.
Sends a summary so you know the bot is still running.
"""

import os
import smtplib
import json
import urllib.request
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta

def get_recent_runs():
    """Fetch recent workflow runs from the GitHub API."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    workflow = "check-availability.yml"

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page=30"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("workflow_runs", [])
    except Exception as e:
        print(f"Warning: Could not fetch workflow runs: {e}")
        return []

def build_status_email():
    """Build the daily status email body."""
    runs = get_recent_runs()

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)

    # Filter to last 24 hours
    recent = []
    for run in runs:
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created >= yesterday:
            recent.append(run)

    total = len(recent)
    success = sum(1 for r in recent if r["conclusion"] == "success")
    failed = sum(1 for r in recent if r["conclusion"] == "failure")
    other = total - success - failed

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    actions_url = f"https://github.com/{repo}/actions"

    lines = []
    lines.append("Lake O'Hara Backcountry Bot - Daily Status")
    lines.append("=" * 44)
    lines.append("")
    lines.append(f"Report time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Last 24 hours: {total} checks ran")
    lines.append(f"  Successful: {success}")
    if failed:
        lines.append(f"  Failed:     {failed}  <-- check the Actions tab")
    if other:
        lines.append(f"  Other:      {other}")
    lines.append("")

    if total == 0:
        lines.append("WARNING: No runs detected in the last 24 hours!")
        lines.append("The bot may have been disabled by GitHub (this happens")
        lines.append("after 60 days of no repo activity). Visit the Actions tab")
        lines.append("to re-enable it.")
    elif failed == 0:
        lines.append("All checks passed. The bot is running normally.")
        lines.append("You will receive a separate alert if a campsite opens up.")
    else:
        lines.append(f"{failed} check(s) failed in the last 24 hours.")
        lines.append("This could be a temporary Parks Canada API issue.")
        lines.append("If failures persist, check the logs in the Actions tab.")

    lines.append("")
    lines.append(f"View details: {actions_url}")
    lines.append("")
    lines.append("-- ")
    lines.append("Lake O'Hara Reservation Bot")

    return "\n".join(lines)

def send_email(body):
    """Send the status email via SMTP."""
    smtp_server = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    username = os.environ["EMAIL_USERNAME"]
    password = os.environ["EMAIL_PASSWORD"]
    to_addr = os.environ["EMAIL_TO_ADDRESS"]
    from_addr = os.environ.get("EMAIL_FROM_ADDRESS", username)

    msg = MIMEText(body)
    msg["Subject"] = "Lake O'Hara Bot - Daily Status Report"
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
        server.login(username, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    print("Daily status email sent successfully.")

if __name__ == "__main__":
    body = build_status_email()
    print(body)
    print()
    send_email(body)
