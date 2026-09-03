#!/usr/bin/env python3
"""Messaging-channel provider abstraction.

Both the poller (voicepoll.py/signalpoll.py) and the button service
(button_send.py) drive send/react/presence operations through a
``MessagingProvider`` implementation chosen by a contact's ``channel``
field, rather than calling ``wacli`` or an HTTP client directly. This keeps
WhatsApp's existing wacli subprocess behavior unchanged while giving Signal
(via signal-cli-rest-api) a parallel, independently testable implementation.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from messagebox.guided_reply import voice_send_command
from messagebox.listened_receipts import parse_wacli_send_id


class ProviderError(RuntimeError):
    """A provider request failed in a way callers should log and retry."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_message_id: str | None
    raw_stdout: str
    raw_stderr: str


class MessagingProvider:
    """Common interface implemented per messaging channel."""

    channel: str

    def send_voice(self, recipient: str, file_path: str, *, lock_wait: str) -> SendResult:
        raise NotImplementedError

    def react(
        self,
        chat: str,
        message_id: str,
        emoji: str,
        *,
        sender: str | None = None,
        lock_wait: str,
    ) -> None:
        """Best-effort; failures are swallowed by the caller."""
        raise NotImplementedError

    def set_presence(self, kind: str, recipient: str, *, lock_wait: str) -> None:
        """Best-effort; failures are swallowed by the caller."""
        raise NotImplementedError


class WhatsAppProvider(MessagingProvider):
    """Thin wrapper around the existing wacli subprocess calls.

    Behavior-identical to the inline calls previously in button_send.py:
    same arguments, same fire-and-forget semantics for react/presence.
    """

    channel = "whatsapp"

    def __init__(self, wacli_bin: str = "/usr/local/bin/wacli", *, run=None, popen=None):
        self.wacli_bin = wacli_bin
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen

    def send_voice(self, recipient, file_path, *, lock_wait):
        sent = self._run(
            voice_send_command(self.wacli_bin, file_path, recipient, lock_wait),
            capture_output=True,
            text=True,
        )
        return SendResult(
            ok=sent.returncode == 0,
            provider_message_id=parse_wacli_send_id(sent.stdout),
            raw_stdout=sent.stdout,
            raw_stderr=sent.stderr,
        )

    def react(self, chat, message_id, emoji, *, sender=None, lock_wait):
        command = [
            self.wacli_bin,
            "send",
            "react",
            "--to",
            chat,
            "--id",
            message_id,
            "--reaction",
            emoji,
            "--lock-wait",
            lock_wait,
        ]
        if sender:
            command += ["--sender", sender]
        self._popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set_presence(self, kind, recipient, *, lock_wait):
        subcommand = ["typing", "--media", "audio"] if kind == "recording" else ["paused"]
        self._popen(
            [self.wacli_bin, "presence", *subcommand, "--to", recipient, "--lock-wait", lock_wait],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class SignalProvider(MessagingProvider):
    """HTTP client for the bbernhard/signal-cli-rest-api container.

    Uses only the standard library so the runtime has no new third-party
    dependency. Endpoints follow the container's documented v1/v2 API:
    https://github.com/bbernhard/signal-cli-rest-api
    """

    channel = "signal"

    def __init__(self, base_url: str | None = None, number: str | None = None, *, opener=None, timeout=30):
        self.base_url = (base_url or os.environ.get("MSGBOX_SIGNAL_REST_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.number = number or os.environ.get("MSGBOX_SIGNAL_NUMBER", "")
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read()
                parsed = json.loads(body) if body else None
                return response.status, parsed
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body) if body else None
            except (TypeError, ValueError):
                parsed = None
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"signal-cli-rest-api request failed: {exc}") from exc

    def send_voice(self, recipient, file_path, *, lock_wait=None):
        try:
            with open(file_path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
        except OSError as exc:
            raise ProviderError(f"could not read {file_path}: {exc}") from exc
        try:
            status, body = self._request(
                "POST",
                "/v2/send",
                {
                    "message": "",
                    "number": self.number,
                    "recipients": [recipient],
                    "base64_attachments": [encoded],
                },
            )
        except ProviderError as exc:
            return SendResult(ok=False, provider_message_id=None, raw_stdout="", raw_stderr=str(exc))
        ok = 200 <= status < 300
        message_id = None
        if ok and isinstance(body, dict) and body.get("timestamp") is not None:
            message_id = str(body["timestamp"])
        return SendResult(
            ok=ok,
            provider_message_id=message_id,
            raw_stdout=json.dumps(body) if body is not None else "",
            raw_stderr="" if ok else json.dumps(body or {}),
        )

    def react(self, chat, message_id, emoji, *, sender=None, lock_wait=None):
        try:
            timestamp = int(message_id)
        except (TypeError, ValueError):
            return
        try:
            self._request(
                "POST",
                f"/v1/reactions/{self.number}",
                {
                    "reaction": emoji,
                    "recipient": chat,
                    "target_author": sender or chat,
                    "timestamp": timestamp,
                },
            )
        except ProviderError:
            pass

    def set_presence(self, kind, recipient, *, lock_wait=None):
        method = "PUT" if kind == "recording" else "DELETE"
        try:
            self._request(method, f"/v1/typing-indicator/{self.number}", {"recipient": recipient})
        except ProviderError:
            pass

    def receive(self) -> list[dict]:
        """Return new envelopes since the last call (receive-and-clear).

        Uses GET /v1/receive/{number}, which signal-cli-rest-api documents as
        a one-shot poll: undelivered messages are returned and considered
        delivered. Verify this against the deployed container version before
        relying on it in production, since receive semantics have changed
        across signal-cli-rest-api releases.
        """
        try:
            status, body = self._request("GET", f"/v1/receive/{self.number}")
        except ProviderError:
            return []
        if not (200 <= status < 300) or not isinstance(body, list):
            return []
        return body

    def fetch_attachment(self, attachment_id: str) -> bytes:
        url = f"{self.base_url}/v1/attachments/{attachment_id}"
        request = urllib.request.Request(url, method="GET")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"attachment fetch failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"attachment fetch failed: {exc}") from exc


def provider_for_channel(channel: str, **kwargs) -> MessagingProvider:
    """Return the provider implementation for a contact's ``channel`` field."""
    if channel == "signal":
        return SignalProvider(**kwargs)
    if channel == "whatsapp":
        return WhatsAppProvider(**kwargs)
    raise ValueError(f"unknown channel: {channel!r}")
