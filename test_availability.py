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

import os

import unittest.mock

import check_availability as bot
import send_status
import watchdog


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

    def test_alert_subject_names_the_night(self):
        """Read on a watch face, the subject is often all you get."""
        one = [{"check_in": "2026-09-20", "check_out": "2026-09-21", "nights": 1}]
        self.assertEqual(bot.build_alert_subject(one),
                         "LAKE O'HARA OPEN: Sun Sep 20 (1 night) - BOOK NOW")
        two = [{"check_in": "2026-09-21", "check_out": "2026-09-23", "nights": 2}]
        self.assertIn("2 nights", bot.build_alert_subject(two))
        many = one + [{"check_in": "2026-09-26", "check_out": "2026-09-27",
                       "nights": 1}]
        self.assertIn("2 spots", bot.build_alert_subject(many))

    def test_alert_email_contains_a_usable_booking_link(self):
        body = bot.build_alert_email(
            [{"check_in": "2026-09-20", "check_out": "2026-09-22", "nights": 2}])
        self.assertIn("2026-09-20", body)
        self.assertIn("Sunday", body)
        self.assertIn(f"resourceLocationId={bot.RESOURCE_LOCATION_ID}", body)
        self.assertIn(f"mapId={bot.ROOT_MAP_ID}", body)


class CanaryStateTests(unittest.TestCase):
    """
    A transient API failure must never be reported as a broken detector.
    Conflating the two took the bot offline for three hours and sent a false
    alarm at 03:26 UTC.
    """

    def setUp(self):
        self._real_fetch = bot.fetch_nights
        self.addCleanup(setattr, bot, "fetch_nights", self._real_fetch)

    def test_request_failure_is_unreachable_not_broken(self):
        def boom(*a, **kw):
            raise bot.ApiError("HTTP Error 403: Forbidden")
        bot.fetch_nights = boom
        status, message = bot.run_canary()
        self.assertEqual(status, bot.CANARY_UNREACHABLE)
        self.assertNotEqual(status, bot.CANARY_BROKEN)
        self.assertIn("403", message)

    def test_control_zone_reading_full_is_broken(self):
        bot.fetch_nights = lambda *a, **kw: {"2026-09-20": slot(5, 1, 3)}
        status, _ = bot.run_canary()
        self.assertEqual(status, bot.CANARY_BROKEN)

    def test_control_zone_reading_open_is_ok(self):
        bot.fetch_nights = lambda *a, **kw: {"2026-09-20": slot(5, 0, 3)}
        status, _ = bot.run_canary()
        self.assertEqual(status, bot.CANARY_OK)


class CoverageTests(unittest.TestCase):
    """
    The status email must measure *coverage*, not run count.

    On 2026-09-16 it mailed "PROBLEM: the workflow is not running as often as
    expected" while the bot was watching ~100% of the day. The old heuristic
    wanted 12+ runs, which was right for 70-minute runs and exactly backwards
    for the 350-minute runs introduced later.
    """

    def _fake_api(self, runs):
        """runs: list of (run_id, conclusion, job_start, job_end_or_None)."""
        def fake(path):
            if "/jobs" in path:
                rid = int(path.split("/runs/")[1].split("/")[0])
                for run_id, _c, s, e in runs:
                    if run_id == rid:
                        job = {"started_at": s.strftime("%Y-%m-%dT%H:%M:%SZ")}
                        job["completed_at"] = (
                            e.strftime("%Y-%m-%dT%H:%M:%SZ") if e else None)
                        return {"jobs": [job]}
                return {"jobs": []}
            return {"workflow_runs": [
                {"id": r, "conclusion": c, "status":
                 "completed" if e else "in_progress",
                 "updated_at": (e or s).strftime("%Y-%m-%dT%H:%M:%SZ")}
                for r, c, s, e in runs]}
        return fake

    def _measure(self, runs, now):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "a/b",
                                          "GITHUB_TOKEN": "x"}), \
             mock.patch.object(send_status, "_github_json",
                               side_effect=self._fake_api(runs)):
            return send_status.measure_coverage(hours=24, now=now)

    def test_long_runs_are_full_coverage_not_a_problem(self):
        """Back-to-back 350-minute runs cover the day -- the shape that false-alarmed.

        A 24h window needs five of them (5 x 350 = 1750 min), which is exactly
        why counting runs is the wrong signal: five is plenty, twelve would be
        worse.
        """
        now = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
        runs, cursor = [], now - timedelta(hours=24)
        for i in range(5):
            end = cursor + timedelta(minutes=350)
            runs.append((i, "success", cursor, end if end < now else None))
            cursor = end + timedelta(seconds=3)
        cov = self._measure(runs, now)
        self.assertGreater(cov["covered_minutes"], 23 * 60)
        self.assertLess(cov["max_gap_minutes"], 5)
        self.assertTrue(cov["watching_now"])
        self.assertEqual(cov["runs"], 5)

    def test_many_short_runs_are_poor_coverage(self):
        """12 one-minute runs would have passed the old count>=12 check."""
        now = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
        runs = []
        for i in range(12):
            s = now - timedelta(hours=24) + timedelta(hours=2 * i)
            runs.append((i, "success", s, s + timedelta(minutes=1)))
        cov = self._measure(runs, now)
        self.assertLess(cov["covered_minutes"], 20)
        self.assertGreater(cov["max_gap_minutes"], 100)
        self.assertFalse(cov["watching_now"])

    def test_queued_time_is_not_counted_as_coverage(self):
        """A run queued for hours must not report that wait as watching."""
        now = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
        job_start = now - timedelta(minutes=30)
        cov = self._measure([(1, None, job_start, None)], now)
        self.assertAlmostEqual(cov["covered_minutes"], 30, delta=1)

    def test_gap_between_runs_is_reported(self):
        now = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
        a_start = now - timedelta(hours=12)
        a_end = a_start + timedelta(hours=4)
        b_start = a_end + timedelta(minutes=45)
        cov = self._measure([(1, "success", a_start, a_end),
                             (2, None, b_start, None)], now)
        self.assertAlmostEqual(cov["max_gap_minutes"], 720, delta=1)
        self.assertTrue(cov["watching_now"])

    def test_merge_intervals_collapses_overlaps(self):
        base = datetime(2026, 9, 16, tzinfo=timezone.utc)
        h = lambda n: base + timedelta(hours=n)
        merged = send_status.merge_intervals(
            [(h(0), h(2)), (h(1), h(3)), (h(5), h(6))])
        self.assertEqual(merged, [[h(0), h(3)], [h(5), h(6)]])


class PushTests(unittest.TestCase):
    def test_no_topic_configured_is_a_no_op(self):
        real = bot.NTFY_TOPIC
        bot.NTFY_TOPIC = ""
        self.addCleanup(setattr, bot, "NTFY_TOPIC", real)
        self.assertFalse(bot.send_push("t", "b"))

    def test_headers_are_latin1_safe(self):
        """HTTP headers cannot carry arbitrary unicode; an em dash must not 500."""
        out = bot._header_safe("Lake O'Hara \u2014 Sun Sep 20 \u2713")
        out.encode("latin-1")  # must not raise
        self.assertIn("Lake O'Hara", out)

    def test_push_failure_does_not_raise(self):
        """A dead push service must never stop the email going out."""
        real = bot.NTFY_TOPIC
        bot.NTFY_TOPIC = "x"
        self.addCleanup(setattr, bot, "NTFY_TOPIC", real)
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=OSError("boom")):
            self.assertFalse(bot.send_push("t", "b"))


class BackoffTests(unittest.TestCase):
    """At 10s polling, hammering a WAF that is already blocking earns a longer
    block -- which costs more coverage than the latency it saves."""

    def test_backoff_doubles_and_is_capped(self):
        interval = bot.POLL_INTERVAL_SECONDS
        seen = []
        for _ in range(12):
            interval = min(interval * 2, bot.MAX_POLL_INTERVAL_SECONDS)
            seen.append(interval)
        self.assertEqual(seen[0], bot.POLL_INTERVAL_SECONDS * 2)
        self.assertEqual(max(seen), bot.MAX_POLL_INTERVAL_SECONDS)
        self.assertLessEqual(seen[-1], bot.MAX_POLL_INTERVAL_SECONDS)

    def test_base_interval_is_faster_than_the_cap(self):
        self.assertLess(bot.POLL_INTERVAL_SECONDS,
                        bot.MAX_POLL_INTERVAL_SECONDS)


class WatchdogTests(unittest.TestCase):
    """
    The watchdog restarts the checker via workflow_call, so the restarted job
    runs under the *watchdog's* run. If the probe cannot see that, it starts a
    new six-hour poller every five minutes forever.
    """

    def _probe(self, runs, jobs_by_run):
        import unittest.mock as mock

        def fake(path):
            if "/jobs" in path:
                rid = int(path.split("/runs/")[1].split("/")[0])
                return {"jobs": jobs_by_run.get(rid, [])}
            return {"workflow_runs": runs}

        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "a/b",
                                          "GITHUB_TOKEN": "x"}), \
             mock.patch.object(watchdog, "_github_json", side_effect=fake):
            return watchdog.polling_now()

    def test_sees_a_normal_polling_run(self):
        runs = [{"id": 1, "path": ".github/workflows/check-availability.yml"}]
        jobs = {1: [{"name": "check-campsites", "status": "in_progress"}]}
        self.assertTrue(self._probe(runs, jobs))

    def test_sees_its_own_restart_under_the_watchdog_run(self):
        """Reusable-workflow jobs are named '<caller job> / <called job>'."""
        runs = [{"id": 9, "path": ".github/workflows/watchdog.yml"}]
        jobs = {9: [{"name": "probe", "status": "completed"},
                    {"name": "restart / check-campsites",
                     "status": "in_progress"}]}
        self.assertTrue(self._probe(runs, jobs))

    def test_reports_stale_when_nothing_is_polling(self):
        runs = [{"id": 9, "path": ".github/workflows/watchdog.yml"}]
        jobs = {9: [{"name": "probe", "status": "in_progress"}]}
        self.assertFalse(self._probe(runs, jobs))

    def test_completed_polling_job_does_not_count(self):
        runs = [{"id": 1, "path": ".github/workflows/check-availability.yml"}]
        jobs = {1: [{"name": "check-campsites", "status": "completed"}]}
        self.assertFalse(self._probe(runs, jobs))

    def test_unreadable_api_never_triggers_a_restart(self):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "a/b",
                                          "GITHUB_TOKEN": "x"}), \
             mock.patch.object(watchdog, "_github_json", return_value=None):
            self.assertIsNone(watchdog.polling_now())


class LiveTests(unittest.TestCase):
    """Hit the real Parks Canada API."""

    def test_canary_reports_open_nights(self):
        status, message = bot.run_canary()
        if status == bot.CANARY_UNREACHABLE:
            self.skipTest(f"API unreachable, not a detector fault: {message}")
        self.assertEqual(status, bot.CANARY_OK, message)

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
