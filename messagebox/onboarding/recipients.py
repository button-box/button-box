"""Private recipient selection and two-way onboarding proof state."""

from __future__ import annotations

import json
import math
import os
import secrets
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path

from messagebox.contacts import ContactError, ContactStore, validate_contact
from messagebox.runtime_paths import (
    CONTACTS_FILE,
    NFC_ANNOUNCEMENT_FILE,
    NFC_ENROLLMENT_FILE,
    NFC_SELECTION_FILE,
    STATE_DIR,
)


STATE_VERSION = 1
TOKEN_BYTES = 18
MAX_CANDIDATES = 10
MAX_VISIBLE_RECIPIENTS = 20
PUBLIC_STATUSES = frozenset({"choose", "deferred", "testing", "complete"})
RECIPIENT_STATE_FILE = STATE_DIR / "recipient-onboarding.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
VOICE_REQUEST_FILE = STATE_DIR / "onboarding-voice-request.json"


class RecipientError(RuntimeError):
    """Safe recipient-onboarding error for the private worker boundary."""


def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


def _kind(jid):
    return "group" if jid.endswith("@g.us") else "person"


def _public_label(candidate):
    if candidate["kind"] == "person":
        return f"+{candidate['jid'].split('@', 1)[0]}"
    return candidate["label"]


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
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


class RecipientSetup:
    """Own private chat identities and expose only opaque recipient references."""

    def __init__(
        self,
        *,
        state_path=RECIPIENT_STATE_FILE,
        contacts_path=CONTACTS_FILE,
        events_path=EVENTS_FILE,
        voice_request_path=VOICE_REQUEST_FILE,
        account_reset_paths=None,
        clock=time.time,
        token_factory=None,
    ):
        self.state_path = Path(state_path)
        self.contacts = ContactStore(contacts_path, clock=clock)
        self.events_path = Path(events_path)
        self.voice_request_path = Path(voice_request_path)
        self.account_reset_paths = tuple(
            Path(path)
            for path in (
                account_reset_paths
                if account_reset_paths is not None
                else (
                    STATE_DIR / "nfc-onboarding.json",
                    NFC_SELECTION_FILE,
                    NFC_ENROLLMENT_FILE,
                    NFC_ANNOUNCEMENT_FILE,
                )
            )
        )
        self.clock = clock
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(TOKEN_BYTES))
        self._lock = threading.RLock()

    @staticmethod
    def _default_state():
        return {
            "version": STATE_VERSION,
            "status": "choose",
            "default_token": None,
            "started_at": None,
            "candidates": {},
            "proof": {
                "received": False,
                "played": False,
                "replied": False,
                "received_file": None,
                "session_id": None,
                "message_id": None,
            },
        }

    def _load(self):
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._from_contacts()
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RecipientError("recipient state is unavailable") from exc
        if not isinstance(document, dict) or set(document) != set(self._default_state()):
            raise RecipientError("recipient state is unavailable")
        if document.get("version") != STATE_VERSION or document.get("status") not in PUBLIC_STATUSES:
            raise RecipientError("recipient state is unavailable")
        if not isinstance(document.get("candidates"), dict):
            raise RecipientError("recipient state is unavailable")
        default_token = document.get("default_token")
        if default_token is not None and default_token not in document["candidates"]:
            raise RecipientError("recipient state is unavailable")
        started_at = document.get("started_at")
        if started_at is not None and (
            type(started_at) not in (int, float)
            or not math.isfinite(started_at)
            or started_at < 0
        ):
            raise RecipientError("recipient state is unavailable")
        proof = document.get("proof")
        if not isinstance(proof, dict) or set(proof) != set(self._default_state()["proof"]):
            raise RecipientError("recipient state is unavailable")
        for key in ("received", "played", "replied"):
            if not isinstance(proof[key], bool):
                raise RecipientError("recipient state is unavailable")
        for token, candidate in document["candidates"].items():
            if not isinstance(token, str) or not isinstance(candidate, dict):
                raise RecipientError("recipient state is unavailable")
            if set(candidate) != {"jid", "label", "kind", "available"}:
                raise RecipientError("recipient state is unavailable")
            try:
                validated = validate_contact(candidate["jid"], candidate["label"])
            except ContactError as exc:
                raise RecipientError("recipient state is unavailable") from exc
            if candidate["kind"] != validated["kind"] or not isinstance(candidate["available"], bool):
                raise RecipientError("recipient state is unavailable")
        if document["status"] == "complete":
            try:
                default_jid = self.contacts.load()["default_recipient"]
            except ContactError as exc:
                raise RecipientError("recipient state is unavailable") from exc
            matching_tokens = [
                token
                for token, candidate in document["candidates"].items()
                if candidate["jid"] == default_jid
            ]
            if len(matching_tokens) != 1:
                raise RecipientError("recipient state is unavailable")
            if document["default_token"] != matching_tokens[0]:
                document["default_token"] = matching_tokens[0]
                self._write(document)
        return document

    def _from_contacts(self):
        state = self._default_state()
        document = self.contacts.load()
        default_jid = document["default_recipient"]
        for jid, contact in document["contacts"].items():
            token = self.token_factory()
            state["candidates"][token] = {
                "jid": jid,
                "label": contact["label"],
                "kind": contact["kind"],
                "available": False,
            }
            if jid == default_jid:
                state["default_token"] = token
        if default_jid is not None:
            state["status"] = "testing"
            state["started_at"] = self.clock()
        if document["contacts"]:
            self._write(state)
        return state

    def _write(self, state):
        _atomic_json(self.state_path, state)

    @synchronized
    def reset_for_whatsapp_relink(self):
        """Erase all state that could route to the previously linked account."""
        try:
            self.contacts.clear_for_whatsapp_relink()
        except ContactError as exc:
            raise RecipientError("recipient state is unavailable") from exc
        for path in (
            self.state_path,
            self.voice_request_path,
            *self.account_reset_paths,
        ):
            Path(path).unlink(missing_ok=True)

    def _candidate(self, state, token, *, require_available=True):
        if not isinstance(token, str):
            raise RecipientError("recipient token is invalid")
        candidate = state["candidates"].get(token)
        if candidate is None or (require_available and not candidate["available"]):
            raise RecipientError("recipient is no longer available; refresh and try again")
        return candidate

    def _manual_candidate(self, state, phone):
        if (
            not isinstance(phone, str)
            or not phone.startswith("+")
            or not phone[1:].isdigit()
            or len(phone[1:]) < 8
            or len(phone[1:]) > 15
            or phone[1] == "0"
        ):
            raise RecipientError("phone number is invalid")
        jid = f"{phone[1:]}@s.whatsapp.net"
        for token, candidate in state["candidates"].items():
            if candidate["jid"] == jid:
                candidate.update(label=phone, kind="person", available=True)
                return token
        contacts = self.contacts.load()["contacts"]
        visible = sum(
            candidate["available"] or candidate["jid"] in contacts
            for candidate in state["candidates"].values()
        )
        if visible >= MAX_VISIBLE_RECIPIENTS:
            raise RecipientError("recipient limit reached")
        token = self.token_factory()
        state["candidates"][token] = {
            "jid": jid,
            "label": phone,
            "kind": "person",
            "available": True,
        }
        return token

    @synchronized
    def reconcile(self, rows):
        state = self._load()
        by_jid = {candidate["jid"]: token for token, candidate in state["candidates"].items()}
        for candidate in state["candidates"].values():
            candidate["available"] = False
        for row in rows[:MAX_CANDIDATES]:
            try:
                candidate = validate_contact(row.get("jid"), row.get("label"))
            except (AttributeError, ContactError):
                continue
            token = by_jid.get(candidate["jid"])
            if token is None:
                token = self.token_factory()
                by_jid[candidate["jid"]] = token
            state["candidates"][token] = {
                "jid": candidate["jid"],
                "label": candidate["label"],
                "kind": candidate["kind"],
                "available": True,
            }
        configured = self.contacts.load()["contacts"]
        for token, candidate in state["candidates"].items():
            contact = configured.get(candidate["jid"])
            if contact is not None:
                candidate["label"] = contact["label"]
                candidate["kind"] = contact["kind"]
        self._write(state)
        return self.public_state(state)

    @synchronized
    def defer(self):
        state = self._load()
        if state["default_token"] is not None:
            raise RecipientError("recipient setup has already started")
        state["status"] = "deferred"
        self._write(state)
        return self.public_state(state)

    @synchronized
    def select_default(self, token):
        state = self._load()
        if state["default_token"] is not None:
            raise RecipientError("default recipient is fixed")
        candidate = self._candidate(state, token)
        try:
            contacts = self.contacts.load()["contacts"]
            if candidate["jid"] in contacts:
                self.contacts.set_default_recipient(candidate["jid"])
            else:
                self.contacts.add_contact(
                    candidate["jid"], candidate["label"], make_default=True
                )
        except ContactError as exc:
            raise RecipientError(str(exc)) from exc
        state["default_token"] = token
        state["status"] = "testing"
        state["started_at"] = self.clock()
        state["proof"] = self._default_state()["proof"]
        self._write(state)
        _atomic_json(self.voice_request_path, {"version": 1, "enabled": True})
        return self.public_state(state)

    @synchronized
    def select_phone(self, phone):
        state = self._load()
        if state["default_token"] is not None:
            raise RecipientError("default recipient is fixed")
        token = self._manual_candidate(state, phone)
        self._write(state)
        return self.select_default(token)

    @synchronized
    def ensure_voice_request(self):
        state = self._load()
        if state["default_token"] is None:
            return False
        _atomic_json(self.voice_request_path, {"version": 1, "enabled": True})
        return True

    @synchronized
    def add(self, token):
        state = self._load()
        if state["status"] != "complete":
            raise RecipientError("complete the voice test before adding recipients")
        candidate = self._candidate(state, token)
        try:
            self.contacts.add_contact(candidate["jid"], candidate["label"])
        except ContactError as exc:
            raise RecipientError(str(exc)) from exc
        return self.public_state(state)

    @synchronized
    def add_phone(self, phone):
        state = self._load()
        if state["status"] != "complete":
            raise RecipientError("complete the voice test before adding recipients")
        token = self._manual_candidate(state, phone)
        candidate = self._candidate(state, token)
        if candidate["jid"] in self.contacts.load()["contacts"]:
            raise RecipientError("contact already exists")
        self._write(state)
        return self.add(token)

    @synchronized
    def remove(self, token):
        state = self._load()
        if state["status"] != "complete":
            raise RecipientError("complete the voice test before removing recipients")
        if token == state["default_token"]:
            raise RecipientError("default recipient cannot be removed during onboarding")
        candidate = self._candidate(state, token, require_available=False)
        try:
            if not self.contacts.remove_contact(candidate["jid"]):
                raise RecipientError("recipient is not configured")
        except ContactError as exc:
            raise RecipientError(str(exc)) from exc
        return self.public_state(state)

    @synchronized
    def choose_default(self, token):
        state = self._load()
        if state["status"] != "complete":
            raise RecipientError("complete the voice test before changing the default")
        candidate = self._candidate(state, token, require_available=False)
        try:
            if candidate["jid"] not in self.contacts.load()["contacts"]:
                raise RecipientError("recipient is not configured")
            self.contacts.choose_default_recipient(candidate["jid"])
        except ContactError as exc:
            raise RecipientError(str(exc)) from exc
        state["default_token"] = token
        self._write(state)
        return self.public_state(state)

    @synchronized
    def configured_candidate(self, token):
        """Resolve an opaque browser token only inside a private worker."""
        state = self._load()
        if state["status"] != "complete":
            raise RecipientError("recipient setup is incomplete")
        candidate = self._candidate(state, token, require_available=False)
        contact = self.contacts.contact(candidate["jid"])
        if contact is None:
            raise RecipientError("recipient is not configured")
        return {
            "token": token,
            "jid": candidate["jid"],
            "label": _public_label(candidate),
            "kind": candidate["kind"],
        }

    @synchronized
    def configured_candidate_by_jid(self, jid):
        """Resolve a private exact identity to its browser-safe display fields."""
        state = self._load()
        for token, candidate in state["candidates"].items():
            if candidate["jid"] == jid and self.contacts.contact(jid) is not None:
                return {
                    "token": token,
                    "label": _public_label(candidate),
                    "kind": candidate["kind"],
                }
        raise RecipientError("recipient is not configured")

    def _events(self, started_at):
        try:
            with open(self.events_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    ts = event.get("ts")
                    if type(ts) in (int, float) and ts >= started_at:
                        yield event
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RecipientError("voice proof is unavailable") from exc

    def _refresh_proof(self, state):
        if state["status"] != "testing" or state["default_token"] is None:
            return state
        candidate = self._candidate(state, state["default_token"], require_available=False)
        proof = state["proof"]
        received_files = set()
        for event in self._events(state["started_at"]):
            event_type = event.get("type")
            if (
                event_type == "received"
                and event.get("chat") == candidate["jid"]
                and isinstance(event.get("file"), str)
            ):
                received_files.add(event["file"])
                if not proof["received"]:
                    proof["received"] = True
                    proof["received_file"] = event["file"]
            elif (
                event_type == "guided_session_started"
                and event.get("flow") == "reply"
                and event.get("source_file") in received_files
                and isinstance(event.get("session_id"), str)
            ):
                if not proof["replied"] and proof["session_id"] != event["session_id"]:
                    proof.update(
                        received=True,
                        played=False,
                        replied=False,
                        received_file=event["source_file"],
                        session_id=event["session_id"],
                        message_id=None,
                    )
            elif (
                proof["session_id"]
                and event.get("session_id") == proof["session_id"]
                and event_type == "guided_inbound_played"
            ):
                proof["played"] = True
            elif (
                proof["session_id"]
                and event.get("session_id") == proof["session_id"]
                and event_type == "guided_approved"
                and event.get("flow") == "reply"
                and isinstance(event.get("message_id"), str)
            ):
                proof["message_id"] = event["message_id"]
            elif (
                proof["message_id"]
                and event_type == "sent"
                and event.get("flow") == "reply"
                and event.get("message_id") == proof["message_id"]
                and event.get("target") == candidate["jid"]
            ):
                proof["replied"] = True
        if proof["received"] and proof["played"] and proof["replied"]:
            state["status"] = "complete"
        self._write(state)
        return state

    @synchronized
    def public_state(self, state=None):
        state = self._refresh_proof(state or self._load())
        contacts = self.contacts.load()
        configured = contacts["contacts"]
        recipients = []
        for token, candidate in state["candidates"].items():
            is_configured = candidate["jid"] in configured
            if not candidate["available"] and not is_configured:
                continue
            recipients.append(
                {
                    "token": token,
                    "label": _public_label(candidate),
                    "kind": candidate["kind"],
                    "configured": is_configured,
                    "is_default": token == state["default_token"],
                    "available": candidate["available"],
                    "card_count": len(configured[candidate["jid"]]["card_uids"])
                    if is_configured
                    else 0,
                }
            )
        recipients.sort(key=lambda item: (not item["is_default"], item["label"].casefold()))
        default = next((item for item in recipients if item["is_default"]), None)
        return {
            "status": state["status"],
            "default": (
                {key: default[key] for key in ("token", "label", "kind")}
                if default is not None
                else None
            ),
            "proof": {
                key: state["proof"][key] for key in ("received", "played", "replied")
            },
            "recipients": recipients,
        }
