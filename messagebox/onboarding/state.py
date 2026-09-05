"""Durable, pure onboarding state transitions."""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from messagebox.onboarding.connectivity import PROOFS


VERSION = 1

WIFI_SELECT = "WIFI_SELECT"
WIFI_CONNECTING = "WIFI_CONNECTING"
WIFI_ASSOCIATED = "WIFI_ASSOCIATED"
WIFI_FAILED = "WIFI_FAILED"
WHATSAPP_PENDING = "WHATSAPP_PENDING"
WHATSAPP_READY = "WHATSAPP_READY"

CURRENT_PHASES = frozenset(
    {
        WIFI_SELECT,
        WIFI_CONNECTING,
        WIFI_ASSOCIATED,
        WIFI_FAILED,
        WHATSAPP_PENDING,
        WHATSAPP_READY,
    }
)
SAFE_ERRORS = frozenset(
    {
        "ASSOCIATION_FAILED",
        "CONNECTION_LOST",
        "WLAN0_NOT_ACTIVE",
        "WLAN0_AP_MODE",
        "WLAN0_NO_IPV4",
        "WLAN0_NO_DEFAULT_ROUTE",
        "DNS_FAILED",
        "HTTPS_204_FAILED",
        "CHECK_COMMAND_FAILED",
    }
)
WHATSAPP_PROOFS = frozenset(
    {
        "whatsapp_authenticated",
        "whatsapp_connected",
    }
)
ALL_PROOFS = PROOFS | WHATSAPP_PROOFS

_STATE_KEYS = frozenset(
    {
        "version",
        "phase",
        "safe_error",
        "generation",
        "dispatched_generation",
        "transitions",
        "proofs",
        "updated_at",
    }
)
_TRANSITION_KEYS = frozenset({"phase", "at"})


class StateError(ValueError):
    """A non-sensitive state validation or transition error."""


def _is_timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _validate(document):
    if not isinstance(document, dict) or frozenset(document) != _STATE_KEYS:
        raise StateError("onboarding state has an invalid schema")
    if document["version"] != VERSION or type(document["version"]) is not int:
        raise StateError("onboarding state has an unsupported version")
    if document["phase"] not in CURRENT_PHASES:
        raise StateError("onboarding state has an invalid phase")
    if document["safe_error"] is not None and document["safe_error"] not in SAFE_ERRORS:
        raise StateError("onboarding state has an invalid error code")
    generation = document["generation"]
    dispatched = document["dispatched_generation"]
    if type(generation) is not int or generation < 0:
        raise StateError("onboarding state has an invalid generation")
    if dispatched is not None and (
        type(dispatched) is not int or dispatched < 1 or dispatched > generation
    ):
        raise StateError("onboarding state has an invalid dispatch generation")
    if not _is_timestamp(document["updated_at"]):
        raise StateError("onboarding state has an invalid update timestamp")
    transitions = document["transitions"]
    if not isinstance(transitions, list) or not transitions:
        raise StateError("onboarding state has invalid transitions")
    previous = -1
    for transition in transitions:
        if not isinstance(transition, dict) or frozenset(transition) != _TRANSITION_KEYS:
            raise StateError("onboarding state has an invalid transition")
        if transition["phase"] not in CURRENT_PHASES or not _is_timestamp(transition["at"]):
            raise StateError("onboarding state has an invalid transition")
        if transition["at"] < previous:
            raise StateError("onboarding transitions are out of order")
        previous = transition["at"]
    if transitions[-1]["phase"] != document["phase"]:
        raise StateError("onboarding state phase does not match its transitions")
    proofs = document["proofs"]
    if (
        not isinstance(proofs, list)
        or any(not isinstance(item, str) or item not in ALL_PROOFS for item in proofs)
        or len(proofs) != len(set(proofs))
    ):
        raise StateError("onboarding state has invalid connectivity proofs")
    if document["phase"] == WHATSAPP_PENDING and (
        set(proofs) != PROOFS or document["safe_error"] is not None
    ):
        raise StateError("completed Wi-Fi state lacks connectivity proof")
    if document["phase"] == WHATSAPP_READY and (
        set(proofs) != ALL_PROOFS or document["safe_error"] is not None
    ):
        raise StateError("completed WhatsApp state lacks proof")
    return document


class StateStore:
    """Serialize onboarding updates through a separate advisory lock file."""

    def __init__(self, path, *, clock=time.time, owner=None):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.clock = clock
        self.owner = owner

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise StateError("onboarding state lock is not a regular file")
            os.fchmod(lock.fileno(), 0o600)
            if self.owner is not None:
                os.fchown(lock.fileno(), *self.owner)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _now(self):
        now = self.clock()
        if not _is_timestamp(now):
            raise StateError("clock returned an invalid timestamp")
        return now

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise StateError("could not read onboarding state") from exc
        return _validate(document)

    def _write(self, document):
        _validate(document)
        owner = self.owner
        if owner is None:
            try:
                info = self.path.stat()
                owner = (info.st_uid, info.st_gid)
            except FileNotFoundError:
                pass
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                if owner is not None:
                    os.fchown(handle.fileno(), *owner)
                json.dump(document, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def load(self):
        with self._locked():
            document = self._read()
            if document is None:
                raise StateError("onboarding state is not initialized")
            return document

    def initialize(self):
        with self._locked():
            existing = self._read()
            if existing is not None:
                return existing
            now = self._now()
            document = {
                "version": VERSION,
                "phase": WIFI_SELECT,
                "safe_error": None,
                "generation": 0,
                "dispatched_generation": None,
                "transitions": [{"phase": WIFI_SELECT, "at": now}],
                "proofs": [],
                "updated_at": now,
            }
            self._write(document)
            return document

    def _update(self, operation):
        with self._locked():
            document = self._read()
            if document is None:
                raise StateError("onboarding state is not initialized")
            result = operation(document)
            self._write(document)
            return document if result is None else result

    def _transition(self, document, phase, now):
        document["phase"] = phase
        document["updated_at"] = now
        document["transitions"].append({"phase": phase, "at": now})

    def begin_connect(self, selected_ssid):
        if not _valid_ssid(selected_ssid):
            raise StateError("SSID must contain between 1 and 32 bytes")

        def operation(document):
            if document["phase"] not in {WIFI_SELECT, WIFI_FAILED}:
                raise StateError("a Wi-Fi connection cannot begin in the current phase")
            now = self._now()
            document["generation"] += 1
            document["safe_error"] = None
            document["proofs"] = []
            self._transition(document, WIFI_CONNECTING, now)

        return self._update(operation)

    def mark_dispatched(self, generation):
        """Claim one connector dispatch; return False for a duplicate claim."""
        def operation(document):
            if type(generation) is not int or generation != document["generation"]:
                raise StateError("connection generation is stale")
            if document["phase"] != WIFI_CONNECTING:
                raise StateError("connection dispatch is not expected")
            if document["dispatched_generation"] == generation:
                return False
            document["dispatched_generation"] = generation
            document["updated_at"] = self._now()
            return True

        return self._update(operation)

    def reconcile_hotspot(self):
        """Fail an interrupted connecting attempt after the hotspot returns."""
        def operation(document):
            if document["phase"] == WIFI_CONNECTING:
                document["safe_error"] = "ASSOCIATION_FAILED"
                self._transition(document, WIFI_FAILED, self._now())
            elif document["phase"] in {WIFI_ASSOCIATED, WHATSAPP_PENDING, WHATSAPP_READY}:
                document["safe_error"] = "CONNECTION_LOST"
                document["proofs"] = []
                self._transition(document, WIFI_FAILED, self._now())

        return self._update(operation)

    def mark_whatsapp_ready(self, proofs):
        """Advance only after the private worker proves auth and connectivity."""
        proofs = set(proofs)
        if proofs != WHATSAPP_PROOFS:
            raise StateError("WhatsApp proof is invalid")

        def operation(document):
            if document["phase"] == WHATSAPP_READY:
                return
            if document["phase"] != WHATSAPP_PENDING:
                raise StateError("WhatsApp readiness is not expected")
            document["proofs"] = sorted(PROOFS | proofs)
            document["safe_error"] = None
            self._transition(document, WHATSAPP_READY, self._now())

        return self._update(operation)

    def mark_whatsapp_unlinked(self):
        """Return to pairing after the worker confirms remote logout."""
        def operation(document):
            if document["phase"] == WHATSAPP_PENDING:
                return
            if document["phase"] != WHATSAPP_READY:
                raise StateError("WhatsApp unlink is not expected")
            document["proofs"] = sorted(PROOFS)
            document["safe_error"] = None
            self._transition(document, WHATSAPP_PENDING, self._now())

        return self._update(operation)

    def mark_associated(self, generation):
        def operation(document):
            if type(generation) is not int or generation != document["generation"]:
                raise StateError("connection generation is stale")
            if document["phase"] not in {WIFI_CONNECTING, WIFI_FAILED}:
                raise StateError("Wi-Fi association is not expected")
            document["safe_error"] = None
            document["proofs"] = []
            self._transition(document, WIFI_ASSOCIATED, self._now())

        return self._update(operation)

    def record_connectivity_result(self, proofs, safe_error=None):
        """Commit one complete connectivity observation atomically."""
        proofs = set(proofs)
        if not proofs <= PROOFS:
            raise StateError("connectivity proof is invalid")
        if safe_error is not None and safe_error not in SAFE_ERRORS:
            raise StateError("error code is not safe to persist")

        def operation(document):
            if document["phase"] != WIFI_ASSOCIATED:
                raise StateError("connectivity result is not expected")
            now = self._now()
            document["proofs"] = sorted(proofs)
            document["safe_error"] = safe_error
            document["updated_at"] = now
            if proofs == PROOFS and safe_error is None:
                self._transition(document, WHATSAPP_PENDING, now)

        return self._update(operation)

    def fail(self, safe_error):
        if safe_error not in SAFE_ERRORS:
            raise StateError("error code is not safe to persist")

        def operation(document):
            if document["phase"] not in {WIFI_CONNECTING, WIFI_ASSOCIATED}:
                raise StateError("Wi-Fi failure is not expected")
            document["safe_error"] = safe_error
            self._transition(document, WIFI_FAILED, self._now())

        return self._update(operation)

    def reset(self, *, recreate=False):
        with self._locked():
            try:
                document = self._read()
            except StateError:
                if not recreate:
                    raise
                document = None

            if document is None and not recreate:
                raise StateError("onboarding state is not initialized")

            now = self._now()
            generation = 0 if document is None else document["generation"] + 1
            document = {
                "version": VERSION,
                "phase": WIFI_SELECT,
                "safe_error": None,
                "generation": generation,
                "dispatched_generation": None,
                "transitions": [{"phase": WIFI_SELECT, "at": now}],
                "proofs": [],
                "updated_at": now,
            }
            self._write(document)
            return document


def _valid_ssid(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return False
    return 1 <= len(encoded) <= 32 and not any(ord(char) < 32 or ord(char) == 127 for char in value)
