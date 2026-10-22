"""Validated, revision-safe caregiver settings for Button Box."""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from messagebox.runtime_paths import APP_DIR, SETTINGS_FILE


SCHEMA_VERSION = 1
TALK_MODES = frozenset({"tap", "hold"})
RECORDING_MODES = frozenset({"tap_review", "hold_release"})
AFTER_LISTENING = frozenset({"play_only", "invite_reply"})
MAX_RECORDING_SECONDS = frozenset({30, 60, 120})
RINGTONES = {
    "hello_piano": "hello_piano.wav",
    "sunshine": "sunshine.wav",
    "bouncy": "bouncy.wav",
    "sing_along": "sing_along.wav",
    "island": "island.wav",
    "hello": "hello.wav",
    "ukulele": "ukulele.wav",
}
LEGACY_RINGTONES = frozenset({"gentle_music_box", "playful_chiptune", "ding_dong", "cuckoo_clock"})
DEFAULT_RINGTONE = "hello_piano"
VOICE_PACKS = ("jessica", "pirate", "alien", "dj", "robot", "french", "charlie", "tata", "buttonbox")


def normalize_voice_pack(value):
    return value if isinstance(value, str) and value in VOICE_PACKS else "jessica"


ARRIVAL_SIGNALS = frozenset({"ring_and_lamp", "ring_only", "lamp_only", "silent"})
_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
QUIET_DEFAULTS = {"quiet_schedules": None, "weekend_days": None,
                  "holiday_until": None, "morning_ring": True}
_ROOT_KEYS = {
    "version",
    "revision",
    "timezone",
    "recording_mode",
    "talk_mode",
    "review_before_send",
    "after_listening",
    "max_recording_seconds",
    "ringtone_id",
    "voice_pack",
    "master_volume_percent",
    "arrival_signal",
    "quiet_hours",
    "quiet_schedules",
    "weekend_days",
    "holiday_until",
    "morning_ring",
    "nfc_confirmation_beep",
    "say_reactions",
    "card_name_prompt",
    "swoosh_sound_enabled",
    "announce_messages_voice",
}
_REQUIRED_ROOT_KEYS = _ROOT_KEYS - set(QUIET_DEFAULTS) - {"say_reactions", "swoosh_sound_enabled", "voice_pack", "card_name_prompt",
                                   "recording_mode", "talk_mode", "review_before_send",
                                   "announce_messages_voice"}


class SettingsError(ValueError):
    """A safe caregiver-settings error."""


class RevisionConflict(SettingsError):
    """The settings changed after a caregiver loaded the form."""


def in_quiet_hours(settings, now=None, *, cloud=False):
    if cloud:
        current = now or datetime.now(ZoneInfo(settings["timezone"]))
        current = current.astimezone(ZoneInfo(settings["timezone"]))
        return any(start <= current < end for start, end in quiet_intervals(settings, current.date()))
    quiet = settings["quiet_hours"]
    if not quiet["enabled"]:
        return False
    current = now or datetime.now(ZoneInfo(settings["timezone"]))
    minute = current.hour * 60 + current.minute
    start, end = (int(value[:2]) * 60 + int(value[3:])
                  for value in (quiet["start"], quiet["end"]))
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def quiet_schedule(settings, day):
    """A crossing-midnight period belongs to its ending day's schedule."""
    schedules = settings.get("quiet_schedules")
    if schedules is None:
        quiet = settings["quiet_hours"]
        return [{"start": quiet["start"], "end": quiet["end"]}] if quiet["enabled"] else []
    days = settings.get("weekend_days")
    if days is None:
        days = [4, 5] if settings["timezone"] == "Asia/Jerusalem" else [5, 6]
    holiday = settings.get("holiday_until")
    weekend = day.weekday() in days or (holiday is not None and day <= date.fromisoformat(holiday))
    return schedules["weekend" if weekend else "school_days"]


def quiet_intervals(settings, day):
    zone = ZoneInfo(settings["timezone"])
    intervals = []
    # Include tomorrow's night: Friday evening uses Saturday's schedule.
    for end_day in (day, day + timedelta(days=1)):
        for period in quiet_schedule(settings, end_day):
            start = datetime.combine(end_day, datetime.strptime(period["start"], "%H:%M").time(), zone)
            end = datetime.combine(end_day, datetime.strptime(period["end"], "%H:%M").time(), zone)
            if start == end:
                start = start.replace(hour=0, minute=0)
                end = start + timedelta(days=1)
            elif start > end:
                start -= timedelta(days=1)
            intervals.append((start, end))
    return intervals


def morning_quiet_end(settings, current):
    """Return only the latest morning end, even when periods overlap."""
    periods = [p for p in quiet_schedule(settings, current.date())
               if p["start"] != p["end"] and "04:00" <= p["end"] <= "11:00"]
    if not periods:
        return None
    period = max(periods, key=lambda p: p["end"])
    hour, minute = map(int, period["end"].split(":"))
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)


def normalize_quiet_settings(document):
    document = {**QUIET_DEFAULTS, **document}
    schedules = document["quiet_schedules"]
    if schedules is not None:
        if not isinstance(schedules, dict) or set(schedules) != {"school_days", "weekend"}:
            raise SettingsError("quiet schedules are invalid")
        for periods in schedules.values():
            if not isinstance(periods, list) or len(periods) > 3:
                raise SettingsError("quiet periods are invalid")
            for period in periods:
                if (not isinstance(period, dict) or set(period) != {"start", "end"}
                        or any(not isinstance(period[key], str) or not _TIME.fullmatch(period[key])
                               for key in ("start", "end"))):
                    raise SettingsError("quiet period times are invalid")
    days = document["weekend_days"]
    if days is not None and (not isinstance(days, list) or len(days) > 7
                            or any(type(day) is not int or not 0 <= day <= 6 for day in days)
                            or len(set(days)) != len(days)):
        raise SettingsError("weekend days are invalid")
    holiday = document["holiday_until"]
    if holiday is not None:
        if not isinstance(holiday, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", holiday):
            raise SettingsError("holiday end date is invalid")
        try:
            date.fromisoformat(holiday)
        except ValueError as exc:
            raise SettingsError("holiday end date is invalid") from exc
    if type(document["morning_ring"]) is not bool:
        raise SettingsError("morning ring value is invalid")
    return document


def _env_flag(environ, name, default):
    raw = environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(environ, name, default):
    try:
        return int(str(environ.get(name, default)).rstrip("%"))
    except (TypeError, ValueError):
        return default


def normalize_ringtone_id(ringtone_id):
    if not isinstance(ringtone_id, str):
        return None
    return DEFAULT_RINGTONE if ringtone_id in LEGACY_RINGTONES else ringtone_id



def defaults(environ=None):
    environ = os.environ if environ is None else environ
    maximum = _env_int(environ, "MSGBOX_MAX_SECONDS", 60)
    if maximum not in MAX_RECORDING_SECONDS:
        maximum = 60
    ring_name = Path(environ.get("MSGBOX_RING_WAV", RINGTONES[DEFAULT_RINGTONE])).name
    ringtone = next((key for key, name in RINGTONES.items() if name == ring_name), DEFAULT_RINGTONE)
    volume = min(100, max(0, _env_int(environ, "MSGBOX_SPEAKER_VOLUME", 50)))
    start_hour = min(23, max(0, _env_int(environ, "MSGBOX_QUIET_START_H", 22)))
    end_hour = min(23, max(0, _env_int(environ, "MSGBOX_QUIET_END_H", 7)))
    timezone = environ.get("TZ", "UTC") or "UTC"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = "UTC"
    return {
        **QUIET_DEFAULTS,
        "version": SCHEMA_VERSION,
        "revision": 0,
        "timezone": timezone,
        "recording_mode": "tap_review",
        "talk_mode": "tap",
        "review_before_send": True,
        "after_listening": "play_only",
        "max_recording_seconds": maximum,
        "ringtone_id": ringtone,
        "master_volume_percent": volume,
        "arrival_signal": "ring_and_lamp",
        "quiet_hours": {
            "enabled": True,
            "start": f"{start_hour:02d}:00",
            "end": f"{end_hour:02d}:00",
        },
        "nfc_confirmation_beep": _env_flag(environ, "MSGBOX_NFC_DETECTION_BEEP", True),
        "swoosh_sound_enabled": True,
        "card_name_prompt": True,
        "say_reactions": True,
        "voice_pack": "buttonbox",
        "announce_messages_voice": False,
    }


def normalize_recording_settings(document):
    """New fields are authoritative; legacy-only saved documents retain their choice."""
    document = dict(document)
    legacy = document.get("recording_mode")
    if "recording_mode" in document and (not isinstance(legacy, str) or legacy not in RECORDING_MODES):
        raise SettingsError("recording mode is invalid")
    if "talk_mode" not in document and "review_before_send" not in document:
        if legacy is None:
            raise SettingsError("recording settings are missing")
        document["talk_mode"] = "tap" if legacy == "tap_review" else "hold"
        document["review_before_send"] = legacy == "tap_review"
    if "talk_mode" not in document or "review_before_send" not in document:
        raise SettingsError("recording settings are incomplete")
    # The old interface can represent only these two combinations. Keep its
    # nearest mode for consumers that still display the legacy field.
    document["recording_mode"] = "tap_review" if document["talk_mode"] == "tap" else "hold_release"
    return document


def ignored_settings(document):
    """Return skipped names that fit the bounded Cloud acknowledgement field."""
    if not isinstance(document, dict):
        return []
    return sorted(key for key in document if key not in _ROOT_KEYS
                  and isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key))[:32]


def validate(document, *, default_voice_pack="buttonbox"):
    if not isinstance(document, dict):
        raise SettingsError("settings have an invalid schema")
    document = normalize_recording_settings(
        {key: value for key, value in document.items() if key in _ROOT_KEYS})
    if _REQUIRED_ROOT_KEYS <= set(document) <= _ROOT_KEYS:
        # Callers that accept a Cloud document must supply its retained voice.
        document = normalize_quiet_settings({"say_reactions": True, "swoosh_sound_enabled": True, "voice_pack": default_voice_pack, "card_name_prompt": True,
                                               "announce_messages_voice": False, **document})
        document["voice_pack"] = normalize_voice_pack(document["voice_pack"])
    if set(document) != _ROOT_KEYS:
        raise SettingsError("settings have an invalid schema")
    if document["version"] != SCHEMA_VERSION:
        raise SettingsError("settings version is unsupported")
    if type(document["revision"]) is not int or document["revision"] < 0:
        raise SettingsError("settings revision is invalid")
    timezone = document["timezone"]
    if not isinstance(timezone, str) or not timezone or len(timezone) > 64:
        raise SettingsError("time zone is invalid")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingsError("time zone is invalid") from exc
    if not isinstance(document["talk_mode"], str) or document["talk_mode"] not in TALK_MODES:
        raise SettingsError("talk mode is invalid")
    if type(document["review_before_send"]) is not bool:
        raise SettingsError("review before send value is invalid")
    if document["recording_mode"] not in RECORDING_MODES:
        raise SettingsError("recording mode is invalid")
    if document["after_listening"] not in AFTER_LISTENING:
        raise SettingsError("after-listening behavior is invalid")
    if document["max_recording_seconds"] not in MAX_RECORDING_SECONDS:
        raise SettingsError("maximum recording length is invalid")
    document = {**document, "ringtone_id": normalize_ringtone_id(document["ringtone_id"])}
    if document["ringtone_id"] not in RINGTONES:
        raise SettingsError("ringtone is invalid")
    volume = document["master_volume_percent"]
    if type(volume) is not int or not 0 <= volume <= 100:
        raise SettingsError("master volume is invalid")
    if document["arrival_signal"] not in ARRIVAL_SIGNALS:
        raise SettingsError("arrival signal is invalid")
    quiet = document["quiet_hours"]
    if not isinstance(quiet, dict) or set(quiet) != {"enabled", "start", "end"}:
        raise SettingsError("quiet hours are invalid")
    if type(quiet["enabled"]) is not bool:
        raise SettingsError("quiet hours enabled value is invalid")
    if not isinstance(quiet["start"], str) or not _TIME.fullmatch(quiet["start"]):
        raise SettingsError("quiet hours start time is invalid")
    if not isinstance(quiet["end"], str) or not _TIME.fullmatch(quiet["end"]):
        raise SettingsError("quiet hours end time is invalid")
    if type(document["nfc_confirmation_beep"]) is not bool:
        raise SettingsError("NFC confirmation beep value is invalid")
    if type(document["say_reactions"]) is not bool:
        raise SettingsError("reaction setting is invalid")
    if type(document["card_name_prompt"]) is not bool:
        raise SettingsError("card name prompt value is invalid")
    if type(document["swoosh_sound_enabled"]) is not bool:
        raise SettingsError("swoosh sound value is invalid")
    if type(document["announce_messages_voice"]) is not bool:
        raise SettingsError("message voice intro value is invalid")
    return copy.deepcopy(document)


def ringtone_path(document):
    return APP_DIR / "ringtones" / RINGTONES[normalize_ringtone_id(document["ringtone_id"])]


def load_ring_lamp_schedule(path, ringtone_id):
    """Reject malformed schedules before the hardware loop uses their spans."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("ringtone_id") != ringtone_id:
        raise ValueError("ringtone lamp schedule is invalid")
    seconds = document.get("seconds")
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ringtone lamp duration is invalid")
    spans = document.get("lamp_on")
    if not isinstance(spans, list) or not spans:
        raise ValueError("ringtone lamp spans are invalid")
    previous = -1
    for span in spans:
        if (not isinstance(span, list) or len(span) != 2
                or any(type(value) not in {int, float} or not math.isfinite(value) for value in span)
                or not 0 <= span[0] < span[1] <= seconds or span[0] < previous):
            raise ValueError("ringtone lamp spans are invalid")
        previous = span[0]
    return tuple((start, end) for start, end in spans)


def _atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o660)
            json.dump(document, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _open_shared_lock(path):
    """Set the shared mode only while we own a newly created lock."""
    while True:
        try:
            lock = open(path, "x+b")
        except FileExistsError:
            try:
                return open(path, "r+b")
            except FileNotFoundError:
                continue
        try:
            os.fchmod(lock.fileno(), 0o660)
        except OSError:
            lock.close()
            raise
        return lock


class SettingsStore:
    def __init__(self, path=SETTINGS_FILE, *, environ=None):
        self.path = Path(path)
        self.last_good_path = self.path.with_name(f"{self.path.stem}.last-good.json")
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.environ = os.environ if environ is None else environ

    def _read(self, path):
        with open(path, encoding="utf-8") as handle:
            # An existing legacy Cloud file is not a fresh install.
            fallback = "jessica" if self.environ.get("MSGBOX_TRANSPORT") == "cloud" else "buttonbox"
            return validate(json.load(handle), default_voice_pack=fallback)

    def load(self):
        try:
            return self._read(self.path), False
        except FileNotFoundError:
            document = defaults(self.environ)
            try:
                self._write_pair(document)
            except OSError:
                return document, True
            return document, False
        except (OSError, ValueError, SettingsError):
            try:
                return self._read(self.last_good_path), True
            except (FileNotFoundError, OSError, ValueError, SettingsError):
                # A broken saved configuration is not a fresh box. Retain the
                # earlier fallback rather than silently opting into tap mode.
                document = defaults(self.environ)
                document.update(recording_mode="hold_release", talk_mode="hold", review_before_send=False)
                return document, True

    def _write_pair(self, document):
        _atomic_json(self.path, document)
        _atomic_json(self.last_good_path, document)

    def update(self, candidate, expected_revision, *, desired_revision=None):
        if not isinstance(candidate, dict):
            raise SettingsError("settings request must be an object")
        retain_voice = self.environ.get("MSGBOX_TRANSPORT") == "cloud" and "voice_pack" not in candidate
        candidate = {**QUIET_DEFAULTS, "voice_pack": "buttonbox", "card_name_prompt": True, "say_reactions": True,
                     "announce_messages_voice": False, **candidate}
        value_keys = _ROOT_KEYS - {"version", "revision"}
        candidate = normalize_recording_settings(
            {key: value for key, value in candidate.items() if key in _ROOT_KEYS})
        if set(candidate) != value_keys:
            raise SettingsError("settings request has an invalid schema")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _open_shared_lock(self.lock_path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current, _warning = self.load()
                if retain_voice:
                    candidate["voice_pack"] = current["voice_pack"]
                if type(expected_revision) is not int or expected_revision != current["revision"]:
                    raise RevisionConflict("settings changed in another browser; reload and try again")
                revision = current["revision"] + 1 if desired_revision is None else desired_revision
                if type(revision) is not int or revision <= current["revision"]:
                    raise SettingsError("settings revision must advance")
                document = {
                    "version": SCHEMA_VERSION,
                    "revision": revision,
                    **candidate,
                }
                document = validate(document)
                self._write_pair(document)
                return document
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class SettingsReader:
    """Cache settings briefly so the hardware loop never blocks on every poll."""

    def __init__(self, store=None, *, clock=time.monotonic, refresh_seconds=0.5):
        self.store = store or SettingsStore()
        self.clock = clock
        self.refresh_seconds = refresh_seconds
        self._loaded_at = float("-inf")
        self._document = defaults()

    def snapshot(self):
        now = self.clock()
        if now - self._loaded_at >= self.refresh_seconds:
            self._document, _warning = self.store.load()
            self._loaded_at = now
        return copy.deepcopy(self._document)
