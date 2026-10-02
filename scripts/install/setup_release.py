#!/usr/bin/env python3
"""Preflight fresh Cloud setup and record a verified installed release."""

import argparse
import hashlib
import json
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path

import audio_config
import bounded_update


FRESH_PATHS = (
    "/opt/messagebox",
    "/etc/messagebox",
    "/etc/messagebox-onboarding",
    "/etc/messagebox-box-id",
    "/var/lib/messagebox",
    "/var/lib/messagebox-onboarding",
    "/var/lib/messagebox-settings",
    "/var/lib/messagebox-cloud",
    "/usr/lib/messagebox",
    bounded_update.MODE_GENERATOR,
    "/etc/tmpfiles.d/messagebox.conf",
    "/usr/local/bin/messageboxctl",
    "/usr/local/bin/messagebox-contact",
    "/usr/local/bin/messagebox-dev-onboard",
    "/usr/local/sbin/messagebox-comitup-state",
    "/usr/local/sbin/messagebox-init-wifi-onboarding",
)


def check_fresh(root=Path("/"), *, check_accounts=True):
    for absolute in FRESH_PATHS:
        path = bounded_update._rooted(root, absolute)
        bounded_update._check_parents(path, root)
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise bounded_update.UpdateError(
            "Cloud setup requires a fresh Pi; existing Button Box installation or state found"
        )
    systemd = bounded_update._rooted(root, "/etc/systemd/system")
    if any(systemd.glob("messagebox*")) or any(systemd.glob("comitup*.service.d/messagebox.conf")):
        raise bounded_update.UpdateError("Cloud setup requires a fresh Pi; existing Button Box units found")
    if check_accounts:
        for name in ("messagebox", "messagebox-onboarding"):
            try:
                pwd.getpwnam(name)
            except KeyError:
                continue
            raise bounded_update.UpdateError("Cloud setup requires a fresh Pi; existing service account found")


def source_uid():
    if os.geteuid() != 0:
        return os.getuid()
    if "SUDO_UID" not in os.environ:
        return 0
    try:
        uid = int(os.environ["SUDO_UID"])
        account = pwd.getpwnam(os.environ["SUDO_USER"])
    except (KeyError, ValueError):
        raise bounded_update.UpdateError("cannot verify setup source owner") from None
    if uid < 0 or account.pw_uid != uid:
        raise bounded_update.UpdateError("cannot verify setup source owner")
    return uid


def load_release(source_root, *, owner=None):
    """Use the updater's allowlist and file trust rules without changing device mode."""
    source_root = Path(source_root).resolve()
    owner = source_uid() if owner is None else owner
    manifest_path = source_root / "release-manifest.json"
    canonical_path = source_root / "scripts/dev/release-manifest.py"
    version_path = source_root / "VERSION"
    for path in (manifest_path, canonical_path, version_path):
        bounded_update._validate_staged_file(path, source_root, owner)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise bounded_update.UpdateError("release manifest has an invalid schema")
    version, commit = manifest.get("version"), manifest.get("commit")
    if (
        not isinstance(version, str) or not version or len(version) > 64
        or any(c not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.+_-" for c in version)
        or not isinstance(commit, str) or len(commit) != 40
        or any(c not in "0123456789abcdef" for c in commit)
        or version_path.read_text(encoding="utf-8").strip() != version
    ):
        raise bounded_update.UpdateError("release manifest identity does not match VERSION")
    entries = []
    sources, targets = set(), set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise bounded_update.UpdateError("release manifest has an invalid file entry")
        source, target, expected_hash = (item.get(key) for key in ("source", "installed", "sha256"))
        if not all(isinstance(value, str) for value in (source, target, expected_hash)):
            raise bounded_update.UpdateError("release manifest has an invalid file entry")
        path = Path(source)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != source:
            raise bounded_update.UpdateError("release manifest contains an unsafe source path")
        if bounded_update._expected_target(source) != target:
            raise bounded_update.UpdateError("release manifest contains a source outside the install allowlist")
        if source in sources or target in targets:
            raise bounded_update.UpdateError("release manifest contains a duplicate source or destination")
        if len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
            raise bounded_update.UpdateError("release manifest contains an invalid hash")
        source_path = source_root / source
        bounded_update._validate_staged_file(source_path, source_root, owner)
        if bounded_update._sha256(source_path) != expected_hash:
            raise bounded_update.UpdateError("release source hash does not match the manifest")
        if target == bounded_update.MODE_GENERATOR and stat.S_IMODE(source_path.stat().st_mode) != 0o755:
            raise bounded_update.UpdateError("staged mode generator is not executable")
        entries.append(item)
        sources.add(source)
        targets.add(target)
    canonical = bounded_update._load_module("messagebox_setup_manifest", canonical_path).installed_paths(source_root)
    if {item["source"]: item["installed"] for item in entries} != canonical:
        raise bounded_update.UpdateError("release manifest is not the complete installed release")
    if not {bounded_update.MODE_GENERATOR, bounded_update.MODE_MIGRATION} <= targets:
        raise bounded_update.UpdateError("release manifest lacks the checked mode migration inputs")
    return manifest, hashlib.sha256(manifest_bytes).hexdigest()


def check_destinations(manifest, root=Path("/")):
    trusted_uid = 0 if Path(root) == Path("/") else os.geteuid()
    for target in [item["installed"] for item in manifest["files"]] + [bounded_update.RELEASE_METADATA]:
        path = bounded_update._rooted(root, target)
        bounded_update._check_parents(path, root)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != trusted_uid:
            raise bounded_update.UpdateError("installed destination is not a trusted regular file")


def record_release(source_root, root=Path("/")):
    manifest, manifest_hash = load_release(source_root)
    trusted_uid = 0 if Path(root) == Path("/") else os.geteuid()
    for item in manifest["files"]:
        path = bounded_update._rooted(root, item["installed"])
        bounded_update._check_parents(path, root)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != trusted_uid:
            raise bounded_update.UpdateError("installed destination is not a trusted regular file")
        if bounded_update._sha256(path) != item["sha256"]:
            raise bounded_update.UpdateError("installed file hash does not match the manifest")
        if stat.S_IMODE(metadata.st_mode) != bounded_update._install_mode(item["installed"]):
            raise bounded_update.UpdateError("installed file mode does not match policy")
    destination = bounded_update._rooted(root, bounded_update.RELEASE_METADATA)
    bounded_update._check_parents(destination, root)
    if destination.exists() or destination.is_symlink():
        metadata = destination.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != trusted_uid:
            raise bounded_update.UpdateError("existing release identity is unsafe")
    identity = {"version": manifest["version"], "commit": manifest["commit"], "manifest_sha256": manifest_hash}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as output:
        json.dump(identity, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        bounded_update._atomic_install(Path(output.name), destination, 0o644)
    if json.loads(destination.read_text()) != identity:
        raise bounded_update.UpdateError("installed release identity verification failed")


def cloud_environment(template_path):
    microphone_card, microphone_device = audio_config.detect_microphone()
    speaker_card, speaker_device = audio_config.detect_speaker()
    rendered = audio_config.render_environment(
        Path(template_path).read_text(encoding="utf-8"),
        microphone_card, microphone_device, speaker_card, speaker_device,
    )
    lines = rendered.splitlines()
    transport_lines = [index for index, line in enumerate(lines) if line.split("=", 1)[0] == "MSGBOX_TRANSPORT"]
    if len(transport_lines) != 1:
        raise audio_config.AudioConfigError("configuration template must define transport exactly once")
    lines[transport_lines[0]] = "MSGBOX_TRANSPORT=cloud"
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-fresh")
    for command in ("check-release", "check-install", "record-release"):
        commands.add_parser(command).add_argument("source")
    commands.add_parser("cloud-environment").add_argument("template")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-fresh":
            check_fresh()
        elif args.command == "check-release":
            load_release(args.source)
        elif args.command == "check-install":
            manifest, _ = load_release(args.source)
            check_destinations(manifest)
        elif args.command == "record-release":
            record_release(args.source)
        else:
            print(cloud_environment(args.template), end="")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
