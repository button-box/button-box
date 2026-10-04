"""Cloud API v1 polling and local command execution for one claimed Pi."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import time
import uuid
import wave
from pathlib import Path

from messagebox.cloud_device import CLOUD_DIR, CloudAckGone, CloudDeviceClient, CloudDeviceError, CloudVoiceNotFound, atomic_json, capabilities
from messagebox.contacts import ContactError, ContactStore
from messagebox.guided_reply import cloud_outbox_lock, cloud_upload_payload
from messagebox.nfc_state import EnrollmentStore, NfcError, NfcRouter, SelectionStore
from messagebox.played_history import played_history_lock
from messagebox.runtime_paths import (NFC_ENROLLMENT_FILE,
    NFC_HEALTH_FILE, NFC_SELECTION_FILE, OUTBOX_DIR, QUEUE_DIR, SETTINGS_FILE)
from messagebox.settings import SettingsError, SettingsStore, RINGTONES
from messagebox.voicepoll import queue_message

STATE_FILE = CLOUD_DIR / "runtime.json"
ACK_DIR = CLOUD_DIR / "acks"
COMPLETED_DIR = CLOUD_DIR / "completed"
INTENT_DIR = CLOUD_DIR / "intents"
APPLIED_FILE = CLOUD_DIR / "applied-settings.json"
CONTACTS_FILE = CLOUD_DIR / "contacts.json"
_ID = re.compile(r"^[A-Za-z0-9:._-]{1,240}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_PHONE = re.compile(r"^[1-9][0-9]{6,14}$")
MAX_MEDIA_BYTES = 10 * 1024 * 1024
LEDGER_SECONDS = 90 * 86400
RINGTONE_DIR = Path("/opt/messagebox/ringtones")
RINGTONE_PREVIEW_MAX_SECONDS = 30
RINGTONE_PREVIEW_GRACE_SECONDS = 5
RINGTONE_PREVIEW_MAX_PCM_BYTES = 32 * 1024 * 1024


def _current_boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f-]{36}", value) else None


class CloudRuntimeError(Exception):
    """A safe error without response bodies or family identifiers."""


class CloudCommandDeferred(CloudRuntimeError):
    """Temporary local gate; the server must keep the operation pending."""


def _valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _valid_time(value):
    return type(value) in (int, float) and 1_700_000_000 <= value < 4_102_444_800


def _effective_expiry(metadata, snapshot):
    expiry = metadata.get("expires_at")
    created = metadata.get("cloud_created_at")
    days = snapshot.get("retention_days")
    if not _valid_time(expiry) or days not in {7, 30, 90}:
        raise CloudRuntimeError("cloud retention metadata is invalid")
    if created is None:
        return expiry
    if not _valid_time(created):
        raise CloudRuntimeError("cloud retention metadata is invalid")
    return min(expiry, created + days * 86400)


def _ringtone_preview_timeout(path):
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            if (source.getcomptype() != "NONE" or channels <= 0 or width not in {1, 2, 3, 4}
                    or rate <= 0 or frames <= 0):
                raise CloudRuntimeError("ringtone preview failed")
            duration = frames / rate
            expected_bytes = frames * channels * width
            if (duration > RINGTONE_PREVIEW_MAX_SECONDS
                    or expected_bytes > RINGTONE_PREVIEW_MAX_PCM_BYTES
                    or len(source.readframes(frames)) != expected_bytes):
                raise CloudRuntimeError("ringtone preview failed")
    except (EOFError, OSError, OverflowError, wave.Error) as exc:
        raise CloudRuntimeError("ringtone preview failed") from exc
    return duration + RINGTONE_PREVIEW_GRACE_SECONDS


class CloudRuntime:
    def __init__(self, client=None, *, state_path=STATE_FILE, contacts_path=CONTACTS_FILE,
                 queue_dir=QUEUE_DIR, outbox_dir=OUTBOX_DIR, settings_path=SETTINGS_FILE, clock=time.time,
                 converter=queue_message, monotonic=time.monotonic, boot_id=None):
        self.client = client or CloudDeviceClient.from_environment()
        self.state_path = Path(state_path)
        self.contacts = ContactStore(contacts_path, clock=clock)
        self.queue_dir = Path(queue_dir)
        self.outbox_dir = Path(outbox_dir)
        self.settings = SettingsStore(settings_path)
        self.clock = clock
        self.converter = converter
        self.monotonic = monotonic
        self.boot_id = _current_boot_id() if boot_id is None else boot_id
        self.state = self._load()

    def _load(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "cursor": 0, "acks": {}, "seen": {}, "deleted": [], "pending_nfc": {}, "pending_settings": {}, "outbox_status_cursor": 0}
        except (OSError, ValueError) as exc:
            raise CloudRuntimeError("cloud state is unavailable") from exc
        if (not isinstance(state, dict) or state.get("version") != 1
                or type(state.get("cursor")) is not int or state["cursor"] < 0
                or not isinstance(state.get("acks"), dict)
                or not isinstance(state.get("seen"), dict)
                or not isinstance(state.get("deleted"), list)
                or not isinstance(state.get("pending_nfc"), dict)
                or not isinstance(state.get("pending_settings"), dict)
                or type(state.get("outbox_status_cursor", 0)) is not int
                or state.get("outbox_status_cursor", 0) < 0
                or type(state.get("queue_hold_sequence", -1)) is not int
                or state.get("queue_hold_sequence", -1) < -1):
            raise CloudRuntimeError("cloud state is invalid")
        return state

    def _save(self):
        atomic_json(self.state_path, self.state)

    def trusted_now(self):
        now = self.clock()
        snapshot = self.state.get("snapshot")
        if (not _valid_time(now) or not isinstance(snapshot, dict)
                or not _valid_time(snapshot.get("server_time"))
                or abs(now - snapshot["server_time"]) > 90):
            raise CloudRuntimeError("device time is unverified")
        return now

    def _snapshot(self):
        snapshot = self.state.get("snapshot")
        if not isinstance(snapshot, dict) or not _valid_id(snapshot.get("box_id")):
            raise CloudRuntimeError("cloud authorization is unavailable")
        self.trusted_now()
        verified_at = snapshot.get("verified_at")
        verified_mono = snapshot.get("verified_mono")
        elapsed = self.monotonic() - verified_mono if type(verified_mono) in (int, float) else -1
        if (not self.boot_id or snapshot.get("boot_id") != self.boot_id
                or not 0 <= elapsed <= 90
                or not _valid_time(verified_at) or not 0 <= self.clock() - verified_at <= 90):
            raise CloudRuntimeError("cloud authorization is stale")
        return snapshot

    def _people(self):
        snapshot = self._snapshot()
        return {person["id"]: person for person in snapshot["people"]}

    def recipient_id(self, jid):
        snapshot = self._snapshot()
        if (not snapshot["entitlement"]["send"] or
                (snapshot["entitlement"].get("until") is not None and self.trusted_now() >= snapshot["entitlement"]["until"])):
            raise CloudRuntimeError("cloud sending is paused")
        for person in snapshot["people"]:
            if person["wa_id"] + "@s.whatsapp.net" == jid:
                return person["id"]
        raise CloudRuntimeError("recipient is no longer authorized")

    def playable(self, metadata):
        try:
            snapshot = self._snapshot()
            return bool(
                isinstance(metadata, dict) and metadata.get("cloud") is True
                and snapshot["entitlement"]["deliver"]
                and not snapshot["queue_hold"]
                # A fresh heartbeat may authorize previously accepted inbound audio
                # during payment suspension, up to the message expiry.
                and metadata.get("sender_id") in self._people()
                and self.trusted_now() < _effective_expiry(metadata, snapshot)
                and metadata.get("cloud_message_id") not in self.state["deleted"]
            )
        except CloudRuntimeError:
            return False

    def _sync_contacts(self, snapshot):
        people = snapshot["people"]
        wanted = {p["wa_id"] + "@s.whatsapp.net": p for p in people}
        default = next((p["wa_id"] + "@s.whatsapp.net" for p in people
                        if p["id"] == snapshot["default_recipient_id"]), None)
        now = snapshot["server_time"]
        def sync(document):
            old = document["contacts"]
            contacts = {}
            for jid, person in wanted.items():
                prior = old.get(jid, {})
                contacts[jid] = {
                    "label": (person["display_name"][:80] or jid.split("@", 1)[0]),
                    "kind": "person", "receive_after": prior.get("receive_after", now),
                    "card_uids": prior.get("card_uids", []), "card_clip": prior.get("card_clip", ""),
                }
            changed = contacts != old or default != document["default_recipient"]
            if changed:
                document["contacts"] = contacts
                document["default_recipient"] = default
            return changed, None
        self.contacts._mutate(sync)

    def heartbeat(self):
        current, warning = self.settings.load()
        if warning:
            raise CloudRuntimeError("local settings are unavailable")
        try:
            marker = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CloudRuntimeError("physical settings state is unavailable") from exc
        settings = marker.get("settings") if isinstance(marker, dict) else None
        if (not isinstance(settings, dict) or settings.get("version") != 1
                or type(settings.get("revision")) is not int
                or settings["revision"] > current["revision"]):
            raise CloudRuntimeError("physical settings state is invalid")
        response = self.client.heartbeat({
            "version": "cloud-mvp-1", "capabilities": capabilities(nfc=NFC_HEALTH_FILE.exists()),
            "settings": settings, "applied_revision": settings["revision"],
            "queue": {"held": bool((self.state.get("snapshot") or {}).get("queue_hold", False)),
                      "count": len(list(self.queue_dir.glob("*.wav"))), "playing_message_id": None},
            "last_error": None,
        })
        people = response.get("people")
        entitlement = response.get("entitlement")
        if (not _valid_id(response.get("box_id")) or not _valid_time(response.get("server_time"))
                or not isinstance(response.get("account_scope"), str)
                or not _SHA.fullmatch(response["account_scope"])
                or not isinstance(people, list) or len(people) > 100
                or not isinstance(entitlement, dict)
                or any(type(entitlement.get(key)) is not bool for key in ("ingest", "deliver", "send"))
                or type(response.get("queue_hold")) is not bool
                or response.get("retention_days") not in {7, 30, 90}
                or type(response.get("desired_revision")) is not int
                or response["desired_revision"] < 0):
            raise CloudRuntimeError("cloud heartbeat is invalid")
        if (self.state.get("snapshot") or {}).get("box_id") not in (None, response["box_id"]):
            raise CloudRuntimeError("cloud box identity changed")
        ids, numbers = set(), set()
        safe_people = []
        for person in people:
            if (not isinstance(person, dict) or not _valid_id(person.get("id"))
                    or not isinstance(person.get("wa_id"), str) or not _PHONE.fullmatch(person["wa_id"])
                    or not isinstance(person.get("display_name"), str)
                    or len(person["display_name"]) > 100
                    or person.get("role") not in {"owner", "coadmin", "family"}
                    or person["id"] in ids or person["wa_id"] in numbers):
                raise CloudRuntimeError("cloud family list is invalid")
            ids.add(person["id"])
            numbers.add(person["wa_id"])
            safe_people.append({key: person[key] for key in ("id", "wa_id", "display_name", "role")})
        default = response.get("default_recipient_id")
        if default is not None and default not in ids:
            raise CloudRuntimeError("cloud default recipient is invalid")
        until = entitlement.get("until")
        if until is not None and not _valid_time(until):
            raise CloudRuntimeError("cloud entitlement is invalid")
        snapshot = {"box_id": response["box_id"], "server_time": response["server_time"],
                    "account_scope": response["account_scope"],
                    "verified_at": self.clock(), "verified_mono": self.monotonic(),
                    "boot_id": self.boot_id,
                    "retention_days": response["retention_days"],
                    "people": safe_people, "default_recipient_id": default,
                    "entitlement": {**{key: entitlement[key] for key in ("ingest", "deliver", "send")}, "until": until},
                    "queue_hold": response["queue_hold"]}
        self._sync_contacts(snapshot)
        self.state["snapshot"] = snapshot
        self._save()
        return response

    def _ack_paths(self, operation_id):
        digest = hashlib.sha256(operation_id.encode()).hexdigest()
        ACK_DIR.mkdir(parents=True, exist_ok=True)
        return ACK_DIR / (digest + ".json"), ACK_DIR / (digest + ".lock")

    def _completed_path(self, operation_id):
        return COMPLETED_DIR / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")

    def _intent_path(self, operation_id):
        return INTENT_DIR / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")

    def _ack(self, operation_id, state, **fields):
        if not _valid_id(operation_id):
            raise CloudRuntimeError("cloud operation is invalid")
        path, lock_path = self._ack_paths(operation_id)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    previous = None
                completed_path = self._completed_path(operation_id)
                if completed_path.exists() and state == "received":
                    return
                if previous and previous.get("state") == "played" and state == "received":
                    return
                ack = {"operation_id": operation_id, "state": state, **fields}
                if state in {"applied", "played", "rejected", "expired"}:
                    atomic_json(completed_path, ack)
                atomic_json(path, ack)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def flush_acks(self, allowed=None):
        for path in sorted(ACK_DIR.glob("*.json")):
            with path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    if not path.exists():
                        continue
                    try:
                        ack = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        raise CloudRuntimeError("cloud acknowledgment is invalid") from exc
                    if allowed is not None and ack.get("operation_id") not in allowed:
                        continue
                    try:
                        response = self.client.ack(ack)
                    except CloudAckGone:
                        atomic_json(ACK_DIR / "gone" / path.name, ack)
                        path.unlink()
                        continue
                    if response.get("operation_id") != ack.get("operation_id") or response.get("accepted") is not True:
                        raise CloudRuntimeError("cloud acknowledgment was rejected")
                    path.unlink()
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _delete(self, message_id):
        if not _valid_id(message_id):
            raise CloudRuntimeError("delete target is invalid")
        if message_id not in self.state["deleted"]:
            self.state["deleted"].append(message_id)
            self._save()  # Persist the tombstone before removing any copy.
        with played_history_lock(self.queue_dir):
            for directory in (self.queue_dir, *(self.queue_dir / part for part in
                               (".inflight", ".hold", ".trash", ".played"))):
                for metadata_path in directory.glob("*.wav.json"):
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if metadata.get("cloud_message_id") == message_id:
                        Path(str(metadata_path)[:-5]).unlink(missing_ok=True)
                        metadata_path.unlink(missing_ok=True)
        for job_dir in self.outbox_dir.glob("*.job"):
            try:
                metadata = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if metadata.get("transport") == "cloud" and metadata.get("cloud_message_id") == message_id:
                shutil.rmtree(job_dir)

    def expire_local(self):
        self._snapshot()
        # Only an authenticated server timestamp authorizes irreversible expiry deletion.
        now = self.state["snapshot"]["server_time"]
        with played_history_lock(self.queue_dir):
            for directory in (self.queue_dir, *(self.queue_dir / part for part in
                               (".inflight", ".hold", ".trash", ".played"))):
                for metadata_path in directory.glob("*.wav.json"):
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if metadata.get("cloud") is True:
                        try:
                            expired = _effective_expiry(metadata, self._snapshot()) <= now
                        except CloudRuntimeError:
                            continue
                        if expired:
                            Path(str(metadata_path)[:-5]).unlink(missing_ok=True)
                            metadata_path.unlink(missing_ok=True)
        for job_dir in self.outbox_dir.glob("*.job"):
            try:
                metadata = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (metadata.get("transport") == "cloud"
                    and _valid_time(metadata.get("expires_at"))
                    and now >= metadata["expires_at"]):
                shutil.rmtree(job_dir)
        self.state["seen"] = {key: timestamp for key, timestamp in self.state["seen"].items()
                              if _valid_time(timestamp) and now - timestamp < LEDGER_SECONDS}
        self._save()

    def recover_outbox(self):
        """Resolve keyed uploads; retry only a verified original payload."""
        candidates = []
        for job_dir in self.outbox_dir.glob("*.job"):
            try:
                metadata = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (metadata.get("transport") == "cloud"
                    and metadata.get("state") in {"sending", "uncertain", "cloud_retained"}
                    and _valid_id(metadata.get("message_id"))):
                candidates.append((job_dir.name, job_dir))
        candidates.sort()
        if not candidates:
            return
        start = self.state.get("outbox_status_cursor", 0) % len(candidates)
        count = min(5, len(candidates))
        selected = [candidates[(start + offset) % len(candidates)] for offset in range(count)]
        # Advance durably even when every lookup fails, so an offline key cannot
        # starve a later recording. This cursor is scheduling state, not time proof.
        self.state["outbox_status_cursor"] = (start + count) % len(candidates)
        self._save()
        for _, job_dir in selected:
            try:
                with cloud_outbox_lock(job_dir) as acquired:
                    if acquired:
                        self._recover_outbox_job(job_dir)
            except (OSError, ValueError, CloudDeviceError):
                continue

    def _recover_outbox_job(self, job_dir):
        metadata = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        if (metadata.get("transport") != "cloud"
                or metadata.get("state") not in {"sending", "uncertain", "cloud_retained"}
                or not _valid_id(metadata.get("message_id"))):
            return
        try:
            status = self.client.voice_status(metadata["message_id"])
        except CloudVoiceNotFound:
            # A 404 may race the first request. Reusing its exact bytes/key is
            # safe under the server's unique message key; a new encoding is not.
            if metadata.get("state") == "cloud_retained" or metadata.get("cloud_message_id"):
                return
            try:
                _, upload = cloud_upload_payload(job_dir, metadata)
                snapshot = self._snapshot()
                if (upload["account_scope"] != snapshot["account_scope"]
                        or upload["recipient_id"] != self.recipient_id(upload["recipient"])
                        or self.trusted_now() >= upload["retry_until"]):
                    return
            except (CloudRuntimeError, OSError, ValueError):
                return
            metadata["state"] = "pending"
            atomic_json(job_dir / "job.json", metadata)
            return
        metadata.update({"state": "cloud_retained", "cloud_message_id": status["message_id"],
                         "cloud_state": status["state"], "expires_at": status["expires_at"],
                         "server_time": status["server_time"],
                         "cloud_status_checked_at": status["server_time"]})
        if (status["deleted"] is True or status["message_id"] in self.state["deleted"]
                or status["server_time"] >= status["expires_at"]):
            shutil.rmtree(job_dir)
        else:
            atomic_json(job_dir / "job.json", metadata)

    def _audio(self, item, server_time):
        payload = item["payload"]
        message_id = payload.get("message_id")
        sender_id = payload.get("sender_id")
        expires_at = payload.get("expires_at")
        message_created_at = payload.get("message_created_at")
        if (not _valid_id(message_id) or not _valid_time(expires_at)
                or expires_at > item["expires_at"] or sender_id not in self._people()
                or (message_created_at is not None and not _valid_time(message_created_at))
                or not isinstance(payload.get("sha256"), str) or not _SHA.fullmatch(payload["sha256"])
                or payload.get("content_type") not in {"audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr", "audio/wav", "audio/x-wav"}
                or not isinstance(payload.get("media_url"), str)):
            raise CloudRuntimeError("cloud audio metadata is invalid")
        if not self._snapshot()["entitlement"]["deliver"]:
            raise CloudRuntimeError("cloud delivery is paused")
        if message_id in self.state["deleted"] or server_time >= expires_at:
            self._ack(item["operation_id"], "expired")
            return
        if self._snapshot()["queue_hold"]:
            # A newer resume may follow this older audio in the same inbox.
            # Keep it pending for a fresh authorized fetch after the hold clears.
            raise CloudCommandDeferred("cloud queue is held")
        self._refresh_message_expiry(message_id, expires_at, message_created_at)
        digest = hashlib.sha256((message_id + ":" + item["operation_id"]).encode("utf-8")).hexdigest()
        if digest in self.state["seen"]:
            self._ack(item["operation_id"], "received")
            return
        person = self._people()[sender_id]
        jid = person["wa_id"] + "@s.whatsapp.net"
        name = f"{int(item['created_at'] * 1000):013d}-{digest[:24]}.wav"
        metadata = {"version": 1, "chat": jid, "msgid": payload.get("reply_to") or message_id,
                    "sender_jid": jid, "media_type": "audio", "cloud": True,
                    "cloud_message_id": message_id, "cloud_operation_id": item["operation_id"],
                    "sender_id": sender_id, "expires_at": expires_at}
        if message_created_at is not None:
            metadata["cloud_created_at"] = message_created_at
        for folder in (self.queue_dir, *(self.queue_dir / part for part in (".inflight", ".hold", ".played"))):
            if (folder / name).exists():
                try:
                    existing = json.loads(Path(str(folder / name) + ".json").read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise CloudRuntimeError("queued cloud route is invalid") from exc
                if existing.get("cloud_message_id") != message_id or existing.get("sender_id") != sender_id:
                    raise CloudRuntimeError("queued cloud route conflicts")
                if not _valid_time(existing.get("expires_at")):
                    raise CloudRuntimeError("queued cloud retention is invalid")
                if (expires_at < existing["expires_at"]
                        or message_created_at is not None and existing.get("cloud_created_at") != message_created_at):
                    existing["expires_at"] = min(existing["expires_at"], expires_at)
                    if message_created_at is not None:
                        existing["cloud_created_at"] = message_created_at
                    atomic_json(Path(str(folder / name) + ".json"), existing)
                self.state["seen"][digest] = self.trusted_now()
                self._save()
                self._ack(item["operation_id"], "received")
                return
        audio = self.client.media(payload.get("media_url"), limit=MAX_MEDIA_BYTES)
        if not audio or hashlib.sha256(audio).hexdigest() != payload["sha256"]:
            raise CloudRuntimeError("cloud audio hash is invalid")
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.queue_dir / f".cloud-{uuid.uuid4().hex}.part"
        try:
            with temporary.open("wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
            self.converter({"ChatJID": jid, "SenderJID": jid, "MsgID": metadata["msgid"],
                            "MediaType": "audio", "QueueFilename": name,
                            "CloudMetadata": metadata}, temporary, queue_dir=self.queue_dir)
        finally:
            temporary.unlink(missing_ok=True)
        self.state["seen"][digest] = self.trusted_now()
        self._save()
        self._ack(item["operation_id"], "received")

    def _refresh_message_expiry(self, message_id, expires_at, message_created_at):
        with played_history_lock(self.queue_dir):
            for directory in (self.queue_dir, *(self.queue_dir / part for part in
                               (".inflight", ".hold", ".trash", ".played"))):
                for metadata_path in directory.glob("*.wav.json"):
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if metadata.get("cloud_message_id") != message_id:
                        continue
                    if not _valid_time(metadata.get("expires_at")):
                        raise CloudRuntimeError("queued cloud retention is invalid")
                    effective = min(metadata["expires_at"], expires_at)
                    if (effective != metadata["expires_at"] or message_created_at is not None
                            and metadata.get("cloud_created_at") != message_created_at):
                        metadata["expires_at"] = effective
                        if message_created_at is not None:
                            metadata["cloud_created_at"] = message_created_at
                        atomic_json(metadata_path, metadata)

    def _nfc(self, item):
        kind, payload = item["kind"], item["payload"]
        router = NfcRouter(self.contacts, SelectionStore(NFC_SELECTION_FILE),
                           EnrollmentStore(NFC_ENROLLMENT_FILE))
        if kind == "nfc_enroll":
            person = self._people().get(payload.get("recipient_id"))
            if person is None:
                raise CloudRuntimeError("NFC recipient is no longer authorized")
            active = router.enrollment.active()
            if active is None:
                active = router.begin_enrollment(label=person["display_name"] or person["wa_id"],
                    jid=person["wa_id"] + "@s.whatsapp.net", ttl_s=min(120, item["expires_at"] - self.trusted_now()),
                    create_contact=False)
            elif active["jid"] != person["wa_id"] + "@s.whatsapp.net":
                raise CloudRuntimeError("another NFC enrollment is active")
            self.state["pending_nfc"][item["operation_id"]] = active["request_id"]
            self._save()
            self._ack(item["operation_id"], "received")
        elif kind == "nfc_cancel":
            intent_path = self._intent_path(item["operation_id"])
            if intent_path.exists():
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise CloudRuntimeError("NFC cancel intent is invalid") from exc
                if intent.get("operation_id") != item["operation_id"]:
                    raise CloudRuntimeError("NFC cancel intent conflicts")
            else:
                active = router.enrollment.active()
                intent = {"operation_id": item["operation_id"],
                          "request_id": active["request_id"] if active else None}
                atomic_json(intent_path, intent)
            if intent["request_id"] is not None:
                router.cancel_enrollment(intent["request_id"])
            self._ack(item["operation_id"], "applied")
        else:
            intent_path = self._intent_path(item["operation_id"])
            if intent_path.exists():
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise CloudRuntimeError("NFC operation intent is invalid") from exc
                if intent.get("operation_id") != item["operation_id"] or not isinstance(intent.get("uid"), str):
                    raise CloudRuntimeError("NFC operation intent conflicts")
            else:
                selection = router.selection.load(max_age=30)
                if selection is None:
                    raise CloudRuntimeError("no presented card to unpair")
                intent = {"operation_id": item["operation_id"], "uid": selection["uid"]}
                atomic_json(intent_path, intent)
            # A crash can only resume the originally bound UID; a later presented
            # card is never selected for this same cloud operation.
            self.contacts.remove_card(intent["uid"])
            self._ack(item["operation_id"], "applied")

    def _command(self, item, server_time):
        completed = self._completed_path(item["operation_id"])
        if completed.exists():
            try:
                prior = json.loads(completed.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CloudRuntimeError("cloud operation ledger is invalid") from exc
            if prior.get("operation_id") != item["operation_id"]:
                raise CloudRuntimeError("cloud operation ledger conflicts")
            self._ack(item["operation_id"], prior["state"],
                      **{key: value for key, value in prior.items() if key not in {"operation_id", "state"}})
            return
        if (item["operation_id"] in self.state["pending_settings"]
                or item["operation_id"] in self.state["pending_nfc"]):
            return
        kind, payload = item["kind"], item["payload"]
        if kind == "audio":
            return self._audio(item, server_time)
        if kind == "delete_message":
            self._delete(payload.get("message_id"))
            self._ack(item["operation_id"], "applied")
        elif kind == "settings":
            current, warning = self.settings.load()
            expected, desired = payload.get("expected_revision"), payload.get("desired_revision")
            document = payload.get("settings")
            candidate = ({key: value for key, value in document.items() if key not in {"version", "revision"}}
                         if isinstance(document, dict) else None)
            if (warning or type(expected) is not int or type(desired) is not int or desired != expected + 1
                    or not isinstance(document, dict) or document.get("version") != 1
                    or document.get("revision") != desired):
                raise CloudRuntimeError("settings revision is invalid")
            if current["revision"] == desired:
                if current != {"version": 1, "revision": desired, **candidate}:
                    raise CloudRuntimeError("settings revision conflicts")
            else:
                updated = self.settings.update(candidate, expected)
                if updated["revision"] != desired:
                    raise CloudRuntimeError("settings revision conflicts")
            self.state["pending_settings"][item["operation_id"]] = document
            self._save()
            self._ack(item["operation_id"], "received")
        elif kind in {"nfc_enroll", "nfc_cancel", "nfc_unpair"}:
            self._nfc(item)
        elif kind == "queue_hold":
            if type(payload.get("held")) is not bool:
                raise CloudRuntimeError("queue hold is invalid")
            intent_path = self._intent_path(item["operation_id"])
            if intent_path.exists():
                intent = json.loads(intent_path.read_text(encoding="utf-8"))
                if (not isinstance(intent, dict) or intent.get("operation_id") != item["operation_id"]
                        or type(intent.get("held")) is not bool or intent["held"] != payload["held"]
                        or ("sequence" in intent and (type(intent["sequence"]) is not int
                            or intent["sequence"] != item["sequence"]))):
                    raise CloudRuntimeError("queue hold intent conflicts")
            else:
                atomic_json(intent_path, {"operation_id": item["operation_id"],
                                         "held": payload["held"], "sequence": item["sequence"]})
            # Setting a boolean is safe to resume after a crash. The sequence is
            # saved with the effect so a delayed retry cannot undo a newer hold.
            if item["sequence"] > self.state.get("queue_hold_sequence", -1):
                self.state["snapshot"]["queue_hold"] = payload["held"]
                self.state["queue_hold_sequence"] = item["sequence"]
                self._save()
            self._ack(item["operation_id"], "applied")
        elif kind == "preview_ringtone":
            ringtone = payload.get("ringtone_id")
            if ringtone not in RINGTONES:
                raise CloudRuntimeError("ringtone is invalid")
            intent_path = self._intent_path(item["operation_id"])
            if intent_path.exists():
                self._ack(item["operation_id"], "rejected", error_code="preview_outcome_unknown")
                return
            atomic_json(intent_path, {"operation_id": item["operation_id"], "ringtone_id": ringtone})
            path = RINGTONE_DIR / RINGTONES[ringtone]
            timeout = _ringtone_preview_timeout(path)
            speaker = os.environ.get("MSGBOX_SPK_DEV", "default")
            try:
                result = subprocess.run(["aplay", "-q", "-D", speaker, str(path)], timeout=timeout,
                                        check=False)
            except subprocess.TimeoutExpired as exc:
                raise CloudRuntimeError("ringtone preview failed") from exc
            if result.returncode:
                raise CloudRuntimeError("ringtone preview failed")
            self._ack(item["operation_id"], "applied")
        else:
            raise CloudRuntimeError("cloud command is unsupported")

    def _finish_settings(self):
        try:
            marker = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        revision = marker.get("revision") if isinstance(marker, dict) else None
        for operation_id, document in list(self.state["pending_settings"].items()):
            desired = document.get("revision") if isinstance(document, dict) else None
            if type(revision) is int and type(desired) is int and revision >= desired:
                if marker.get("settings") == document:
                    self._ack(operation_id, "applied", applied_revision=desired)
                else:
                    self._ack(operation_id, "rejected", error_code="settings_superseded")
                del self.state["pending_settings"][operation_id]
                self._save()

    def _finish_nfc(self):
        enrollment = EnrollmentStore(NFC_ENROLLMENT_FILE)
        for operation_id, request_id in list(self.state["pending_nfc"].items()):
            outcome = enrollment.outcome(request_id)
            if outcome is not None:
                self._ack(operation_id, "applied")
                del self.state["pending_nfc"][operation_id]
                self._save()

    def poll_once(self):
        inbox = self.client.inbox(self.state["cursor"])
        deletion_only = inbox.get("deleted") is True
        if deletion_only:
            self.state["snapshot"] = None
            self._save()
        elif inbox.get("box_id") != self._snapshot()["box_id"]:
            raise CloudRuntimeError("cloud inbox box does not match")
        if not _valid_time(inbox.get("server_time")) or not isinstance(inbox.get("items"), list):
            raise CloudRuntimeError("cloud inbox is invalid")
        items = inbox["items"]
        deletion_ids = {item.get("operation_id") for item in items if isinstance(item, dict)
                        and item.get("kind") == "delete_message"}
        self.flush_acks(deletion_ids if deletion_only else None)
        if not deletion_only:
            self._finish_nfc()
            self._finish_settings()
            self.flush_acks()
        for item in sorted(items, key=lambda entry: (entry.get("kind") != "delete_message", entry.get("sequence", 0))):
            if (not isinstance(item, dict) or not _valid_id(item.get("operation_id"))
                    or type(item.get("sequence")) is not int or item["sequence"] < 0
                    or item.get("kind") not in {"audio", "settings", "preview_ringtone", "nfc_enroll", "nfc_cancel", "nfc_unpair", "queue_hold", "delete_message"}
                    or not _valid_time(item.get("created_at")) or not _valid_time(item.get("expires_at"))
                    or not isinstance(item.get("payload"), dict)):
                raise CloudRuntimeError("cloud inbox item is invalid")
            if inbox.get("deleted") is True and item["kind"] != "delete_message":
                continue
            now = inbox["server_time"]
            if not deletion_only:
                self._snapshot()
            if now >= item["expires_at"] and item["kind"] != "delete_message":
                self._ack(item["operation_id"], "expired")
            else:
                try:
                    self._command(item, now)
                except CloudCommandDeferred:
                    # Inbox cursors are hints; the service returns all pending
                    # operations until ACKed, including work before the cursor.
                    continue
                except (CloudRuntimeError, SettingsError, ContactError, NfcError, OSError, ValueError):
                    self._ack(item["operation_id"], "rejected", error_code="device_command_rejected")
            self.state["cursor"] = max(self.state["cursor"], item["sequence"])
            self._save()
            self.flush_acks(deletion_ids if deletion_only else None)
        if self.state.get("snapshot") is not None:
            self.expire_local()
        self.recover_outbox()

    def run(self):
        failures = 0
        last_heartbeat = 0
        while True:
            try:
                if self.clock() - last_heartbeat >= 30 or not self.state.get("snapshot"):
                    try:
                        self.heartbeat()
                    except CloudDeviceError:
                        pass  # A revoked device may still receive deletion tombstones.
                    last_heartbeat = self.clock()
                self.poll_once()
                failures = 0
            except (CloudDeviceError, CloudRuntimeError, OSError, ValueError):
                print("cloud poll failed; local work remains pending", flush=True)
                failures += 1
            time.sleep(min(60, 2 * 2 ** min(failures, 5)) + random.random())


def recipient_id(jid):
    return CloudRuntime().recipient_id(jid)


def playable(metadata):
    return CloudRuntime().playable(metadata)


def record_played(metadata):
    if metadata and metadata.get("cloud") is True and _valid_id(metadata.get("cloud_operation_id")):
        runtime = CloudRuntime()
        runtime._ack(metadata["cloud_operation_id"], "played")


def account_scope(*, fresh=False):
    """Bind offline recordings to the last verified owner; sending needs freshness."""
    runtime = CloudRuntime(client=object())
    snapshot = runtime._snapshot() if fresh else runtime.state.get("snapshot", {})
    scope = snapshot.get("account_scope") if isinstance(snapshot, dict) else None
    if not isinstance(scope, str) or not _SHA.fullmatch(scope):
        raise CloudRuntimeError("cloud recording account is unavailable")
    return scope


def outbox_retry_until():
    """Bound retries before server upload-key tombstones can be collected."""
    runtime = CloudRuntime(client=object())
    snapshot = runtime._snapshot()
    return runtime.trusted_now() + snapshot["retention_days"] * 86400


def outbox_now():
    runtime = CloudRuntime(client=object())
    runtime._snapshot()
    return runtime.trusted_now()
