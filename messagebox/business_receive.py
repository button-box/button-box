"""Poll the Business bench's acknowledged audio inbox into the Pi WAV queue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from messagebox.business_send import BusinessSendClient, BUSINESS_USER_AGENT, _OPEN_REQUEST
from messagebox.contacts import ContactStore
from messagebox.played_history import played_history_lock
from messagebox.runtime_paths import CONTACTS_FILE, QUEUE_DIR, STATE_DIR
from messagebox.voicepoll import message_is_authorized, queue_message


MAX_AUDIO_BYTES = 12 * 1024 * 1024
_PHONE = re.compile(r"^[1-9][0-9]{6,14}$")
_UPLOAD_KEY = re.compile(r"^up:[A-Za-z0-9:._-]{1,240}$")
_LEDGER = STATE_DIR / "business-queued.json"


class BusinessReceiveError(Exception):
    """Keep the server item unacknowledged for a later poll or operator review."""


class BusinessInboxClient:
    def __init__(self, config: BusinessSendClient, *, opener=None):
        self.config = config
        self.open_request = opener or _OPEN_REQUEST

    @classmethod
    def from_environment(cls):
        return cls(BusinessSendClient.from_environment())

    def _request(self, path: str, *, body=None, limit=128 * 1024) -> bytes:
        # Cloudflare rejects urllib's default agent before it reaches the Worker.
        headers = {
            "Authorization": "Bearer " + self.config.token,
            "User-Agent": BUSINESS_USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.config.api_url + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=headers,
            method="GET" if body is None else "POST",
        )
        try:
            with self.open_request(request, timeout=30) as response:
                if response.status != 200:
                    raise BusinessReceiveError("business inbox is unavailable")
                payload = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise BusinessReceiveError("business inbox is unavailable") from exc
        if len(payload) > limit:
            raise BusinessReceiveError("business inbox response is too large")
        return payload

    def inbox(self) -> tuple[int, list[dict]]:
        raw = self._request("/api/device-inbox?box=" + self.config.box)
        try:
            result = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BusinessReceiveError("business inbox response is invalid") from exc
        cursor = result.get("cursor") if isinstance(result, dict) else None
        entries = result.get("messages") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("box") != self.config.box
            or type(cursor) is not int
            or cursor < 0
            or not isinstance(entries, list)
            or len(entries) > 20
        ):
            raise BusinessReceiveError("business inbox response is invalid")
        for expected, entry in enumerate(entries, cursor + 1):
            message = entry.get("message") if isinstance(entry, dict) else None
            audio = message.get("audio") if isinstance(message, dict) else None
            if (
                not isinstance(entry, dict)
                or type(entry.get("cursor")) is not int
                or entry["cursor"] != expected
                or not isinstance(message, dict)
                or message.get("type") != "audio"
                or not isinstance(message.get("wamid"), str)
                or not 0 < len(message["wamid"]) <= 500
                or not isinstance(message.get("from"), str)
                or not _PHONE.fullmatch(message["from"])
                or type(message.get("timestamp")) is not int
                or not 0 <= message["timestamp"] < 4102444800
                or not isinstance(audio, dict)
                or not isinstance(audio.get("uploadKey"), str)
                or not _UPLOAD_KEY.fullmatch(audio["uploadKey"])
            ):
                raise BusinessReceiveError("business inbox message is invalid")
        return cursor, entries

    def download(self, upload_key: str) -> bytes:
        if not _UPLOAD_KEY.fullmatch(upload_key):
            raise BusinessReceiveError("business media key is invalid")
        return self._request(
            "/api/upload/" + upload_key[3:], limit=MAX_AUDIO_BYTES
        )

    def ack(self, cursor: int, wamid: str) -> None:
        raw = self._request(
            "/api/device-ack",
            body={"box": self.config.box, "cursor": cursor, "wamid": wamid},
        )
        try:
            result = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BusinessReceiveError("business acknowledgement is invalid") from exc
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("box") != self.config.box
            or type(result.get("cursor")) is not int
            or result["cursor"] < cursor
        ):
            raise BusinessReceiveError("business acknowledgement is invalid")


def _hash_id(wamid: str) -> str:
    return hashlib.sha256(wamid.encode("utf-8")).hexdigest()


def _load_ledger(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    if not isinstance(data, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
        for item in data
    ):
        raise BusinessReceiveError("business queue ledger is invalid")
    return set(data)


def _save_ledger(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(sorted(seen), handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _already_queued(queue_dir: Path, name: str, wamid: str, jid: str) -> bool:
    with played_history_lock(queue_dir):
        for folder in (queue_dir, queue_dir / ".inflight", queue_dir / ".hold",
                       queue_dir / ".trash", queue_dir / ".played"):
            wav = folder / name
            if not wav.exists():
                continue
            try:
                metadata = json.loads(Path(str(wav) + ".json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BusinessReceiveError("business queued media has no valid route") from exc
            if metadata.get("msgid") != wamid or metadata.get("chat") != jid:
                raise BusinessReceiveError("business queued media route mismatch")
            return True
    return False


def poll_once(
    client: BusinessInboxClient, *, queue_dir: Path = QUEUE_DIR,
    ledger_path: Path = _LEDGER, contacts_path: Path = CONTACTS_FILE,
    queue_func=queue_message,
) -> int:
    # A corrupt or unavailable contact store must not acknowledge any item.
    contacts = ContactStore(contacts_path).load()["contacts"]
    authorizations = {jid: contact["receive_after"] for jid, contact in contacts.items()}
    cursor, entries = client.inbox()
    seen = _load_ledger(ledger_path)
    queued = 0
    for entry in entries:
        cursor = entry["cursor"]
        message = entry["message"]
        wamid = message["wamid"]
        jid = message["from"] + "@s.whatsapp.net"
        media = {
            "ChatJID": jid, "SenderJID": jid, "MsgID": wamid,
            "Timestamp": message["timestamp"], "MediaType": "audio",
        }
        if not message_is_authorized(media, authorizations):
            client.ack(cursor, wamid)
            continue
        digest = _hash_id(wamid)
        name = f"{message['timestamp'] * 1000:013d}-{digest[:24]}.wav"
        if digest not in seen:
            if not _already_queued(queue_dir, name, wamid, jid):
                audio = client.download(message["audio"]["uploadKey"])
                if not audio:
                    raise BusinessReceiveError("business media is empty")
                queue_dir.mkdir(parents=True, exist_ok=True)
                temporary = queue_dir / f".business-{uuid.uuid4().hex}.part"
                try:
                    with temporary.open("wb") as handle:
                        os.chmod(temporary, 0o600)
                        handle.write(audio)
                        handle.flush()
                        os.fsync(handle.fileno())
                    queue_func({**media, "QueueFilename": name}, temporary, queue_dir=queue_dir)
                finally:
                    temporary.unlink(missing_ok=True)
                queued += 1
            seen.add(digest)
            _save_ledger(ledger_path, seen)
        client.ack(cursor, wamid)
    return queued


def main() -> None:
    client = BusinessInboxClient.from_environment()
    while True:
        try:
            poll_once(client)
        except Exception:
            print("business inbox poll failed; item remains unacknowledged", flush=True)
        time.sleep(3)
