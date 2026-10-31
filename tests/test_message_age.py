import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from messagebox import cloud_runtime
from messagebox.message_age import PHRASE_KEYS, age_phrase, day_part, MORNING, AFTERNOON, EVENING, NIGHT

LISBON = ZoneInfo("Europe/Lisbon")


def at(year, month, day, hour, minute=0, second=0, *, fold=0, zone=LISBON):
    return datetime(year, month, day, hour, minute, second, fold=fold, tzinfo=zone).timestamp()


# Wednesday 14 October 2026 (WEST, UTC+1). Tuesday is the 13th.
def wed(hour, minute=0, second=0):
    return at(2026, 10, 14, hour, minute, second)


def tue(hour, minute=0, second=0):
    return at(2026, 10, 13, hour, minute, second)


class MessageAgeRuleTests(unittest.TestCase):
    def say(self, sent, now, zone="Europe/Lisbon"):
        return age_phrase(sent, now, zone)

    def test_three_hour_threshold(self):
        self.assertIsNone(self.say(wed(10, 0), wed(12, 59)))
        self.assertIsNone(self.say(wed(10, 0), wed(12, 59, 59)))
        self.assertEqual(self.say(wed(10, 0), wed(13, 0)), "this-morning")
        # Across the night: Wednesday 02:01 is Tuesday night; 05:00 is morning.
        self.assertIsNone(self.say(wed(2, 1), wed(5, 0)))
        self.assertEqual(self.say(wed(2, 0), wed(5, 0)), "last-night")

    def test_day_part_edges(self):
        cases = (
            ((0, 0, 0), (datetime(2026, 10, 13).date(), NIGHT)),
            ((4, 59, 59), (datetime(2026, 10, 13).date(), NIGHT)),
            ((5, 0, 0), (datetime(2026, 10, 14).date(), MORNING)),
            ((11, 59, 59), (datetime(2026, 10, 14).date(), MORNING)),
            ((12, 0, 0), (datetime(2026, 10, 14).date(), AFTERNOON)),
            ((16, 59, 59), (datetime(2026, 10, 14).date(), AFTERNOON)),
            ((17, 0, 0), (datetime(2026, 10, 14).date(), EVENING)),
            ((20, 59, 59), (datetime(2026, 10, 14).date(), EVENING)),
            ((21, 0, 0), (datetime(2026, 10, 14).date(), NIGHT)),
            ((23, 59, 59), (datetime(2026, 10, 14).date(), NIGHT)),
        )
        for (hour, minute, second), expected in cases:
            with self.subTest(hour=hour, minute=minute, second=second):
                self.assertEqual(day_part(datetime(2026, 10, 14, hour, minute, second, tzinfo=LISBON)), expected)

    def test_phrases_change_exactly_at_day_part_edges(self):
        self.assertEqual(self.say(wed(11, 59, 59), wed(17, 0)), "this-morning")
        self.assertEqual(self.say(wed(12, 0), wed(17, 0)), "this-afternoon")
        self.assertEqual(self.say(wed(16, 59, 59), wed(21, 0)), "this-afternoon")
        self.assertEqual(self.say(wed(17, 0), wed(21, 0)), "this-evening")
        self.assertEqual(self.say(wed(17, 0), wed(20, 59, 59)), None)  # Same evening.
        self.assertEqual(self.say(wed(5, 0), wed(11, 59, 59)), None)   # Same morning, 7 hours old.
        self.assertEqual(self.say(tue(21, 0), wed(4, 59, 59)), None)   # Same night, 8 hours old.
        self.assertEqual(self.say(tue(21, 0), wed(5, 0)), "last-night")

    def test_a_night_belongs_to_the_day_it_started_on(self):
        for sent in (tue(23, 0), wed(2, 0)):
            with self.subTest(sent=sent):
                self.assertEqual(self.say(sent, wed(8, 0)), "last-night")
                self.assertEqual(self.say(sent, wed(16, 0)), "last-night")
        self.assertEqual(self.say(tue(10, 0), wed(8, 0)), "yesterday")
        self.assertEqual(self.say(tue(20, 0), wed(1, 0)), "this-evening")
        self.assertIsNone(self.say(tue(23, 0), wed(3, 0)))
        # Wednesday 22:00 is Wednesday night, so Tuesday night is still last night.
        self.assertEqual(self.say(tue(23, 0), wed(22, 0)), "last-night")
        # Tuesday 02:00 is Monday night: two day anchors before Wednesday.
        self.assertEqual(self.say(tue(2, 0), wed(10, 0)), "2-days-ago")

    def test_midnight(self):
        self.assertIsNone(self.say(tue(23, 59), wed(0, 0)))
        self.assertIsNone(self.say(tue(21, 0, 1), wed(0, 0)))  # 2:59:59 old, same night.
        self.assertEqual(self.say(tue(20, 59, 59), wed(0, 0)), "this-evening")
        self.assertEqual(self.say(tue(14, 0), wed(0, 0)), "this-afternoon")
        self.assertEqual(self.say(tue(14, 0), wed(5, 0)), "yesterday")
        self.assertEqual(self.say(tue(14, 0), wed(4, 59, 59)), "this-afternoon")

    def test_days_weeks_and_a_while(self):
        sent = at(2026, 10, 1, 10)
        cases = {2: "2-days-ago", 3: "3-days-ago", 4: "4-days-ago", 5: "5-days-ago", 6: "6-days-ago",
                 7: "last-week", 13: "last-week", 14: "a-while-ago", 60: "a-while-ago"}
        for days, expected in cases.items():
            with self.subTest(days=days):
                now = at(2026, 10, 1 + days, 9) if days < 31 else at(2026, 11, 30, 9)
                self.assertEqual(self.say(sent, now), expected)

    def test_each_of_the_phrases(self):
        cases = {
            "this-morning": (wed(8), wed(13)),
            "this-afternoon": (wed(13), wed(18)),
            "this-evening": (wed(17), wed(22)),
            "last-night": (tue(22), wed(8)),
            "yesterday": (tue(9), wed(9)),
            "2-days-ago": (at(2026, 10, 12, 9), wed(9)),
            "3-days-ago": (at(2026, 10, 11, 9), wed(9)),
            "4-days-ago": (at(2026, 10, 10, 9), wed(9)),
            "5-days-ago": (at(2026, 10, 9, 9), wed(9)),
            "6-days-ago": (at(2026, 10, 8, 9), wed(9)),
            "last-week": (at(2026, 10, 7, 9), wed(9)),
            "a-while-ago": (at(2026, 9, 30, 9), wed(9)),
        }
        self.assertEqual(set(cases), set(PHRASE_KEYS))
        self.assertEqual(len(PHRASE_KEYS), 12)
        for key, (sent, now) in cases.items():
            with self.subTest(key=key):
                self.assertEqual(self.say(sent, now), key)

    def test_lisbon_spring_forward(self):
        # 29 March 2026: 01:00 WET becomes 02:00 WEST.
        self.assertEqual(self.say(at(2026, 3, 28, 23, 30), at(2026, 3, 29, 5, 0)), "last-night")
        self.assertEqual(self.say(at(2026, 3, 29, 0, 30), at(2026, 3, 29, 5, 0)), "last-night")
        # Age is real elapsed time: 04:59 WEST is 2:59 after 02:00 WEST.
        self.assertIsNone(self.say(at(2026, 3, 29, 2, 0), at(2026, 3, 29, 4, 59)))
        # Days are calendar days: 23.5 real hours apart, but yesterday morning.
        self.assertEqual(self.say(at(2026, 3, 28, 10, 0), at(2026, 3, 29, 10, 30)), "yesterday")
        self.assertEqual(self.say(at(2026, 3, 28, 9, 0), at(2026, 3, 29, 12, 30)), "yesterday")

    def test_lisbon_fall_back(self):
        # 25 October 2026: 02:00 WEST becomes 01:00 WET; 01:30 happens twice.
        first, second = at(2026, 10, 25, 1, 30, fold=0), at(2026, 10, 25, 1, 30, fold=1)
        self.assertEqual(second - first, 3600)
        for sent in (first, second):
            with self.subTest(sent=sent):
                self.assertEqual(self.say(sent, at(2026, 10, 25, 5, 0)), "last-night")
        self.assertIsNone(self.say(second, at(2026, 10, 25, 4, 29)))  # 2:59 real, same night.
        self.assertEqual(self.say(at(2026, 10, 24, 23, 30), at(2026, 10, 25, 9, 0)), "last-night")
        self.assertEqual(self.say(at(2026, 10, 20, 10, 0), at(2026, 10, 26, 9, 30)), "6-days-ago")
        self.assertEqual(self.say(at(2026, 10, 20, 10, 0), at(2026, 10, 27, 9, 30)), "last-week")

    def test_uses_the_box_timezone(self):
        utc = ZoneInfo("UTC")
        sent, now = at(2026, 10, 14, 20, 0, zone=utc), at(2026, 10, 15, 8, 0, zone=utc)
        self.assertEqual(self.say(sent, now, "UTC"), "yesterday")
        self.assertEqual(self.say(sent, now, "Europe/Lisbon"), "last-night")
        self.assertEqual(self.say(sent, now, ZoneInfo("Asia/Jerusalem")), "last-night")
        self.assertEqual(self.say(sent, at(2026, 10, 15, 4, 0, zone=utc), "America/New_York"), "this-afternoon")

    def test_says_nothing_without_a_trustworthy_send_time_or_clock(self):
        now = wed(12)
        for sent in (None, "1760000000", True, float("nan"), float("inf"), 0, -5, 1_000_000_000,
                     now + 1, now + 3 * 86400):
            with self.subTest(sent=sent):
                self.assertIsNone(self.say(sent, now))
        sent = tue(9)
        for clock in (None, float("nan"), 0, "now"):
            with self.subTest(clock=clock):
                self.assertIsNone(self.say(sent, clock))
        for zone in (None, "", "Mars/Base", "../etc/passwd"):
            with self.subTest(zone=zone):
                self.assertIsNone(self.say(sent, now, zone))
        self.assertEqual(self.say(sent, now), "yesterday")
        self.assertEqual(self.say(int(sent), int(now)), "yesterday")

    def test_verified_now_needs_a_fresh_cloud_clock(self):
        runtime = mock.Mock()
        runtime.server_now.return_value = 1_760_000_000.5
        with mock.patch.object(cloud_runtime, "CloudRuntime", return_value=runtime) as factory:
            self.assertEqual(cloud_runtime.verified_now(), 1_760_000_000.5)
        self.assertIs(type(factory.call_args.kwargs["client"]), object)
        runtime.server_now.side_effect = cloud_runtime.CloudRuntimeError("device time is unverified")
        with mock.patch.object(cloud_runtime, "CloudRuntime", return_value=runtime):
            self.assertIsNone(cloud_runtime.verified_now())
        with mock.patch.object(cloud_runtime, "CloudRuntime", side_effect=cloud_runtime.CloudRuntimeError("x")):
            self.assertIsNone(cloud_runtime.verified_now())


if __name__ == "__main__":
    unittest.main()
