"""Private ten-minute cloud claim handoff and physical-button confirmation."""

from __future__ import annotations

import fcntl
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from messagebox.cloud_device import CLAIM_FILE, CloudDeviceClient, CloudDeviceError, atomic_json, capabilities, open_private_lock
from messagebox.runtime_paths import NFC_HEALTH_FILE
from messagebox.qrcodegen import QrCode

_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class CloudClaimError(Exception):
    """A safe claim error without tokens or device identity."""


class CloudClaimClockError(CloudClaimError):
    """Home Wi-Fi is ready but the box clock is still catching up."""


class CloudClaim:
    def __init__(self, client=None, *, path=CLAIM_FILE, clock=time.time):
        self.client = client or CloudDeviceClient.from_environment()
        self.path = Path(path)
        self.clock = clock
        self.lock_path = self.path.with_name(".claim.lock")

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CloudClaimError("claim state is invalid") from exc
        if (not isinstance(value, dict) or not isinstance(value.get("claim_id"), str)
                or not _ID.fullmatch(value["claim_id"])
                or type(value.get("expires_at")) is not int
                or type(value.get("physical_confirmed")) is not bool
                or not isinstance(value.get("whatsapp_url"), str)
                or type(value.get("cancel_pending", False)) is not bool):
            raise CloudClaimError("claim state is invalid")
        return value

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = open_private_lock(self.lock_path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return lock

    def start(self):
        with self._locked():
            prior = self._read()
            if prior and (prior.get("cancel_pending") or prior["expires_at"] > self.clock()):
                return self._public(prior)
            # Setup cannot read the runtime user's private health directory.
            # Optional hardware discovery must not prevent claiming the box;
            # the runtime heartbeat reports its capabilities after setup.
            try:
                nfc = NFC_HEALTH_FILE.exists()
            except OSError:
                nfc = False
            try:
                result = self.client.register(capabilities(nfc=nfc))
            except CloudDeviceError as exc:
                raise CloudClaimError("cloud registration is unavailable") from exc
            if result.get("claimed") is True:
                self.path.unlink(missing_ok=True)
                return {"status": "claimed"}
            claim_id, token = result.get("claim_id"), result.get("claim_token")
            link, expires = result.get("whatsapp_url"), result.get("expires_at")
            if (not isinstance(claim_id, str) or not _ID.fullmatch(claim_id)
                    or not isinstance(token, str) or not _TOKEN.fullmatch(token)
                    or type(expires) is not int or expires <= self.clock()
                    or not isinstance(link, str)):
                raise CloudClaimError("cloud registration response is invalid")
            parsed = urlsplit(link)
            text = parse_qs(parsed.query).get("text", [])
            if (parsed.scheme != "https" or parsed.netloc != "wa.me"
                    or not re.fullmatch(r"/[1-9][0-9]{6,14}", parsed.path)
                    or len(text) != 1 or text[0] != "claim " + token
                    or parsed.fragment or parsed.username or parsed.password):
                raise CloudClaimError("cloud claim link is invalid")
            # A Pi without a battery clock can reach Cloud before NTP corrects
            # its time. Keep the expiry guard; give setup a retryable reason.
            if expires > self.clock() + 605:
                raise CloudClaimClockError("cloud clock is not ready")
            document = {"claim_id": claim_id, "expires_at": expires,
                        "whatsapp_url": link, "physical_confirmed": False}
            atomic_json(self.path, document)
            return self._public(document)

    def _public(self, document):
        if document.get("cancel_pending"):
            return {"status": "cancellation_pending", "claim_id": document["claim_id"]}
        return {"status": "waiting_for_whatsapp" if document["physical_confirmed"] else "awaiting_button",
                "claim_id": document["claim_id"],
                "whatsapp_url": document["whatsapp_url"],
                "expires_at": document["expires_at"]}

    def status(self):
        # Do not hold the button's lock across a read-only network poll.
        # Compare the claim again before using the response for cleanup.
        with self._locked():
            document = self._read()
        try:
            remote = self.client.claim()
        except CloudDeviceError as exc:
            if document is None:
                return {"status": "not_started"}
            raise CloudClaimError("cloud claim status is unavailable") from exc
        with self._locked():
            current = self._read()
            if (document or {}).get("claim_id") != (current or {}).get("claim_id"):
                return self._public(current) if current else {"status": "not_started"}
            if remote.get("claimed") is True:
                self.path.unlink(missing_ok=True)
                return {"status": "claimed"}
            if current is None:
                return {"status": "not_started"}
            if not current.get("cancel_pending") and current["expires_at"] <= self.clock():
                return {"status": "expired"}
            return self._public(current)

    def cancel(self, claim_id):
        if not isinstance(claim_id, str) or not _ID.fullmatch(claim_id):
            raise CloudClaimError("claim ID is invalid")
        with self._locked():
            document = self._read()
            if document is not None and document["claim_id"] != claim_id:
                raise CloudClaimError("connection link changed; check its current status")
            if document is not None:
                # Save intent before the remote boundary. A timeout/restart must
                # not revive the link or turn a retry into a recording press.
                document["cancel_pending"] = True
                atomic_json(self.path, document)
            try:
                result = self.client.cancel_claim(claim_id)
            except CloudDeviceError as exc:
                raise CloudClaimError("cloud cancellation is unconfirmed; retry cancellation") from exc
            if (result.get("claim_id") != claim_id or type(result.get("claimed")) is not bool
                    or type(result.get("cancelled")) is not bool
                    or result["claimed"] == result["cancelled"]):
                raise CloudClaimError("cloud cancellation response is invalid; retry cancellation")
            self.path.unlink(missing_ok=True)
            return {"status": "claimed" if result["claimed"] else "cancelled"}

    def qr_svg(self):
        document = self._read()
        if document is None or document.get("cancel_pending") or document["expires_at"] <= self.clock():
            raise CloudClaimError("claim is unavailable")
        qr = QrCode.encode_text(document["whatsapp_url"], QrCode.Ecc.MEDIUM)
        size = qr.get_size()
        cells = []
        for y in range(size):
            for x in range(size):
                if qr.get_module(x, y):
                    cells.append(f"M{x+4},{y+4}h1v1h-1z")
        path = "".join(cells)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size+8} {size+8}" '
                f'role="img" aria-label="WhatsApp claim QR code"><rect width="100%" height="100%" '
                f'fill="white"/><path d="{path}" fill="black"/></svg>').encode("ascii")

    def consume_press(self):
        """Return true only while explicit claim mode owns this button press."""
        with self._locked():
            document = self._read()
            if document is not None and document.get("cancel_pending"):
                return True
            if document is None or document["expires_at"] <= self.clock():
                return False
            if document["physical_confirmed"]:
                return True
            try:
                result = self.client.confirm_claim(document["claim_id"])
            except CloudDeviceError:
                return True  # Keep claim mode; a later press may retry safely.
            if result.get("claimed") is True:
                self.path.unlink(missing_ok=True)
            elif result.get("claimed") is False:
                document["physical_confirmed"] = True
                atomic_json(self.path, document)
            else:
                raise CloudClaimError("cloud claim confirmation is invalid")
            return True


def consume_claim_press():
    if not CLAIM_FILE.exists():
        return False
    try:
        return CloudClaim().consume_press()
    except (CloudClaimError, CloudDeviceError, OSError):
        return True  # A corrupt active claim never becomes a recording press.
