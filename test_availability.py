"""
Tests for the Lake O'Hara availability detector.

The offline tests pin down the API state table that the detector depends on.
The live tests confirm the detector still fires against real inventory --
that is the part that would have caught the original bug, where the detector
was structurally incapable of ever returning a match.

    python3 -m unittest test_availability -v
    python3 -m unittest test_availability.OfflineTests -v   # no network
"""

import collections
import json
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import check_availability as bot


def slot(processed, availability, restriction):
    return {
        "processedAvailability": processed,
        "availability": availability,
        "restrictionReason": restriction,
        "remainingQuota": None,
    }


# Every (processedAvailability, availability, restrictionReason) combination
# observed across Parks Canada frontcountry sites and backcountry zones,
# and whether it means "you can book this night".
OBSERVED_STATES = [
    ((0, 0, 0), True,  "frontcountry site, bookable"),
    ((1, 1, 0), False, "frontcountry site, booked"),
    ((2, 0, 1), False, "outside booking window"),
    ((3, 0, 2), False, "closed for the season"),
    ((4, 4, 1), False, "resource closed"),
    ((5, 0, 3), True,  "backcountry zone, quota free"),
    ((5, 1, 3), False, "backcountry zone, full"),
]


class OfflineTests(unittest.TestCase):
    def test_state_table(self):
        for state, expected, label in OBSERVED_STATES:
            with self.subTest(state=state, label=label):
                self.assertEqual(bot.night_is_open(slot(*state)), expected, label)

    def test_out_of_season_is_not_an_opening(self):
        """(2, 0, 1) has availability == 0; only restrictionReason separates it."""
        self.assertEqual(slot(2, 0, 1)["availability"], bot.AVAILABILITY_FREE)
        self.assertFalse(bot.night_is_open(slot(2, 0, 1)))

    def test_consecutive_nights_collapse_into_one_window(self):
        nights = {
            "2026-09-18": slot(5, 0, 3),
            "2026-09-19": slot(5, 0, 3),
            "2026-09-20": slot(5, 0, 3),
            "2026-09-21": slot(5, 1, 3),
        }
        windows = bot.find_windows(nights, 1, ["Friday", "Saturday", "Sunday"])
        self.assertEqual(windows, [{
            "check_in": "2026-09-18", "check_out": "2026-09-21", "nights": 3,
        }])

    def test_gap_splits_windows(self):
        nights = {
            "2026-09-18": slot(5, 0, 3),
            "2026-09-19": slot(5, 1, 3),
            "2026-09-20": slot(5, 0, 3),
        }
        windows = bot.find_windows(nights, 1, ["Friday", "Saturday", "Sunday"])
        self.assertEqual([w["check_in"] for w in windows],
                         ["2026-09-18", "2026-09-20"])

    def test_min_nights_filters_short_windows(self):
        nights = {"2026-09-18": slot(5, 0, 3), "2026-09-19": slot(5, 1, 3)}
        self.assertEqual(bot.find_windows(nights, 2, ["Friday"]), [])

    def test_allowed_days_filters_check_in(self):
        nights = {"2026-09-16": slot(5, 0, 3)}  # a Wednesday
        self.assertEqual(bot.find_windows(nights, 1, ["Friday", "Saturday"]), [])
        self.assertEqual(len(bot.find_windows(nights, 1, ["Wednesday"])), 1)

    def test_checkout_is_the_morning_after_the_last_night(self):
        nights = {"2026-09-18": slot(5, 0, 3), "2026-09-19": slot(5, 0, 3)}
        w = bot.find_windows(nights, 1, ["Friday"])[0]
        self.assertEqual(w["check_in"], "2026-09-18")
        self.assertEqual(w["check_out"], "2026-09-20")
        self.assertEqual(w["nights"], 2)

    def test_cooldown_suppresses_then_re_alerts(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        windows = [{"check_in": "2026-09-20", "check_out": "2026-09-21", "nights": 1}]

        state = {}
        self.assertEqual(bot.windows_to_alert(windows, state, now), windows)
        bot.record_alerts(windows, state, now)

        self.assertEqual(bot.windows_to_alert(windows, state, now), [])

        later = now + timedelta(hours=bot.ALERT_COOLDOWN_HOURS + 1)
        self.assertEqual(bot.windows_to_alert(windows, state, later), windows)

    def test_longer_window_re_alerts_immediately(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        state = {}
        bot.record_alerts(
            [{"check_in": "2026-09-20", "check_out": "2026-09-21", "nights": 1}],
            state, now)
        grown = [{"check_in": "2026-09-20", "check_out": "2026-09-22", "nights": 2}]
        self.assertEqual(bot.windows_to_alert(grown, state, now), grown)

    def test_closed_window_is_forgotten_so_it_can_re_alert(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        windows = [{"check_in": "2026-09-20", "check_out": "2026-09-21", "nights": 1}]
        state = {}
        bot.record_alerts(windows, state, now)
        bot.prune_state(state, [])
        self.assertEqual(state, {})
        self.assertEqual(bot.windows_to_alert(windows, state, now), windows)

    def test_alert_email_contains_a_usable_booking_link(self):
        body = bot.build_alert_email(
            [{"check_in": "2026-09-20", "check_out": "2026-09-22", "nights": 2}])
        self.assertIn("2026-09-20", body)
        self.assertIn("Sunday", body)
        self.assertIn(f"resourceLocationId={bot.RESOURCE_LOCATION_ID}", body)
        self.assertIn(f"mapId={bot.ROOT_MAP_ID}", body)


class LiveTests(unittest.TestCase):
    """Hit the real Parks Canada API."""

    def test_canary_reports_open_nights(self):
        ok, message = bot.run_canary()
        self.assertTrue(ok, message)

    def test_detector_finds_windows_on_real_open_inventory(self):
        """
        End-to-end: point the detector at a backcountry zone that is actually
        open and confirm it produces windows and a well-formed alert email.
        This is the assertion the original bot could never have passed.
        """
        today = datetime.now(timezone.utc).date()
        first = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        last = (today + timedelta(days=9)).strftime("%Y-%m-%d")

        nights = bot.fetch_nights(
            bot.CANARY["resource_location_id"], bot.CANARY["map_id"],
            bot.CANARY["resource_id"], first, last)

        self.assertTrue(any(bot.night_is_open(s) for s in nights.values()),
                        "control resource reported no open nights")

        windows = bot.find_windows(nights, 1, [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"])
        self.assertTrue(windows, "open nights did not produce any window")

        body = bot.build_alert_email(windows)
        self.assertIn(windows[0]["check_in"], body)
        self.assertIn("Book:", body)

    def test_lake_ohara_returns_a_well_formed_response(self):
        nights = bot.fetch_nights(
            bot.RESOURCE_LOCATION_ID, bot.ROOT_MAP_ID,
            bot.CAMPGROUND_RESOURCE_ID, bot.FIRST_NIGHT, bot.LAST_NIGHT)
        expected = (datetime.strptime(bot.LAST_NIGHT, "%Y-%m-%d")
                    - datetime.strptime(bot.FIRST_NIGHT, "%Y-%m-%d")).days + 1
        self.assertEqual(len(nights), expected)
        for date_str, s in nights.items():
            self.assertIn("availability", s, date_str)
            self.assertIn("restrictionReason", s, date_str)

    def test_old_map_endpoint_still_cannot_see_backcountry(self):
        """
        Regression guard documenting the original bug: /api/availability/map
        reports processedAvailability == 5 for the Lake O'Hara zone on every
        date, so `== 0` can never match. If this ever starts failing, Parks
        Canada changed the endpoint and the comment in check_availability.py
        needs revisiting.
        """
        url = f"{bot.BASE_URL}/api/availability/map?" + urllib.parse.urlencode({
            "mapId": bot.ROOT_MAP_ID,
            "resourceLocationId": bot.RESOURCE_LOCATION_ID,
            "bookingCategoryId": 0,
            "startDate": bot.FIRST_NIGHT,
            "endDate": bot.LAST_NIGHT,
            "isReserving": "true",
            "getDailyAvailability": "true",
            "filterData": "[]",
            "bopi498": "",
        })
        req = urllib.request.Request(url, headers=bot.HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        codes = collections.Counter(
            s["availability"]
            for lst in data["resourceAvailabilities"].values()
            for s in lst
        )
        self.assertEqual(set(codes), {5},
                         f"map endpoint now returns {dict(codes)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
