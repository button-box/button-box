"""Narrow root gate for the onboarding voice-preview target."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from messagebox.contacts import ContactError, ContactStore
from messagebox.cloud_device import CLAIM_FILE
from messagebox.onboarding.paths import ONBOARDING_ENABLED_PATH
from messagebox.onboarding.recipients import VOICE_REQUEST_FILE
from messagebox.runtime_paths import CONTACTS_FILE


VOICE_TARGET = "messagebox-onboarding-voice.target"


def requested(path=None):
    path = Path(path or VOICE_REQUEST_FILE)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("voice request is unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document != {"version": 1, "enabled": True}:
        raise RuntimeError("voice request is invalid")
    return True


def main(*, run=subprocess.run):
    if os.geteuid() != 0:
        raise RuntimeError("voice gate requires root")
    if not Path(ONBOARDING_ENABLED_PATH).is_file():
        run(["systemctl", "stop", VOICE_TARGET], check=True)
        if os.environ.get("MSGBOX_TRANSPORT") == "cloud":
            run(["systemctl", "stop", "messagebox-onboarding-button.service"], check=True)
        return 0
    request_path = Path(VOICE_REQUEST_FILE)
    if os.environ.get("MSGBOX_TRANSPORT") == "cloud":
        try:
            metadata = CLAIM_FILE.lstat()
            document = json.loads(CLAIM_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o007
                or not isinstance(document, dict)
                or type(document.get("expires_at")) is not int
                or document["expires_at"] <= time.time()
                or type(document.get("physical_confirmed")) is not bool):
            raise RuntimeError("cloud claim button request is invalid")
        run(["systemctl", "start", "messagebox-onboarding-button.service"], check=True)
        return 0
    if not request_path.exists():
        run(["systemctl", "stop", VOICE_TARGET], check=True)
        return 0
    if not requested(request_path):
        return 0
    document = ContactStore(CONTACTS_FILE).load()
    default = document["default_recipient"]
    if default is None or default not in document["contacts"]:
        raise RuntimeError("voice gate requires a valid default recipient")
    run(["systemctl", "start", VOICE_TARGET], check=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContactError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"voice gate: {exc}", file=sys.stderr)
        raise SystemExit(1)
