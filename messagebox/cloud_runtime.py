"""Cloud API v1 polling and local command execution for one claimed Pi."""

from __future__ import annotations

import hashlib
import fcntl
import io
import json
import os
import random
import re
import shutil
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from messagebox.cloud_device import CLOUD_DIR, CloudAckGone, CloudDeviceClient, CloudDeviceError, CloudVoiceNotFound, atomic_json, capabilities
from messagebox.audio_requests import AudioRequests, preview_key, success_key
from messagebox.contacts import ContactError, ContactStore
from messagebox.guided_reply import cloud_outbox_lock, cloud_upload_payload
from messagebox.cloud_events import CloudWorkEvents
from messagebox.listened_receipts import ReceiptStore
from messagebox.nfc_state import CardReferenceStore, EnrollmentStore, NfcError, NfcRouter, SelectionStore
from messagebox.played_history import played_history_lock
from messagebox.runtime_paths import (NFC_CARD_REFERENCES_FILE, NFC_ENROLLMENT_FILE,
    NFC_HEALTH_FILE, NFC_SELECTION_FILE, OUTBOX_DIR, QUEUE_DIR, SETTINGS_FILE, STATE_DIR)
from messagebox.settings import (
    in_quiet_hours, ignored_settings, SettingsError, SettingsStore, RINGTONES, VOICE_PACKS, normalize_ringtone_id, normalize_voice_pack, validate as validate_settings,
)
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
MAX_LISTENED_CLIP_BYTES = 1024 * 1024
MAX_LISTENED_CLIPS = 32
LISTENED_DIR = STATE_DIR / "listened-receipts"
LEDGER_SECONDS = 90 * 86400
RINGTONE_DIR = Path("/opt/messagebox/ringtones")
RINGTONE_PREVIEW_MIN_SECONDS = 17
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


def _connected_boxes(value):
    if not isinstance(value, list):
        return []
    boxes, ids = [], set()
    for box in value:
        if (not isinstance(box, dict) or not _valid_id(box.get("id"))
                or box["id"] in ids or not isinstance(box.get("box_name"), str)
                or not box["box_name"].strip() or box["box_name"] != box["box_name"].strip()
                or len(box["box_name"]) > 60 or any(ord(char) < 32 or ord(char) == 127
                                                     for char in box["box_name"])):
            continue
        ids.add(box["id"])
        boxes.append({"id": box["id"], "box_name": box["box_name"]})
        if len(boxes) == 50:
            break
    return boxes


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
    return max(RINGTONE_PREVIEW_MIN_SECONDS, duration + RINGTONE_PREVIEW_GRACE_SECONDS)


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
        self.audio_requests = AudioRequests(self.state_path.parent / "audio-requests",
            clock=clock, monotonic=monotonic, boot_id=self.boot_id)
        self.state = self._load()

    def _receipt_store(self):
        return ReceiptStore(str(LISTENED_DIR))

    def _load(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "cursor": 0, "acks": {}, "seen": {}, "deleted": [], "pending_nfc": {}, "pending_settings": {}, "outbox_status_cursor": 0, "pending_previews": {}, "pending_listened": {}}
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
        if not isinstance(state.setdefault("pending_previews", {}), dict):
            raise CloudRuntimeError("cloud state is invalid")
        if not isinstance(state.setdefault("pending_listened", {}), dict):
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

    def server_now(self):
        """Estimate authoritative time without adding the local wall-clock skew."""
        snapshot = self._snapshot()
        return snapshot["server_time"] + self.monotonic() - snapshot["verified_mono"]

    def _people(self):
        snapshot = self._snapshot()
        return {person["id"]: person for person in snapshot["people"]}

    def _boxes(self):
        return {box["id"]: box for box in self._snapshot().get("connected_boxes", [])}

    def _recipient(self, recipient_id):
        if not _valid_id(recipient_id):
            raise CloudRuntimeError("recipient is no longer authorized")
        box = self._boxes().get(recipient_id)
        if box is not None:
            return "box:" + box["id"], box["box_name"]
        person = self._people().get(recipient_id)
        if person is not None:
            return person["wa_id"] + "@s.whatsapp.net", person["display_name"] or person["wa_id"]
        raise CloudRuntimeError("recipient is no longer authorized")

    def recipient_id(self, jid):
        snapshot = self._snapshot()
        if (not snapshot["entitlement"]["send"] or
                (snapshot["entitlement"].get("until") is not None and self.trusted_now() >= snapshot["entitlement"]["until"])):
            raise CloudRuntimeError("cloud sending is paused")
        for box in snapshot.get("connected_boxes", []):
            if "box:" + box["id"] == jid:
                return box["id"]
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
                and metadata.get("sender_id") in (self._boxes() if metadata.get("sender_kind") == "box" else self._people())
                and self.trusted_now() < _effective_expiry(metadata, snapshot)
                and metadata.get("cloud_message_id") not in self.state["deleted"]
            )
        except CloudRuntimeError:
            return False

    def _sync_contacts(self, snapshot):
        people = snapshot["people"]
        wanted = {p["wa_id"] + "@s.whatsapp.net": p for p in people}
        wanted.update({"box:" + box["id"]: {"id": box["id"], "display_name": box["box_name"]}
                       for box in snapshot.get("connected_boxes", [])})
        default = next((jid for jid, person in wanted.items()
                        if person["id"] == snapshot["default_recipient_id"]), None)
        now = snapshot["server_time"]
        def sync(document):
            old = document["contacts"]
            contacts = {}
            for jid, person in wanted.items():
                prior = old.get(jid, {})
                contacts[jid] = {
                    "label": (person["display_name"][:80] or jid.split("@", 1)[0]),
                    "kind": "box" if jid.startswith("box:") else "person", "receive_after": prior.get("receive_after", now),
                    "card_uids": prior.get("card_uids", []), "card_clip": prior.get("card_clip", ""),
                }
            changed = contacts != old or default != document["default_recipient"]
            if changed:
                document["contacts"] = contacts
                document["default_recipient"] = default
            return changed, None
        self.contacts._mutate(sync)

    def _nfc_inventory(self):
        snapshot = self.state.get("snapshot")
        if not snapshot:
            return None
        try:
            document = self.contacts.load()
        except ContactError:
            return None
        cards = []
        for person in snapshot["people"]:
            contact = document["contacts"].get(person["wa_id"] + "@s.whatsapp.net")
            if contact:
                cards.extend((person["id"], uid) for uid in contact["card_uids"])
        for box in snapshot.get("connected_boxes", []):
            contact = document["contacts"].get("box:" + box["id"])
            if contact:
                cards.extend((box["id"], uid) for uid in contact["card_uids"])
        cards = CardReferenceStore(NFC_CARD_REFERENCES_FILE).sync(snapshot["account_scope"], cards)
        return {"account_scope": snapshot["account_scope"],
                "revision": document["revision"], "cards": cards}

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
        inventory = self._nfc_inventory()
        response = self.client.heartbeat({
            "version": "cloud-mvp-1", "capabilities": capabilities(nfc=NFC_HEALTH_FILE.exists()),
            "settings": settings, "applied_revision": settings["revision"],
            "queue": {"held": bool((self.state.get("snapshot") or {}).get("queue_hold", False)),
                      "count": len(list(self.queue_dir.glob("*.wav"))), "playing_message_id": None},
            "last_error": None,
            **({"nfc_inventory": inventory} if inventory is not None else {}),
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
        boxes = [box for box in _connected_boxes(response.get("connected_boxes"))
                 if box["id"] not in ids]
        ids.update(box["id"] for box in boxes)
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
                    "people": safe_people, "connected_boxes": boxes, "default_recipient_id": default,
                    "entitlement": {**{key: entitlement[key] for key in ("ingest", "deliver", "send")}, "until": until},
                    "queue_hold": response["queue_hold"]}
        self._sync_contacts(snapshot)
        self.state["snapshot"] = snapshot
        self._save()
        self._prune_listened_clips()
        return response

    def _ack_paths(self, operation_id):
        digest = hashlib.sha256(operation_id.encode()).hexdigest()
        ACK_DIR.mkdir(parents=True, exist_ok=True)
        return ACK_DIR / (digest + ".json"), ACK_DIR / (digest + ".lock")

    def _completed_path(self, operation_id):
        return COMPLETED_DIR / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")

    def _replay_completed(self, operation_id):
        completed = self._completed_path(operation_id)
        if not completed.exists():
            return False
        try:
            prior = json.loads(completed.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CloudRuntimeError("cloud operation ledger is invalid") from exc
        if prior.get("operation_id") != operation_id:
            raise CloudRuntimeError("cloud operation ledger conflicts")
        self._ack(operation_id, prior["state"],
                  **{key: value for key, value in prior.items() if key not in {"operation_id", "state"}})
        return True

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
                        or self.server_now() >= upload["retry_until"]):
                    return
            except (CloudRuntimeError, OSError, ValueError):
                return
            metadata["state"] = "pending"
            atomic_json(job_dir / "job.json", metadata)
            return
        self.queue_send_success(metadata, status)
        metadata.update({"state": "cloud_retained", "cloud_message_id": status["message_id"],
                         "cloud_state": status["state"], "expires_at": status["expires_at"],
                         "server_time": status["server_time"],
                         "cloud_status_checked_at": status["server_time"]})
        if (status["deleted"] is True or status["message_id"] in self.state["deleted"]
                or status["server_time"] >= status["expires_at"]):
            shutil.rmtree(job_dir)
        else:
            atomic_json(job_dir / "job.json", metadata)

    def queue_send_success(self, metadata, status):
        """Only recent, scoped acceptance may become a child-facing cue."""
        if (status.get("state") not in {"accepted", "delivered", "read"}
                or status.get("deleted") is True
                or status.get("message_id") in self.state["deleted"]
                or status["server_time"] >= status["expires_at"]
                or metadata.get("state") not in {"sending", "uncertain", "cloud_retained"}
                or metadata.get("cloud_state") not in {None, "queued", "waiting_for_reply", "held_for_review", "uncertain"}):
            return
        observed = metadata.get("cloud_status_checked_at", metadata.get("cloud_send_started_at"))
        # The API has no acceptance timestamp. A recent nonaccepted observation
        # bounds the transition; after a long outage we keep old sends silent.
        if not _valid_time(observed) or not 0 <= status["server_time"] - observed <= 30:
            return
        try:
            scope = self._snapshot()["account_scope"]
            remaining = status["server_time"] + 30 - self.server_now()
        except CloudRuntimeError:
            return
        if metadata.get("account_scope") != scope:
            return
        self.audio_requests.enqueue_for(success_key(scope, metadata["message_id"]),
            "success", scope, remaining)

    def _audio(self, item, server_time):
        payload = item["payload"]
        message_id = payload.get("message_id")
        sender_id = payload.get("sender_id")
        expires_at = payload.get("expires_at")
        message_created_at = payload.get("message_created_at")
        if (not _valid_id(message_id) or not _valid_time(expires_at) or not _valid_id(sender_id)
                or expires_at > item["expires_at"]
                or payload.get("sender_kind") not in {None, "person", "box"}
                or sender_id not in (self._boxes() if payload.get("sender_kind") == "box" else self._people())
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
        jid, _label = self._recipient(sender_id)
        name = f"{int(item['created_at'] * 1000):013d}-{digest[:24]}.wav"
        metadata = {"version": 1, "chat": jid, "msgid": payload.get("reply_to") or message_id,
                    "sender_jid": jid, "media_type": "audio", "cloud": True,
                    "cloud_message_id": message_id, "cloud_operation_id": item["operation_id"],
                    "sender_id": sender_id, "expires_at": expires_at}
        if payload.get("sender_kind") == "box":
            metadata["sender_kind"] = "box"
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
            jid, label = self._recipient(payload.get("recipient_id"))
            active = router.enrollment.active()
            if active is None:
                active = router.begin_enrollment(label=label,
                    jid=jid, ttl_s=min(120, item["expires_at"] - self.trusted_now()),
                    create_contact=False)
            elif active["jid"] != jid:
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
            targeted = bool(payload)
            if intent_path.exists():
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise CloudRuntimeError("NFC operation intent is invalid") from exc
                if intent.get("operation_id") != item["operation_id"] or not isinstance(intent.get("uid"), str):
                    raise CloudRuntimeError("NFC operation intent conflicts")
                if targeted and any(intent.get(key) != payload.get(key)
                                    for key in ("card_ref", "recipient_id")):
                    raise CloudRuntimeError("saved card removal intent conflicts")
            else:
                if targeted:
                    recipient_id = payload.get("recipient_id")
                    jid, _label = self._recipient(recipient_id)
                    snapshot = self.state.get("snapshot") or {}
                    uid = CardReferenceStore(NFC_CARD_REFERENCES_FILE).resolve(
                        snapshot.get("account_scope"), payload.get("card_ref"))
                    if uid is None:
                        raise CloudRuntimeError("selected saved card is no longer available")
                    document = self.contacts.load()
                    if uid not in document["contacts"].get(jid, {}).get("card_uids", []):
                        raise CloudRuntimeError("selected saved card changed")
                    intent = {"operation_id": item["operation_id"], "uid": uid,
                              "jid": jid, "card_ref": payload["card_ref"],
                              "recipient_id": recipient_id}
                else:
                    selection = router.selection.load(max_age=30)
                    if selection is None:
                        raise CloudRuntimeError("no presented card to unpair")
                    intent = {"operation_id": item["operation_id"], "uid": selection["uid"]}
                atomic_json(intent_path, intent)
            # A crash can only resume the originally bound UID; a later presented
            # card is never selected for this same cloud operation.
            if not self.contacts.remove_card(intent["uid"], expected_jid=intent.get("jid")):
                document = self.contacts.load()
                current_jid, _contact = next(((jid, contact) for jid, contact in document["contacts"].items()
                    if intent["uid"] in contact["card_uids"]), (None, None))
                if current_jid is not None:
                    raise CloudRuntimeError("selected saved card changed")
            self._ack(item["operation_id"], "applied")

    def _listened_prefix(self, scope, listener_id):
        return hashlib.sha256((scope + ":" + listener_id).encode()).hexdigest()

    def _prune_listened_clips(self):
        directory = self.state_path.parent / "listened-clips"
        # Only the poller writes this directory; discard interrupted downloads.
        for temporary in directory.glob(".*.part"):
            temporary.unlink(missing_ok=True)
        snapshot = self.state.get("snapshot") or {}
        allowed = {self._listened_prefix(snapshot.get("account_scope", ""), person["id"])
                   for person in [*snapshot.get("people", []), *snapshot.get("connected_boxes", [])]}
        paths = sorted(directory.glob("*.wav"), key=lambda path: path.stat().st_mtime,
                       reverse=True)
        retained = 0
        for path in paths:
            if (path.name.split("-", 1)[0] not in allowed
                    or path.stat().st_size > MAX_LISTENED_CLIP_BYTES
                    or retained >= MAX_LISTENED_CLIPS):
                path.unlink(missing_ok=True)
            else:
                retained += 1

    def _listened_clip(self, payload, scope):
        """A failed optional clip always leaves the bundled voice available."""
        fields = ("text_hash", "media_url", "sha256", "content_type")
        if not any(field in payload for field in fields):
            return ""
        if (not isinstance(payload.get("text_hash"), str)
                or not _SHA.fullmatch(payload["text_hash"])
                or not isinstance(payload.get("sha256"), str)
                or not _SHA.fullmatch(payload["sha256"])
                or not isinstance(payload.get("media_url"), str)
                or payload.get("content_type") != "audio/wav"):
            return ""
        directory = self.state_path.parent / "listened-clips"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        prefix = self._listened_prefix(scope, payload.get("listener_link_id", payload.get("listener_identity_id")))
        voice_key = hashlib.sha256(json.dumps(payload.get("voice_pack"),
                                             separators=(",", ":")).encode()).hexdigest()[:16]
        prefix += "-" + voice_key
        path = directory / f"{prefix}-{payload['text_hash']}.wav"
        # Changed text invalidates this listener's cached clip in the same pack.
        for prior in directory.glob(prefix + "-*.wav"):
            if prior != path:
                prior.unlink(missing_ok=True)
        temporary = directory / f".{uuid.uuid4().hex}.part"
        try:
            if path.exists() and path.stat().st_size <= MAX_LISTENED_CLIP_BYTES:
                audio = path.read_bytes()
            else:
                audio = self.client.media(payload["media_url"], limit=MAX_LISTENED_CLIP_BYTES)
            if (not audio or len(audio) > MAX_LISTENED_CLIP_BYTES
                    or hashlib.sha256(audio).hexdigest() != payload["sha256"]):
                raise CloudRuntimeError("listened clip hash is invalid")
            with wave.open(io.BytesIO(audio), "rb") as source:
                frames = source.getnframes()
                if (source.getnchannels() != 1 or source.getsampwidth() != 2
                        or source.getframerate() != 48000 or source.getcomptype() != "NONE"
                        or frames <= 0 or frames * 2 > MAX_LISTENED_CLIP_BYTES
                        or len(source.readframes(frames)) != frames * 2):
                    raise CloudRuntimeError("listened clip format is invalid")
            with temporary.open("wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._prune_listened_clips()
            return str(path)
        except (CloudDeviceError, CloudRuntimeError, OSError, ValueError, EOFError, wave.Error):
            path.unlink(missing_ok=True)
            return ""
        finally:
            temporary.unlink(missing_ok=True)

    def listened_status(self, metadata):
        snapshot = self._snapshot()
        if (not isinstance(metadata, dict) or not _valid_id(metadata.get("operation_id"))
                or not _valid_id(metadata.get("message_id"))
                or not _valid_id(metadata.get("listener_id"))
                or not _valid_time(metadata.get("expires_at"))):
            return "rejected"
        if self.server_now() >= metadata["expires_at"]:
            return "expired"
        if (metadata.get("account_scope") != snapshot["account_scope"]
                or metadata["listener_id"] not in (self._boxes() if metadata.get("listener_kind") == "box" else self._people())
                or metadata["message_id"] in self.state["deleted"]):
            return "rejected"
        if metadata.get("listener_kind") == "box":
            settings, warning = self.settings.load()
            now = datetime.fromtimestamp(self.server_now(), ZoneInfo(settings["timezone"]))
            if warning or in_quiet_hours(settings, now) or settings["arrival_signal"] == "silent":
                return "expired"
        if (snapshot["queue_hold"] or not snapshot["entitlement"]["deliver"]
                or not snapshot["entitlement"]["send"]
                or snapshot["entitlement"].get("until") is not None
                and self.server_now() >= snapshot["entitlement"]["until"]):
            return "expired" if metadata.get("listener_kind") == "box" else "pending"
        if metadata.get("listener_kind") == "box" and settings["arrival_signal"] == "lamp_only":
            return "lamp_only"
        return "ready"

    def _listened(self, item, server_time):
        payload = item["payload"]
        box_listener = "listener_link_id" in payload
        listener_id = payload.get("listener_link_id" if box_listener else "listener_identity_id")
        listener_name = payload.get("listener_name" if box_listener else "listener_first_name")
        if (not _valid_id(payload.get("message_id")) or not _valid_id(listener_id)
                or not isinstance(listener_name, str) or len(listener_name) > (60 if box_listener else 24)
                or box_listener and "listener_identity_id" in payload):
            raise CloudRuntimeError("listened notice is invalid")
        snapshot = self._snapshot()
        metadata = {"operation_id": item["operation_id"], "message_id": payload["message_id"],
                    "listener_id": listener_id,
                    "account_scope": snapshot["account_scope"], "expires_at": item["expires_at"]}
        if box_listener:
            metadata["listener_kind"] = "box"
        status = self.listened_status(metadata)
        if server_time >= item["expires_at"] or status == "expired":
            self._ack(item["operation_id"], "expired")
            return
        if status == "rejected":
            raise CloudRuntimeError("listened listener is no longer authorized")
        if status == "pending":
            raise CloudCommandDeferred("listened notice is paused")
        store = self._receipt_store()
        notice_id = store.cloud_notice_id(item["operation_id"])
        if not store.cloud_exists(notice_id):
            clip = (self._listened_clip(payload, snapshot["account_scope"])
                    if len(listener_name) <= 24 and status != "lamp_only" else "")
            store.enqueue_cloud(item["operation_id"], payload["message_id"],
                listener_id, listener_name, clip,
                account_scope=snapshot["account_scope"], expires_at=item["expires_at"],
                received_at=server_time, voice_pack=payload.get("voice_pack"),
                listener_kind="box" if box_listener else None)
        self.state["pending_listened"][item["operation_id"]] = notice_id
        self._save()  # Queue and deduplication are durable before receipt ACK.
        self._ack(item["operation_id"], "received")
        self._finish_listened()

    def _finish_listened(self):
        if not self.state["pending_listened"]:
            return
        store = self._receipt_store()
        for operation_id, notice_id in list(self.state["pending_listened"].items()):
            outcome = store.cloud_outcome(notice_id)
            if outcome in {"applied", "rejected", "expired"}:
                self._ack(operation_id, outcome)
                del self.state["pending_listened"][operation_id]
                self._save()

    def _command(self, item, server_time):
        if self._replay_completed(item["operation_id"]):
            return
        if (item["operation_id"] in self.state["pending_settings"]
                or item["operation_id"] in self.state["pending_nfc"]
                or item["operation_id"] in self.state["pending_previews"]
                or item["operation_id"] in self.state["pending_listened"]):
            return
        kind, payload = item["kind"], item["payload"]
        if kind == "audio":
            return self._audio(item, server_time)
        if kind == "listened":
            return self._listened(item, server_time)
        if kind == "delete_message":
            self._delete(payload.get("message_id"))
            self._ack(item["operation_id"], "applied")
        elif kind == "settings":
            current, warning = self.settings.load()
            expected, desired = payload.get("expected_revision"), payload.get("desired_revision")
            document = payload.get("settings")
            if (warning or type(expected) is not int or type(desired) is not int or desired <= expected
                    or not isinstance(document, dict) or document.get("version") != 1
                    or document.get("revision") != desired):
                raise CloudRuntimeError("settings revision is invalid")
            # Normalize pre-update commands before replay comparison and ACKs.
            ignored = ignored_settings(document)
            document = validate_settings(document)
            candidate = {key: value for key, value in document.items()
                         if key not in {"version", "revision"}}
            intent_path = self._intent_path(item["operation_id"])
            if current["revision"] == desired:
                if current != {"version": 1, "revision": desired, **candidate}:
                    raise CloudRuntimeError("settings revision conflicts")
            else:
                # Persist before mutation so a command replay retains its origin
                # and whether values changed, rather than just its revision.
                if not intent_path.exists():
                    atomic_json(intent_path, {"settings_changed": any(current[key] != value
                        for key, value in candidate.items()),
                        # Volume and ringtone changes preview the ringtone instead of the saved cue.
                        "volume_changed": any(key in candidate and current.get(key) != candidate[key]
                                              for key in ("master_volume_percent", "ringtone_id")),
                        "ringtone_changed": ("ringtone_id" in candidate
                                             and current.get("ringtone_id") != candidate["ringtone_id"]),
                        "voice_changed": current.get("voice_pack") != candidate["voice_pack"],
                        "ignored_settings": ignored,
                        "boot_id": self.boot_id,
                        "account_scope": (self.state.get("snapshot") or {}).get("account_scope")})
                updated = self.settings.update(candidate, expected, desired_revision=desired)
                if updated["revision"] != desired:
                    raise CloudRuntimeError("settings revision conflicts")
            if ignored:
                # Also cover replay after the settings write but before pending-state persistence.
                intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.exists() else {}
                if intent.get("ignored_settings") != ignored:
                    atomic_json(intent_path, {**intent, "ignored_settings": ignored})
            self.state["pending_settings"][item["operation_id"]] = document
            self._save()
            self._ack(item["operation_id"], "received", **({"ignored_settings": ignored} if ignored else {}))
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
        elif kind == "voice_preview":
            pack = payload.get("voice_pack")
            if not isinstance(pack, str) or pack not in VOICE_PACKS:
                raise CloudRuntimeError("voice pack is invalid")
            scope = self._snapshot()["account_scope"]
            key = preview_key(item["operation_id"])
            self.audio_requests.enqueue_for(key, "voice_preview", scope,
                min(item["expires_at"], server_time + 30) - self.server_now(), voice_pack=pack)
            self.state["pending_previews"][item["operation_id"]] = key
            self._save()
            self._ack(item["operation_id"], "received")
        elif kind == "preview_ringtone":
            ringtone = normalize_ringtone_id(payload.get("ringtone_id"))
            if ringtone not in RINGTONES:
                raise CloudRuntimeError("ringtone is invalid")
            # Old releases left this marker before crossing aplay. Its result is
            # uncertain and must never be replayed by the new audio owner.
            if self._intent_path(item["operation_id"]).exists():
                self._ack(item["operation_id"], "rejected", error_code="preview_outcome_unknown")
                return
            scope = self._snapshot()["account_scope"]
            key = preview_key(item["operation_id"])
            self.audio_requests.enqueue_for(key, "preview", scope,
                min(item["expires_at"], server_time + 30) - self.server_now(), ringtone_id=ringtone)
            self.state["pending_previews"][item["operation_id"]] = key
            self._save()
            self._ack(item["operation_id"], "received")
        else:
            raise CloudRuntimeError("cloud command is unsupported")

    def _finish_previews(self):
        for operation_id, key in list(self.state["pending_previews"].items()):
            outcome = self.audio_requests.outcome(key)
            if outcome in {None, "pending", "claimed"}:
                continue
            if outcome == "played":
                self._ack(operation_id, "applied")
            elif outcome == "expired":
                self._ack(operation_id, "expired")
            else:
                self._ack(operation_id, "rejected", error_code="preview_outcome_unknown"
                          if outcome == "unknown" else "preview_failed")
            del self.state["pending_previews"][operation_id]
            self._save()

    def _finish_settings(self):
        try:
            marker = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        revision = marker.get("revision") if isinstance(marker, dict) else None
        for operation_id, document in list(self.state["pending_settings"].items()):
            if isinstance(document, dict) and "ringtone_id" in document:
                document = {**document, "ringtone_id": normalize_ringtone_id(document["ringtone_id"]),
                            "voice_pack": normalize_voice_pack(document.get("voice_pack"))}
            desired = document.get("revision") if isinstance(document, dict) else None
            if type(revision) is int and type(desired) is int and revision >= desired:
                try:
                    intent = json.loads(self._intent_path(operation_id).read_text(encoding="utf-8"))
                except FileNotFoundError:
                    intent = {}
                ignored = intent.get("ignored_settings", [])
                fields = {"ignored_settings": ignored} if ignored else {}
                if marker.get("settings") == document:
                    self._queue_settings_saved(operation_id, marker)
                    self._ack(operation_id, "applied", applied_revision=desired, **fields)
                else:
                    self._ack(operation_id, "rejected", error_code="settings_superseded", **fields)
                del self.state["pending_settings"][operation_id]
                self._save()

    def _queue_settings_saved(self, operation_id, marker):
        try:
            intent = json.loads(self._intent_path(operation_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return  # Already adopted locally, or pending from pre-cue software.
        if (not isinstance(intent, dict) or not intent.get("settings_changed") or not marker.get("settings_sound")
                or not self.boot_id or intent.get("boot_id") != self.boot_id
                or marker.get("boot_id") != self.boot_id
                or not intent.get("account_scope")
                or intent.get("account_scope") != (self.state.get("snapshot") or {}).get("account_scope")):
            return
        applied = marker.get("applied_mono")
        if type(applied) not in (int, float) or not 0 <= self.monotonic() - applied < 30:
            return
        self.audio_requests.enqueue_settings_saved("settings_saved:" + operation_id,
            intent["account_scope"], 30 - (self.monotonic() - applied),
            volume_changed=intent.get("volume_changed") is True,
            ringtone_changed=intent.get("ringtone_changed") is True,
            voice_changed=intent.get("voice_changed") is True)

    def _finish_nfc(self):
        if not self.state["pending_nfc"]:
            return
        enrollment = EnrollmentStore(NFC_ENROLLMENT_FILE)
        # Read active state before the receipt: a tap can commit its receipt and
        # remove the active request between these reads, and must still succeed.
        active = enrollment.active()
        for operation_id, request_id in list(self.state["pending_nfc"].items()):
            if not self._replay_completed(operation_id):
                if enrollment.outcome(request_id) is not None:
                    self._ack(operation_id, "applied")
                elif active is None or active["request_id"] != request_id:
                    self._ack(operation_id, "rejected", error_code="nfc_enrollment_ended")
                else:
                    continue
            del self.state["pending_nfc"][operation_id]
            self._save()

    def poll_once(self):
        inbox = self.client.inbox(self.state["cursor"])
        deletion_only = inbox.get("deleted") is True
        if deletion_only:
            self.state["snapshot"] = None
            self._save()
            self._prune_listened_clips()
        elif inbox.get("box_id") != self._snapshot()["box_id"]:
            raise CloudRuntimeError("cloud inbox box does not match")
        if not _valid_time(inbox.get("server_time")) or not isinstance(inbox.get("items"), list):
            raise CloudRuntimeError("cloud inbox is invalid")
        items = inbox["items"]
        deletion_ids = {item.get("operation_id") for item in items if isinstance(item, dict)
                        and item.get("kind") == "delete_message"}
        self.flush_acks(deletion_ids if deletion_only else None)
        if not deletion_only:
            self._finish_listened()
            self._finish_previews()
            self._finish_nfc()
            self._finish_settings()
            self.flush_acks()
        for item in sorted(items, key=lambda entry: (entry.get("kind") != "delete_message", entry.get("sequence", 0))):
            if (not isinstance(item, dict) or not _valid_id(item.get("operation_id"))
                    or type(item.get("sequence")) is not int or item["sequence"] < 0
                    or item.get("kind") not in {"audio", "listened", "settings", "preview_ringtone", "voice_preview", "nfc_enroll", "nfc_cancel", "nfc_unpair", "queue_hold", "delete_message"}
                    or not _valid_time(item.get("created_at")) or not _valid_time(item.get("expires_at"))
                    or not isinstance(item.get("payload"), dict)
                    or item.get("kind") == "nfc_unpair" and item["payload"] and (
                        set(item["payload"]) != {"card_ref", "recipient_id", "inventory_revision"}
                        or not isinstance(item["payload"].get("card_ref"), str)
                        or not re.fullmatch(r"[a-f0-9]{32}", item["payload"]["card_ref"])
                        or not isinstance(item["payload"].get("recipient_id"), str)
                        or not _ID.fullmatch(item["payload"]["recipient_id"])
                        or type(item["payload"].get("inventory_revision")) is not int
                        or item["payload"]["inventory_revision"] < 0)):
                raise CloudRuntimeError("cloud inbox item is invalid")
            if inbox.get("deleted") is True and item["kind"] != "delete_message":
                continue
            now = inbox["server_time"]
            if not deletion_only:
                self._snapshot()
            if item["kind"] == "listened" and self._replay_completed(item["operation_id"]):
                pass  # Expiry must not replace an already reported playback result.
            elif now >= item["expires_at"] and item["kind"] != "delete_message":
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

    def _maintain_local(self):
        # Cross-process playback receipts and local completions need no inbox hint.
        self._snapshot()
        self.flush_acks()
        self._finish_listened()
        self._finish_previews()
        self._finish_nfc()
        self._finish_settings()
        self.flush_acks()
        self.recover_outbox()

    def run(self, *, stop=None, events=None):
        stop = stop if stop is not None else threading.Event()
        if events is None and os.environ.get("MSGBOX_CLOUD_EVENTS", "1") == "1":
            events = CloudWorkEvents(self.client)
        wake = events.wake if events is not None else threading.Event()
        failures = 0
        last_heartbeat = float("-inf")
        next_poll = 0
        earliest_hint_poll = 0
        next_local_check = 0
        local_failures = 0
        if events is not None:
            events.start()
        try:
            while not stop.is_set():
                now = self.monotonic()
                hinted = wake.is_set()
                if now >= next_poll or (hinted and not failures and now >= earliest_hint_poll):
                    # Clear before HTTP so a hint arriving during the request survives.
                    wake.clear()
                    try:
                        if now - last_heartbeat >= 30 or not self.state.get("snapshot"):
                            try:
                                self.heartbeat()
                            except CloudDeviceError:
                                pass  # A revoked device may still receive deletion tombstones.
                            last_heartbeat = self.monotonic()
                        self.poll_once()
                        failures = 0
                    except (CloudDeviceError, CloudRuntimeError, OSError, ValueError):
                        print("cloud poll failed; local work remains pending", flush=True)
                        failures += 1
                    now = self.monotonic()
                    connected = events is not None and events.connected.is_set()
                    interval = 30 if connected else 2 + random.random()
                    if failures:
                        interval = min(60, 2 * 2 ** min(failures, 5)) + random.random()
                    next_poll = now + interval
                    earliest_hint_poll = now + 0.25
                    next_local_check = now + (0.1 if self.state.get("pending_settings") else 2 + random.random())
                    local_failures = 0
                now = self.monotonic()
                # Heartbeats stay on their own 30-second cadence, including outages.
                if now - last_heartbeat >= 30:
                    try:
                        self.heartbeat()
                    except (CloudDeviceError, CloudRuntimeError, OSError, ValueError):
                        pass
                    last_heartbeat = self.monotonic()
                connected = events is not None and events.connected.is_set()
                if connected and not failures and self.monotonic() >= next_local_check:
                    try:
                        self._maintain_local()
                        local_failures = 0
                    except (CloudDeviceError, CloudRuntimeError, OSError, ValueError):
                        local_failures += 1
                        print("cloud local work failed; receipts remain pending", flush=True)
                    delay = min(60, 2 * 2 ** min(local_failures, 5)) + random.random()
                    next_local_check = self.monotonic() + delay
                deadline = min(next_poll, last_heartbeat + 30)
                if connected and not failures:
                    deadline = min(deadline, next_local_check)
                if wake.is_set() and not failures:
                    deadline = min(deadline, earliest_hint_poll)
                # An external stop is observed within one second; socket shutdown is bounded.
                timeout = max(0.01, min(1, deadline - self.monotonic()))
                if wake.is_set():
                    stop.wait(timeout)  # Coalesced hints must not create a busy loop.
                else:
                    wake.wait(timeout)
        finally:
            if events is not None:
                events.stop()


def read_snapshot():
    """Read fresh authorization without creating identity or contacting the service."""
    snapshot = CloudRuntime(client=object(), state_path=STATE_FILE)._snapshot()
    people = snapshot.get("people")
    if (not isinstance(people, list) or len(people) > 100
            or any(not isinstance(person, dict) or not _valid_id(person.get("id"))
                   for person in people)):
        raise CloudRuntimeError("cloud family list is invalid")
    ids = {person["id"] for person in people}
    if len(ids) != len(people):
        raise CloudRuntimeError("cloud family list is invalid")
    ids.update(box["id"] for box in snapshot.get("connected_boxes", []))
    default = snapshot.get("default_recipient_id")
    if default is not None and (not _valid_id(default) or default not in ids):
        raise CloudRuntimeError("cloud default recipient is invalid")
    entitlement = snapshot.get("entitlement")
    if (not isinstance(entitlement, dict)
            or any(type(entitlement.get(key)) is not bool for key in ("ingest", "deliver", "send"))
            or (entitlement.get("until") is not None and not _valid_time(entitlement["until"]))):
        raise CloudRuntimeError("cloud entitlement is invalid")
    return snapshot


def recipient_id(jid):
    return CloudRuntime().recipient_id(jid)


def playable(metadata):
    return CloudRuntime().playable(metadata)


def record_played(metadata):
    if metadata and metadata.get("cloud") is True and _valid_id(metadata.get("cloud_operation_id")):
        runtime = CloudRuntime()
        runtime._ack(metadata["cloud_operation_id"], "played")


def listened_status(metadata):
    """Check local authorization only; the button owner never downloads media."""
    return CloudRuntime(client=object()).listened_status(metadata)


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
    return runtime.server_now() + snapshot["retention_days"] * 86400


def outbox_now():
    runtime = CloudRuntime(client=object())
    return runtime.server_now()
