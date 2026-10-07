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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from messagebox.runtime_paths import APP_DIR, SETTINGS_FILE


SCHEMA_VERSION = 1
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
VOICE_PACKS = ("jessica", "pirate", "alien", "dj", "robot", "french", "charlie")


def normalize_voice_pack(value):
    return value if isinstance(value, str) and value in VOICE_PACKS else "jessica"


ARRIVAL_SIGNALS = frozenset({"ring_and_lamp", "ring_only", "lamp_only", "silent"})
_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_ROOT_KEYS = {
    "version",
    "revision",
    "timezone",
    "recording_mode",
    "after_listening",
    "max_recording_seconds",
    "ringtone_id",
    "voice_pack",
    "master_volume_percent",
    "arrival_signal",
    "quiet_hours",
    "nfc_confirmation_beep",
    "swoosh_sound_enabled",
}
_REQUIRED_ROOT_KEYS = _ROOT_KEYS - {"swoosh_sound_enabled", "voice_pack"}


class SettingsError(ValueError):
    """A safe caregiver-settings error."""


class RevisionConflict(SettingsError):
    """The settings changed after a caregiver loaded the form."""


def in_quiet_hours(settings, now=None):
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
        "version": SCHEMA_VERSION,
        "revision": 0,
        "timezone": timezone,
        "recording_mode": "hold_release",
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
        "voice_pack": "jessica",
    }


def validate(document):
    if not isinstance(document, dict):
        raise SettingsError("settings have an invalid schema")
    if _REQUIRED_ROOT_KEYS <= set(document) <= _ROOT_KEYS:
        # Persisted settings from earlier releases retain their cue and default voice.
        document = {"swoosh_sound_enabled": True, "voice_pack": "jessica", **document}
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
    if type(document["swoosh_sound_enabled"]) is not bool:
        raise SettingsError("swoosh sound value is invalid")
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
            return validate(json.load(handle))

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
                return defaults(self.environ), True

    def _write_pair(self, document):
        _atomic_json(self.path, document)
        _atomic_json(self.last_good_path, document)

    def update(self, candidate, expected_revision, *, desired_revision=None):
        if not isinstance(candidate, dict):
            raise SettingsError("settings request must be an object")
        candidate = {"voice_pack": "jessica", **candidate}
        value_keys = _ROOT_KEYS - {"version", "revision"}
        if set(candidate) != value_keys:
            raise SettingsError("settings request has an invalid schema")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _open_shared_lock(self.lock_path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current, _warning = self.load()
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
