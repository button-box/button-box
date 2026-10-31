"""When a message plays long after it was sent, name when it was sent.

Pure rule, no I/O. The box says a phrase only when the message is at least
three hours old AND was sent in an earlier day part or on an earlier day, both
in the box's timezone. Day parts: morning 05-12, afternoon 12-17, evening
17-21, night 21-05. A night belongs to the day it started on, so Tuesday 23:00
and Wednesday 02:00 are both "Tuesday night". Day counts compare those day
anchors as calendar dates, so DST changes never shift them.
"""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MIN_AGE_SECONDS = 3 * 3600
MORNING, AFTERNOON, EVENING, NIGHT = range(4)
TODAY_KEYS = {MORNING: "this-morning", AFTERNOON: "this-afternoon", EVENING: "this-evening"}
PHRASE_KEYS = (
    "this-morning", "this-afternoon", "this-evening", "last-night", "yesterday",
    "2-days-ago", "3-days-ago", "4-days-ago", "5-days-ago", "6-days-ago",
    "last-week", "a-while-ago",
)
# The same plausible range the Cloud runtime accepts for server times.
_EARLIEST, _LATEST = 1_700_000_000, 4_102_444_800


def _valid_time(value):
    return (type(value) in (int, float) and math.isfinite(value)
            and _EARLIEST <= value < _LATEST)


def day_part(moment):
    """Return (anchor date, part) for a local datetime."""
    hour = moment.hour
    if hour < 5:
        return moment.date() - timedelta(days=1), NIGHT
    if hour < 12:
        return moment.date(), MORNING
    if hour < 17:
        return moment.date(), AFTERNOON
    if hour < 21:
        return moment.date(), EVENING
    return moment.date(), NIGHT


def age_phrase(sent_at, now, zone):
    """Return the phrase key for a message, or None to say nothing.

    sent_at and now are Unix seconds; now must come from a verified clock.
    A missing, invalid or future send time, or an unverified clock, says nothing.
    """
    if not _valid_time(sent_at) or not _valid_time(now) or sent_at > now:
        return None
    if now - sent_at < MIN_AGE_SECONDS:
        return None
    try:
        zone = zone if isinstance(zone, ZoneInfo) else ZoneInfo(zone)
    except (TypeError, ValueError, KeyError, OSError):
        return None
    sent_day, sent_part = day_part(datetime.fromtimestamp(sent_at, timezone.utc).astimezone(zone))
    now_day, now_part = day_part(datetime.fromtimestamp(now, timezone.utc).astimezone(zone))
    days = (now_day - sent_day).days
    if days == 0:
        # Same day part: the message is old, but not from an earlier part of the day.
        return TODAY_KEYS[sent_part] if sent_part < now_part else None
    if days == 1:
        return "last-night" if sent_part == NIGHT else "yesterday"
    if days <= 6:
        return f"{days}-days-ago"
    if days <= 13:
        return "last-week"
    return "a-while-ago"
