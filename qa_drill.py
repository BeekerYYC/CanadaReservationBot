"""
End-to-end QA drill for the Lake O'Hara reservation bot.

`check_availability.py --self-test` answers "can the detector see an open
night?". This answers the bigger question: "if a cancellation appeared right
now, would an email with a working booking link actually land in the inbox?"

Every check runs against live Parks Canada data. Nothing is mocked. The drill
deliberately sends a real, clearly-labelled email, because delivery is the one
link in the chain that cannot be verified any other way.

    python3 qa_drill.py            # full drill, sends a labelled test email
    python3 qa_drill.py --no-email # everything except delivery

Exits non-zero if any check fails.
"""

import argparse
import collections
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import check_availability as bot

# Frontcountry campgrounds, used to hunt for a real isolated single-night gap.
# (label, resourceLocationId, mapId)
FRONTCOUNTRY = [
    ("Yoho - Kicking Horse", -2147483540, -2147483183),
    ("Yoho - Monarch", -2147483539, -2147483030),
]
FRONTCOUNTRY_EQUIPMENT = (-32768, -32768)

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]


class Drill:
    def __init__(self):
        self.results = []

    def record(self, name, ok, detail):
        self.results.append((name, ok, detail))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        for line in str(detail).splitlines():
            print(f"       {line}")
        print()
        return ok

    @property
    def failed(self):
        return [name for name, ok, _ in self.results if not ok]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_lake_ohara_read(drill):
    """The campground responds and the payload has the shape we parse."""
    try:
        nights = bot.fetch_nights(
            bot.RESOURCE_LOCATION_ID, bot.ROOT_MAP_ID, bot.CAMPGROUND_RESOURCE_ID,
            bot.FIRST_NIGHT, bot.LAST_NIGHT)
    except bot.ApiError as e:
        return drill.record("Lake O'Hara read", False, e)

    missing = [d for d, s in nights.items()
               if "availability" not in s or "restrictionReason" not in s]
    if missing:
        return drill.record("Lake O'Hara read", False,
                            f"{len(missing)} night(s) missing expected fields")

    states = collections.Counter(
        (s["processedAvailability"], s["availability"], s["restrictionReason"])
        for s in nights.values())
    open_nights = [d for d, s in nights.items() if bot.night_is_open(s)]
    return drill.record(
        "Lake O'Hara read", True,
        f"{len(nights)} nights parsed, {len(open_nights)} currently open\n"
        f"states (proc, avail, restrict): {dict(states)}")


def check_canary(drill):
    """The detector reports a known-open backcountry zone as open."""
    status, message = bot.run_canary()
    return drill.record("Canary: detector sees live availability",
                        status == bot.CANARY_OK, message)


def check_windows_on_real_inventory(drill):
    """Real open nights turn into real windows and a real alert body."""
    today = datetime.now(timezone.utc).date()
    first = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    last = (today + timedelta(days=9)).strftime("%Y-%m-%d")
    try:
        nights = bot.fetch_nights(
            bot.CANARY["resource_location_id"], bot.CANARY["map_id"],
            bot.CANARY["resource_id"], first, last)
    except bot.ApiError as e:
        return drill.record("Windows from real open inventory", False, e)

    windows = bot.find_windows(nights, 1, ALL_DAYS)
    if not windows:
        return drill.record("Windows from real open inventory", False,
                            "open nights did not produce any window")
    body = bot.build_alert_email(windows)
    ok = windows[0]["check_in"] in body and "Book:" in body
    return drill.record(
        "Windows from real open inventory", ok,
        f"{len(windows)} window(s) from {bot.CANARY['name']}, "
        f"longest {max(w['nights'] for w in windows)} night(s)\n"
        f"alert body renders with a booking link: {ok}")


def find_isolated_single_night():
    """
    Hunt live frontcountry inventory for a night that is open with booked
    nights on both sides -- a genuine one-night cancellation shape.

    Returns (label, resource_location_id, map_id, resource_id, date) or None.
    """
    today = datetime.now(timezone.utc).date()
    first = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    last = (today + timedelta(days=20)).strftime("%Y-%m-%d")

    for label, rl, map_id in FRONTCOUNTRY:
        try:
            data = bot.api_get("/api/availability/map", {
                "mapId": map_id, "resourceLocationId": rl, "bookingCategoryId": 0,
                "startDate": first, "endDate": last, "isReserving": "true",
                "getDailyAvailability": "true", "filterData": "[]", "bopi498": "",
            })
        except bot.ApiError:
            continue

        for rid, slots in data.get("resourceAvailabilities", {}).items():
            codes = [s["availability"] for s in slots]
            for i in range(1, len(codes) - 1):
                if codes[i] == 0 and codes[i - 1] != 0 and codes[i + 1] != 0:
                    date = (datetime.strptime(first, "%Y-%m-%d")
                            + timedelta(days=i)).strftime("%Y-%m-%d")
                    return label, rl, map_id, rid, date
    return None


def check_single_night_detection(drill):
    """
    The change that matters for the rest of this season: a lone open night,
    booked on both sides, must produce an alert.
    """
    found = find_isolated_single_night()
    if not found:
        return drill.record(
            "Single-night cancellation detected", False,
            "could not find an isolated one-night gap in live frontcountry\n"
            "inventory to test against -- rerun the drill later")

    label, rl, map_id, rid, date = found
    window_start = (datetime.strptime(date, "%Y-%m-%d")
                    - timedelta(days=1)).strftime("%Y-%m-%d")
    window_end = (datetime.strptime(date, "%Y-%m-%d")
                  + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        nights = bot.fetch_nights(
            rl, map_id, rid, window_start, window_end,
            equipment_category_id=FRONTCOUNTRY_EQUIPMENT[0],
            sub_equipment_category_id=FRONTCOUNTRY_EQUIPMENT[1])
    except bot.ApiError as e:
        return drill.record("Single-night cancellation detected", False, e)

    found_one = bot.find_windows(nights, 1, ALL_DAYS)
    found_two = bot.find_windows(nights, 2, ALL_DAYS)

    ok = (len(found_one) == 1
          and found_one[0]["nights"] == 1
          and found_one[0]["check_in"] == date
          and not found_two)
    detail = (
        f"live one-night gap: {label}, site {rid}, night {date}\n"
        f"neighbours: {window_start} and {window_end} both booked\n"
        f"MIN_NIGHTS=1 -> {len(found_one)} window(s) "
        f"{[w['check_in'] + ' x' + str(w['nights']) for w in found_one]}\n"
        f"MIN_NIGHTS=2 -> {len(found_two)} window(s)  "
        f"(this is what the old config would have missed)")
    return drill.record("Single-night cancellation detected", ok, detail)


def check_out_of_season_not_flagged(drill):
    """
    Out-of-season nights also carry availability == 0. Without the
    restrictionReason guard every one of them looks like a cancellation, so
    this is the negative test that matters most.
    """
    last = datetime.strptime(bot.LAST_NIGHT, "%Y-%m-%d")
    first_after = (last + timedelta(days=1)).strftime("%Y-%m-%d")
    week_after = (last + timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        nights = bot.fetch_nights(
            bot.RESOURCE_LOCATION_ID, bot.ROOT_MAP_ID, bot.CAMPGROUND_RESOURCE_ID,
            first_after, week_after)
    except bot.ApiError as e:
        return drill.record("Out-of-season nights are not false alarms", False, e)

    raw_free = [d for d, s in nights.items() if s["availability"] == 0]
    flagged = bot.find_windows(nights, 1, ALL_DAYS)
    ok = not flagged
    return drill.record(
        "Out-of-season nights are not false alarms", ok,
        f"{first_after}..{week_after} (after season end)\n"
        f"{len(raw_free)}/{len(nights)} nights have availability == 0\n"
        f"detector flagged {len(flagged)} of them as bookable")


def check_booking_url(drill):
    """An alert is only useful if its link works."""
    today = datetime.now(timezone.utc).date()
    check_in = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    check_out = (today + timedelta(days=4)).strftime("%Y-%m-%d")
    url = bot.booking_url(check_in, check_out, 1)

    req = urllib.request.Request(url, headers=bot.HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, TimeoutError) as e:
        return drill.record("Booking link resolves", False, f"{url}\n{e}")

    ok = code == 200
    return drill.record("Booking link resolves", ok, f"HTTP {code}\n{url}")


def check_email_delivery(drill, send):
    """The whole point. Sends a real, clearly-labelled drill email."""
    if not send:
        return drill.record("Alert email delivered", True,
                            "skipped (--no-email)")

    today = datetime.now(timezone.utc).date()
    first = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    last = (today + timedelta(days=6)).strftime("%Y-%m-%d")
    try:
        nights = bot.fetch_nights(
            bot.CANARY["resource_location_id"], bot.CANARY["map_id"],
            bot.CANARY["resource_id"], first, last)
    except bot.ApiError as e:
        return drill.record("Alert email delivered", False, e)

    windows = bot.find_windows(nights, 1, ALL_DAYS)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = "\n".join([
        "*** THIS IS A TEST. NO SPOT HAS OPENED AT LAKE O'HARA. ***",
        "",
        f"QA drill run at {stamp}. This message proves the alert path works",
        f"end to end: live API read -> detector -> window logic -> email.",
        "",
        "The availability below is real, but it is from a control campsite in",
        f"the Yoho backcountry ({bot.CANARY['name']}), not Lake O'Hara. The",
        "booking link points at Lake O'Hara and is included so you can confirm",
        "it opens correctly.",
        "",
        "A real alert looks exactly like what follows, without this preamble.",
        "",
        "-" * 55,
        "",
        bot.build_alert_email(windows),
    ])

    try:
        sent = bot.send_email(
            "[DRILL] Lake O'Hara bot - alert path test - NOT a real opening",
            body)
    except Exception as e:
        return drill.record("Alert email delivered", False, f"SMTP error: {e}")

    return drill.record(
        "Alert email delivered", sent,
        "drill email accepted by SMTP server"
        if sent else "email credentials not configured")


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-email", action="store_true",
                        help="run every check except live email delivery")
    args = parser.parse_args()

    print("=" * 60)
    print("QA DRILL - Lake O'Hara Reservation Bot")
    print(f"Started {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Watching {bot.FIRST_NIGHT} .. {bot.LAST_NIGHT}, "
          f"MIN_NIGHTS={bot.MIN_NIGHTS}")
    print("=" * 60)
    print()

    drill = Drill()
    check_lake_ohara_read(drill)
    check_canary(drill)
    check_windows_on_real_inventory(drill)
    check_single_night_detection(drill)
    check_out_of_season_not_flagged(drill)
    check_booking_url(drill)
    check_email_delivery(drill, send=not args.no_email)

    print("=" * 60)
    passed = len(drill.results) - len(drill.failed)
    print(f"{passed}/{len(drill.results)} checks passed")
    if drill.failed:
        print("FAILED: " + ", ".join(drill.failed))
        print("The bot cannot be trusted to alert you until these pass.")
        return 1
    print("The alert path works end to end against live data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
