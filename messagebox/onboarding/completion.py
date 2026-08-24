"""Request and execute the narrow onboarding-to-runtime handoff."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from messagebox.contacts import ContactError, ContactStore
from messagebox.onboarding.paths import (
    ONBOARDING_COMPLETION_REQUEST_PATH,
    ONBOARDING_ENABLED_PATH,
)
from messagebox.onboarding.recipients import RecipientError, RecipientSetup
from messagebox.runtime_paths import CONTACTS_FILE


RUNTIME_TARGET = "messagebox.target"
RUNTIME_UNITS = (
    "messagebox-button.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
)


def _atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def request_completion(path=ONBOARDING_COMPLETION_REQUEST_PATH):
    _atomic_json(path, {"version": 1, "complete": True})


def _valid_request(path=ONBOARDING_COMPLETION_REQUEST_PATH):
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("completion request is unsafe")
    if json.loads(path.read_text(encoding="utf-8")) != {"version": 1, "complete": True}:
        raise RuntimeError("completion request is invalid")


def _restore_onboarding(enabled_path, *, run):
    enabled_path = Path(enabled_path)
    descriptor = os.open(enabled_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, b"enabled\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    run(["systemctl", "stop", RUNTIME_TARGET], check=False)
    run(["systemctl", "start", "comitup.service"], check=False)


def complete(
    *,
    request_path=ONBOARDING_COMPLETION_REQUEST_PATH,
    enabled_path=ONBOARDING_ENABLED_PATH,
    contacts_path=CONTACTS_FILE,
    recipients=None,
    run=subprocess.run,
    sleep=time.sleep,
    response_grace=3.0,
):
    if os.geteuid() != 0:
        raise RuntimeError("completion gate requires root")
    _valid_request(request_path)
    enabled_path = Path(enabled_path)
    if not enabled_path.is_file() or enabled_path.is_symlink():
        raise RuntimeError("onboarding gate is unavailable")
    recipient_setup = recipients or RecipientSetup(contacts_path=contacts_path)
    recipient_state = recipient_setup.public_state()
    contacts = ContactStore(contacts_path).load()
    default = contacts["default_recipient"]
    if recipient_state["status"] != "complete" or default not in contacts["contacts"]:
        raise RuntimeError("recipient setup is incomplete")
    has_cards = any(contact["card_uids"] for contact in contacts["contacts"].values())
    sleep(response_grace)
    removed_gate = False
    try:
        run(["systemctl", "enable", *RUNTIME_UNITS, RUNTIME_TARGET], check=True)
        if has_cards:
            run(["systemctl", "enable", "messagebox-nfc.service"], check=True)
        else:
            run(["systemctl", "disable", "messagebox-nfc.service"], check=True)
        run(["systemctl", "disable", "messagebox-dash.service"], check=True)
        run(
            [
                "systemctl",
                "stop",
                "messagebox-onboarding-voice.target",
                "messagebox-onboarding-nfc.service",
            ],
            check=True,
        )
        enabled_path.unlink()
        removed_gate = True
        run(["systemctl", "stop", "comitup.service"], check=True)
        run(["systemctl", "start", RUNTIME_TARGET], check=True)
        Path(request_path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        if removed_gate:
            _restore_onboarding(enabled_path, run=run)
        raise
    return {"has_cards": has_cards}


def main():
    complete()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContactError, RecipientError, OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"completion gate: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
