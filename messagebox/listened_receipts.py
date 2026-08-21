#!/usr/bin/env python3
"""Durable WhatsApp voice-note played receipts for Message Box.

The wacli process posts signed receipt events to the dashboard.  This module
correlates those events with voice notes sent by the box and creates a separate
acknowledgement queue for the button/audio owner.  It intentionally never puts
an acknowledgement into the incoming family-message queue.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from messagebox.contacts import ContactError, ContactStore


MAX_WEBHOOK_BYTES = 64 * 1024
DEFAULT_RETENTION_DAYS = 45


class AnnouncementGate:
    """Throttle idle receipt checks and back off after an audio failure."""

    def __init__(self, poll_seconds: float = 0.2, retry_seconds: float = 30.0):
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.retry_seconds = max(self.poll_seconds, float(retry_seconds))
        self.next_check = 0.0
        self.retry_after = 0.0

    def ready(self, *, busy: bool, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        if busy or now < self.next_check or now < self.retry_after:
            return False
        self.next_check = now + self.poll_seconds
        return True

    def blocked(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        self.retry_after = now + self.retry_seconds

    def succeeded(self) -> None:
        self.retry_after = 0.0


def parse_wacli_send_id(raw: str) -> str | None:
    """Return the WhatsApp message ID from a successful ``--json`` send."""
    try:
        payload = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("sent") is not True:
        return None
    message_id = payload.get("id")
    if not isinstance(message_id, str):
        return None
    message_id = message_id.strip()
    return message_id if 1 <= len(message_id) <= 256 else None


def webhook_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def valid_webhook_signature(secret: str, body: bytes, supplied: str | None) -> bool:
    if not secret or not supplied:
        return False
    return hmac.compare_digest(webhook_signature(secret, body), supplied.strip())


def canonical_jid(raw: str | None) -> str:
    """Normalize a participant JID and remove a linked-device suffix."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip().lower()
    if len(value) > 320 or "@" not in value:
        return ""
    user, server = value.rsplit("@", 1)
    if not user or not server:
        return ""
    if ":" in user:
        user = user.split(":", 1)[0]
    return f"{user}@{server}"


def _safe_text(raw, fallback: str, limit: int = 80) -> str:
    if not isinstance(raw, str):
        return fallback
    value = " ".join(raw.split()).strip()
    return value[:limit] or fallback


def load_listener_profiles(path: str) -> dict[str, dict[str, str]]:
    """Load ``JID -> {name, clip}`` mappings from the unified contact store."""
    try:
        return ContactStore(path).listener_profiles()
    except ContactError:
        return {}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _create_json_once(path: Path, payload: dict) -> bool:
    """Create one durable JSON file without replacing an existing receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _fsync_dir(path.parent)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _key(*parts: str) -> str:
    joined = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


@dataclass(frozen=True)
class PlayedNotice:
    notice_id: str
    path: Path
    whatsapp_id: str
    listener_jid: str
    listener_name: str
    clip: str
    received_at: float


class ReceiptStore:
    """File-backed sent-message index and idempotent announcement queue."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.sent = self.root / "sent"
        self.pending = self.root / "pending"
        self.inflight = self.root / "inflight"
        self.seen = self.root / "seen"
        for path in (self.sent, self.pending, self.inflight, self.seen):
            path.mkdir(parents=True, exist_ok=True)

    def track_sent(
        self,
        whatsapp_id: str,
        chat: str,
        *,
        local_message_id: str | None = None,
        flow: str | None = None,
        sent_at: float | None = None,
    ) -> bool:
        whatsapp_id = (whatsapp_id or "").strip()
        chat = canonical_jid(chat)
        if not whatsapp_id or len(whatsapp_id) > 256 or not chat:
            return False
        payload = {
            "version": 1,
            "whatsapp_id": whatsapp_id,
            "chat": chat,
            "local_message_id": _safe_text(local_message_id, "", 128),
            "flow": _safe_text(flow, "", 32),
            "sent_at": time.time() if sent_at is None else float(sent_at),
        }
        _write_json_atomic(self.sent / f"{_key(whatsapp_id)}.json", payload)
        self.prune()
        return True

    def _tracked(self, whatsapp_id: str) -> dict | None:
        try:
            with open(self.sent / f"{_key(whatsapp_id)}.json", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return None
        if payload.get("whatsapp_id") != whatsapp_id:
            return None
        return payload

    def ingest_played(
        self,
        payload: dict,
        profiles: dict[str, dict[str, str]],
        fallback_clip: str,
        *,
        received_at: float | None = None,
    ) -> list[PlayedNotice]:
        """Validate and enqueue the tracked IDs in one wacli ``played`` event."""
        if not isinstance(payload, dict):
            raise ValueError("receipt payload must be an object")
        if payload.get("EventType") != "receipt" or payload.get("Type") != "played":
            return []
        chat = canonical_jid(payload.get("Chat"))
        listener_jid = canonical_jid(payload.get("Sender")) or chat
        raw_ids = payload.get("MessageIDs")
        if not chat or not listener_jid or not isinstance(raw_ids, list):
            raise ValueError("played receipt is missing chat, sender, or message IDs")
        if len(raw_ids) > 256:
            raise ValueError("played receipt contains too many message IDs")

        profile = profiles.get(listener_jid, {})
        listener_name = _safe_text(profile.get("name"), "Someone")
        clip = profile.get("clip") or fallback_clip
        clip = os.path.expanduser(clip) if isinstance(clip, str) else ""
        now = time.time() if received_at is None else float(received_at)
        created = []
        for raw_id in raw_ids:
            if not isinstance(raw_id, str):
                continue
            whatsapp_id = raw_id.strip()
            if not whatsapp_id or len(whatsapp_id) > 256:
                continue
            tracked = self._tracked(whatsapp_id)
            if not tracked or canonical_jid(tracked.get("chat")) != chat:
                continue
            notice_id = _key(whatsapp_id, listener_jid, "played")
            if (self.seen / f"{notice_id}.json").exists() or (
                self.inflight / f"{notice_id}.json"
            ).exists():
                continue
            notice_payload = {
                "version": 1,
                "notice_id": notice_id,
                "whatsapp_id": whatsapp_id,
                "listener_jid": listener_jid,
                "listener_name": listener_name,
                "clip": clip,
                "received_at": now,
            }
            path = self.pending / f"{notice_id}.json"
            if not _create_json_once(path, notice_payload):
                continue
            created.append(self.load(path))
        return created

    def load(self, path: Path) -> PlayedNotice:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return PlayedNotice(
            notice_id=data["notice_id"],
            path=path,
            whatsapp_id=data["whatsapp_id"],
            listener_jid=data["listener_jid"],
            listener_name=_safe_text(data.get("listener_name"), "Someone"),
            clip=str(data.get("clip") or ""),
            received_at=float(data.get("received_at") or 0),
        )

    def pending_count(self) -> int:
        return sum(1 for _ in self.pending.glob("*.json"))

    def claim_next(self) -> PlayedNotice | None:
        for source in sorted(self.pending.glob("*.json"), key=lambda path: path.stat().st_mtime):
            target = self.inflight / source.name
            try:
                os.replace(source, target)
            except FileNotFoundError:
                continue
            return self.load(target)
        return None

    def complete(self, notice: PlayedNotice, announced_at: float | None = None) -> None:
        with open(notice.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["announced_at"] = time.time() if announced_at is None else float(announced_at)
        _write_json_atomic(notice.path, payload)
        os.replace(notice.path, self.seen / notice.path.name)
        _fsync_dir(self.seen)

    def release(self, notice: PlayedNotice) -> None:
        if notice.path.exists():
            os.replace(notice.path, self.pending / notice.path.name)
            _fsync_dir(self.pending)

    def recover_inflight(self) -> int:
        recovered = 0
        for source in self.inflight.glob("*.json"):
            target = self.pending / source.name
            if target.exists():
                source.unlink()
            else:
                os.replace(source, target)
                recovered += 1
        if recovered:
            _fsync_dir(self.pending)
        return recovered

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        cutoff = time.time() - max(1, retention_days) * 86400
        for directory in (self.sent, self.seen):
            for path in directory.glob("*.json"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except FileNotFoundError:
                    pass
