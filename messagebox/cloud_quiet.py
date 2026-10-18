"""Durable Cloud silence controls, independent of settings revisions."""

import fcntl
import json
import math
import subprocess
import threading
import time
from pathlib import Path

from messagebox import cloud_health
from messagebox.cloud_device import CLOUD_DIR, atomic_json
from messagebox.settings import _open_shared_lock


SILENCE_FILE = CLOUD_DIR / "silence.json"


class AudioSilenced(subprocess.SubprocessError):
    pass


def normalize_quiet_until(value):
    if value is None or value == "manual":
        return value
    if type(value) not in {int, float} or not math.isfinite(value) or not 1_700_000_000 <= value < 4_102_444_800:
        raise ValueError("quiet until is invalid")
    return value


class SilenceStore:
    def __init__(self, path=SILENCE_FILE):
        self.path = Path(path)

    def load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or "quiet_until" not in state or type(state.get("muted")) is not bool
                    or any(type(state.get(key)) is not int or state[key] < -1
                           for key in ("mute_sequence", "quiet_sequence", "stop_sequence"))):
                raise ValueError("silence state is invalid")
            normalize_quiet_until(state.get("quiet_until"))
            return state
        except FileNotFoundError:
            return {"muted": False, "quiet_until": None, "mute_sequence": -1,
                    "quiet_sequence": -1, "stop_sequence": -1}
        except (OSError, ValueError):
            # A damaged control file must never turn sound back on.
            return {"muted": True, "quiet_until": "manual", "mute_sequence": -1,
                    "quiet_sequence": -1, "stop_sequence": -1}

    def apply(self, kind, value, sequence, *, now=None):
        if kind not in {"mute", "quiet_now"} or type(sequence) is not int or sequence < 0:
            raise ValueError("silence command is invalid")
        if kind == "mute":
            if type(value) is not bool:
                raise ValueError("mute value is invalid")
        else:
            normalize_quiet_until(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _open_shared_lock(self.path.with_suffix(".lock")) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self.load()
            key = "mute_sequence" if kind == "mute" else "quiet_sequence"
            if sequence > state[key]:
                state["muted" if kind == "mute" else "quiet_until"] = value
                state[key] = sequence
                if (kind == "mute" and value) or (kind == "quiet_now" and quiet_now(state, now)):
                    state["stop_sequence"] = max(state["stop_sequence"], sequence)
                atomic_json(self.path, state)
            return state


silence_store = SilenceStore()


def quiet_now(state=None, now=None):
    state = silence_store.load() if state is None else state
    until = state["quiet_until"]
    return until == "manual" or (until is not None and until > (time.time() if now is None else now))


def muted():
    return silence_store.load()["muted"]


def _track_stops(process, kind):
    """Mark deliberate stops (a press, mute, a timeout) so health counts only real faults."""
    process.health_kind = kind
    for name in ("terminate", "kill"):
        stop = getattr(process, name, None)
        if stop is None:
            continue

        def stopped(stop=stop):
            process.health_stopped = True
            stop()
        setattr(process, name, stopped)


def start_player(args, *, allow_muted=False, **kwargs):
    """Watch only this process; never kill other services' audio players."""
    before = silence_store.load()
    if before["muted"] and not allow_muted:
        raise AudioSilenced("box is muted")
    from messagebox.maintenance import interaction
    reservation = interaction()
    if not reservation.__enter__():
        reservation.__exit__(None, None, None)
        raise AudioSilenced("software installation in progress")
    kind = cloud_health.classify(args[-1])
    try:
        process = subprocess.Popen(args, **kwargs)
    except BaseException as exc:
        reservation.__exit__(None, None, None)
        if isinstance(exc, OSError):
            cloud_health.record_audio(kind, "player_unavailable")
        raise
    _track_stops(process, kind)

    # Set once the shared maintenance reservation is released, so a caller can
    # wait for it before asking the updater for the exclusive one.
    released = threading.Event()
    process.reservation_released = released

    def watch():
        try:
            while process.poll() is None:
                state = silence_store.load()
                if state["muted"] and not allow_muted or state["stop_sequence"] > before["stop_sequence"]:
                    process.terminate()
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.2)
                    return
                time.sleep(0.02)
        finally:
            reservation.__exit__(None, None, None)
            released.set()
            cloud_health.player_finished(process, args[-1])

    threading.Thread(target=watch, daemon=True).start()
    return process


def play(args, *, timeout, allow_muted=False):
    process = start_player(args, allow_muted=allow_muted)
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise subprocess.CalledProcessError(code, "aplay")
        return subprocess.CompletedProcess(args, code)
    finally:
        if process.poll() is None:
            process.health_code = "play_timeout"
            process.kill()
            process.wait()
