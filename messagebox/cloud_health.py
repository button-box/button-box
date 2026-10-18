"""Box health for the Cloud heartbeat: codes and numbers only.

Never report audio, transcripts, names, phone numbers, message or box ids,
hostnames, or full paths. Every collector is cheap or cached, and a collector
that fails reports null; it never fails the heartbeat.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from messagebox.cloud_device import CLOUD_DIR, atomic_json
from messagebox.runtime_paths import APP_DIR, DATA_DIR, NFC_HEALTH_FILE

MAX_BYTES = 2048
DAY = 86400
AUDIO_KINDS = ("message", "voice_line", "cue", "ringtone", "preview")
AUDIO_FILE = CLOUD_DIR / "audio-health.json"
SERVICES_FILE = CLOUD_DIR / "service-restarts.json"
# Long-running box services. Units that are not installed are skipped.
SERVICES = ("button", "poller", "sync", "dash", "nfc", "update-controller", "wifi-watchdog")
SERVICES_TTL = 300
CLOCK_TTL = 300
NFC_FRESH_S = 30
MAX_FAILURES_PER_KIND = 200
SOUND_CHECK_FIRST_S = 120
SOUND_CHECK_EVERY_S = 6 * 3600
MAX_BAD = 10
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BAD = re.compile(r"^[a-z0-9_-]{1,40}/[a-z0-9_.-]{1,80}$")
_SERVICE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_kind = threading.local()


# Audio player outcomes, shared across the box's processes through one small file.

@contextmanager
def audio_kind(kind):
    """Count players started in this block as `kind` (for example a preview)."""
    previous = getattr(_kind, "value", None)
    _kind.value = kind
    try:
        yield
    finally:
        _kind.value = previous


def classify(path):
    override = getattr(_kind, "value", None)
    if override in AUDIO_KINDS:
        return override
    try:
        path = Path(path)
        for root, kind in ((APP_DIR / "ringtones", "ringtone"), (APP_DIR / "sounds" / "ringtones", "ringtone"),
                           (APP_DIR / "sounds" / "cues", "cue"), (APP_DIR / "sounds" / "voice", "voice_line"),
                           (APP_DIR / "sounds" / "voices", "voice_line"), (APP_DIR / "sounds" / "cloud", "voice_line"),
                           (CLOUD_DIR / "voice-lines", "voice_line")):
            if path.is_relative_to(root):
                return kind
    except (TypeError, ValueError):
        pass
    return "message"


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load_audio(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": None, "failures": {}, "last_error": None}
    return state if isinstance(state, dict) else {"ok": None, "failures": {}, "last_error": None}


def record_audio(kind, code=None, *, path=None, now=None):
    """Record one player outcome: code None means it played. Never raises."""
    path = AUDIO_FILE if path is None else Path(path)
    now = int(time.time() if now is None else now)
    try:
        with _locked(path):
            state = _load_audio(path)
            if code is None:
                if state.get("ok") is True:
                    return  # Common case: no write for a normal playback.
                state["ok"] = True
            else:
                if kind not in AUDIO_KINDS or not _CODE.fullmatch(code):
                    return
                failures = state.get("failures") if isinstance(state.get("failures"), dict) else {}
                times = [t for t in failures.get(kind, []) if type(t) is int and now - DAY < t <= now]
                failures[kind] = (times + [now])[-MAX_FAILURES_PER_KIND:]
                state.update(ok=False, failures=failures, last_error={"code": code, "at": now})
            atomic_json(path, state)
    except OSError:
        pass


def player_finished(process, path):
    """Called once a player exits. Deliberate stops (a press, mute) are not failures."""
    try:
        _player_finished(process, path)
    except Exception:
        pass  # Health bookkeeping must never disturb playback.


def _player_finished(process, path):
    code = getattr(process, "health_code", None)
    if code is None and getattr(process, "health_stopped", False):
        return
    returncode = process.returncode
    if code is None:
        if returncode == 0:
            record_audio(getattr(process, "health_kind", "message"))
            return
        if returncode is None or returncode < 0:
            return  # Killed from outside (shutdown); not a playback fault.
        code = "play_failed" if Path(path).exists() else "file_missing"
    record_audio(getattr(process, "health_kind", "message"), code)


def audio_report(*, path=None, now=None):
    state = _load_audio(AUDIO_FILE if path is None else Path(path))
    now = int(time.time() if now is None else now)
    failures = state.get("failures") if isinstance(state.get("failures"), dict) else {}
    counts = {kind: sum(1 for t in failures.get(kind, []) if type(t) is int and now - DAY < t <= now)
              for kind in AUDIO_KINDS}
    error = state.get("last_error")
    if not (isinstance(error, dict) and isinstance(error.get("code"), str) and _CODE.fullmatch(error["code"])
            and type(error.get("at")) is int and error["at"] >= 0):
        error = None
    ok = state.get("ok")
    return {"ok": ok if type(ok) is bool else None, "failures_24h": counts,
            "last_error": None if error is None else {"code": error["code"], "at": error["at"]}}


# Bundled sound files against the installed release manifest.

def _bad_name(source):
    parts = source.lower().split("/")
    pack, line = parts[-2], parts[-1]
    pack = re.sub(r"[^a-z0-9_-]", "_", pack)[:40] or "sounds"
    line = re.sub(r"[^a-z0-9_.-]", "_", line)[:80] or "file"
    return pack + "/" + line


def _digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(256 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def check_sounds(manifest=None, app_dir=None):
    """Return (ok, bad); ok is None when there is no release manifest (a dev tree)."""
    app_dir = APP_DIR if app_dir is None else Path(app_dir)
    # The updater copies the signed release manifest into each installed release.
    manifest = app_dir / "release-manifest.json" if manifest is None else Path(manifest)
    try:
        files = json.loads(manifest.read_bytes())["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, []
    bad = []
    for entry in files if isinstance(files, list) else []:
        if not isinstance(entry, dict):
            continue
        source, installed, expected = entry.get("source"), entry.get("installed"), entry.get("sha256")
        if (not isinstance(source, str) or not source.startswith("sounds/")
                or not source.endswith((".wav", ".json")) or not isinstance(installed, str)
                or not installed.startswith("/opt/messagebox/")):
            continue
        try:
            good = _digest(app_dir / installed.removeprefix("/opt/messagebox/")) == expected
        except OSError:
            good = False
        if not good:
            bad.append(_bad_name(source))
    return not bad, sorted(set(bad))[:MAX_BAD]


class SoundCheck:
    """Background, low-priority check; the heartbeat only reads the result."""

    def __init__(self, check=check_sounds, clock=time.time):
        self.check = check
        self.clock = clock
        self.result = {"ok": None, "bad": [], "checked_at": None}

    def run_once(self):
        ok, bad = self.check()
        self.result = {"ok": ok, "bad": bad, "checked_at": int(self.clock()) if ok is not None else None}

    def loop(self, stop):
        lower_priority()
        delay = SOUND_CHECK_FIRST_S
        while not stop.wait(delay):
            try:
                self.run_once()
            except Exception:  # A failed check keeps the previous result.
                pass
            delay = SOUND_CHECK_EVERY_S

    def start(self, stop=None):
        stop = threading.Event() if stop is None else stop
        thread = threading.Thread(target=self.loop, args=(stop,), name="sound-check", daemon=True)
        thread.start()
        return thread


def lower_priority():
    """Lowest CPU priority for this thread; Linux derives idle-ish I/O priority from it."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
    except (AttributeError, OSError):
        pass


sound_check = SoundCheck()


# Cheap system readings.

def _disk():
    for path in (DATA_DIR, Path("/")):
        try:
            return {"free_mb": shutil.disk_usage(path).free // (1024 * 1024)}
        except OSError:
            continue
    return {"free_mb": None}


def _memory(path="/proc/meminfo"):
    try:
        for line in Path(path).read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return {"free_mb": int(line.split()[1]) // 1024}
    except (OSError, ValueError, IndexError):
        pass
    return {"free_mb": None}


def _uptime(path="/proc/uptime"):
    try:
        return max(0, int(float(Path(path).read_text(encoding="ascii").split()[0])))
    except (OSError, ValueError, IndexError):
        return None


def _boot_id(path="/proc/sys/kernel/random/boot_id"):
    try:
        return Path(path).read_text(encoding="ascii").strip() or None
    except OSError:
        return None


_cache = {}


def _cached(key, ttl, read):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    try:
        value = read()
    except (OSError, ValueError, subprocess.SubprocessError):
        value = None  # Cache the failure too: no retry storm on every heartbeat.
    _cache[key] = (now, value)
    return value


def read_units(run=None):
    """One systemctl call for every service: {name: {"restarts": int, "enabled": bool}}."""
    units = ["messagebox-" + name + ".service" for name in SERVICES]
    result = (run or subprocess.run)(["systemctl", "show", "--property=Id,LoadState,UnitFileState,NRestarts", *units],
                 capture_output=True, text=True, check=False, timeout=3)
    if result.returncode != 0:
        return None
    found = {}
    for block in result.stdout.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = fields.get("Id", "").removeprefix("messagebox-").removesuffix(".service")
        if fields.get("LoadState") != "loaded" or not _SERVICE.fullmatch(name) or name not in SERVICES:
            continue
        restarts = fields.get("NRestarts", "")
        found[name] = {"restarts": int(restarts) if restarts.isdigit() else None,
                       "enabled": fields.get("UnitFileState") in {"enabled", "enabled-runtime", "static"}}
    return found


def restarts_24h(counts, *, path=None, now=None, boot=None, uptime=None):
    """Restarts within 24 h from hourly samples of the per-boot counter.

    systemd resets NRestarts at boot; restarts before a reboot are not counted.
    """
    path = SERVICES_FILE if path is None else Path(path)
    now = int(time.time() if now is None else now)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        samples = state["samples"] if state.get("boot") == boot else []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        samples = []
    samples = [s for s in samples if isinstance(s, list) and len(s) == 2 and type(s[0]) is int
               and isinstance(s[1], dict) and now - DAY <= s[0] <= now]
    if not samples and uptime is not None and uptime < DAY:
        samples = [[now - uptime, {}]]  # Counters started at zero at boot.
    baseline = samples[0][1] if samples else counts
    if not samples or now - samples[-1][0] >= 3600:
        samples.append([now, counts])
        try:
            atomic_json(path, {"boot": boot, "samples": samples[-26:]})
        except OSError:
            pass
    report = {}
    for name, current in sorted(counts.items())[:12]:
        before = baseline.get(name, 0)
        before = before if type(before) is int else 0
        report[name] = current - before if current >= before else current
    return report


def _services(units, uptime):
    if units is None:
        return {"restarts_24h": None}
    counts = {name: unit["restarts"] for name, unit in units.items() if unit["restarts"] is not None}
    return {"restarts_24h": restarts_24h(counts, boot=_boot_id(), uptime=uptime)}


def _nfc(units, path=None, now=None):
    unit = (units or {}).get("nfc")
    if unit is None or not unit["enabled"]:
        return {"ok": None}  # No reader installed, or unknown.
    try:
        age = (time.time() if now is None else now) - Path(NFC_HEALTH_FILE if path is None else path).stat().st_mtime
    except OSError:
        return {"ok": False}
    return {"ok": age <= NFC_FRESH_S}


def read_clock(run=None):
    result = (run or subprocess.run)(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                 capture_output=True, text=True, check=False, timeout=2)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return {"yes": True, "no": False}.get(value)


def _safe(collect, fallback):
    try:
        return collect()
    except Exception:
        return fallback


def fit(health):
    """Keep the serialized object within MAX_BYTES by trimming the longest lists."""
    def size():
        return len(json.dumps(health, separators=(",", ":")).encode())
    sounds = health.get("sounds") or {}
    while size() > MAX_BYTES and sounds.get("bad"):
        sounds["bad"] = sounds["bad"][:-1]
    restarts = (health.get("services") or {}).get("restarts_24h")
    while size() > MAX_BYTES and restarts:
        restarts.pop(sorted(restarts)[-1])
    return health


def collect():
    units = _safe(lambda: _cached("units", SERVICES_TTL, read_units), None)
    uptime = _safe(_uptime, None)
    sounds = dict(sound_check.result)
    sounds["bad"] = [name for name in sounds.get("bad", []) if _BAD.fullmatch(name)][:MAX_BAD]
    return fit({
        "version": 1,
        "audio": _safe(audio_report, {"ok": None, "failures_24h": dict.fromkeys(AUDIO_KINDS, 0), "last_error": None}),
        "sounds": sounds,
        "disk": _safe(_disk, {"free_mb": None}),
        "memory": _safe(_memory, {"free_mb": None}),
        "services": _safe(lambda: _services(units, uptime), {"restarts_24h": None}),
        "nfc": _safe(lambda: _nfc(units), {"ok": None}),
        "clock": {"synced": _safe(lambda: _cached("clock", CLOCK_TTL, read_clock), None)},
        "uptime_s": uptime,
    })


def heartbeat_fields():
    """The optional `health` section; empty when collection fails as a whole."""
    try:
        return {"health": collect()}
    except Exception:
        return {}
