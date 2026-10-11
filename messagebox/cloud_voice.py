"""Cloud-only voice cache and durable reaction playback. No network in the player."""

import fcntl
import hashlib
import io
import json
import os
import re
import time
import uuid
import wave
from pathlib import Path

from messagebox.cloud_device import CloudDeviceError, atomic_json, open_private_lock
from messagebox.runtime_paths import APP_DIR, STATE_DIR

MAX_CLIP_BYTES = 48000 * 2 * 60 + 44
SHA = re.compile(r"[a-f0-9]{64}")


def fallback(name, count=None):
    if name == "more":
        name = f"more-{min(97, max(1, count))}"
    if not re.fullmatch(r"(?:reaction|text-message|nothing|more-\d{1,3})", name):
        raise ValueError("unknown cloud fallback")
    return APP_DIR / "sounds" / "cloud" / f"{name}.wav"


class VoiceCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def download(self, client, payload):
        if payload.get("use_fallback"):
            return ""
        clip_id = payload.get("clip_id")
        if (not isinstance(clip_id, str) or not SHA.fullmatch(clip_id)
                or not isinstance(payload.get("sha256"), str) or not SHA.fullmatch(payload["sha256"])
                or payload.get("content_type") != "audio/wav"):
            return ""
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (clip_id + ".wav")
        temporary = directory / ("." + uuid.uuid4().hex + ".part")
        try:
            if path.is_symlink():
                return ""
            audio = (path.read_bytes() if path.exists() and path.stat().st_size <= MAX_CLIP_BYTES
                     else client.media(payload.get("media_url"), limit=MAX_CLIP_BYTES))
            if not audio or len(audio) > MAX_CLIP_BYTES or hashlib.sha256(audio).hexdigest() != payload["sha256"]:
                return ""
            with wave.open(io.BytesIO(audio), "rb") as sound:
                frames = sound.getnframes()
                if (sound.getnchannels() != 1 or sound.getsampwidth() != 2 or sound.getframerate() != 48000
                        or sound.getcomptype() != "NONE" or frames <= 0
                        or len(sound.readframes(frames)) != frames * 2):
                    return ""
            with temporary.open("wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            files = sorted(directory.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[100:]:
                old.unlink(missing_ok=True)
            return str(path)
        except (CloudDeviceError, OSError, ValueError, EOFError, wave.Error):
            return ""
        finally:
            temporary.unlink(missing_ok=True)


class ReactionQueue:
    def __init__(self, path=STATE_DIR / "cloud-reactions.json"):
        self.path = Path(path)

    def enqueue(self, operation, scope, expires_at, payload, clip):
        with open_private_lock(self.path.with_suffix(".lock")) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._load(scope)
            if operation in state["seen"]:
                return
            if len(state["pending"]) >= 100:
                raise ValueError("reaction queue is full")
            state["seen"][operation] = expires_at
            state["pending"].append({"operation": operation, "expires_at": expires_at,
                                     "sender_id": payload["sender_id"], "sender_kind": payload.get("sender_kind"),
                                     "clip": clip})
            atomic_json(self.path, state)

    def _load(self, scope):
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            state = {"scope": scope, "seen": {}, "pending": []}
        if state.get("scope") != scope:
            return {"scope": scope, "seen": {}, "pending": []}
        if not isinstance(state.get("seen"), dict) or not isinstance(state.get("pending"), list):
            raise ValueError("reaction queue is invalid")
        return state

    def take(self, scope, allowed, *, now=None):
        now = time.time() if now is None else now
        with open_private_lock(self.path.with_suffix(".lock")) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._load(scope)
            valid = [row for row in state["pending"] if row["expires_at"] > now
                     and (row["sender_kind"], row["sender_id"]) in allowed]
            # Receipt before playback prevents a second announcement after a restart.
            state["pending"] = []
            state["seen"] = {k: v for k, v in state["seen"].items() if v > now}
            atomic_json(self.path, state)
            return valid[:3], max(0, len(valid) - 3)
