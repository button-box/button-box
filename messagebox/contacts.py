#!/usr/bin/env python3
"""Authoritative private contact, NFC card, and listener profile store."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
import os
import posixpath
import pwd
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from messagebox.runtime_paths import ASSET_DIR, CONTACTS_FILE, NFC_ENROLLMENT_FILE


SCHEMA_VERSION = 3
ASSET_ROOT = str(ASSET_DIR)
MAX_LABEL_LENGTH = 80
MAX_NAME_LENGTH = 80
SERVICE_USER = "messagebox"
ENV_FILE = Path("/etc/messagebox/env")

_GROUP_JID = re.compile(r"^[0-9]+(?:-[0-9]+)?@g\.us$")
_PERSON_JID = re.compile(r"^[0-9]+@s\.whatsapp\.net$")
_SIGNAL_GROUP_ID = re.compile(r"^group\.[A-Za-z0-9+/=]{8,128}$")
_SIGNAL_PERSON_ID = re.compile(r"^\+[1-9][0-9]{6,14}$")
_UID = re.compile(r"^[0-9A-F]+$")
_CHANNELS = {"whatsapp", "signal"}
_LEGACY_ROOT_KEYS = {"version", "revision", "contacts", "listeners"}
_ROOT_KEYS = _LEGACY_ROOT_KEYS | {"default_recipient"}
_CONTACT_KEYS_LEGACY = {
    "label",
    "kind",
    "receive_after",
    "card_uids",
    "card_clip",
}
_CONTACT_KEYS = _CONTACT_KEYS_LEGACY | {"channel"}
_LISTENER_KEYS = {"name", "listened_clip"}
JsonObject = dict[str, Any]


class ContactError(ValueError):
    """A safe validation or persistence error suitable for callers to display."""


def _empty_document() -> JsonObject:
    return {
        "version": SCHEMA_VERSION,
        "revision": 0,
        "default_recipient": None,
        "contacts": {},
        "listeners": {},
    }


def _chat_kind(value: object, channel: str | None = "whatsapp") -> str:
    """Derive person/group kind for one channel, or any channel if unset."""
    if not isinstance(value, str):
        raise ContactError("contact identifier is invalid")
    candidates = (channel,) if channel is not None else ("whatsapp", "signal")
    for candidate in candidates:
        if candidate == "signal":
            if _SIGNAL_GROUP_ID.fullmatch(value):
                return "group"
            if _SIGNAL_PERSON_ID.fullmatch(value):
                return "person"
        else:
            if _GROUP_JID.fullmatch(value):
                return "group"
            if _PERSON_JID.fullmatch(value):
                return "person"
    raise ContactError("contact identifier is invalid")


def _clean_chat_jid(value: object, channel: str | None = "whatsapp") -> str:
    if not isinstance(value, str):
        raise ContactError("contact identifier is invalid")
    jid = value.strip()
    _chat_kind(jid, channel)
    return jid


def _clean_channel(value: object) -> str:
    if value not in _CHANNELS:
        raise ContactError("channel must be 'whatsapp' or 'signal'")
    return value


def _canonical_listener_jid(value: object) -> str:
    if not isinstance(value, str):
        raise ContactError("listener JID is invalid")
    jid = value.strip().lower()
    if not jid or len(jid) > 320 or jid.count("@") != 1:
        raise ContactError("listener JID is invalid")
    user, server = jid.split("@", 1)
    if ":" in user:
        user = user.split(":", 1)[0]
    if (
        not user
        or not server
        or ":" in server
        or any(character.isspace() for character in user + server)
    ):
        raise ContactError("listener JID is invalid")
    return f"{user}@{server}"


def _clean_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ContactError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or "\x00" in cleaned:
        raise ContactError(f"{field} is invalid")
    return cleaned


def _clean_clip(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ContactError(f"{field} must be text")
    clip = value.strip()
    if not clip:
        return ""
    if "\x00" in clip or not clip.startswith("/") or not clip.endswith(".wav"):
        raise ContactError(f"{field} is invalid")
    normalized = posixpath.normpath(clip)
    if not normalized.startswith(f"{ASSET_ROOT}/"):
        raise ContactError(f"{field} is invalid")
    return normalized


def _clean_receive_after(value: object) -> int | float:
    if type(value) is int:
        if value >= 0:
            return value
    elif type(value) is float and math.isfinite(value) and value >= 0:
        return value
    raise ContactError("receive_after must be a finite nonnegative number")


def _normalize_uid(value: object) -> str:
    if isinstance(value, bytes):
        raw = value.hex().upper()
    elif isinstance(value, str):
        raw = re.sub(r"[\s:\-]", "", value).upper()
    else:
        raise ContactError("card UID must be bytes or hexadecimal text")
    if len(raw) < 8 or len(raw) > 20 or len(raw) % 2 or not _UID.fullmatch(raw):
        raise ContactError("card UID must contain 4 to 10 hexadecimal bytes")
    return ":".join(raw[index : index + 2] for index in range(0, len(raw), 2))


def normalize_card_uid(value: object) -> str:
    """Return the canonical UID stored for a physical card."""
    return _normalize_uid(value)


def validate_contact(jid, label, *, channel="whatsapp", card_clip="") -> JsonObject:
    """Validate contact input without reading or changing the store."""
    channel = _clean_channel(channel)
    jid = _clean_chat_jid(jid, channel)
    return {
        "jid": jid,
        "label": _clean_text(label, "label", MAX_LABEL_LENGTH),
        "kind": _chat_kind(jid, channel),
        "channel": channel,
        "card_clip": _clean_clip(card_clip, "card clip"),
    }


def _validate_document(document: object) -> JsonObject:
    if not isinstance(document, dict):
        raise ContactError("contact store has an invalid schema")
    version = document.get("version")
    if type(version) is not int or version not in {1, 2, SCHEMA_VERSION}:
        raise ContactError("contact store version is unsupported")
    expected_keys = _LEGACY_ROOT_KEYS if version == 1 else _ROOT_KEYS
    if set(document) != expected_keys:
        raise ContactError("contact store has an invalid schema")
    revision = document["revision"]
    if type(revision) is not int or revision < 0:
        raise ContactError("contact store has an invalid revision")
    contacts = document["contacts"]
    listeners = document["listeners"]
    if not isinstance(contacts, dict) or not isinstance(listeners, dict):
        raise ContactError("contact store has an invalid schema")

    seen_uids: set[str] = set()
    contact_keys = _CONTACT_KEYS if version == SCHEMA_VERSION else _CONTACT_KEYS_LEGACY
    for jid, contact in contacts.items():
        if not isinstance(contact, dict) or set(contact) != contact_keys:
            raise ContactError("contact store contains an invalid contact")
        channel = _clean_channel(contact["channel"]) if version == SCHEMA_VERSION else "whatsapp"
        kind = _chat_kind(jid, channel)
        if contact["kind"] != kind:
            raise ContactError("contact store contains an invalid contact kind")
        if _clean_text(contact["label"], "label", MAX_LABEL_LENGTH) != contact["label"]:
            raise ContactError("contact store contains an invalid label")
        _clean_receive_after(contact["receive_after"])
        if _clean_clip(contact["card_clip"], "card clip") != contact["card_clip"]:
            raise ContactError("contact store contains an invalid card clip")
        card_uids = contact["card_uids"]
        if not isinstance(card_uids, list):
            raise ContactError("contact store contains invalid card UIDs")
        for uid in card_uids:
            if not isinstance(uid, str) or _normalize_uid(uid) != uid or uid in seen_uids:
                raise ContactError("contact store contains invalid card UIDs")
            seen_uids.add(uid)

    for jid, listener in listeners.items():
        if _canonical_listener_jid(jid) != jid:
            raise ContactError("contact store contains an invalid listener JID")
        if not isinstance(listener, dict) or set(listener) != _LISTENER_KEYS:
            raise ContactError("contact store contains an invalid listener")
        if _clean_text(listener["name"], "name", MAX_NAME_LENGTH) != listener["name"]:
            raise ContactError("contact store contains an invalid listener name")
        if _clean_clip(listener["listened_clip"], "listened clip") != listener["listened_clip"]:
            raise ContactError("contact store contains an invalid listened clip")
    upgraded = copy.deepcopy(document)
    if version == 1:
        upgraded["default_recipient"] = (
            next(iter(contacts)) if len(contacts) == 1 else None
        )
    if version < SCHEMA_VERSION:
        upgraded["version"] = SCHEMA_VERSION
        for contact in upgraded["contacts"].values():
            contact.setdefault("channel", "whatsapp")
    default_recipient = upgraded["default_recipient"]
    if default_recipient is not None:
        if not isinstance(default_recipient, str):
            raise ContactError("contact store has an invalid default recipient")
        default_recipient = default_recipient.strip()
        if default_recipient not in contacts:
            raise ContactError("contact store has an invalid default recipient")
        upgraded["default_recipient"] = default_recipient
    return upgraded


def _contact_result(jid: str, contact: JsonObject) -> JsonObject:
    return {"jid": jid, **copy.deepcopy(contact)}


class ContactStore:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def _read(self) -> JsonObject:
        try:
            with open(self.path, encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return _empty_document()
        except (OSError, ValueError) as exc:
            raise ContactError("contact store could not be read") from exc
        return _validate_document(document)

    @contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+b") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except ContactError:
            raise
        except OSError as exc:
            raise ContactError("contact store could not be locked") from exc

    def _write(self, document: JsonObject) -> None:
        temporary: str | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                json.dump(
                    document,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
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
        except (OSError, TypeError, ValueError) as exc:
            raise ContactError("contact store could not be saved") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _mutate(self, mutation):
        with self._locked():
            document = self._read()
            changed, result = mutation(document)
            if changed:
                document["revision"] += 1
                self._write(document)
            return result

    def load(self) -> JsonObject:
        """Return a validated snapshot without creating a missing store."""
        return copy.deepcopy(self._read())

    def add_contact(
        self,
        jid,
        label,
        *,
        channel="whatsapp",
        card_clip="",
        receive_after=None,
        make_default=False,
    ) -> JsonObject:
        channel = _clean_channel(channel)
        jid = _clean_chat_jid(jid, channel)
        label = _clean_text(label, "label", MAX_LABEL_LENGTH)
        card_clip = _clean_clip(card_clip, "card clip")
        if not isinstance(make_default, bool):
            raise ContactError("make_default must be true or false")

        def add(document):
            if jid in document["contacts"]:
                raise ContactError("contact already exists")
            if make_default and document["default_recipient"] is not None:
                raise ContactError("default recipient already exists")
            if receive_after is None:
                try:
                    effective_receive_after = self.clock()
                except Exception as exc:
                    raise ContactError("clock did not return a valid time") from exc
            else:
                effective_receive_after = receive_after
            effective_receive_after = _clean_receive_after(effective_receive_after)
            contact = {
                "label": label,
                "kind": _chat_kind(jid, channel),
                "channel": channel,
                "receive_after": effective_receive_after,
                "card_uids": [],
                "card_clip": card_clip,
            }
            document["contacts"][jid] = contact
            if make_default or (
                len(document["contacts"]) == 1
                and document["default_recipient"] is None
            ):
                document["default_recipient"] = jid
            return True, _contact_result(jid, contact)

        return self._mutate(add)

    def remove_contact(self, jid) -> bool:
        jid = _clean_chat_jid(jid, channel=None)

        def remove(document):
            if jid not in document["contacts"]:
                return False, False
            del document["contacts"][jid]
            if document["default_recipient"] == jid:
                document["default_recipient"] = None
            return True, True

        return self._mutate(remove)

    def set_default_recipient(self, jid) -> JsonObject:
        """Set the first explicit default without replacing an existing one."""
        jid = _clean_chat_jid(jid, channel=None)

        def set_default(document):
            contact = document["contacts"].get(jid)
            if contact is None:
                raise ContactError("contact does not exist")
            existing = document["default_recipient"]
            if existing is not None:
                if existing == jid:
                    return False, _contact_result(jid, contact)
                raise ContactError("default recipient already exists")
            document["default_recipient"] = jid
            return True, _contact_result(jid, contact)

        return self._mutate(set_default)

    def choose_default_recipient(self, jid) -> JsonObject:
        """Choose any configured contact as the current default recipient."""
        jid = _clean_chat_jid(jid, channel=None)

        def choose_default(document):
            contact = document["contacts"].get(jid)
            if contact is None:
                raise ContactError("contact does not exist")
            if document["default_recipient"] == jid:
                return False, _contact_result(jid, contact)
            document["default_recipient"] = jid
            return True, _contact_result(jid, contact)

        return self._mutate(choose_default)

    def assign_card(self, jid, uid) -> JsonObject:
        jid = _clean_chat_jid(jid, channel=None)
        uid = _normalize_uid(uid)

        def assign(document):
            contacts = document["contacts"]
            if jid not in contacts:
                raise ContactError("contact does not exist")
            if uid in contacts[jid]["card_uids"]:
                return False, _contact_result(jid, contacts[jid])
            for contact in contacts.values():
                if uid in contact["card_uids"]:
                    contact["card_uids"].remove(uid)
            contacts[jid]["card_uids"].append(uid)
            return True, _contact_result(jid, contacts[jid])

        return self._mutate(assign)

    def enroll_card(
        self,
        jid,
        uid,
        *,
        label,
        channel="whatsapp",
        card_clip="",
        create_contact=True,
    ) -> JsonObject:
        """Assign a card, optionally creating its contact in the same revision."""
        candidate = validate_contact(jid, label, channel=channel, card_clip=card_clip)
        jid = candidate["jid"]
        uid = _normalize_uid(uid)
        if not isinstance(create_contact, bool):
            raise ContactError("create_contact must be true or false")

        def enroll(document):
            contacts = document["contacts"]
            changed = False
            if jid not in contacts:
                if not create_contact:
                    raise ContactError("contact no longer exists")
                try:
                    receive_after = _clean_receive_after(self.clock())
                except ContactError:
                    raise
                except Exception as exc:
                    raise ContactError("clock did not return a valid time") from exc
                contacts[jid] = {
                    "label": candidate["label"],
                    "kind": candidate["kind"],
                    "channel": candidate["channel"],
                    "receive_after": receive_after,
                    "card_uids": [],
                    "card_clip": candidate["card_clip"],
                }
                if len(contacts) == 1 and document["default_recipient"] is None:
                    document["default_recipient"] = jid
                changed = True
            for other_jid, contact in contacts.items():
                if other_jid != jid and uid in contact["card_uids"]:
                    contact["card_uids"].remove(uid)
                    changed = True
            if uid not in contacts[jid]["card_uids"]:
                contacts[jid]["card_uids"].append(uid)
                changed = True
            return changed, {
                "contact": _contact_result(jid, contacts[jid]),
                "contact_count": len(contacts),
                "revision": document["revision"] + (1 if changed else 0),
            }

        return self._mutate(enroll)

    def remove_card(self, uid) -> bool:
        uid = _normalize_uid(uid)

        def remove(document):
            for contact in document["contacts"].values():
                if uid in contact["card_uids"]:
                    contact["card_uids"].remove(uid)
                    return True, True
            return False, False

        return self._mutate(remove)

    def resolve_card(self, uid) -> JsonObject | None:
        uid = _normalize_uid(uid)
        document = self._read()
        for jid, contact in document["contacts"].items():
            if uid in contact["card_uids"]:
                return _contact_result(jid, contact)
        return None

    def contact(self, jid) -> JsonObject | None:
        jid = _clean_chat_jid(jid, channel=None)
        contact = self._read()["contacts"].get(jid)
        return _contact_result(jid, contact) if contact is not None else None

    def allowed_jids(self) -> tuple[str, ...]:
        return tuple(sorted(self._read()["contacts"]))

    def upsert_listener(self, jid, name, *, listened_clip="") -> JsonObject:
        jid = _canonical_listener_jid(jid)
        name = _clean_text(name, "name", MAX_NAME_LENGTH)
        listened_clip = _clean_clip(listened_clip, "listened clip")
        listener = {"name": name, "listened_clip": listened_clip}

        def upsert(document):
            if document["listeners"].get(jid) == listener:
                return False, {"jid": jid, **copy.deepcopy(listener)}
            document["listeners"][jid] = listener
            return True, {"jid": jid, **copy.deepcopy(listener)}

        return self._mutate(upsert)

    def remove_listener(self, jid) -> bool:
        jid = _canonical_listener_jid(jid)

        def remove(document):
            if jid not in document["listeners"]:
                return False, False
            del document["listeners"][jid]
            return True, True

        return self._mutate(remove)

    def listener_profiles(self) -> dict[str, dict[str, str]]:
        listeners = self._read()["listeners"]
        return {
            jid: {"name": listener["name"], "clip": listener["listened_clip"]}
            for jid, listener in listeners.items()
        }

    def public_view(self) -> JsonObject:
        document = self._read()
        contacts = {}
        for jid, contact in document["contacts"].items():
            public_contact = {
                key: copy.deepcopy(value)
                for key, value in contact.items()
                if key != "card_uids"
            }
            public_contact["card_count"] = len(contact["card_uids"])
            public_contact["paired"] = bool(contact["card_uids"])
            contacts[jid] = public_contact
        return {
            "version": document["version"],
            "revision": document["revision"],
            "default_recipient": document["default_recipient"],
            "contacts": contacts,
            "listeners": copy.deepcopy(document["listeners"]),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="messagebox-contact",
        description="Manage Message Box contacts and NFC cards.",
        epilog=(
            "CHAT_JID must be exact. WhatsApp: group 123456789@g.us; direct "
            "chat 15551234567@s.whatsapp.net. Signal (--channel signal): "
            "group group.<base64id>; direct chat +15551234567"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser(
        "add", help="add a contact and arm enrollment for its first card"
    )
    add.add_argument("label", metavar="LABEL")
    add.add_argument(
        "chat_jid",
        metavar="CHAT_JID",
        help=(
            "WhatsApp: group JID (123456789@g.us) or direct-chat JID "
            "(15551234567@s.whatsapp.net); Signal: group.<base64id> or "
            "+15551234567"
        ),
    )
    add.add_argument(
        "--channel",
        choices=sorted(_CHANNELS),
        default="whatsapp",
        help="messaging channel this contact is reached on (default: whatsapp)",
    )
    add.add_argument(
        "--no-card",
        action="store_true",
        help="add immediately without enrolling an NFC card",
    )
    add.add_argument("--card-clip", default="", metavar="WAV")

    enroll = commands.add_parser(
        "enroll", help="arm enrollment for an additional card"
    )
    enroll.add_argument(
        "chat_jid",
        metavar="CHAT_JID",
        help=(
            "exact group JID (123456789@g.us) or direct-chat JID "
            "(15551234567@s.whatsapp.net)"
        ),
    )

    remove = commands.add_parser("remove", help="remove a contact and all its cards")
    remove.add_argument(
        "chat_jid",
        metavar="CHAT_JID",
        help=(
            "exact group JID (123456789@g.us) or direct-chat JID "
            "(15551234567@s.whatsapp.net)"
        ),
    )
    commands.add_parser("list", help="list contacts without exposing card IDs")
    commands.add_parser("count", help="print only the number of contacts")
    return parser


def _reexec_as_service_user(argv: list[str]) -> None:
    try:
        service_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    except KeyError as exc:
        raise ContactError(f"service user {SERVICE_USER} does not exist") from exc
    if os.geteuid() == service_uid:
        return
    command = [
        "sudo",
        "-u",
        SERVICE_USER,
        "-H",
        "--",
        sys.executable,
        "-m",
        "messagebox.contacts",
        *argv,
    ]
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        raise ContactError(f"could not run contact as {SERVICE_USER}") from exc
    raise ContactError(f"could not run contact as {SERVICE_USER}")


def _detection_beep_enabled(path=ENV_FILE) -> bool:
    enabled = {"1", "true", "yes", "on"}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, separator, value = line.partition("=")
                if separator and key.strip() == "MSGBOX_NFC_DETECTION_BEEP":
                    return value.strip().lower() in enabled
    except OSError as exc:
        raise ContactError(f"could not read {path}") from exc
    return False


def _verify_enrollment_services() -> None:
    for unit in ("messagebox-nfc.service", "messagebox-button.service"):
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit], check=False
            )
        except OSError as exc:
            raise ContactError("could not query Message Box services") from exc
        if result.returncode != 0:
            raise ContactError(f"{unit} must be active before enrolling a card")
    if not _detection_beep_enabled():
        raise ContactError(
            "MSGBOX_NFC_DETECTION_BEEP must be enabled in /etc/messagebox/env"
        )


def _stage_enrollment(contact, *, create_contact, enrollment_factory=None) -> None:
    if enrollment_factory is None:
        from messagebox.nfc_state import EnrollmentStore

        enrollment = EnrollmentStore(NFC_ENROLLMENT_FILE)
    else:
        enrollment = enrollment_factory(NFC_ENROLLMENT_FILE)
    try:
        enrollment.begin(
            label=contact["label"],
            jid=contact["jid"],
            channel=contact.get("channel", "whatsapp"),
            card_clip=contact["card_clip"],
            create_contact=create_contact,
            ttl_s=300,
        )
    except ValueError as exc:
        raise ContactError(str(exc)) from exc
    print(f"Card enrollment for {contact['label']} is armed for five minutes.")
    print("This helper now exits; the NFC service continues listening asynchronously.")
    print("Present one card. A beep confirms enrollment.")


def main(
    argv=None,
    *,
    store=None,
    enrollment_factory=None,
    service_check=None,
    reexec=True,
):
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(arguments)
    if reexec and store is None:
        try:
            _reexec_as_service_user(arguments)
        except ContactError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    contacts = store if store is not None else ContactStore(CONTACTS_FILE)
    check_services = service_check or _verify_enrollment_services

    try:
        if args.command == "count":
            print(len(contacts.load()["contacts"]))
            return 0
        if args.command == "list":
            public = contacts.public_view()["contacts"]
            if not public:
                print("No contacts configured.")
            for jid, contact in sorted(public.items()):
                cards = contact["card_count"]
                noun = "card" if cards == 1 else "cards"
                print(f"{contact['label']}: {jid} ({cards} {noun})")
            return 0
        if args.command == "remove":
            contact = contacts.contact(args.chat_jid)
            if contact is None:
                raise ContactError("contact does not exist")
            contacts.remove_contact(args.chat_jid)
            print(f"Removed {contact['label']} and all enrolled cards.")
            return 0
        if args.command == "add":
            candidate = validate_contact(
                args.chat_jid, args.label, channel=args.channel, card_clip=args.card_clip
            )
            if contacts.contact(candidate["jid"]) is not None:
                raise ContactError("contact already exists")
            if args.no_card:
                contacts.add_contact(
                    candidate["jid"],
                    candidate["label"],
                    channel=candidate["channel"],
                    card_clip=candidate["card_clip"],
                )
                count = len(contacts.load()["contacts"])
                print(f"Added {candidate['label']} without an NFC card.")
                if count > 1:
                    print(
                        "The existing default remains active; an enrolled card can "
                        "temporarily select another contact."
                    )
                return 0
            check_services()
            _stage_enrollment(
                candidate,
                create_contact=True,
                enrollment_factory=enrollment_factory,
            )
            return 0
        if args.command == "enroll":
            contact = contacts.contact(args.chat_jid)
            if contact is None:
                raise ContactError("contact does not exist")
            check_services()
            _stage_enrollment(
                contact,
                create_contact=False,
                enrollment_factory=enrollment_factory,
            )
            return 0
    except ContactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
