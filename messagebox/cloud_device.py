"""Private device identity and bounded HTTPS client for Cloud API v1."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import secrets
import stat
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from messagebox.settings import RINGTONES
from messagebox.guided_reply import valid_account_scope
from messagebox.device_http import DEVICE_USER_AGENT, NoRedirect


CLOUD_DIR = Path("/var/lib/messagebox-cloud")
IDENTITY_FILE = CLOUD_DIR / "device.json"
CLAIM_FILE = CLOUD_DIR / "claim.json"
ACK_FILE = CLOUD_DIR / "pending-acks.json"
_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_PERSON_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9:._-]{1,240}$")
_VOICE_STATES = {"queued", "waiting_for_reply", "held_for_review", "accepted", "uncertain",
                 "delivered", "read", "failed", "expired", "deleted", "canceled"}
_OPENER = urllib.request.build_opener(NoRedirect()).open


class CloudDeviceError(Exception):
    """A safe error that never includes server data or credentials."""


class CloudSendRejected(CloudDeviceError):
    """A definitive rejection before a provider send."""


class CloudSendUncertain(CloudDeviceError):
    """The upload may have crossed the provider boundary."""


class CloudAckGone(CloudDeviceError):
    """A previously issued operation no longer exists on the server."""


class CloudVoiceNotFound(CloudDeviceError):
    """No outbound recording is bound to this idempotency key yet."""


def open_private_lock(path: Path):
    """Open a group-shared lock without chmoding another service user's file."""
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o660)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
    try:
        if created:
            os.fchmod(descriptor, 0o660)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o660
                or (os.geteuid() != 0 and metadata.st_gid not in {*os.getgroups(), os.getegid()})):
            raise CloudDeviceError("cloud lock is unsafe")
        return os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise


def atomic_json(path: Path, document: object) -> None:
    """Write a private JSON file durably without exposing a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o660)
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
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


class DeviceIdentityStore:
    def __init__(self, path: Path = IDENTITY_FILE):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def load_or_create(self) -> dict[str, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open_private_lock(self.lock_path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    source_fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except FileNotFoundError:
                    document = {
                        "device_id": secrets.token_urlsafe(24),
                        "credential": secrets.token_urlsafe(32),
                    }
                    atomic_json(self.path, document)
                else:
                    with os.fdopen(source_fd, "rb") as source:
                        information = os.fstat(source.fileno())
                        if (not stat.S_ISREG(information.st_mode)
                                or information.st_mode & 0o007
                                or information.st_size > 1024):
                            raise CloudDeviceError("device identity is invalid")
                        document = json.loads(source.read(1025).decode("utf-8"))
                if (
                    not isinstance(document, dict)
                    or set(document) != {"device_id", "credential"}
                    or not isinstance(document["device_id"], str)
                    or not _ID.fullmatch(document["device_id"])
                    or not isinstance(document["credential"], str)
                    or not _SECRET.fullmatch(document["credential"])
                ):
                    raise CloudDeviceError("device identity is invalid")
                return document
            except (OSError, ValueError) as exc:
                raise CloudDeviceError("device identity is unavailable") from exc
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class CloudDeviceClient:
    def __init__(self, api_url: str, identity: dict[str, str], *, opener=None):
        parsed = urlsplit(api_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/cloud-api/v1"
            or parsed.query
            or parsed.fragment
        ):
            raise CloudDeviceError("cloud API URL is invalid")
        if not isinstance(identity, dict) or not _ID.fullmatch(identity.get("device_id", "")) or not _SECRET.fullmatch(
            identity.get("credential", "")
        ):
            raise CloudDeviceError("device identity is invalid")
        self.api_url = api_url.rstrip("/")
        self.identity = identity
        self.open_request = opener or _OPENER

    @classmethod
    def from_environment(cls, environ=None, *, identity_store=None):
        environ = os.environ if environ is None else environ
        api_url = environ.get("MSGBOX_CLOUD_API_URL", "https://button.box/cloud-api/v1")
        return cls(api_url, (identity_store or DeviceIdentityStore()).load_or_create())

    def request(self, path: str, *, method="GET", body=None, limit=128 * 1024,
                ack_gone=False, voice_not_found=False) -> bytes:
        if not path.startswith("/device/") or ".." in path or "#" in path:
            raise CloudDeviceError("device request path is invalid")
        headers = {
            "Authorization": "Bearer " + self.identity["credential"],
            "User-Agent": DEVICE_USER_AGENT,
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            if len(data) > 128 * 1024:
                raise CloudDeviceError("device request is too large")
        request = urllib.request.Request(
            self.api_url + path, data=data, headers=headers, method=method
        )
        try:
            with self.open_request(request, timeout=30) as response:
                if response.status != 200:
                    raise CloudDeviceError("cloud request was rejected")
                payload = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            try:
                if (ack_gone or voice_not_found) and exc.code == 404:
                    try:
                        error = json.loads(exc.read(1025))
                    except (ValueError, OSError):
                        error = None
                    if (isinstance(error, dict) and isinstance(error.get("error"), dict)
                            and error["error"].get("code") == "operation_not_found" and ack_gone):
                        raise CloudAckGone("cloud operation has expired") from exc
                    if (isinstance(error, dict) and isinstance(error.get("error"), dict)
                            and error["error"].get("code") == "voice_upload_not_found" and voice_not_found):
                        raise CloudVoiceNotFound("voice upload is not found") from exc
            finally:
                exc.close()
            raise CloudDeviceError("cloud request was rejected") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise CloudDeviceError("cloud request failed") from exc
        if len(payload) > limit:
            raise CloudDeviceError("cloud response is too large")
        return payload

    def json(self, path: str, *, method="GET", body=None) -> dict:
        try:
            result = json.loads(self.request(path, method=method, body=body))
        except (ValueError, TypeError) as exc:
            raise CloudDeviceError("cloud response is invalid") from exc
        if not isinstance(result, dict):
            raise CloudDeviceError("cloud response is invalid")
        return result

    def register(self, capabilities: dict) -> dict:
        return self.json(
            "/device/register", method="POST",
            body={**self.identity, "capabilities": capabilities},
        )

    def claim(self) -> dict:
        return self.json("/device/claim")

    def setup_checkin(self) -> dict:
        return self.json("/device/setup-checkin", method="POST",
                         body={"device_id": self.identity["device_id"]})

    def confirm_claim(self, claim_id: str) -> dict:
        if not isinstance(claim_id, str) or not _ID.fullmatch(claim_id):
            raise CloudDeviceError("claim ID is invalid")
        return self.json("/device/claim/confirm", method="POST", body={"claim_id": claim_id})

    def cancel_claim(self, claim_id: str) -> dict:
        if not isinstance(claim_id, str) or not _ID.fullmatch(claim_id):
            raise CloudDeviceError("claim ID is invalid")
        return self.json("/device/claim/cancel", method="POST", body={"claim_id": claim_id})

    def heartbeat(self, state: dict) -> dict:
        return self.json("/device/heartbeat", method="POST", body=state)

    def inbox(self, cursor: int) -> dict:
        if type(cursor) is not int or cursor < 0:
            raise CloudDeviceError("inbox cursor is invalid")
        return self.json(f"/device/inbox?cursor={cursor}")

    def ack(self, ack: dict) -> dict:
        try:
            result = json.loads(self.request("/device/ack", method="POST", body=ack,
                                             ack_gone=True))
        except (ValueError, TypeError) as exc:
            raise CloudDeviceError("cloud response is invalid") from exc
        if not isinstance(result, dict):
            raise CloudDeviceError("cloud response is invalid")
        return result

    def voice_status(self, idempotency_key: str) -> dict:
        if not isinstance(idempotency_key, str) or not _ID.fullmatch(idempotency_key):
            raise CloudDeviceError("voice idempotency key is invalid")
        path = "/device/voice/status?idempotency_key=" + idempotency_key
        try:
            result = json.loads(self.request(path, voice_not_found=True))
        except (ValueError, TypeError) as exc:
            raise CloudDeviceError("voice status is invalid") from exc
        if (not isinstance(result, dict) or not isinstance(result.get("message_id"), str)
                or not _MESSAGE_ID.fullmatch(result["message_id"])
                or result.get("state") not in _VOICE_STATES
                or type(result.get("expires_at")) is not int
                or not 1_700_000_000 <= result["expires_at"] < 4_102_444_800
                or type(result.get("server_time")) is not int
                or not 1_700_000_000 <= result["server_time"] < 4_102_444_800
                or type(result.get("deleted")) is not bool):
            raise CloudDeviceError("voice status is invalid")
        return result

    def media(self, media_url: str, *, limit=10 * 1024 * 1024) -> bytes:
        """Fetch only an authenticated media path on this exact API origin."""
        parsed = urlsplit(media_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != urlsplit(self.api_url).netloc
            or not parsed.path.startswith(("/cloud-api/v1/device/media/",
                                           "/cloud-api/v1/device/listened-media/"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise CloudDeviceError("media URL is invalid")
        return self.request(parsed.path.removeprefix("/cloud-api/v1"), limit=limit)

    def send_voice(
        self, ogg_path: str | Path, recipient_id: str, idempotency_key: str,
        duration_seconds: float, *, account_scope: str, reply_to: str | None = None,
    ) -> dict:
        if (
            not valid_account_scope(account_scope)
            or not isinstance(recipient_id, str)
            or not _PERSON_ID.fullmatch(recipient_id)
            or not isinstance(idempotency_key, str)
            or not _ID.fullmatch(idempotency_key)
            or type(duration_seconds) not in (int, float)
            or not math.isfinite(duration_seconds)
            or not 0 < duration_seconds <= 120
            or (reply_to is not None and (not isinstance(reply_to, str) or not _MESSAGE_ID.fullmatch(reply_to)))
        ):
            raise CloudSendRejected("voice target or metadata is invalid")
        try:
            audio = Path(ogg_path).read_bytes()
        except OSError as exc:
            raise CloudSendRejected("voice audio is unavailable") from exc
        if not audio.startswith(b"OggS") or len(audio) > 10 * 1024 * 1024:
            raise CloudSendRejected("voice audio is invalid")
        boundary = secrets.token_hex(16)
        values = {
            "recipient_id": recipient_id,
            "account_scope": account_scope,
            "idempotency_key": idempotency_key,
            "duration_seconds": str(round(duration_seconds, 3)),
        }
        if reply_to is not None:
            values["reply_to"] = reply_to
        parts = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("ascii")
            for key, value in values.items()
        )
        parts += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"voice.ogg\"\r\n"
            "Content-Type: audio/ogg\r\n\r\n"
        ).encode("ascii")
        data = parts + audio + f"\r\n--{boundary}--\r\n".encode("ascii")
        if len(data) > 10 * 1024 * 1024:
            raise CloudSendRejected("voice audio is too large")
        request = urllib.request.Request(
            self.api_url + "/device/voice", data=data, method="POST",
            headers={
                "Authorization": "Bearer " + self.identity["credential"],
                "User-Agent": DEVICE_USER_AGENT,
                "Idempotency-Key": idempotency_key,
                "Content-Type": "multipart/form-data; boundary=" + boundary,
            },
        )
        try:
            with self.open_request(request, timeout=30) as response:
                status = response.status
                payload = response.read(4097)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {400, 401, 403, 404, 409, 413, 422}:
                raise CloudSendRejected("voice upload was rejected") from exc
            raise CloudSendUncertain("voice upload outcome is unknown") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise CloudSendUncertain("voice upload outcome is unknown") from exc
        if status not in {200, 202} or len(payload) > 4096:
            raise CloudSendUncertain("voice upload outcome is unknown")
        try:
            result = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise CloudSendUncertain("voice upload response is invalid") from exc
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("message_id"), str)
            or not _MESSAGE_ID.fullmatch(result["message_id"])
            or result.get("state") not in _VOICE_STATES
            or type(result.get("expires_at")) is not int
            or not 1_700_000_000 <= result["expires_at"] < 4_102_444_800
            or type(result.get("server_time")) is not int
            or not 1_700_000_000 <= result["server_time"] < 4_102_444_800
        ):
            raise CloudSendUncertain("voice upload response is invalid")
        return result


def capabilities(*, nfc=False) -> dict:
    return {
        "ringtones": list(RINGTONES),
        "recording_modes": ["tap_review", "hold_release"],
        "audio": True,
        "nfc": bool(nfc),
        "settings_version": 1,
        "swoosh_sound_enabled": True,
        "listened_announcements": True,
    }
