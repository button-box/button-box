"""Private PN532 worker for tag-first NFC onboarding."""

from __future__ import annotations

import argparse
import grp
import json
import math
import os
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

from messagebox.contacts import ContactError, ContactStore
from messagebox.nfc import PN532I2CReader
from messagebox.nfc_state import NfcError, normalize_uid
from messagebox.onboarding.paths import NFC_ONBOARDING_SOCKET_PATH
from messagebox.onboarding.recipients import RecipientError, RecipientSetup
from messagebox.runtime_paths import CONTACTS_FILE, STATE_DIR


STATE_VERSION = 1
STATE_FILE = STATE_DIR / "nfc-onboarding.json"
PENDING_TTL_S = 120.0
REMOVAL_GRACE_S = 0.8
MAX_MESSAGE_BYTES = 4096
ACTIVE_STATUSES = frozenset({"waiting", "choose", "already_paired", "success", "unavailable"})


class NfcOnboardingError(RuntimeError):
    """Safe error across the private onboarding socket."""


def _atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _default_state():
    return {
        "version": STATE_VERSION,
        "status": "idle",
        "pending_uid": None,
        "captured_at": None,
        "reassign_allowed": False,
        "recipient_label": None,
        "recipient_kind": None,
        "sound_warning": False,
    }


def _load_state(path, *, clock=time.time):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise NfcOnboardingError("NFC setup state is unavailable") from exc
    if not isinstance(document, dict) or set(document) != set(_default_state()):
        raise NfcOnboardingError("NFC setup state is unavailable")
    if document["version"] != STATE_VERSION or document["status"] not in {
        "idle", *ACTIVE_STATUSES
    }:
        raise NfcOnboardingError("NFC setup state is unavailable")
    if not isinstance(document["reassign_allowed"], bool) or not isinstance(
        document["sound_warning"], bool
    ):
        raise NfcOnboardingError("NFC setup state is unavailable")
    uid = document["pending_uid"]
    captured = document["captured_at"]
    if uid is not None:
        try:
            document["pending_uid"] = normalize_uid(uid)
        except NfcError as exc:
            raise NfcOnboardingError("NFC setup state is unavailable") from exc
        if type(captured) not in (int, float) or not math.isfinite(captured) or captured < 0:
            raise NfcOnboardingError("NFC setup state is unavailable")
        if clock() - captured > PENDING_TTL_S:
            document = _default_state()
            document["status"] = "waiting"
    elif captured is not None:
        raise NfcOnboardingError("NFC setup state is unavailable")
    for key in ("recipient_label", "recipient_kind"):
        if document[key] is not None and not isinstance(document[key], str):
            raise NfcOnboardingError("NFC setup state is unavailable")
    return document


class TonePlayer:
    """Generate simple local tones and play them without private audio assets."""

    def __init__(self, directory="/run/messagebox-onboarding-nfc", *, run=subprocess.run):
        self.directory = Path(directory)
        self.run = run

    @staticmethod
    def _write_tone(path, frequencies):
        import math as _math
        import struct

        rate = 16000
        samples = bytearray()
        for frequency, duration in frequencies:
            count = int(rate * duration)
            for index in range(count):
                envelope = min(1.0, index / 80, (count - index) / 80)
                value = int(12000 * envelope * _math.sin(2 * _math.pi * frequency * index / rate))
                samples.extend(struct.pack("<h", value))
            samples.extend(b"\x00\x00" * int(rate * 0.04))
        with wave.open(os.fspath(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(samples)

    def __call__(self, kind):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{kind}.wav"
        if not path.exists():
            sequence = [(1760, 0.08)] if kind == "read" else [(1320, 0.08), (1760, 0.11)]
            self._write_tone(path, sequence)
            os.chmod(path, 0o600)
        card = os.environ.get("MSGBOX_SPEAKER_CARD", "Device")
        self.run(
            ["aplay", "-q", "-D", f"plughw:CARD={card}", os.fspath(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


class NfcOnboardingEngine:
    def __init__(
        self,
        *,
        state_path=STATE_FILE,
        contacts_path=CONTACTS_FILE,
        recipients=None,
        reader_factory=PN532I2CReader,
        tone_player=None,
        clock=time.time,
    ):
        self.state_path = Path(state_path)
        self.contacts = ContactStore(contacts_path, clock=clock)
        self.recipients = recipients or RecipientSetup(contacts_path=contacts_path)
        self.reader_factory = reader_factory
        self.tone_player = tone_player or TonePlayer()
        self.clock = clock
        self.reader = None
        self.reader_error = False
        self.current_uid = None
        self.last_seen = None
        self._lock = threading.RLock()
        self._stop = threading.Event()

    def _write(self, state):
        _atomic_json(self.state_path, state)

    def _state(self):
        state = _load_state(self.state_path, clock=self.clock)
        if state["status"] == "waiting" and not self.state_path.exists():
            self._write(state)
        return state

    def _require_complete_recipients(self):
        state = self.recipients.public_state()
        if state["status"] != "complete" or state["default"] is None:
            raise NfcOnboardingError("Complete recipient setup first")
        return state

    def _open_reader(self):
        if self.reader is not None:
            return True
        try:
            self.reader = self.reader_factory()
            self.reader_error = False
            return True
        except (OSError, RuntimeError, ValueError):
            self.reader = None
            self.reader_error = True
            return False

    def _play(self, kind, state):
        try:
            self.tone_player(kind)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
            state["sound_warning"] = True

    def start(self):
        with self._lock:
            self._require_complete_recipients()
            state = self._state()
            if state["status"] in {"choose", "already_paired", "success"}:
                return self.public_state(state)
            if not self._open_reader():
                state = _default_state()
                state["status"] = "unavailable"
            else:
                state = _default_state()
                state["status"] = "waiting"
            self._write(state)
            return self.public_state(state)

    def retry(self):
        with self._lock:
            self.reader = None
            self.reader_error = False
        return self.start()

    def observe(self, raw_uid):
        now = self.clock()
        with self._lock:
            state = self._state()
            if raw_uid is None:
                if self.current_uid is not None and self.last_seen is not None:
                    if now - self.last_seen >= REMOVAL_GRACE_S:
                        self.current_uid = None
                        self.last_seen = None
                return self.public_state(state)
            uid = normalize_uid(raw_uid)
            is_new = self.current_uid != uid
            self.current_uid = uid
            self.last_seen = now
            if not is_new or state["status"] != "waiting":
                return self.public_state(state)
            state = _default_state()
            state["pending_uid"] = uid
            state["captured_at"] = now
            mapped = self.contacts.resolve_card(uid)
            if mapped is None:
                state["status"] = "choose"
            else:
                state["status"] = "already_paired"
                candidate = self.recipients.configured_candidate_by_jid(mapped["jid"])
                state["recipient_label"] = candidate["label"]
                state["recipient_kind"] = candidate["kind"]
            self._play("read", state)
            self._write(state)
            return self.public_state(state)

    def allow_reassign(self):
        with self._lock:
            state = self._state()
            if state["status"] != "already_paired" or state["pending_uid"] is None:
                raise NfcOnboardingError("There is no mapped tag to reassign")
            state["status"] = "choose"
            state["reassign_allowed"] = True
            self._write(state)
            return self.public_state(state)

    def assign(self, token):
        with self._lock:
            state = self._state()
            if state["status"] != "choose" or state["pending_uid"] is None:
                raise NfcOnboardingError("Scan a tag before choosing a recipient")
            existing = self.contacts.resolve_card(state["pending_uid"])
            if existing is not None and not state["reassign_allowed"]:
                raise NfcOnboardingError("Confirm reassignment first")
            candidate = self.recipients.configured_candidate(token)
            self.contacts.assign_card(candidate["jid"], state["pending_uid"])
            state["status"] = "success"
            state["pending_uid"] = None
            state["captured_at"] = None
            state["reassign_allowed"] = False
            state["recipient_label"] = candidate["label"]
            state["recipient_kind"] = candidate["kind"]
            self._play("success", state)
            self._write(state)
            return self.public_state(state)

    def next(self):
        with self._lock:
            state = self._state()
            if state["status"] not in {"success", "already_paired"}:
                raise NfcOnboardingError("Finish the current tag first")
            state = _default_state()
            state["status"] = "waiting"
            self._write(state)
            return self.public_state(state)

    def cancel(self):
        with self._lock:
            state = _default_state()
            self._write(state)
            return self.public_state(state)

    def finish(self):
        with self._lock:
            state = self._state()
            if state["pending_uid"] is not None or state["status"] == "choose":
                raise NfcOnboardingError("Finish or cancel the detected tag first")
            state = _default_state()
            self._write(state)
            return self.public_state(state)

    def public_state(self, state=None):
        with self._lock:
            state = state or self._state()
            recipient_state = self._require_complete_recipients()
            configured = [
                {
                    key: recipient[key]
                    for key in ("token", "label", "kind", "is_default", "card_count")
                }
                for recipient in recipient_state["recipients"]
                if recipient["configured"]
            ]
            mapped_count = sum(recipient["card_count"] for recipient in configured)
            return {
                "status": state["status"],
                "recipients": configured,
                "mapped_count": mapped_count,
                "recipient": (
                    {
                        "label": state["recipient_label"],
                        "kind": state["recipient_kind"],
                    }
                    if state["recipient_label"] is not None
                    else None
                ),
                "remove_tag": state["status"] == "waiting" and self.current_uid is not None,
                "sound_warning": state["sound_warning"],
            }

    def run_reader(self):
        while not self._stop.is_set():
            with self._lock:
                reader = self.reader
                active = self._state()["status"] == "waiting"
            if reader is None or not active:
                self._stop.wait(0.1)
                continue
            try:
                raw_uid = reader.read()
                self.observe(bytes(raw_uid) if raw_uid is not None else None)
            except (OSError, RuntimeError, ValueError, NfcError):
                with self._lock:
                    self.reader = None
                    self.reader_error = True
                    state = _default_state()
                    state["status"] = "unavailable"
                    self._write(state)
                self._stop.wait(0.25)

    def stop(self):
        self._stop.set()


class NfcOnboardingClient:
    def __init__(self, socket_path=NFC_ONBOARDING_SOCKET_PATH, *, timeout=5):
        self.socket_path = os.fspath(socket_path)
        self.timeout = timeout

    def _request(self, action, **fields):
        encoded = json.dumps({"action": action, **fields}, separators=(",", ":")).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall(encoded)
            response = connection.makefile("rb").readline(MAX_MESSAGE_BYTES + 1)
        if len(response) > MAX_MESSAGE_BYTES:
            raise NfcOnboardingError("NFC setup response is invalid")
        try:
            document = json.loads(response)
        except (ValueError, json.JSONDecodeError) as exc:
            raise NfcOnboardingError("NFC setup response is invalid") from exc
        if not isinstance(document, dict):
            raise NfcOnboardingError("NFC setup response is invalid")
        if document.get("ok") is not True:
            raise NfcOnboardingError(str(document.get("error") or "NFC setup is unavailable"))
        state = document.get("state")
        if not isinstance(state, dict):
            raise NfcOnboardingError("NFC setup response is invalid")
        return state

    def status(self):
        return self._request("status")

    def start(self):
        return self._request("start")

    def retry(self):
        return self._request("retry")

    def reassign(self):
        return self._request("reassign")

    def assign(self, token):
        return self._request("assign", token=token)

    def next(self):
        return self._request("next")

    def cancel(self):
        return self._request("cancel")

    def finish(self):
        return self._request("finish")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            return self._respond({"ok": False, "error": "NFC setup request is invalid"})
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError
            action = request.get("action")
            if action in {"status", "start", "retry", "reassign", "next", "cancel", "finish"} and set(request) == {"action"}:
                method = {
                    "status": self.server.engine.public_state,
                    "start": self.server.engine.start,
                    "retry": self.server.engine.retry,
                    "reassign": self.server.engine.allow_reassign,
                    "next": self.server.engine.next,
                    "cancel": self.server.engine.cancel,
                    "finish": self.server.engine.finish,
                }[action]
                state = method()
            elif action == "assign" and set(request) == {"action", "token"}:
                state = self.server.engine.assign(request["token"])
            else:
                raise NfcOnboardingError("NFC setup request is invalid")
            self._respond({"ok": True, "state": state})
        except (ContactError, NfcError, NfcOnboardingError, RecipientError, OSError, ValueError, json.JSONDecodeError):
            self._respond({"ok": False, "error": "NFC setup could not be updated"})

    def _respond(self, document):
        self.wfile.write(json.dumps(document, separators=(",", ":")).encode() + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, engine):
        self.engine = engine
        super().__init__(path, _Handler)


def serve(socket_path=NFC_ONBOARDING_SOCKET_PATH):
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise NfcOnboardingError("NFC setup socket is unsafe")
    path.unlink(missing_ok=True)
    engine = NfcOnboardingEngine()
    server = _Server(os.fspath(path), engine)
    os.chmod(path, 0o660)
    group = os.environ.get("MSGBOX_NFC_ONBOARDING_SOCKET_GROUP", "messagebox-onboarding")
    os.chown(path, -1, grp.getgrnam(group).gr_gid)
    reader_thread = threading.Thread(target=engine.run_reader, daemon=True)
    reader_thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        engine.stop()
        server.server_close()
        path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.serve:
        parser.error("--serve is required")
    serve()


if __name__ == "__main__":
    main()
