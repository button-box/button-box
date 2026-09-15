#!/usr/bin/env python3
"""Emit content hashes for installed code; never read device configuration/data."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def installed_paths(root):
    paths = {}
    for source in sorted((root / "messagebox").rglob("*")):
        if not source.is_file() or source.suffix not in {".py", ".sh", ".html", ".js", ".css"}:
            continue
        if "__pycache__" in source.parts or source.name == "midi_ringtone.py":
            continue
        paths[source.relative_to(root).as_posix()] = "/opt/messagebox/" + source.relative_to(root).as_posix()
    for source in sorted((root / "systemd").rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(root).as_posix()
        if source.suffix in {".service", ".target", ".path"}:
            paths[relative] = "/etc/systemd/system/" + source.name
        elif source.name == "messagebox.tmpfiles.conf":
            paths[relative] = "/etc/tmpfiles.d/messagebox.conf"
        elif source.name == "messagebox.conf" and source.parent.name.endswith(".service.d"):
            paths[relative] = "/etc/systemd/system/" + source.parent.name + "/messagebox.conf"
    for name in ("reply-countdown", "standalone-countdown", "press-to-send", "delete-warning", "not-sent"):
        source = f"sounds/guided-reply/{name}.wav"
        paths[source] = "/opt/messagebox/" + source
    paths.update({
        "sounds/feedback/sent-swoosh.wav": "/opt/messagebox/sounds/feedback/sent-swoosh.wav",
        "scripts/install/audio_config.py": "/usr/lib/messagebox/audio_config.py",
        "scripts/messageboxctl": "/usr/local/bin/messageboxctl",
        "scripts/commands/messagebox-contact": "/usr/local/bin/messagebox-contact",
        "scripts/commands/messagebox-comitup-state": "/usr/local/sbin/messagebox-comitup-state",
        "scripts/commands/messagebox-init-wifi-onboarding": "/usr/local/sbin/messagebox-init-wifi-onboarding",
        "scripts/dev/onboard.sh": "/usr/local/bin/messagebox-dev-onboard",
        "scripts/dev/hardware-test.sh": "/opt/messagebox/dev/hardware-test.sh",
        "config/requirements-nfc.txt": "/opt/messagebox/config/requirements-nfc.txt",
    })
    return paths


def manifest(root=ROOT):
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return {
        "version": (root / "VERSION").read_text().strip(),
        "commit": revision,
        "scope": "Installed program files and bundled prompts only; excludes user state, generated audio, OS packages and private configuration.",
        "files": [{"source": source, "installed": target,
                   "sha256": hashlib.sha256((root / source).read_bytes()).hexdigest()}
                  for source, target in sorted(installed_paths(root).items())],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "sha256sum"), default="json")
    args = parser.parse_args()
    result = manifest()
    if args.format == "sha256sum":
        for item in result["files"]:
            print(f"{item['sha256']}  {item['installed']}")
    else:
        print(json.dumps(result, indent=2))
