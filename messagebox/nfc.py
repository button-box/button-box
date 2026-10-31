#!/usr/bin/env python3
"""PN532 daemon, NFC contact enrollment, and recipient selection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from messagebox import sound_pack
from messagebox.contacts import ContactError, ContactStore
from messagebox.cloud_runtime import CONTACTS_FILE as CLOUD_CONTACTS_FILE
from messagebox.nfc_state import (
    AnnouncementStore,
    DEFAULT_ENROLLMENT_TTL_S,
    EnrollmentStore,
    NfcError,
    NfcRouter,
    REANNOUNCE_COOLDOWN_S,
    SelectionStore,
    normalize_uid,
    public_enrollment,
)
from messagebox.runtime_paths import (
    CONTACTS_FILE,
    NFC_ANNOUNCEMENT_FILE,
    NFC_ENROLLMENT_FILE,
    NFC_HEALTH_FILE,
    NFC_SELECTION_FILE,
)


def unknown_token_wav():
    """Resolve per tap so a voice pack change applies without a service restart."""
    override = os.environ.get("MSGBOX_UNKNOWN_TOKEN_WAV")
    return override if override is not None else str(sound_pack.voice_path("card-unknown"))
REMOVAL_GRACE_S = float(os.environ.get("MSGBOX_NFC_REMOVAL_GRACE_S", "0.8"))
REFRESH_S = float(os.environ.get("MSGBOX_NFC_REFRESH_S", "0.75"))
READ_TIMEOUT_S = float(os.environ.get("MSGBOX_NFC_READ_TIMEOUT_S", "0.2"))
HEALTH_INTERVAL_S = 2.0


def mark_healthy(path=NFC_HEALTH_FILE):
    path = os.fspath(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    os.utime(path, None)


class PN532I2CReader:
    """Waveshare PN532 HAT adapter for the installed Raspberry Pi wiring."""

    def __init__(self, timeout=READ_TIMEOUT_S):
        try:
            import board
            import busio
            from adafruit_pn532.i2c import PN532_I2C
            from digitalio import DigitalInOut
        except ImportError as exc:
            raise RuntimeError(
                "PN532 dependencies are missing; install adafruit-circuitpython-pn532"
            ) from exc
        reset_name = os.environ.get("MSGBOX_NFC_RESET_PIN", "D20")
        request_name = os.environ.get("MSGBOX_NFC_REQUEST_PIN", "D16")
        try:
            reset = DigitalInOut(getattr(board, reset_name))
            request = DigitalInOut(getattr(board, request_name))
        except AttributeError as exc:
            raise RuntimeError("invalid PN532 reset/request board pin") from exc
        i2c = busio.I2C(board.SCL, board.SDA)
        self.device = PN532_I2C(i2c, debug=False, reset=reset, req=request)
        self.device.SAM_configuration()
        self.timeout = timeout

    def read(self):
        return self.device.read_passive_target(timeout=self.timeout)


def hardware_reader():
    return PN532I2CReader()


class Announcer:
    def __init__(self, store=None):
        self.store = store or AnnouncementStore(NFC_ANNOUNCEMENT_FILE)

    def announce(self, result, *, card_switch=False):
        if not result.announce or result.uid is None:
            return
        if result.contact is not None:
            prompt = result.contact.get("card_clip", "")
        elif result.action == "unknown":
            prompt = unknown_token_wav()
        else:
            return
        self.store.put(action=result.action, uid=result.uid, prompt=prompt, card_switch=card_switch)


class NfcRuntime:
    """Debounce presentations while maintaining a fail-closed scan latch."""

    def __init__(
        self,
        router,
        announcer,
        *,
        removal_grace=REMOVAL_GRACE_S,
        refresh=REFRESH_S,
        reannounce_cooldown=REANNOUNCE_COOLDOWN_S,
    ):
        self.router = router
        self.announcer = announcer
        self.removal_grace = removal_grace
        self.refresh = refresh
        self.reannounce_cooldown = reannounce_cooldown
        self.uid, self.resuming_presence = self.router.selection.presentation()
        self.last_seen = None
        self.last_refresh = None
        self.absent_since = None
        self.last_presented_uid = self.router.announcements.last_presentation()

    def _announce(self, result, now, *, previous_uid=None):
        # Routing still sees every presentation; only repeated selection audio
        # is suppressed. Enrollment feedback must always reach the audio owner.
        if not result.announce:
            # A held card resumes silently after restart, but the button must
            # also retain its confirmation before the next recording press.
            if result.action in {"refreshed", "recognized"}:
                self.router.announcements.reuse_confirmation(
                    result.uid, now=now, cooldown=self.reannounce_cooldown
                )
            return result
        # Cloud reaction handoffs trigger sends, with their own per-message
        # deduplication in the audio owner, rather than selecting a recipient.
        reaction = (
            os.environ.get("MSGBOX_TRANSPORT") == "cloud"
            and result.contact is not None
            and result.contact["jid"].startswith("box:reaction_")
        )
        if (
            result.action in {"selected", "recognized"}
            and not reaction
            and previous_uid in (None, result.uid)
            and (
                self.router.announcements.reuse_confirmation(
                    result.uid, now=now, cooldown=self.reannounce_cooldown
                ) or self.router.announcements.pending_for(result.uid)
            )
        ):
            return replace(result, announce=False)
        self.announcer.announce(result, card_switch=previous_uid not in (None, result.uid))
        return result

    def observe(self, raw_uid, now):
        if raw_uid is None:
            if self.last_seen is None and (self.resuming_presence or self.router.selection.presentation()[1]):
                # A restarted reader must prove absence before rearming a card.
                if self.absent_since is None:
                    self.absent_since = now
                if now - self.absent_since >= self.removal_grace:
                    result = self.router.card_absent()
                    self.uid = None
                    self.resuming_presence = False
                    self.absent_since = None
                    return result
                return None
            if (
                self.uid is not None
                and self.last_seen is not None
                and now - self.last_seen >= self.removal_grace
            ):
                result = self.router.card_absent()
                self.uid = self.last_seen = self.last_refresh = None
                return result
            return None
        self.absent_since = None
        uid = normalize_uid(raw_uid)
        if self.uid != uid or self.resuming_presence:
            new_presentation = self.uid != uid and not (self.resuming_presence and self.uid is None)
            result = self.router.card_seen(uid, new_presentation=new_presentation)
            previous_uid = self.last_presented_uid
            if previous_uid != uid:
                self.router.announcements.record_presentation(uid)
                self.last_presented_uid = uid
            self.resuming_presence = False
            self.uid = uid
            self.last_seen = self.last_refresh = now
            return self._announce(result, now, previous_uid=previous_uid)
        self.last_seen = now
        if self.last_refresh is None or now - self.last_refresh >= self.refresh:
            result = self.router.card_seen(uid, new_presentation=False)
            self.last_refresh = now
            if result.action == "unknown":
                return None
            return self._announce(result, now)
        return None


def router(announcement_store=None):
    return NfcRouter(
        ContactStore(CLOUD_CONTACTS_FILE if os.environ.get("MSGBOX_TRANSPORT") == "cloud" else CONTACTS_FILE),
        SelectionStore(NFC_SELECTION_FILE),
        EnrollmentStore(NFC_ENROLLMENT_FILE),
        announcement_store,
    )


def run_daemon():
    if (
        REMOVAL_GRACE_S <= 0 or REFRESH_S <= 0 or READ_TIMEOUT_S <= 0
        or not 0 < REANNOUNCE_COOLDOWN_S < float("inf")
    ):
        raise NfcError("NFC timing values must be positive")
    announcement_store = AnnouncementStore(NFC_ANNOUNCEMENT_FILE)
    nfc_router = router(announcement_store)
    announcer = Announcer(announcement_store)
    runtime = NfcRuntime(nfc_router, announcer)
    announcement_store.clear(preserve_claimed=True)
    try:
        reader = hardware_reader()
        recovered = nfc_router.reconcile_enrollment(new_presentation=False)
        print("NFC reader ready: transport=i2c", flush=True)
        if recovered is not None:
            print(f"NFC enrolled: {recovered.contact['label']}", flush=True)
        last_health = 0.0
        while True:
            now = time.monotonic()
            if now - last_health >= HEALTH_INTERVAL_S:
                mark_healthy(NFC_HEALTH_FILE)
                last_health = now
            uid = reader.read()
            result = runtime.observe(
                bytes(uid) if uid is not None else None, time.monotonic()
            )
            if result is not None and result.announce:
                label = result.contact["label"] if result.contact else "unknown"
                print(f"NFC {result.action}: {label}", flush=True)
    finally:
        try:
            Path(NFC_HEALTH_FILE).unlink()
        except FileNotFoundError:
            pass
        announcement_store.clear(preserve_claimed=True)


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon", help="read PN532 cards continuously")
    commands.add_parser("status", help="show contacts and NFC state")

    begin = commands.add_parser(
        "begin-enrollment", help="assign the next scanned card to a contact"
    )
    begin.add_argument("--label", required=True)
    begin.add_argument("--jid", required=True)
    begin.add_argument("--seconds", type=int, default=DEFAULT_ENROLLMENT_TTL_S)
    begin.add_argument("--card-clip", default="")
    commands.add_parser("enrollment-ready", help="check for an active enrollment")
    cancel = commands.add_parser("cancel-enrollment", help="cancel an enrollment")
    cancel.add_argument("request_id", nargs="?")

    scan = commands.add_parser("simulate-scan", help="process a UID without hardware")
    scan.add_argument("uid")
    commands.add_parser("simulate-remove", help="simulate removing a card")

    args = parser.parse_args(argv)
    nfc_router = router(AnnouncementStore(NFC_ANNOUNCEMENT_FILE))
    if args.command == "daemon":
        run_daemon()
        return 0
    if args.command == "status":
        print_json(nfc_router.status())
    elif args.command == "begin-enrollment":
        request = nfc_router.begin_enrollment(
            label=args.label,
            jid=args.jid,
            ttl_s=args.seconds,
            card_clip=args.card_clip,
        )
        print_json(public_enrollment(request))
    elif args.command == "enrollment-ready":
        ready = nfc_router.enrollment.active() is None
        print_json({"ready": ready})
        return 0 if ready else 1
    elif args.command == "cancel-enrollment":
        print_json({"ok": nfc_router.cancel_enrollment(args.request_id)})
    elif args.command == "simulate-scan":
        print_json(nfc_router.card_seen(args.uid).public())
    elif args.command == "simulate-remove":
        print_json(nfc_router.card_absent().public())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContactError, NfcError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
