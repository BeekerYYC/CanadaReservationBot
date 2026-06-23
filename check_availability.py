"""
Lake O'Hara Backcountry Availability Checker

Queries the Parks Canada GoingToCamp API directly for campsite availability.
Bypasses camply because its GoingToCamp provider doesn't support the
"Backcountry Zone" resource category used by Lake O'Hara.
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Parks Canada / GoingToCamp constants
# ---------------------------------------------------------------------------
BASE_URL = "https://reservation.pc.gc.ca"
RESOURCE_LOCATION_ID = -2147483538   # Lake O'Hara Backcountry
ROOT_MAP_ID = -2147483181            # root map for this campground
SUB_ZONE_MAP_ID = -2147483028        # sub-zone containing individual sites

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Configuration — set via env vars in the GitHub Actions workflow
# ---------------------------------------------------------------------------
START_DATE = os.environ.get("START_DATE", "2026-06-19")
END_DATE = os.environ.get("END_DATE", "2026-10-03")
MIN_NIGHTS = int(os.environ.get("MIN_NIGHTS", "2"))
ALLOWED_DAYS = os.environ.get("ALLOWED_DAYS", "Friday,Saturday,Sunday").split(",")
STATE_FILE = os.environ.get("STATE_FILE", "availability_state.json")

AVAIL_AVAILABLE = 0


def api_get(path, params=None, retries=3):
    """GET request to the Parks Canada API with retries."""
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  403 from API, retrying in {wait}s... (attempt {attempt+1})")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  Network error ({e}), retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise


def get_resource_names(start_date, end_date):
    """Fetch human-readable names for resources in the sub-zone.

    The API returns a dict keyed by resource ID strings, each value being
    a resource object with localizedValues containing the name.
    """
    data = api_get("/api/resourceLocation/resources", {
        "mapId": SUB_ZONE_MAP_ID,
        "resourceLocationId": RESOURCE_LOCATION_ID,
        "bookingCategoryId": 0,
        "startDate": start_date,
        "endDate": end_date,
        "isReserving": "true",
        "getDailyAvailability": "false",
        "filterData": "[]",
        "bopi498": "",
    })

    names = {}
    for rid_str, resource in data.items():
        localized = resource.get("localizedValues", [])
        name = localized[0]["name"] if localized else f"Site {rid_str}"
        names[rid_str] = name
    return names


def get_availability(start_date, end_date):
    """
    Fetch per-resource daily availability for the Lake O'Hara sub-zone.

    The API returns:
      {
        "mapId": ...,
        "resourceAvailabilities": {
          "-2147471222": [{"availability": 5, "remainingQuota": null}, ...],
          ...
        },
        ...
      }

    Each list has one entry per day in [start_date, end_date).
    availability=0 means available.

    Returns {resource_id_str: {date_str: avail_code, ...}, ...}
    """
    data = api_get("/api/availability/map", {
        "mapId": SUB_ZONE_MAP_ID,
        "resourceLocationId": RESOURCE_LOCATION_ID,
        "bookingCategoryId": 0,
        "startDate": start_date,
        "endDate": end_date,
        "isReserving": "true",
        "getDailyAvailability": "true",
        "filterData": "[]",
        "bopi498": "",
    })

    resource_avails = data.get("resourceAvailabilities", {})
    start = datetime.strptime(start_date, "%Y-%m-%d")

    result = {}
    for rid_str, daily_list in resource_avails.items():
        daily = {}
        for i, slot in enumerate(daily_list):
            day = start + timedelta(days=i)
            daily[day.strftime("%Y-%m-%d")] = slot["availability"]
        result[rid_str] = daily

    return result


def find_available_windows(availability, resource_names):
    """
    Find consecutive-night windows that match the user's criteria.
    Returns a list of dicts: {resource_id, site_name, check_in, check_out, nights}
    """
    results = []
    start = datetime.strptime(START_DATE, "%Y-%m-%d")
    end = datetime.strptime(END_DATE, "%Y-%m-%d")

    for rid_str, daily in availability.items():
        site_name = resource_names.get(rid_str, f"Site {rid_str}")
        date = start
        while date < end:
            date_str = date.strftime("%Y-%m-%d")
            day_name = date.strftime("%A")

            if day_name not in ALLOWED_DAYS:
                date += timedelta(days=1)
                continue

            if daily.get(date_str) != AVAIL_AVAILABLE:
                date += timedelta(days=1)
                continue

            consecutive = 0
            check = date
            while check < end:
                cs = check.strftime("%Y-%m-%d")
                if daily.get(cs) != AVAIL_AVAILABLE:
                    break
                consecutive += 1
                check += timedelta(days=1)

            if consecutive >= MIN_NIGHTS:
                results.append({
                    "resource_id": rid_str,
                    "site_name": site_name,
                    "check_in": date_str,
                    "check_out": (date + timedelta(days=consecutive)).strftime("%Y-%m-%d"),
                    "nights": consecutive,
                })

            date += timedelta(days=1)

    results.sort(key=lambda r: (r["check_in"], r["site_name"]))
    return results


def load_state():
    """Load previously-seen availability from state file."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            return set(json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def save_state(seen_keys):
    """Persist seen availability keys."""
    Path(STATE_FILE).write_text(json.dumps(sorted(seen_keys), indent=2))


def make_key(result):
    """Unique key for a specific availability window."""
    return f"{result['resource_id']}|{result['check_in']}|{result['nights']}"


def build_booking_url(check_in, check_out):
    """Build a direct booking URL for Parks Canada."""
    return (
        f"https://reservation.pc.gc.ca/create/Start"
        f"?resourceLocationId={RESOURCE_LOCATION_ID}"
        f"&mapId={ROOT_MAP_ID}"
        f"&startDate={check_in}"
        f"&endDate={check_out}"
        f"&nights={MIN_NIGHTS}"
    )


def build_alert_email(new_results):
    """Build the availability alert email body."""
    lines = []
    lines.append("!!! LAKE O'HARA BACKCOUNTRY - CAMPSITE AVAILABLE !!!")
    lines.append("=" * 55)
    lines.append("")
    lines.append(f"Found {len(new_results)} new availability window(s):")
    lines.append("")

    for r in new_results:
        day_name = datetime.strptime(r["check_in"], "%Y-%m-%d").strftime("%A")
        lines.append(f"  Site: {r['site_name']}")
        lines.append(f"  Check-in:  {r['check_in']} ({day_name})")
        lines.append(f"  Check-out: {r['check_out']}")
        lines.append(f"  Nights:    {r['nights']}")
        lines.append(f"  Book now:  {build_booking_url(r['check_in'], r['check_out'])}")
        lines.append("")

    lines.append("-- ")
    lines.append("Lake O'Hara Reservation Bot")
    return "\n".join(lines)


def send_email(body):
    """Send alert email via SMTP."""
    smtp_server = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    username = os.environ["EMAIL_USERNAME"]
    password = os.environ["EMAIL_PASSWORD"]
    to_addr = os.environ["EMAIL_TO_ADDRESS"]
    from_addr = os.environ.get("EMAIL_FROM_ADDRESS", username)
    subject = os.environ.get(
        "EMAIL_SUBJECT_LINE",
        "ALERT - LAKE O'HARA BACKCOUNTRY CAMPSITE AVAILABLE - BOOK NOW",
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
        server.login(username, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    print("Alert email sent!")


def main():
    print("Lake O'Hara Backcountry Availability Check")
    print(f"  Date range: {START_DATE} to {END_DATE}")
    print(f"  Min nights: {MIN_NIGHTS}")
    print(f"  Check-in days: {', '.join(ALLOWED_DAYS)}")
    print()

    print("Fetching resource names...")
    try:
        resource_names = get_resource_names(START_DATE, END_DATE)
    except Exception as e:
        print(f"Warning: Could not fetch resource names ({e}), using IDs")
        resource_names = {}
    print(f"  Found {len(resource_names)} resources")

    print("Fetching availability...")
    availability = get_availability(START_DATE, END_DATE)
    print(f"  Got availability for {len(availability)} resources")

    print("Searching for matching windows...")
    results = find_available_windows(availability, resource_names)
    print(f"  Found {len(results)} matching window(s)")

    if not results:
        print("\nNo availability found. Will check again next run.")
        return

    seen = load_state()
    new_results = [r for r in results if make_key(r) not in seen]

    if not new_results:
        print(f"\nAll {len(results)} window(s) were already reported. No new alerts.")
        for r in results:
            seen.add(make_key(r))
        save_state(seen)
        return

    print(f"\n{len(new_results)} NEW availability window(s) to report:")
    for r in new_results:
        print(f"  {r['site_name']}: {r['check_in']} ({r['nights']} nights)")

    body = build_alert_email(new_results)
    print()
    print(body)

    try:
        send_email(body)
    except KeyError as e:
        print(f"\nEmail not configured (missing {e}), skipping notification.")
    except Exception as e:
        print(f"\nFailed to send email: {e}")
        sys.exit(1)

    for r in results:
        seen.add(make_key(r))
    save_state(seen)
    print(f"\nState saved ({len(seen)} tracked windows).")


if __name__ == "__main__":
    main()
