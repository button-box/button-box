"""Transient NFC state, contact routing, and enrollment workflows."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from contacts import (
        ContactError,
        ContactStore,
        normalize_card_uid,
        validate_contact,
    )
    from runtime_paths import NFC_ANNOUNCEMENT_FILE
except ModuleNotFoundError:
    from src.contacts import (
        ContactError,
        ContactStore,
        normalize_card_uid,
        validate_contact,
    )
    from src.runtime_paths import NFC_ANNOUNCEMENT_FILE


SELECTION_VERSION = 2
ENROLLMENT_VERSION = 1
ANNOUNCEMENT_VERSION = 1
DEFAULT_SELECTION_TTL_S = 30.0
DEFAULT_ENROLLMENT_TTL_S = 300


class NfcError(ValueError):
    """A safe NFC validation or state error suitable for display."""


def normalize_uid(value: object) -> str:
    """Use the contact store's canonical UID representation."""
    try:
        return normalize_card_uid(value)
    except ContactError as exc:
        raise NfcError(str(exc)) from exc


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _load_json(path, missing=None):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return missing
    except (OSError, TypeError, ValueError) as exc:
        raise NfcError(f"could not read {Path(path).name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise NfcError(f"{Path(path).name} must contain a JSON object")
    return payload


@contextmanager
def _locked_path(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with open(lock_path, "a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _public_contact(jid, contact):
    if contact is None:
        return None
    result = {
        "jid": jid,
        **{
            key: copy.deepcopy(value)
            for key, value in contact.items()
            if key != "card_uids"
        },
    }
    result["card_count"] = len(contact["card_uids"])
    result["paired"] = bool(contact["card_uids"])
    return result


def _contact_for_uid(document, uid):
    for jid, contact in document["contacts"].items():
        if uid in contact["card_uids"]:
            return jid, contact
    return None, None


class SelectionStore:
    """A short-lived, one-shot snapshot of an exact contact mapping."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    @property
    def claimed_path(self):
        return self.path.with_name(f".{self.path.name}.claimed")

    @contextmanager
    def locked(self):
        with _locked_path(self.path):
            yield

    def select(self, uid, jid, contacts_revision, *, new_presentation=True):
        uid = normalize_uid(uid)
        if not isinstance(jid, str) or not jid:
            raise NfcError("contact JID is invalid")
        if type(contacts_revision) is not int or contacts_revision < 0:
            raise NfcError("contacts revision is invalid")
        now = self.clock()
        with self.locked():
            if new_presentation:
                try:
                    self.claimed_path.unlink()
                except FileNotFoundError:
                    pass
            elif self.claimed_path.exists():
                return False
            previous = self._load(max_age=None)
            changed = (
                new_presentation
                or previous is None
                or previous["uid"] != uid
                or previous["jid"] != jid
                or previous["contacts_revision"] != contacts_revision
            )
            _atomic_json(
                self.path,
                {
                    "version": SELECTION_VERSION,
                    "uid": uid,
                    "jid": jid,
                    "contacts_revision": contacts_revision,
                    "selected_at": now if changed else previous["selected_at"],
                    "last_seen_at": now,
                },
            )
            return changed

    def claim(self, *, max_age=DEFAULT_SELECTION_TTL_S):
        with self.locked():
            selection = self._load(max_age=max_age)
            if selection is None:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                return None
            os.replace(self.path, self.claimed_path)
            return selection

    def clear(self):
        with self.locked():
            self._clear_locked()

    def _clear_locked(self):
        for path in (self.path, self.claimed_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def load(self, *, max_age=DEFAULT_SELECTION_TTL_S):
        with self.locked():
            return self._load(max_age=max_age)

    def _load(self, *, max_age=DEFAULT_SELECTION_TTL_S):
        payload = _load_json(self.path)
        if payload is None or payload.get("version") != SELECTION_VERSION:
            return None
        try:
            uid = normalize_uid(payload.get("uid"))
            jid = payload.get("jid")
            revision = payload.get("contacts_revision")
            selected_at = float(payload.get("selected_at"))
            last_seen_at = float(payload.get("last_seen_at"))
            if not isinstance(jid, str) or not jid:
                raise ValueError
            if type(revision) is not int or revision < 0:
                raise ValueError
        except (NfcError, TypeError, ValueError):
            return None
        now = self.clock()
        if last_seen_at > now + 5:
            return None
        if max_age is not None and now - last_seen_at > max_age:
            return None
        return {
            "uid": uid,
            "jid": jid,
            "contacts_revision": revision,
            "selected_at": selected_at,
            "last_seen_at": last_seen_at,
        }


class EnrollmentStore:
    """Locked handoff from an enrollment request to one scanned card."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def locked(self):
        with _locked_path(self.path):
            yield

    def begin(
        self,
        *,
        label,
        jid,
        card_clip="",
        create_contact=True,
        ttl_s=DEFAULT_ENROLLMENT_TTL_S,
    ):
        try:
            contact = validate_contact(jid, label, card_clip=card_clip or "")
        except ContactError as exc:
            raise NfcError(str(exc)) from exc
        if type(ttl_s) not in (int, float) or ttl_s <= 0 or ttl_s > 1800:
            raise NfcError("enrollment duration must be between 1 and 1800 seconds")
        if not isinstance(create_contact, bool):
            raise NfcError("create_contact must be true or false")
        now = self.clock()
        payload = {
            "version": ENROLLMENT_VERSION,
            "request_id": uuid.uuid4().hex,
            "label": contact["label"],
            "jid": contact["jid"],
            "card_clip": contact["card_clip"],
            "create_contact": create_contact,
            "created_at": now,
            "expires_at": now + ttl_s,
            "status": "pending",
        }
        with self.locked():
            if self._active_locked() is not None:
                raise NfcError("another card enrollment is already active")
            _atomic_json(self.path, payload)
        return copy.deepcopy(payload)

    def active(self):
        with self.locked():
            active = self._active_locked()
            return copy.deepcopy(active)

    def _active_locked(self):
        payload = _load_json(self.path)
        if payload is None:
            return None
        try:
            if payload.get("version") != ENROLLMENT_VERSION:
                raise ValueError
            request_id = payload.get("request_id")
            contact = validate_contact(
                payload.get("jid"),
                payload.get("label"),
                card_clip=payload.get("card_clip", ""),
            )
            created_at = float(payload.get("created_at"))
            expires_at = float(payload.get("expires_at"))
            status = payload.get("status")
            create_contact = payload.get("create_contact")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError
            if status not in ("pending", "claimed"):
                raise ValueError
            if not isinstance(create_contact, bool):
                raise ValueError
            normalized = {
                "version": ENROLLMENT_VERSION,
                "request_id": request_id,
                "label": contact["label"],
                "jid": contact["jid"],
                "card_clip": contact["card_clip"],
                "create_contact": create_contact,
                "created_at": created_at,
                "expires_at": expires_at,
                "status": status,
            }
            if status == "claimed":
                normalized["claimed_uid"] = normalize_uid(payload.get("claimed_uid"))
        except (ContactError, NfcError, TypeError, ValueError):
            self._remove_locked()
            return None
        if status == "pending" and expires_at <= self.clock():
            self._remove_locked()
            return None
        return normalized

    def claim(self, uid):
        uid = normalize_uid(uid)
        with self.locked():
            request = self._active_locked()
            if request is None or request["status"] != "pending":
                return None
            request["status"] = "claimed"
            request["claimed_uid"] = uid
            _atomic_json(self.path, request)
            return copy.deepcopy(request)

    def complete(self, request_id):
        with self.locked():
            request = self._active_locked()
            if request is None or request["request_id"] != request_id:
                return False
            self._remove_locked()
            return True

    def cancel(self, request_id):
        with self.locked():
            request = self._active_locked()
            if request is None or request["request_id"] != request_id:
                return False
            self._remove_locked()
            return True

    def _remove_locked(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class AnnouncementStore:
    """Single-slot handoff to the process that owns audio playback."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    @property
    def claimed_path(self):
        return self.path.with_name(f".{self.path.name}.claimed")

    @property
    def acknowledged_path(self):
        return self.path.with_name(f".{self.path.name}.acknowledged")

    def put(self, *, action, uid, prompt):
        if not isinstance(action, str) or not action or len(action) > 32:
            raise NfcError("announcement action is invalid")
        if not isinstance(prompt, str) or len(prompt) > 1024 or "\x00" in prompt:
            raise NfcError("announcement prompt is invalid")
        payload = {
            "version": ANNOUNCEMENT_VERSION,
            "action": action,
            "uid": normalize_uid(uid),
            "prompt": prompt,
            "created_at": self.clock(),
        }
        self.clear_acknowledgement()
        _atomic_json(self.path, payload)
        return payload

    def clear(self):
        for pending in (self.path, self.claimed_path):
            try:
                pending.unlink()
            except FileNotFoundError:
                pass
        self.clear_acknowledgement()

    def acknowledge(self, uid):
        _atomic_json(
            self.acknowledged_path,
            {
                "version": ANNOUNCEMENT_VERSION,
                "uid": normalize_uid(uid),
                "announced_at": self.clock(),
            },
        )

    def is_acknowledged(self, uid):
        payload = _load_json(self.acknowledged_path)
        if payload is None or payload.get("version") != ANNOUNCEMENT_VERSION:
            return False
        try:
            return normalize_uid(payload.get("uid")) == normalize_uid(uid)
        except NfcError:
            return False

    def clear_acknowledgement(self):
        try:
            self.acknowledged_path.unlink()
        except FileNotFoundError:
            pass

    def take(self, max_age=10):
        try:
            os.replace(self.path, self.claimed_path)
        except FileNotFoundError:
            if not self.claimed_path.exists():
                return None
        try:
            payload = _load_json(self.claimed_path)
            if payload is None or payload.get("version") != ANNOUNCEMENT_VERSION:
                return None
            created_at = float(payload.get("created_at"))
            if created_at > self.clock() + 5 or self.clock() - created_at > max_age:
                return None
            action = payload.get("action")
            prompt = payload.get("prompt", "")
            if not isinstance(action, str) or not action or len(action) > 32:
                return None
            if not isinstance(prompt, str) or len(prompt) > 1024 or "\x00" in prompt:
                return None
            return {
                "action": action,
                "uid": normalize_uid(payload.get("uid")),
                "prompt": prompt,
            }
        except (NfcError, TypeError, ValueError):
            return None
        finally:
            try:
                self.claimed_path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class ScanResult:
    action: str
    uid: Optional[str] = None
    contact: Optional[dict[str, Any]] = None
    announce: bool = False

    def public(self):
        return {
            "action": self.action,
            "contact": copy.deepcopy(self.contact),
            "announce": self.announce,
        }


class NfcRouter:
    def __init__(self, contacts, selection, enrollment, announcements=None):
        self.contacts = contacts
        self.selection = selection
        self.enrollment = enrollment
        self.announcements = (
            announcements
            if announcements is not None
            else AnnouncementStore(
                enrollment.path.with_name(NFC_ANNOUNCEMENT_FILE.name),
                clock=enrollment.clock,
            )
        )

    def begin_enrollment(
        self,
        *,
        label,
        jid,
        ttl_s=DEFAULT_ENROLLMENT_TTL_S,
        card_clip="",
        create_contact=True,
    ):
        return self.enrollment.begin(
            label=label,
            jid=jid,
            card_clip=card_clip,
            create_contact=create_contact,
            ttl_s=ttl_s,
        )

    def cancel_enrollment(self, request_id=None):
        if request_id is None:
            request = self.enrollment.active()
            if request is None:
                return False
            request_id = request["request_id"]
        return self.enrollment.cancel(request_id)

    def card_seen(self, raw_uid, *, new_presentation=True):
        uid = normalize_uid(raw_uid)
        recovered = self.reconcile_enrollment(new_presentation=new_presentation)
        if recovered is not None:
            return recovered
        request = self.enrollment.claim(uid) if new_presentation else None
        if request is not None:
            return self.reconcile_enrollment(
                request["request_id"], new_presentation=new_presentation
            )

        document = self.contacts.load()
        jid, contact = _contact_for_uid(document, uid)
        if contact is None:
            self.selection.clear()
            return ScanResult("unknown", uid, None, new_presentation)
        public_contact = _public_contact(jid, contact)
        if len(document["contacts"]) < 2:
            self.selection.clear()
            return ScanResult("recognized", uid, public_contact, new_presentation)
        changed = self.selection.select(
            uid,
            jid,
            document["revision"],
            new_presentation=new_presentation,
        )
        return ScanResult(
            "selected" if changed else "refreshed",
            uid,
            public_contact,
            changed,
        )

    def reconcile_enrollment(self, request_id=None, *, new_presentation=True):
        with self.enrollment.locked():
            request = self.enrollment._active_locked()
            if (
                request is None
                or request["status"] != "claimed"
                or (request_id is not None and request["request_id"] != request_id)
            ):
                return None
            try:
                contact, contact_count, revision = self._assign_enrolled_contact(request)
            except ValueError:
                if not request["create_contact"] and self.contacts.contact(request["jid"]) is None:
                    self.enrollment._remove_locked()
                raise
            uid = request["claimed_uid"]
            if contact_count >= 2:
                self.selection.select(
                    uid,
                    request["jid"],
                    revision,
                    new_presentation=new_presentation,
                )
            else:
                self.selection.clear()
            result = ScanResult("enrolled", uid, contact, True)
            self.announcements.put(
                action=result.action,
                uid=uid,
                prompt=contact.get("card_clip", ""),
            )
            self.enrollment._remove_locked()
            return result

    def _assign_enrolled_contact(self, request):
        result = self.contacts.enroll_card(
            request["jid"],
            request["claimed_uid"],
            label=request["label"],
            card_clip=request["card_clip"],
            create_contact=request["create_contact"],
        )
        contact = result["contact"]
        return (
            _public_contact(contact["jid"], contact),
            result["contact_count"],
            result["revision"],
        )

    def card_absent(self):
        return ScanResult("removed")

    def active_contact(self, max_age=DEFAULT_SELECTION_TTL_S):
        resolved = _load_valid_selection(
            self.contacts, self.selection, max_age=max_age, consume=False
        )
        return resolved["contact"] if resolved is not None else None

    def status(self, max_age=DEFAULT_SELECTION_TTL_S):
        public = self.contacts.public_view()
        return {
            **public,
            "active": self.active_contact(max_age=max_age),
            "enrollment": public_enrollment(self.enrollment.active()),
        }


def public_enrollment(request):
    if request is None:
        return None
    return {
        key: copy.deepcopy(value)
        for key, value in request.items()
        if key != "claimed_uid"
    }


def _load_valid_selection(contacts, selection, *, max_age, consume):
    snapshot = (
        selection.claim(max_age=max_age)
        if consume
        else selection.load(max_age=max_age)
    )
    if snapshot is None:
        return None
    document = contacts.load()
    jid, contact = _contact_for_uid(document, snapshot["uid"])
    if (
        document["revision"] != snapshot["contacts_revision"]
        or jid != snapshot["jid"]
        or contact is None
    ):
        selection.clear()
        return None
    return {
        "uid": snapshot["uid"],
        "jid": jid,
        "contacts_revision": snapshot["contacts_revision"],
        "contact": _public_contact(jid, contact),
    }


def active_selection(
    contacts_path,
    selection_path,
    *,
    max_age=DEFAULT_SELECTION_TTL_S,
    clock=time.time,
):
    return _load_valid_selection(
        ContactStore(contacts_path),
        SelectionStore(selection_path, clock=clock),
        max_age=max_age,
        consume=False,
    )


def claim_selection(
    contacts_path,
    selection_path,
    *,
    max_age=DEFAULT_SELECTION_TTL_S,
    clock=time.time,
):
    """Consume a scan only if its exact contact snapshot is still current."""
    return _load_valid_selection(
        ContactStore(contacts_path),
        SelectionStore(selection_path, clock=clock),
        max_age=max_age,
        consume=True,
    )
