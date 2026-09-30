"""Outbound voice-note adapter for the existing WhatsApp Business bench API.

This module has no device setup or inbound polling. Callers retain the approved
audio and recipient locally before crossing the HTTP side-effect boundary.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from messagebox.device_http import DEVICE_USER_AGENT, NoRedirect

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
BUSINESS_USER_AGENT = DEVICE_USER_AGENT
_PERSON_JID = re.compile(r"^([1-9][0-9]{6,14})@s\.whatsapp\.net$")
_TOKEN = re.compile(r"^[^\s]{1,512}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,100}$")


_OPEN_REQUEST = urllib.request.build_opener(NoRedirect()).open


class BusinessSendRejected(Exception):
    """The request was not sent or was explicitly rejected by the bench API."""


class BusinessSendUncertain(Exception):
    """A send may have succeeded; automatic retry could duplicate it."""


def recipient_number(jid: str) -> str:
    """Convert only an exact person JID; groups and malformed routes fail closed."""
    match = _PERSON_JID.fullmatch(jid) if isinstance(jid, str) else None
    if match is None:
        raise BusinessSendRejected("unsupported recipient")
    return match.group(1)


@dataclass(frozen=True)
class BusinessSendClient:
    api_url: str
    box: str
    token: str

    @classmethod
    def from_environment(cls, environ=None) -> BusinessSendClient:
        environ = os.environ if environ is None else environ
        api_url = str(environ.get("MSGBOX_BUSINESS_API_URL", "")).rstrip("/")
        box = str(environ.get("MSGBOX_BUSINESS_BOX", ""))
        token_file = str(environ.get("MSGBOX_BUSINESS_TOKEN_FILE", ""))
        parsed = urlsplit(api_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or box not in {"A", "B", "C"}
            or not token_file
        ):
            raise BusinessSendRejected("business transport is not configured")
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BusinessSendRejected("business credential is unavailable") from exc
        if not _TOKEN.fullmatch(token):
            raise BusinessSendRejected("business credential is invalid")
        return cls(api_url, box, token)

    def send_voice(
        self, ogg_path: str | Path, recipient: str, idempotency_key: str, *, opener=None
    ) -> str:
        """Return the accepted Meta message ID or classify the failed boundary."""
        to = recipient_number(recipient)
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise BusinessSendRejected("outbox idempotency key is invalid")
        try:
            audio = Path(ogg_path).read_bytes()
        except OSError as exc:
            raise BusinessSendRejected("voice audio is unavailable") from exc
        if not audio.startswith(b"OggS") or len(audio) > MAX_UPLOAD_BYTES or not audio:
            raise BusinessSendRejected("voice audio is invalid")

        boundary = uuid.uuid4().hex
        fields = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"box\"\r\n\r\n{self.box}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"to\"\r\n\r\n{to}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"voice.ogg\"\r\n"
            "Content-Type: audio/ogg\r\n\r\n"
        ).encode("ascii")
        body = fields + audio + f"\r\n--{boundary}--\r\n".encode("ascii")
        if len(body) > MAX_UPLOAD_BYTES:
            raise BusinessSendRejected("voice audio is too large")
        request = urllib.request.Request(
            self.api_url + "/api/send-voice",
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "User-Agent": BUSINESS_USER_AGENT,
                "Idempotency-Key": idempotency_key,
                "Content-Type": "multipart/form-data; boundary=" + boundary,
            },
            method="POST",
        )
        open_request = opener or _OPEN_REQUEST
        try:
            with open_request(request, timeout=30) as response:
                status = response.status
                payload = response.read(4096)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {400, 401, 403, 413}:
                raise BusinessSendRejected("business send was rejected") from exc
            raise BusinessSendUncertain("business send outcome is unknown") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise BusinessSendUncertain("business send outcome is unknown") from exc

        if status != 200:
            raise BusinessSendUncertain("business send outcome is unknown")
        try:
            result = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise BusinessSendUncertain("business send response is invalid") from exc
        if not isinstance(result, dict):
            raise BusinessSendUncertain("business send response is invalid")
        message_id = result.get("message_id")
        if (
            result.get("ok") is not True
            or result.get("box") != self.box
            or not isinstance(message_id, str)
            or not message_id
        ):
            raise BusinessSendUncertain("business send response is invalid")
        return message_id
