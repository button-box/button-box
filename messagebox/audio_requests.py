"""Short-lived cloud sounds consumed exclusively by the physical-button owner."""

import fcntl
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from pathlib import Path

from messagebox.cloud_device import atomic_json
from messagebox.settings import RINGTONES, normalize_ringtone_id


def _boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f-]{36}", value) else None


class AudioRequests:
    def __init__(self, directory, *, clock=time.time, monotonic=time.monotonic, boot_id=None):
        self.directory = Path(directory)
        self.clock = clock
        self.monotonic = monotonic
        self.boot_id = _boot_id() if boot_id is None else boot_id

    def _path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _read(self, path):
        request = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(request, dict) or not isinstance(request.get("key"), str)
                or self._path(request["key"]).name != path.name
                or request.get("kind") not in {"success", "preview", "settings_saved"}
                or (request.get("kind") == "preview" and normalize_ringtone_id(request.get("ringtone_id")) not in RINGTONES)
                or not isinstance(request.get("account_scope"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", request["account_scope"])
                or type(request.get("expires_at")) not in (int, float)
                or not math.isfinite(request["expires_at"])
                or (request.get("boot_id") is not None and (not isinstance(request["boot_id"], str)
                    or "expires_mono" not in request))
                or ("expires_mono" in request and (type(request["expires_mono"]) not in (int, float)
                    or not math.isfinite(request["expires_mono"])))
                or any(type(request[field]) not in (int, float) or not math.isfinite(request[field])
                       for field in ("ready_at", "ready_mono") if field in request)
                or request.get("state") not in {"pending", "claimed", "played", "rejected", "unknown", "expired"}):
            raise ValueError("audio request is invalid")
        return request

    def _expired(self, request):
        boot = request.get("boot_id")
        if boot is not None:
            return boot != self.boot_id or self.monotonic() >= request["expires_mono"]
        return self.clock() >= request["expires_at"]

    def _completed(self, path):
        return self.directory / "completed" / path.name

    def _save(self, path, request):
        if request["state"] in {"pending", "claimed"}:
            atomic_json(path, request)
        else:
            # Receipt first: a crash before unlink still cannot replay sound.
            atomic_json(self._completed(path), request)
            path.unlink(missing_ok=True)

    @contextmanager
    def _locked(self, path):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def enqueue(self, key, kind, scope, expires_at, **fields):
        self.enqueue_for(key, kind, scope, expires_at - self.clock(), **fields)

    def enqueue_for(self, key, kind, scope, seconds, **fields):
        """Persist a remaining duration without round-tripping through wall time."""
        remaining = max(0, min(30, seconds))
        expires_mono = self.monotonic() + remaining
        expires_at = self.clock() + remaining
        path = self._path(key)
        with self._locked(path):
            if path.exists() or self._completed(path).exists():
                return  # Terminal receipts prevent replays, including after a crash.
            atomic_json(path, {"key": key, "kind": kind, "account_scope": scope,
                               "expires_at": expires_at, "expires_mono": expires_mono,
                               "boot_id": self.boot_id, "state": "pending", **fields})

    def enqueue_settings_saved(self, key, scope, seconds):
        """Debounce saves, retaining the first request's bounded lifetime."""
        with self._locked(self.directory / "settings-saved.json"):
            path = self._path(key)
            if path.exists() or self._completed(path).exists():
                return
            now, mono = self.clock(), self.monotonic()
            for pending_path in self.directory.glob("*.json"):
                with self._locked(pending_path):
                    if not pending_path.exists():
                        continue
                    request = self._read(pending_path)
                    ready = request.get("ready_mono", 0) if request.get("boot_id") else request.get("ready_at", 0)
                    if (request["kind"] == "settings_saved" and request["state"] == "pending"
                            and request["account_scope"] == scope and not self._expired(request)
                            and ready >= (mono if request.get("boot_id") else now)):
                        request.update(ready_at=now + 3, ready_mono=mono + 3)
                        # A receipt for every merged operation prevents replay after restart.
                        with self._locked(path):
                            self._save(path, {**request, "key": key, "state": "rejected"})
                        self._save(pending_path, request)
                        return
            self.enqueue_for(key, "settings_saved", scope, seconds,
                             ready_at=now + 3, ready_mono=mono + 3)

    @contextmanager
    def owner(self):
        """Hold through playback so recovery can distinguish a live player."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "owner.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def claim_next(self, scope):
        # Serialize a claim with debounce updates from the cloud poller.
        with self._locked(self.directory / "settings-saved.json"):
            return self._claim_next(scope)

    def _claim_next(self, scope):
        # The caller holds owner() until finish(). Commit the claim before sound.
        for path in sorted(self.directory.glob("*.json")):
            with self._locked(path):
                if self._completed(path).exists():
                    path.unlink(missing_ok=True)
                    continue
                request = self._read(path)
                if request["state"] == "claimed":
                    request["state"] = "unknown"
                    self._save(path, request)
                if request["state"] != "pending":
                    continue
                if request["account_scope"] != scope:
                    request["state"] = "rejected"
                elif self._expired(request):
                    request["state"] = "expired"
                elif (request.get("ready_mono", 0) > self.monotonic() if request.get("boot_id")
                      else request.get("ready_at", 0) > self.clock()):
                    continue
                else:
                    request["state"] = "claimed"
                self._save(path, request)
                if request["state"] == "claimed":
                    return request
        return None

    def finish(self, request, state):
        path = self._path(request["key"])
        with self._locked(path):
            current = self._read(path)
            if current["state"] == "claimed":
                current["state"] = state
                self._save(path, current)

    def outcome(self, key):
        path = self._path(key)
        if not path.exists() and not self._completed(path).exists():
            return None
        with self.owner() as idle, self._locked(path):
            completed = self._completed(path)
            request = self._read(completed if completed.exists() else path)
            if request["state"] == "claimed" and idle:
                request["state"] = "unknown"
                self._save(path, request)
            elif request["state"] == "pending" and self._expired(request):
                request["state"] = "expired"
                self._save(path, request)
            return request["state"]


def success_key(scope, message_id):
    return "success:" + scope + ":" + message_id


def preview_key(operation_id):
    return "preview:" + operation_id
