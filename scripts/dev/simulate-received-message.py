#!/usr/bin/env python3
"""Receive one authorized wacli item into scratch, then run the selected driver.

Validation is the default.  Live receive and execute modes are owner-only and
require private authorization files.  Production queue/history/outbox content
is never used as the test namespace.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from types import SimpleNamespace


def _load_selected():
    path = Path(__file__).with_name("simulate-selected-message.py")
    spec = importlib.util.spec_from_file_location("messagebox_selected_receiver_driver", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("selected-message driver is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selected = _load_selected()
shared = selected.shared
SimulationError = selected.SimulationError
HEX = re.compile(r"[0-9a-f]{64}\Z")
JID = re.compile(r"[^\s/]{1,200}\Z")
SYNC_MUTABLE = {"wacli.db", "wacli.db-wal", "wacli.db-shm"}
AUTH_KEYS = {
    "version", "transport", "recipient", "sender_jid", "not_before",
    "max_age_seconds", "contacts_sha256", "settings_sha256", "account_sha256",
    "sync_mutable_paths",
}
MAX_TREE_ENTRIES = 20_000
STATE_FIFOS = {".lgd-nfy0"}
RECEIPT_NAME = "receiver-receipt.json"
RECEIPT_KEYS = {
    "version", "status", "authorization_sha256", "manifest_sha256", "target_msgid",
    "storage_sha256", "sync_mutable_paths", "authorization", "source_timestamp",
    "source_timestamp_unix", "target_observation_sha256",
}


def _digest(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SimulationError("protected files must be regular")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def validate_receive_authorization(value):
    if not isinstance(value, dict) or set(value) != AUTH_KEYS:
        raise SimulationError("receive authorization fields are invalid")
    if value["version"] != 1 or value["transport"] != "wacli":
        raise SimulationError("receive authorization transport is invalid")
    if (not isinstance(value["recipient"], str) or not JID.fullmatch(value["recipient"])
            or not isinstance(value["sender_jid"], str) or not JID.fullmatch(value["sender_jid"])):
        raise SimulationError("receive authorization counterpart is invalid")
    for key in ("contacts_sha256", "settings_sha256", "account_sha256"):
        if not isinstance(value[key], str) or not HEX.fullmatch(value[key]):
            raise SimulationError("receive authorization fingerprints are invalid")
    before, age = value["not_before"], value["max_age_seconds"]
    if (type(before) not in (int, float) or not math.isfinite(before)
            or type(age) not in (int, float) or not math.isfinite(age)
            or not 1 <= age <= 600):
        raise SimulationError("receive authorization freshness is invalid")
    mutable = value["sync_mutable_paths"]
    if (not isinstance(mutable, list) or mutable != sorted(set(mutable))
            or not set(mutable).issubset(SYNC_MUTABLE)):
        raise SimulationError("sync mutable paths are invalid")
    return value


def _tree(root, mutable=frozenset(), allowed_fifos=frozenset()):
    root = Path(root)
    try:
        top = root.lstat()
    except FileNotFoundError:
        return {"exists": False, "entries": []}
    if not stat.S_ISDIR(top.st_mode) or stat.S_ISLNK(top.st_mode):
        raise SimulationError("protected root must be a real directory")
    entries, pending = [], [root]
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise SimulationError("protected tree is unavailable") from exc
        for child in children:
            if len(entries) >= MAX_TREE_ENTRIES:
                raise SimulationError("protected tree exceeds the test bound")
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            metadata = child.stat(follow_symlinks=False)
            if (stat.S_ISFIFO(metadata.st_mode) and relative in allowed_fifos):
                entries.append({
                    "name": relative, "kind": "fifo", "mode": stat.S_IMODE(metadata.st_mode),
                    "uid": metadata.st_uid, "gid": metadata.st_gid,
                })
                continue
            if stat.S_ISLNK(metadata.st_mode) or not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise SimulationError("protected tree contains a link or special entry")
            entry = {
                "name": relative,
                "kind": "directory" if stat.S_ISDIR(metadata.st_mode) else "file",
                "mode": stat.S_IMODE(metadata.st_mode),
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
            }
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(entry)
                pending.append(path)
            else:
                if relative not in mutable:
                    entry["sha256"] = _digest(path)
                entries.append(entry)
    return {
        "exists": True,
        "root": {"mode": stat.S_IMODE(top.st_mode), "uid": top.st_uid, "gid": top.st_gid},
        "entries": sorted(entries, key=lambda item: item["name"]),
    }


def _entry_map(snapshot):
    return {item["name"]: item for item in snapshot["entries"]}


def _read_seen(path):
    try:
        document = json.loads(_regular_bytes(path).decode("utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, UnicodeError) as exc:
        raise SimulationError("production seen ledger is invalid") from exc
    if not isinstance(document, list) or any(not isinstance(item, str) for item in document):
        raise SimulationError("production seen ledger is invalid")
    return set(document)


def _add_seen(path, expected, message_id):
    path = Path(path)
    if message_id in expected:
        raise SimulationError("target was already present in the production seen ledger")
    if _read_seen(path) != expected:
        raise SimulationError("production seen ledger changed before target commit")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
        raise SimulationError("production state root is unsafe")
    mode, uid, gid = 0o600, parent.st_uid, parent.st_gid
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode):
            raise SimulationError("production seen ledger is unsafe")
        mode = stat.S_IMODE(existing.st_mode)
        uid, gid = existing.st_uid, existing.st_gid
    temporary = path.with_name(path.name + ".selected-receiver.tmp")
    if temporary.exists():
        raise SimulationError("production seen staging already exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(sorted(expected | {message_id}), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        committed = path.lstat()
        if (stat.S_IMODE(committed.st_mode), committed.st_uid, committed.st_gid) != (mode, uid, gid):
            raise SimulationError("production seen ledger ownership or mode changed")
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_state_change(before, after, state_root, prior_seen, target_id, seen_name="seen.json"):
    if before["exists"] != after["exists"] or not after["exists"]:
        raise SimulationError("production state root changed unexpectedly")
    if before["root"] != after["root"]:
        raise SimulationError("production state root ownership or mode changed")
    old, new = _entry_map(before), _entry_map(after)
    for staging in (seen_name + ".tmp", seen_name + ".selected-receiver.tmp"):
        if staging in new:
            raise SimulationError("production seen staging was not cleaned")
    old.pop(seen_name, None)
    new.pop(seen_name, None)
    if old != new:
        raise SimulationError("production state outside the seen ledger changed")
    if _read_seen(Path(state_root) / seen_name) != prior_seen | {target_id}:
        raise SimulationError("production seen ledger has unexpected changes")


def _verify_sync_store(before, after, mutable):
    if before["exists"] is not True or after["exists"] is not True:
        raise SimulationError("wacli store is unavailable")
    if before["root"] != after["root"]:
        raise SimulationError("wacli store ownership or mode changed")
    old, new = _entry_map(before), _entry_map(after)
    if (old.get("wacli.db", {}).get("kind") != "file"
            or new.get("wacli.db", {}).get("kind") != "file"):
        raise SimulationError("wacli account database is unavailable")
    for name in set(old) | set(new):
        if name in mutable:
            old_item, new_item = old.get(name), new.get(name)
            if new_item is not None and new_item.get("kind") != "file":
                raise SimulationError("wacli mutable state is not a regular file")
            if old_item is not None and new_item is not None and old_item != new_item:
                raise SimulationError("wacli mutable state ownership or mode changed")
            continue
        if old.get(name) != new.get(name):
            raise SimulationError("wacli state changed outside the authorized database files")


def _production_trees(production):
    return {
        "queue": _tree(production["queue"]),
        "outbox": _tree(production["outbox"]),
        "state": _tree(production["state"], allowed_fifos=STATE_FIFOS),
    }


def _verify_production_nfc_clear(production):
    for key in ("nfc_enrollment", "nfc_selection", "nfc_selection_claimed", "nfc_unknown",
                "nfc_announcement", "nfc_announcement_claimed",
                "nfc_announcement_acknowledged", "nfc_health"):
        if shared.trace_present(production[key]) is not False:
            raise SimulationError("existing or unknown production NFC state must remain untouched")


def _actual_transport_mode():
    mode = os.environ.get("MSGBOX_TRANSPORT")
    if mode not in {"wacli", "business", "cloud"}:
        raise SimulationError("runtime transport is invalid")
    if mode != "wacli":
        raise SimulationError("scratch receiver requires the actual wacli mode")
    return mode


def _verify_service_state():
    selected.verify_services()
    result = subprocess.run(
        ["systemctl", "is-active", "messagebox-sync.service"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or result.stdout.strip() != "active":
        raise SimulationError("normal wacli sync service must be verified active")


def _verify_live_binding(production, manifest, runtime):
    _actual_transport_mode()
    if (_digest(production["contacts"]) != manifest["contacts_sha256"]
            or _digest(production["settings"]) != manifest["settings_sha256"]
            or _account_hash(runtime) != manifest["account_sha256"]):
        raise SimulationError("production route, settings, or account changed")


def _regular_bytes(source, maximum=4 * 1024 * 1024):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > maximum:
            raise SimulationError("protected metadata file is invalid")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > maximum:
                raise SimulationError("protected metadata file is too large")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise SimulationError("protected metadata changed while it was read")
        return bytes(data)
    finally:
        os.close(descriptor)


def _copy_exact(source, destination, expected):
    data = _regular_bytes(source)
    if hashlib.sha256(data).hexdigest() != expected:
        raise SimulationError("contacts or settings changed from receive authorization")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if _digest(destination) != expected:
        raise SimulationError("scratch metadata copy changed")
    _fsync_directory(destination.parent)


def bind_scratch_paths(root, *, loaded=None):
    loaded = sys.modules if loaded is None else loaded
    if any(name.startswith("messagebox.") and name != "messagebox.runtime_paths" for name in loaded):
        raise SimulationError("application modules were imported before scratch path binding")
    from messagebox import runtime_paths
    root = Path(root).resolve()
    runtime_paths.QUEUE_DIR = root / "queue"
    runtime_paths.OUTBOX_DIR = root / "unused-outbox"
    runtime_paths.STATE_DIR = root / "state"
    runtime_paths.CONTACTS_FILE = runtime_paths.STATE_DIR / "contacts.json"
    runtime_paths.SETTINGS_DIR = root / "settings"
    runtime_paths.SETTINGS_FILE = runtime_paths.SETTINGS_DIR / "settings.json"
    runtime_paths.RUNTIME_DIR = root / "runtime"
    runtime_paths.NFC_SELECTION_FILE = runtime_paths.RUNTIME_DIR / "nfc-selection.json"
    runtime_paths.NFC_ENROLLMENT_FILE = runtime_paths.RUNTIME_DIR / "nfc-enrollment.json"
    runtime_paths.NFC_ANNOUNCEMENT_FILE = runtime_paths.RUNTIME_DIR / "nfc-announcement.json"
    runtime_paths.NFC_HEALTH_FILE = runtime_paths.RUNTIME_DIR / "nfc-health"
    return runtime_paths


def _account_hash(runtime):
    if runtime.transport_mode() != "wacli":
        raise SimulationError("scratch receiver requires wacli mode")
    return shared.account_hash(runtime)


def _messages(result):
    try:
        document = json.loads(result.stdout or "{}")
        messages = (document.get("data") or {}).get("messages") or []
    except (AttributeError, TypeError, ValueError) as exc:
        raise SimulationError("wacli message listing is invalid") from exc
    if result.returncode != 0 or not isinstance(messages, list):
        raise SimulationError("wacli message listing is unavailable")
    return messages


def _eligible(voicepoll, messages, seen, authorizations):
    output = []
    for message in reversed(messages):
        if not isinstance(message, dict) or not isinstance(message.get("MsgID"), str):
            raise SimulationError("wacli message listing is invalid")
        if message["MsgID"] in seen or message.get("FromMe"):
            continue
        if not voicepoll.message_is_authorized(message, authorizations):
            continue
        if str(message.get("MediaType") or "").strip().lower() not in voicepoll.PLAYABLE_MEDIA_TYPES:
            continue
        output.append(message)
    return output


def _target(voicepoll, messages, seen, authorizations, authorization, now):
    eligible = _eligible(voicepoll, messages, seen, authorizations)
    if len(eligible) != 1:
        raise SimulationError("normal receiver has work outside the single authorized target")
    message = eligible[0]
    timestamp = voicepoll.parse_wacli_timestamp(message.get("Timestamp"))
    if (str(message.get("MediaType") or "").strip().lower() != "audio"
            or message.get("ChatJID") != authorization["recipient"]
            or message.get("SenderJID") != authorization["sender_jid"]
            or timestamp is None or timestamp < authorization["not_before"]
            or timestamp > now + 5 or now - timestamp > authorization["max_age_seconds"]):
        raise SimulationError("normal receiver target does not match authorization")
    return message


class GuardedWacli:
    def __init__(self, voicepoll, original, seen, authorizations, authorization, target_id, clock=time.time):
        self.voicepoll = voicepoll
        self.original = original
        self.seen = seen
        self.authorizations = authorizations
        self.authorization = authorization
        self.target_id = target_id
        self.clock = clock
        self.list_calls = 0
        self.download_calls = 0

    def __call__(self, *args):
        if args == ("--read-only", "messages", "list", "--limit", "10", "--json", "--full"):
            self.list_calls += 1
            result = self.original(*args)
            message = _target(self.voicepoll, _messages(result), self.seen,
                              self.authorizations, self.authorization, self.clock())
            if message["MsgID"] != self.target_id:
                raise SimulationError("normal receiver target changed during poll")
            return result
        expected = ("--read-only", "media", "download", "--chat", self.authorization["recipient"],
                    "--id", self.target_id, "--output")
        if len(args) == len(expected) + 2 and args[:len(expected)] == expected and args[-1] == "--json":
            output = Path(args[-2]).resolve()
            if output.parent != Path(self.voicepoll.QUEUE_DIR).resolve() or not output.name.startswith(".media-"):
                raise SimulationError("normal receiver media path escaped scratch")
            self.download_calls += 1
            return self.original(*args)
        raise SimulationError("unexpected wacli command during isolated receive")


def _new_private_json(path, value):
    path = Path(path)
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_mode & 0o077 or parent.st_uid != os.getuid()):
        raise SimulationError("private output directory is unsafe")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path):
    directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _replace_private_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise SimulationError("private receipt staging already exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _json_sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _storage_sha(production):
    return _json_sha({key: str(Path(value).resolve()) for key, value in sorted(production.items())})


def _observation_sha(
    recipient, sender, message_id, media_type, source_timestamp, source_timestamp_unix,
):
    return _json_sha({
        "recipient": recipient, "sender_jid": sender, "msgid": message_id,
        "media_type": media_type, "source_timestamp": source_timestamp,
        "source_timestamp_unix": source_timestamp_unix,
    })


def _verify_source_fresh(receipt, clock=time.time):
    observed = clock()
    timestamp = receipt["source_timestamp_unix"]
    authorization = receipt["authorization"]
    if (type(observed) not in (int, float) or not math.isfinite(observed)
            or timestamp < authorization["not_before"] or timestamp > observed + 5
            or observed - timestamp > authorization["max_age_seconds"]):
        raise SimulationError("original receiver timestamp is no longer fresh")


def _separate(path, protected, label):
    path = Path(path).resolve()
    for other in protected:
        other = Path(other).resolve()
        if path == other or path in other.parents or other in path.parents:
            raise SimulationError(f"{label} must be separate from protected storage")
    return path


def _verify_storage_layout(receiver_root, production, *, candidate=None, outbound=None):
    requested = Path(receiver_root)
    metadata = requested.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SimulationError("receiver scratch must be a real directory")
    protected = list(production.values()) + [
        "/var/lib/messagebox", "/var/lib/messagebox-settings",
        "/etc/messagebox", "/run/messagebox", "/opt/messagebox",
    ]
    receiver = _separate(receiver_root, protected, "receiver scratch")
    if candidate is not None:
        candidate = _separate(candidate, protected + [receiver], "candidate manifest")
    if outbound is not None:
        _separate(outbound, protected + [receiver], "outbound scratch")
    return receiver


def _receipt(root):
    try:
        value = json.loads(_regular_bytes(Path(root) / RECEIPT_NAME).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SimulationError("scratch receiver receipt is unavailable") from exc
    if (not isinstance(value, dict) or set(value) != RECEIPT_KEYS
            or value.get("version") != 1 or value.get("status") != "received"
            or any(not isinstance(value.get(key), str) or not HEX.fullmatch(value[key])
                   for key in ("authorization_sha256", "manifest_sha256", "storage_sha256",
                               "target_observation_sha256"))
            or not isinstance(value.get("target_msgid"), str) or not value["target_msgid"]
            or isinstance(value.get("source_timestamp"), bool)
            or not isinstance(value.get("source_timestamp"), (str, int, float))
            or (isinstance(value.get("source_timestamp"), (int, float))
                and not math.isfinite(value["source_timestamp"]))
            or type(value.get("source_timestamp_unix")) not in (int, float)
            or not math.isfinite(value["source_timestamp_unix"])
            or not isinstance(value.get("sync_mutable_paths"), list)
            or value["sync_mutable_paths"] != sorted(set(value["sync_mutable_paths"]))
            or not set(value["sync_mutable_paths"]).issubset(SYNC_MUTABLE)):
        raise SimulationError("scratch receiver receipt is invalid")
    authorization = validate_receive_authorization(value["authorization"])
    if (value["authorization_sha256"] != _json_sha(authorization)
            or value["sync_mutable_paths"] != authorization["sync_mutable_paths"]):
        raise SimulationError("scratch receiver authorization binding changed")
    return value


def receive_one(authorization, receiver_root, candidate_path, production, clock=time.time):
    _actual_transport_mode()
    root = _verify_storage_layout(receiver_root, production, candidate=candidate_path)
    metadata = root.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid() or any(root.iterdir())):
        raise SimulationError("receiver scratch root must be empty, private, and owned by the runtime user")
    baseline = _production_trees(production)
    mutable = set(authorization["sync_mutable_paths"])
    sync_before = _tree(production["sync_store"], mutable)
    prior_seen = _read_seen(production["seen"])
    _verify_service_state()
    _verify_production_nfc_clear(production)
    paths = bind_scratch_paths(root)
    _copy_exact(production["contacts"], paths.CONTACTS_FILE, authorization["contacts_sha256"])
    _copy_exact(production["settings"], paths.SETTINGS_FILE, authorization["settings_sha256"])
    paths.QUEUE_DIR.mkdir(mode=0o700)
    queue_baseline = selected.snapshot_queue_root(paths.QUEUE_DIR)

    from messagebox import voicepoll
    from messagebox.contacts import ContactStore
    runtime = SimpleNamespace(transport_mode=_actual_transport_mode, WACLI_BIN=voicepoll.WACLI_BIN)
    if _account_hash(runtime) != authorization["account_sha256"]:
        raise SimulationError("wacli account does not match receive authorization")
    contacts = ContactStore(paths.CONTACTS_FILE).load()["contacts"]
    if authorization["recipient"] not in contacts:
        raise SimulationError("authorized recipient is no longer allowed")
    authorizations = voicepoll.load_contact_authorizations(paths.CONTACTS_FILE)
    seen = set(prior_seen)
    voicepoll.save_seen(seen)
    original = voicepoll.wacli
    initial = original("--read-only", "messages", "list", "--limit", "10", "--json", "--full")
    target = _target(voicepoll, _messages(initial), seen, authorizations, authorization, clock())
    target_id = target["MsgID"]
    source_timestamp = target.get("Timestamp")
    source_timestamp_unix = voicepoll.parse_wacli_timestamp(source_timestamp)
    guarded = GuardedWacli(voicepoll, original, seen, authorizations, authorization, target_id, clock)
    voicepoll.wacli = guarded
    try:
        if voicepoll.poll_once(seen, authorizations) != 1:
            raise SimulationError("normal receiver did not queue exactly one target")
    finally:
        voicepoll.wacli = original
    if guarded.list_calls != 1 or guarded.download_calls != 1:
        raise SimulationError("normal receiver call graph was incomplete")
    if seen != prior_seen | {target_id} or voicepoll.load_seen() != seen:
        raise SimulationError("scratch seen ledger does not contain exactly the target addition")
    wavs = list(paths.QUEUE_DIR.glob("*.wav"))
    if len(wavs) != 1 or list(paths.QUEUE_DIR.glob("*.part")) or list(paths.QUEUE_DIR.glob("*.json.part")):
        raise SimulationError("normal receiver scratch output is incomplete")
    wav = wavs[0]
    sidecar = Path(str(wav) + ".json")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    if (document.get("chat") != authorization["recipient"]
            or document.get("sender_jid") != authorization["sender_jid"]
            or document.get("msgid") != target_id or document.get("media_type") != "audio"
            or document.get("cloud") is True):
        raise SimulationError("normal receiver sidecar does not match the target")
    candidate = selected.validate_manifest({
        "version": 1, "transport": "wacli",
        "contacts_sha256": authorization["contacts_sha256"],
        "settings_sha256": authorization["settings_sha256"],
        "account_sha256": authorization["account_sha256"],
        "recipient": authorization["recipient"],
        "baseline_entries": queue_baseline,
        "target": {
            "name": wav.name, "wav_sha256": _digest(wav),
            "sidecar_sha256": _digest(sidecar), "sidecar_document": document,
            "not_before": authorization["not_before"],
            "max_age_seconds": authorization["max_age_seconds"],
        },
    })
    _new_private_json(candidate_path, candidate)
    receipt = {
        "version": 1, "status": "seen_commit_pending",
        "authorization": authorization,
        "authorization_sha256": _json_sha(authorization),
        "manifest_sha256": _json_sha(candidate), "target_msgid": target_id,
        "storage_sha256": _storage_sha(production),
        "sync_mutable_paths": authorization["sync_mutable_paths"],
        "source_timestamp": source_timestamp,
        "source_timestamp_unix": source_timestamp_unix,
        "target_observation_sha256": _observation_sha(
            authorization["recipient"], authorization["sender_jid"], target_id,
            "audio", source_timestamp, source_timestamp_unix,
        ),
    }
    receipt_path = root / RECEIPT_NAME
    _new_private_json(receipt_path, receipt)
    # Recheck the real producer boundary immediately before the sole permitted
    # production-state effect. A failure from here leaves both scratch media
    # and a pending receipt for explicit reconciliation.
    _verify_service_state()
    _verify_production_nfc_clear(production)
    current = _production_trees(production)
    if baseline != current:
        raise SimulationError("production queue, outbox, or state changed before target commit")
    _verify_live_binding(production, authorization, runtime)
    _add_seen(production["seen"], prior_seen, target_id)
    after = _production_trees(production)
    if baseline["queue"] != after["queue"] or baseline["outbox"] != after["outbox"]:
        raise SimulationError("production queue or outbox changed during scratch receive")
    _verify_state_change(baseline["state"], after["state"], production["state"], prior_seen, target_id)
    _verify_sync_store(sync_before, _tree(production["sync_store"], mutable), mutable)
    _verify_live_binding(production, authorization, runtime)
    receipt["status"] = "received"
    _replace_private_json(receipt_path, receipt)
    print(json.dumps({"type": "scratch_receiver", "status": "candidate_ready",
                      "target_sha256": candidate["target"]["wav_sha256"]}, sort_keys=True), flush=True)


def execute_selected(
    plan, manifest, receiver_root, outbound_root, production, timeout, clock=time.time,
):
    _actual_transport_mode()
    root = _verify_storage_layout(receiver_root, production, outbound=outbound_root)
    receipt = _receipt(root)
    authorization = receipt["authorization"]
    observation_sha = _observation_sha(
        manifest["recipient"], manifest["target"]["sidecar_document"].get("sender_jid"),
        manifest["target"]["sidecar_document"].get("msgid"),
        manifest["target"]["sidecar_document"].get("media_type"),
        receipt["source_timestamp"], receipt["source_timestamp_unix"],
    )
    if (receipt["manifest_sha256"] != _json_sha(manifest)
            or receipt["storage_sha256"] != _storage_sha(production)
            or receipt["target_msgid"] != manifest["target"]["sidecar_document"].get("msgid")
            or receipt["target_msgid"] not in _read_seen(production["seen"])
            or manifest["recipient"] != authorization["recipient"]
            or manifest["contacts_sha256"] != authorization["contacts_sha256"]
            or manifest["settings_sha256"] != authorization["settings_sha256"]
            or manifest["account_sha256"] != authorization["account_sha256"]
            or manifest["target"]["sidecar_document"].get("sender_jid")
               != authorization["sender_jid"]
            or receipt["target_observation_sha256"] != observation_sha):
        raise SimulationError("scratch receiver receipt does not bind this selected run")
    _verify_source_fresh(receipt, clock)
    baseline = _production_trees(production)
    mutable = set(receipt["sync_mutable_paths"])
    sync_before = _tree(production["sync_store"], mutable)
    _verify_service_state()
    _verify_production_nfc_clear(production)
    paths = bind_scratch_paths(root)
    if (_digest(paths.CONTACTS_FILE) != manifest["contacts_sha256"]
            or _digest(paths.SETTINGS_FILE) != manifest["settings_sha256"]):
        raise SimulationError("scratch route or settings changed before selected execution")
    runtime = SimpleNamespace(transport_mode=_actual_transport_mode, WACLI_BIN="/usr/local/bin/wacli")
    _verify_live_binding(production, manifest, runtime)
    original_verify = selected.verify_environment

    def verify_with_production_nfc(*args, **kwargs):
        _verify_service_state()
        _verify_production_nfc_clear(production)
        _verify_live_binding(production, manifest, runtime)
        _verify_source_fresh(receipt, clock)
        return original_verify(*args, **kwargs)

    selected.verify_environment = verify_with_production_nfc
    try:
        selected.execute(plan, manifest, outbound_root, timeout)
    finally:
        selected.verify_environment = original_verify
    after = _production_trees(production)
    if baseline != after:
        raise SimulationError("production queue, outbox, or state changed during selected execution")
    _verify_sync_store(sync_before, _tree(production["sync_store"], mutable), mutable)
    _verify_live_binding(production, manifest, runtime)


def _production_paths():
    from messagebox import runtime_paths
    selection = Path(runtime_paths.NFC_SELECTION_FILE)
    announcement = Path(runtime_paths.NFC_ANNOUNCEMENT_FILE)
    return {
        "queue": Path(runtime_paths.QUEUE_DIR), "outbox": Path(runtime_paths.OUTBOX_DIR),
        "state": Path(runtime_paths.STATE_DIR), "seen": Path(runtime_paths.STATE_DIR) / "seen.json",
        "contacts": Path(runtime_paths.CONTACTS_FILE), "settings": Path(runtime_paths.SETTINGS_FILE),
        "sync_store": Path(os.environ.get("WACLI_STORE_DIR", "/var/lib/messagebox/wacli")),
        "nfc_enrollment": Path(runtime_paths.NFC_ENROLLMENT_FILE),
        "nfc_selection": selection,
        "nfc_selection_claimed": selection.with_name(f".{selection.name}.claimed"),
        "nfc_unknown": selection.with_name(f".{selection.name}.unknown"),
        "nfc_announcement": announcement,
        "nfc_announcement_claimed": announcement.with_name(f".{announcement.name}.claimed"),
        "nfc_announcement_acknowledged": announcement.with_name(
            f".{announcement.name}.acknowledged"
        ),
        "nfc_health": Path(runtime_paths.NFC_HEALTH_FILE),
    }


def _child(mode, plan, authorization, receiver_root, outbound_root, candidate, production, timeout):
    os.setsid()
    os.umask(0o077)
    try:
        if mode == "receive":
            receive_one(authorization, receiver_root, candidate, production)
        else:
            execute_selected(plan, authorization, receiver_root, outbound_root, production, timeout)
    except Exception:
        print("Scratch receiver run failed; preserve artifacts and reconcile before retry.", file=sys.stderr)
        raise SystemExit(1) from None


def _supervise(*arguments, timeout):
    process = multiprocessing.get_context("spawn").Process(target=_child, args=arguments)
    process.start()
    try:
        process.join(timeout)
        timed_out = process.is_alive()
    finally:
        cleaned = shared.stop_group(process.pid)
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    if timed_out or not cleaned:
        print("Scratch receiver run did not stop cleanly; reconcile before retry.", file=sys.stderr)
        return 1
    return 0 if process.exitcode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--receive", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--receiver-root", type=Path)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--scratch-state-root", type=Path)
    parser.add_argument("--send-generated", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    try:
        plan = selected.validate_selected_plan(shared.private_json(args.plan))
        if not args.receive and not args.execute:
            if any((args.authorization, args.receiver_root, args.candidate_manifest,
                    args.scratch_state_root, args.send_generated)):
                raise SimulationError("execution options require receive or execute mode")
            print("Scratch receiver plan is valid; no application or account state was accessed.")
            return 0
        if (not math.isfinite(args.timeout) or not 1 <= args.timeout <= selected.MAX_RUNTIME_SECONDS
                or not args.authorization or not args.receiver_root):
            raise SimulationError("bounded execution inputs are required")
        production = _production_paths()
        authorization = shared.private_json(args.authorization)
        if args.receive:
            if args.send_generated or args.scratch_state_root or not args.candidate_manifest:
                raise SimulationError("receive mode requires only receiver and candidate paths")
            authorization = validate_receive_authorization(authorization)
            return _supervise("receive", plan, authorization, args.receiver_root, None,
                              args.candidate_manifest, production, args.timeout, timeout=args.timeout)
        if not args.send_generated or not args.scratch_state_root or args.candidate_manifest:
            raise SimulationError("execute mode requires scratch state and explicit send")
        if args.timeout < plan["duration"] + 1:
            raise SimulationError("execute timeout must cover the normal input plan")
        authorization = selected.validate_manifest(authorization)
        if authorization["transport"] != "wacli":
            raise SimulationError("scratch receiver execute supports wacli only")
        return _supervise("execute", plan, authorization, args.receiver_root,
                          args.scratch_state_root, None, production, args.timeout,
                          timeout=args.timeout)
    except (OSError, ValueError, json.JSONDecodeError, SimulationError):
        print("Scratch receiver plan or authorization is invalid.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
