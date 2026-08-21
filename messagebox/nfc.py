#!/usr/bin/env python3
"""PN532 daemon, NFC contact enrollment, and recipient selection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from messagebox.contacts import ContactError, ContactStore
from messagebox.nfc_state import (
    AnnouncementStore,
    DEFAULT_ENROLLMENT_TTL_S,
    EnrollmentStore,
    NfcError,
    NfcRouter,
    SelectionStore,
    normalize_uid,
    public_enrollment,
)
from messagebox.runtime_paths import (
    CONTACTS_FILE,
    NFC_ANNOUNCEMENT_FILE,
    NFC_ENROLLMENT_FILE,
    NFC_SELECTION_FILE,
)


UNKNOWN_TOKEN_WAV = os.environ.get("MSGBOX_UNKNOWN_TOKEN_WAV", "")
REMOVAL_GRACE_S = float(os.environ.get("MSGBOX_NFC_REMOVAL_GRACE_S", "0.8"))
REFRESH_S = float(os.environ.get("MSGBOX_NFC_REFRESH_S", "0.75"))
READ_TIMEOUT_S = float(os.environ.get("MSGBOX_NFC_READ_TIMEOUT_S", "0.2"))


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

    def announce(self, result):
        if not result.announce or result.uid is None:
            return
        if result.contact is not None:
            prompt = result.contact.get("card_clip", "")
        elif result.action == "unknown":
            prompt = UNKNOWN_TOKEN_WAV
        else:
            return
        self.store.put(action=result.action, uid=result.uid, prompt=prompt)


class NfcRuntime:
    """Debounce presentations while maintaining a fail-closed scan latch."""

    def __init__(
        self,
        router,
        announcer,
        *,
        removal_grace=REMOVAL_GRACE_S,
        refresh=REFRESH_S,
    ):
        self.router = router
        self.announcer = announcer
        self.removal_grace = removal_grace
        self.refresh = refresh
        self.uid = None
        self.last_seen = None
        self.last_refresh = None

    def observe(self, raw_uid, now):
        if raw_uid is None:
            if (
                self.uid is not None
                and self.last_seen is not None
                and now - self.last_seen >= self.removal_grace
            ):
                result = self.router.card_absent()
                self.uid = self.last_seen = self.last_refresh = None
                self.announcer.announce(result)
                return result
            return None
        uid = normalize_uid(raw_uid)
        if self.uid != uid:
            result = self.router.card_seen(uid, new_presentation=True)
            self.uid = uid
            self.last_seen = self.last_refresh = now
            self.announcer.announce(result)
            return result
        self.last_seen = now
        if self.last_refresh is None or now - self.last_refresh >= self.refresh:
            result = self.router.card_seen(uid, new_presentation=False)
            self.last_refresh = now
            if result.action == "unknown":
                return None
            self.announcer.announce(result)
            return result
        return None


def router(announcement_store=None):
    return NfcRouter(
        ContactStore(CONTACTS_FILE),
        SelectionStore(NFC_SELECTION_FILE),
        EnrollmentStore(NFC_ENROLLMENT_FILE),
        announcement_store,
    )


def run_daemon():
    if REMOVAL_GRACE_S <= 0 or REFRESH_S <= 0 or READ_TIMEOUT_S <= 0:
        raise NfcError("NFC timing values must be positive")
    announcement_store = AnnouncementStore(NFC_ANNOUNCEMENT_FILE)
    nfc_router = router(announcement_store)
    announcer = Announcer(announcement_store)
    runtime = NfcRuntime(nfc_router, announcer)
    nfc_router.selection.clear()
    announcement_store.clear()
    try:
        reader = hardware_reader()
        recovered = nfc_router.reconcile_enrollment()
        print("NFC reader ready: transport=i2c", flush=True)
        if recovered is not None:
            print(f"NFC enrolled: {recovered.contact['label']}", flush=True)
        while True:
            uid = reader.read()
            result = runtime.observe(
                bytes(uid) if uid is not None else None, time.monotonic()
            )
            if result is not None and result.announce:
                label = result.contact["label"] if result.contact else "unknown"
                print(f"NFC {result.action}: {label}", flush=True)
    finally:
        nfc_router.selection.clear()
        announcement_store.clear()


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
