#!/usr/bin/env python3
"""Validate the bundled ringtone pack before installation has side effects."""

import hashlib
import json
import sys
import wave
from pathlib import Path

from messagebox.settings import DEFAULT_RINGTONE, RINGTONES, load_ring_lamp_schedule


def validate_ringtones(root):
    root = Path(root)
    invalid = []
    for ringtone_id, filename in RINGTONES.items():
        path = root / filename
        lamp = root / f"{ringtone_id}.lamp.json"
        try:
            if path.is_symlink() or not path.is_file() or lamp.is_symlink() or not lamp.is_file():
                raise ValueError
            with wave.open(str(path), "rb") as sound:
                if (sound.getnframes() <= 0 or sound.getnchannels() != 1
                        or sound.getsampwidth() != 2 or sound.getframerate() != 48000
                        or sound.getcomptype() != "NONE"
                        or len(sound.readframes(sound.getnframes())) != sound.getnframes() * 2):
                    raise ValueError
            load_ring_lamp_schedule(lamp, ringtone_id)
        except (OSError, EOFError, ValueError, wave.Error):
            invalid.append(ringtone_id)
    try:
        path = root / "manifest.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError
        manifest = json.loads(path.read_text(encoding="utf-8"))
        rows = manifest["ringtones"]
        if manifest["default"] != DEFAULT_RINGTONE or [row["id"] for row in rows] != list(RINGTONES):
            raise ValueError
        for row in rows:
            if (row["file"] != RINGTONES[row["id"]]
                    or hashlib.sha256((root / row["file"]).read_bytes()).hexdigest() != row["sha256"]):
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        invalid.append("manifest.json")
    if invalid:
        raise ValueError("Missing or invalid ringtones: " + ", ".join(invalid))


if __name__ == "__main__":
    try:
        validate_ringtones(sys.argv[1])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
