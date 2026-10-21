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
from urllib.parse import urlsplit

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
    def __init__(self, directory, *, max_entries=100, max_bytes=None, clip_limit=MAX_CLIP_BYTES,
                 max_seconds=None):
        self.directory = Path(directory)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.clip_limit = clip_limit
        self.max_seconds = max_seconds

    def download(self, client, payload, *, key=None, deadline=None):
        if payload.get("use_fallback"):
            return ""
        clip_id = payload.get("clip_id")
        if (not isinstance(clip_id, str) or not SHA.fullmatch(clip_id)
                or not isinstance(payload.get("sha256"), str) or not SHA.fullmatch(payload["sha256"])
                or payload.get("content_type") != "audio/wav"):
            return ""
        if key is not None and (not isinstance(key, str) or not SHA.fullmatch(key)):
            return ""
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / ((key or clip_id) + ".wav")
        temporary = directory / ("." + uuid.uuid4().hex + ".part")
        try:
            if path.is_symlink():
                return ""
            audio = (path.read_bytes() if path.exists() and path.stat().st_size <= self.clip_limit
                     else client.media(payload.get("media_url"), limit=self.clip_limit,
                                       **({"deadline": deadline} if deadline is not None else {})))
            if not audio or len(audio) > self.clip_limit or hashlib.sha256(audio).hexdigest() != payload["sha256"]:
                return ""
            with wave.open(io.BytesIO(audio), "rb") as sound:
                frames = sound.getnframes()
                if (sound.getnchannels() != 1 or sound.getsampwidth() != 2 or sound.getframerate() != 48000
                        or sound.getcomptype() != "NONE" or frames <= 0
                        or (self.max_seconds is not None and frames > self.max_seconds * 48000)
                        or (key is not None and len(audio) != payload.get("bytes"))
                        or len(sound.readframes(frames)) != frames * 2):
                    return ""
            with temporary.open("wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            files = sorted(directory.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            total = 0
            for index, old in enumerate(files):
                total += old.stat().st_size
                if index >= self.max_entries or (self.max_bytes is not None and total > self.max_bytes):
                    old.unlink(missing_ok=True)
            return str(path)
        except (CloudDeviceError, OSError, ValueError, EOFError, wave.Error):
            return ""
        finally:
            temporary.unlink(missing_ok=True)


RECIPIENT_TEMPLATES = ("recording_recipient", "sent_recipient")
MAX_RECIPIENT_BYTES = 384044


def recipient_voice_key(scope, kind, subject, template, pack, name_hash):
    return hashlib.sha256(json.dumps([scope, kind, subject, template, pack, name_hash],
                                    separators=(",", ":")).encode()).hexdigest()


def recipient_voice_valid(payload, *, scope, kind, subject, template, pack, name_hash, api_url):
    """Bind every known field to the requested snapshot; optional future fields are ignored."""
    if not isinstance(payload, dict):
        return False
    expected = {"status": "ready", "use_fallback": False, "template": template,
                "recipient_kind": kind, "recipient_id": subject, "account_scope": scope,
                "name_hash": name_hash, "voice_pack": pack, "content_type": "audio/wav"}
    if (template not in RECIPIENT_TEMPLATES
            or any(type(payload.get(key)) is not type(value) or payload[key] != value
                   for key, value in expected.items())
            or any(not isinstance(payload.get(key), str) or not SHA.fullmatch(payload[key])
                   for key in ("account_scope", "name_hash", "clip_id", "text_hash", "sha256"))
            or type(payload.get("bytes")) is not int or not 44 < payload["bytes"] <= MAX_RECIPIENT_BYTES
            or not isinstance(payload.get("media_url"), str) or not 0 < len(payload["media_url"]) <= 2048):
        return False
    try:
        url = urlsplit(payload["media_url"])
        return (url.scheme == "https" and url.netloc == urlsplit(api_url).netloc
                and url.username is None and url.password is None and not url.query and not url.fragment
                and url.path.startswith(("/cloud-api/v1/device/voice-lines/", "/cloud-api/v1/device/media/",
                                         "/cloud-api/v1/device/listened-media/"))
                and ".." not in url.path and "%" not in url.path and "\\" not in url.path)
    except ValueError:
        return False


def recipient_voice_cache(directory):
    return VoiceCache(directory, max_entries=300, max_bytes=128 * 1024 * 1024,
                      clip_limit=MAX_RECIPIENT_BYTES, max_seconds=4)


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
