"""
Lake O'Hara Backcountry cancellation checker.

Queries the Parks Canada GoingToCamp API for the Lake O'Hara Backcountry
campground and sends an email when a night opens up.


WHY THIS DOES NOT USE /api/availability/map
-------------------------------------------
That endpoint only exposes `processedAvailability`, which is hard-coded to 5
for every Backcountry Zone resource in the Parks Canada system -- on every
date, regardless of real availability, and regardless of bookingCategoryId,
equipment category, sub-equipment or party size. A detector built on it can
never fire for Lake O'Hara.

Frontcountry campgrounds *do* report 0/1/2/4 correctly through that endpoint,
which is exactly what made this so easy to miss: the code looked right, and
the same logic works fine one campground over.

/api/availability/resourceDailyAvailability exposes the underlying fields:

    availability          0 = quota free, 1 = taken, 4 = resource closed
    restrictionReason     0 = none
                          1 = outside the booking window (pre-open / post-season)
                          2 = closed for the season
                          3 = backcountry-zone booking rules apply
    processedAvailability what the map endpoint would return
    remainingQuota        null for Lake O'Hara

Observed (processedAvailability, availability, restrictionReason) states:

    (0, 0, 0)   frontcountry site, bookable
    (1, 1, 0)   frontcountry site, booked
    (2, 0, 1)   outside booking window
    (3, 0, 2)   closed for the season
    (4, 4, 1)   resource closed
    (5, 0, 3)   backcountry zone, quota free    <-- an opening
    (5, 1, 3)   backcountry zone, full

So: a night is open when availability == 0 and restrictionReason is not 1 or 2.

Note that (2, 0, 1) also carries availability == 0, which is why the
restrictionReason guard matters -- without it every out-of-season date would
look like a cancellation.


THE CANARY
----------
The original bot failed silently for seven months while reporting success
every day. To make that impossible, every run first checks a control resource
(a backcountry zone that is reliably open) and confirms the detector sees it
as available. If the canary reads as full, the detector is broken or the API
changed shape -- the run sends a BOT BROKEN email and exits non-zero rather
than quietly reporting "no availability".
"""

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import random
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Parks Canada / GoingToCamp constants
# ---------------------------------------------------------------------------
BASE_URL = "https://reservation.pc.gc.ca"

# Yoho - Lake O'Hara Backcountry
RESOURCE_LOCATION_ID = -2147483538
ROOT_MAP_ID = -2147483181

# The actual campground. The previous version queried sub-zone map -2147483028,
# which contains the ten alpine climbing bivouacs (Mt. Victoria, Abbot Pass,
# Mt. Odaray...), five "Last-Minute Camping" slots and a staff/emergency site --
# but NOT the campground itself.
CAMPGROUND_RESOURCE_ID = "-2147471963"
CAMPGROUND_NAME = "Lake O'Hara Backcountry Sites"

# Backcountry equipment: "Single Tent". Lake O'Hara returns identical results
# for every party size and equipment combination, so these are just a
# well-formed request, not a filter.
EQUIPMENT_CATEGORY_ID = -32767
SUB_EQUIPMENT_CATEGORY_ID = -32758
PARTY_SIZE = 2

# Control resource: a backcountry zone that reads as open. Same resource type
# and same code path as Lake O'Hara, so if the detector can see this one, the
# detector works.
CANARY = {
    "name": "Ottertail Random (Yoho backcountry)",
    "resource_location_id": -2147483638,
    "map_id": -2147483588,
    "resource_id": "-2147483138",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

CANARY_OK = "ok"
CANARY_BROKEN = "broken"
CANARY_UNREACHABLE = "unreachable"

AVAILABILITY_FREE = 0
RESTRICTION_OUTSIDE_BOOKING_WINDOW = 1
RESTRICTION_CLOSED_FOR_SEASON = 2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FIRST_NIGHT = os.environ.get("FIRST_NIGHT", "2026-09-15")
LAST_NIGHT = os.environ.get("LAST_NIGHT", "2026-10-03")
MIN_NIGHTS = int(os.environ.get("MIN_NIGHTS", "1"))
ALLOWED_DAYS = [d.strip() for d in os.environ.get(
    "ALLOWED_DAYS",
    "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday",
).split(",") if d.strip()]
STATE_FILE = os.environ.get("STATE_FILE", "availability_state.json")
ALERT_COOLDOWN_HOURS = float(os.environ.get("ALERT_COOLDOWN_HOURS", "12"))

POLL_DURATION_MINUTES = float(os.environ.get("POLL_DURATION_MINUTES", "0"))
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
MAX_POLL_INTERVAL_SECONDS = float(
    os.environ.get("MAX_POLL_INTERVAL_SECONDS", "120"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "10"))
API_RETRIES = int(os.environ.get("API_RETRIES", "8"))
API_BACKOFF_CEILING_SECONDS = float(os.environ.get("API_BACKOFF_CEILING_SECONDS", "120"))
# How often to re-verify the detector mid-run, and how long we tolerate being
# unable to reach the API at all before handing the run back to the scheduler.
CANARY_RECHECK_MINUTES = float(os.environ.get("CANARY_RECHECK_MINUTES", "30"))
CANARY_STALE_MINUTES = float(os.environ.get("CANARY_STALE_MINUTES", "60"))


class ApiError(RuntimeError):
    """The Parks Canada API could not be reached or returned something unusable."""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def api_get(path, params, retries=API_RETRIES):
    """GET with retries and jittered exponential backoff.

    Parks Canada sits behind an Azure WAF that intermittently 403s cloud IPs,
    GitHub runners included. Those blocks last minutes, so the backoff ceiling
    is generous and jittered to avoid every retry landing in lockstep.
    """
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"

    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in (403, 429, 500, 502, 503) or attempt == retries - 1:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_error = e
            if attempt == retries - 1:
                break
        wait = min(2 ** (attempt + 1), API_BACKOFF_CEILING_SECONDS)
        wait += random.uniform(0, wait * 0.25)
        print(f"    {last_error}; retrying in {wait:.0f}s "
              f"(attempt {attempt + 1}/{retries})")
        time.sleep(wait)

    raise ApiError(f"GET {path} failed: {last_error}")


def fetch_nights(resource_location_id, map_id, resource_id, first_night, last_night,
                 equipment_category_id=None, sub_equipment_category_id=None):
    """
    Return {date_str: slot_dict} for every night in [first_night, last_night].

    The endpoint is inclusive of both endpoints, so a 2026-09-16..2026-10-03
    request returns 18 entries.
    """
    slots = api_get("/api/availability/resourceDailyAvailability", {
        "resourceLocationId": resource_location_id,
        "mapId": map_id,
        "resourceId": resource_id,
        "bookingCategoryId": 0,
        "startDate": first_night,
        "endDate": last_night,
        "isReserving": "true",
        "getDailyAvailability": "true",
        "filterData": "[]",
        "equipmentCategoryId": (equipment_category_id
                                if equipment_category_id is not None
                                else EQUIPMENT_CATEGORY_ID),
        "subEquipmentCategoryId": (sub_equipment_category_id
                                   if sub_equipment_category_id is not None
                                   else SUB_EQUIPMENT_CATEGORY_ID),
        "partySize": PARTY_SIZE,
        "bopi498": "",
    })

    if not isinstance(slots, list):
        raise ApiError(
            f"expected a list of daily slots, got {type(slots).__name__}: "
            f"{str(slots)[:200]}"
        )

    start = datetime.strptime(first_night, "%Y-%m-%d")
    expected = (datetime.strptime(last_night, "%Y-%m-%d") - start).days + 1
    if len(slots) != expected:
        raise ApiError(
            f"expected {expected} nights for {first_night}..{last_night}, "
            f"got {len(slots)} -- the API response shape has changed"
        )

    return {
        (start + timedelta(days=i)).strftime("%Y-%m-%d"): slot
        for i, slot in enumerate(slots)
    }


def night_is_open(slot):
    """True when this night has free quota and is actually bookable."""
    return (
        slot.get("availability") == AVAILABILITY_FREE
        and slot.get("restrictionReason") not in (
            RESTRICTION_OUTSIDE_BOOKING_WINDOW,
            RESTRICTION_CLOSED_FOR_SEASON,
        )
    )


def describe_slot(slot):
    return (f"proc={slot.get('processedAvailability')} "
            f"avail={slot.get('availability')} "
            f"restrict={slot.get('restrictionReason')}")


# ---------------------------------------------------------------------------
# Window search
# ---------------------------------------------------------------------------
def find_windows(nights, min_nights, allowed_days):
    """
    Collapse open nights into maximal consecutive runs, then keep the ones that
    are long enough and start on an allowed day.

    Returns [{check_in, check_out, nights}] sorted by check-in date.
    """
    open_dates = sorted(d for d, slot in nights.items() if night_is_open(slot))

    runs = []
    for date_str in open_dates:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        if runs and runs[-1][-1] + timedelta(days=1) == date:
            runs[-1].append(date)
        else:
            runs.append([date])

    windows = []
    for run in runs:
        if len(run) < min_nights:
            continue
        check_in = run[0]
        if check_in.strftime("%A") not in allowed_days:
            continue
        windows.append({
            "check_in": check_in.strftime("%Y-%m-%d"),
            "check_out": (run[-1] + timedelta(days=1)).strftime("%Y-%m-%d"),
            "nights": len(run),
        })
    return windows


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------
def run_canary():
    """
    Confirm the detector can still see availability on a backcountry zone that
    is known to be open.

    Returns (status, message) where status is one of:

      CANARY_OK          the detector sees the control zone as open
      CANARY_BROKEN      the request succeeded but the detector reports the
                         control zone as full -- a real logic or API-semantics
                         break, and the thing this canary exists to catch
      CANARY_UNREACHABLE the request itself failed (WAF 403, timeout, DNS)

    The distinction matters. BROKEN means the bot would silently miss a
    cancellation and must stop. UNREACHABLE means we simply could not look
    just now, which is a transient infrastructure problem and must NOT be
    reported as a broken detector or used to abandon the run.
    """
    today = datetime.now(timezone.utc).date()
    first = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    last = (today + timedelta(days=14)).strftime("%Y-%m-%d")

    try:
        nights = fetch_nights(
            CANARY["resource_location_id"], CANARY["map_id"],
            CANARY["resource_id"], first, last,
        )
    except ApiError as e:
        return CANARY_UNREACHABLE, f"canary unreachable: {e}"

    open_count = sum(1 for slot in nights.values() if night_is_open(slot))
    if open_count == 0:
        sample = "; ".join(
            f"{d} {describe_slot(s)}" for d, s in list(nights.items())[:3]
        )
        return CANARY_BROKEN, (
            f"canary '{CANARY['name']}' shows 0 of {len(nights)} nights open. "
            f"The detector cannot see known-available inventory. Sample: {sample}"
        )

    return CANARY_OK, (f"canary '{CANARY['name']}': {open_count}/{len(nights)} "
                       f"nights open -- detector is live")


# ---------------------------------------------------------------------------
# State / de-duplication
# ---------------------------------------------------------------------------
def load_state():
    path = Path(STATE_FILE)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as e:
        print(f"  Warning: could not write {STATE_FILE}: {e}")


def windows_to_alert(windows, state, now):
    """
    Alert on a window if we have not alerted on that check-in date before, if it
    has grown longer, or if the cooldown has elapsed and it is still open.
    """
    fresh = []
    for w in windows:
        previous = state.get(w["check_in"])
        if previous is None:
            fresh.append(w)
            continue
        if w["nights"] > previous.get("nights", 0):
            fresh.append(w)
            continue
        try:
            last = datetime.fromisoformat(previous["last_alert"])
        except (KeyError, ValueError):
            fresh.append(w)
            continue
        if (now - last).total_seconds() >= ALERT_COOLDOWN_HOURS * 3600:
            fresh.append(w)
    return fresh


def record_alerts(windows, state, now):
    for w in windows:
        state[w["check_in"]] = {
            "nights": w["nights"],
            "last_alert": now.isoformat(),
        }


def prune_state(state, windows):
    """Forget check-in dates that are no longer open, so they re-alert if they come back."""
    still_open = {w["check_in"] for w in windows}
    for key in list(state):
        if key not in still_open:
            del state[key]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def booking_url(check_in, check_out, nights):
    return (
        f"{BASE_URL}/create/Start?"
        + urllib.parse.urlencode({
            "resourceLocationId": RESOURCE_LOCATION_ID,
            "mapId": ROOT_MAP_ID,
            "startDate": check_in,
            "endDate": check_out,
            "nights": nights,
            "isReserving": "true",
            "equipmentId": EQUIPMENT_CATEGORY_ID,
            "subEquipmentId": SUB_EQUIPMENT_CATEGORY_ID,
            "partySize": PARTY_SIZE,
        })
    )


def build_alert_subject(windows):
    """
    Put the night in the subject. These alerts get read on a watch face, where
    the subject may be all that is visible, and the spot can be gone in under
    two minutes -- so "which night is it" has to be legible at a glance rather
    than one tap away.
    """
    first = datetime.strptime(windows[0]["check_in"], "%Y-%m-%d")
    when = first.strftime("%a %b %d").replace(" 0", " ")
    if len(windows) == 1:
        nights = windows[0]["nights"]
        plural = "s" if nights != 1 else ""
        return f"LAKE O'HARA OPEN: {when} ({nights} night{plural}) - BOOK NOW"
    return f"LAKE O'HARA OPEN: {len(windows)} spots from {when} - BOOK NOW"


def build_alert_email(windows):
    lines = [
        "LAKE O'HARA BACKCOUNTRY - A SPOT OPENED UP",
        "=" * 55,
        "",
        f"{CAMPGROUND_NAME}",
        f"{len(windows)} opening(s) found. These go fast -- book now.",
        "",
    ]
    for w in windows:
        check_in = datetime.strptime(w["check_in"], "%Y-%m-%d")
        check_out = datetime.strptime(w["check_out"], "%Y-%m-%d")
        lines += [
            f"  Check-in:  {w['check_in']} ({check_in.strftime('%A')})",
            f"  Check-out: {w['check_out']} ({check_out.strftime('%A')})",
            f"  Nights:    {w['nights']}",
            f"  Book:      {booking_url(w['check_in'], w['check_out'], w['nights'])}",
            "",
        ]
    lines += ["-- ", "Lake O'Hara Reservation Bot"]
    return "\n".join(lines)


def build_broken_email(reason):
    return "\n".join([
        "LAKE O'HARA BOT - SELF-CHECK FAILED",
        "=" * 55,
        "",
        "The bot is NOT currently able to detect cancellations.",
        "",
        f"Reason: {reason}",
        "",
        "This means either the Parks Canada API changed shape, or the",
        "detector logic no longer matches it. Until this is fixed, treat",
        "silence from this bot as meaningless.",
        "",
        "-- ",
        "Lake O'Hara Reservation Bot",
    ])


def build_blocked_email(reason, minutes):
    return "\n".join([
        "LAKE O'HARA BOT - CANNOT REACH PARKS CANADA",
        "=" * 55,
        "",
        f"The bot has been unable to reach the reservation API for {minutes:.0f}",
        "minutes and is handing this run back to the scheduler, which will",
        "start a fresh run on a different machine.",
        "",
        f"Last error: {reason}",
        "",
        "This is an infrastructure problem, not a detector bug -- Parks Canada",
        "sits behind a WAF that intermittently blocks cloud IP ranges. It",
        "usually clears on its own. No action needed unless it keeps repeating.",
        "",
        "-- ",
        "Lake O'Hara Reservation Bot",
    ])


def _header_safe(text):
    """HTTP headers must be latin-1. Drop anything that is not."""
    return text.encode("latin-1", "replace").decode("latin-1")


def send_push(title, body, click_url=None, priority="urgent"):
    """
    Fire a push notification via ntfy.

    Email costs 10-60 seconds between SMTP send and the notification actually
    buzzing. Against a window that closes in under two minutes that is a third
    of the budget, so the push goes out first and the email follows as the
    durable copy with the full details.

    No topic configured means no push -- never a failure.
    """
    if not NTFY_TOPIC:
        return False

    headers = {
        "Title": _header_safe(title),
        "Priority": priority,
        "Tags": "tent",
    }
    if click_url:
        headers["Click"] = click_url

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("  Push sent")
        return True
    except Exception as e:
        # A failed push must never stop the email from going out.
        print(f"  Push failed ({e}); email still sending")
        return False


def send_email(subject, body):
    smtp_server = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    try:
        username = os.environ["EMAIL_USERNAME"]
        password = os.environ["EMAIL_PASSWORD"]
        to_addr = os.environ["EMAIL_TO_ADDRESS"]
    except KeyError as e:
        print(f"  Email not configured (missing {e}); skipping notification.")
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM_ADDRESS", username)
    msg["To"] = to_addr

    with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
        server.login(username, password)
        server.sendmail(msg["From"], [to_addr], msg.as_string())
    print(f"  Email sent: {subject}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def check(send=True):
    """One pass. Returns (windows, alerted) or raises ApiError."""
    nights = fetch_nights(
        RESOURCE_LOCATION_ID, ROOT_MAP_ID, CAMPGROUND_RESOURCE_ID,
        FIRST_NIGHT, LAST_NIGHT,
    )
    open_nights = [d for d, s in nights.items() if night_is_open(s)]
    windows = find_windows(nights, MIN_NIGHTS, ALLOWED_DAYS)

    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{stamp}] {len(open_nights)}/{len(nights)} nights open, "
          f"{len(windows)} matching window(s)")

    if not windows:
        state = load_state()
        if state:
            prune_state(state, windows)
            save_state(state)
        return windows, []

    now = datetime.now(timezone.utc)
    state = load_state()
    fresh = windows_to_alert(windows, state, now)

    for w in windows:
        print(f"    OPEN: {w['check_in']} -> {w['check_out']} "
              f"({w['nights']} night(s))"
              + ("" if w in fresh else "  [already alerted]"))

    if fresh and send:
        subject = os.environ.get("EMAIL_SUBJECT_LINE") or build_alert_subject(fresh)
        body = build_alert_email(fresh)
        # Push first: it is the fast path, and every second is the whole game.
        first = fresh[0]
        send_push(
            subject,
            f"{first['check_in']} to {first['check_out']} "
            f"({first['nights']} night(s)). Tap to book.",
            click_url=build_booking_url(first["check_in"], first["check_out"],
                                        first["nights"]),
        )
        if send_email(subject, body):
            record_alerts(fresh, state, now)

    prune_state(state, windows)
    save_state(state)
    return windows, fresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="run the canary and a single check, send nothing")
    parser.add_argument("--once", action="store_true",
                        help="single check, ignore POLL_DURATION_MINUTES")
    args = parser.parse_args()

    print("Lake O'Hara Backcountry Availability Check")
    print(f"  Campground:    {CAMPGROUND_NAME} ({CAMPGROUND_RESOURCE_ID})")
    print(f"  Nights:        {FIRST_NIGHT} .. {LAST_NIGHT}")
    print(f"  Min nights:    {MIN_NIGHTS}")
    print(f"  Check-in days: {', '.join(ALLOWED_DAYS)}")
    print()

    send = not args.self_test

    print("Self-check...")
    status, message = run_canary()
    print(f"  {message}")
    if status == CANARY_BROKEN:
        # The only condition that justifies refusing to run: we can read the
        # API fine, and the detector reports known-open inventory as full.
        if send:
            send_email("LAKE O'HARA BOT - SELF-CHECK FAILED",
                       build_broken_email(message))
        print("\nAborting: the detector cannot be trusted.")
        return 1
    if status == CANARY_UNREACHABLE:
        # Transient. Start polling anyway and re-verify as we go; the loop
        # below escalates if this turns out to be sustained.
        print("  Could not verify the detector yet. Starting anyway and will "
              "re-check during the run.")
    print()

    deadline = None
    if POLL_DURATION_MINUTES > 0 and not (args.once or args.self_test):
        deadline = time.monotonic() + POLL_DURATION_MINUTES * 60
        print(f"Polling every {POLL_INTERVAL_SECONDS:.0f}s for "
              f"{POLL_DURATION_MINUTES:.0f} minutes...")
    else:
        print("Checking availability...")

    # Adaptive interval. 10s is aggressive against a WAF that already 403s
    # casual traffic, so back off on failure and recover on success: polite
    # when Parks Canada is pushing back, fast when it is not. Hammering a
    # blocked endpoint every 10s earns a longer block, which costs far more
    # coverage than the latency it was meant to save.
    interval = POLL_INTERVAL_SECONDS

    now = time.monotonic()
    last_canary_ok = now if status == CANARY_OK else None
    started = now
    next_canary = now + CANARY_RECHECK_MINUTES * 60
    failures = 0
    last_error = "unknown"

    while True:
        try:
            check(send=send)
            failures = 0
            if interval != POLL_INTERVAL_SECONDS:
                print(f"  recovered; back to {POLL_INTERVAL_SECONDS:.0f}s polling")
                interval = POLL_INTERVAL_SECONDS
        except ApiError as e:
            failures += 1
            last_error = str(e)
            interval = min(interval * 2, MAX_POLL_INTERVAL_SECONDS)
            print(f"  API error ({failures}/{MAX_CONSECUTIVE_FAILURES}): {e}"
                  f" -- backing off to {interval:.0f}s")

        now = time.monotonic()

        # Re-verify the detector periodically. A run lasts ~6 hours; the API
        # semantics could change under us partway through.
        if now >= next_canary:
            next_canary = now + CANARY_RECHECK_MINUTES * 60
            status, message = run_canary()
            print(f"  {message}")
            if status == CANARY_BROKEN:
                if send:
                    send_email("LAKE O'HARA BOT - SELF-CHECK FAILED",
                               build_broken_email(message))
                print("\nAborting: the detector cannot be trusted.")
                return 1
            if status == CANARY_OK:
                last_canary_ok = now
                failures = 0

        # Sustained inability to reach the API. End the run so the scheduler
        # starts a fresh one, most likely on a different IP. This is recovery,
        # not failure -- but say so accurately rather than crying "broken".
        blind_since = last_canary_ok if last_canary_ok is not None else started
        blind_minutes = (now - blind_since) / 60
        if failures >= MAX_CONSECUTIVE_FAILURES and blind_minutes >= CANARY_STALE_MINUTES:
            # Deliberately silent. Ending the run IS the fix: the scheduler
            # and watchdog start a fresh one, usually on a different IP. There
            # is nothing for a human to do, and an email here trains you to
            # ignore the ones that matter. The daily status email reports any
            # coverage gap this caused.
            print(f"\nUnable to reach Parks Canada for {blind_minutes:.0f} "
                  f"minutes. Handing back to the scheduler for a fresh runner.")
            return 1

        if deadline is None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
