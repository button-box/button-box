#!/usr/bin/env python3
"""Install or roll back one manifest-bounded Button Box release."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import sys
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath


RELEASE_METADATA = "/opt/messagebox/release.json"
MODE_MARKER = "/etc/messagebox-onboarding/enabled"
LEGACY_WANTS = "/etc/systemd/system/multi-user.target.wants"
SELECTOR_PATHS = (
    MODE_MARKER,
    "/etc/systemd/system/messagebox.target.wants/messagebox-wifi-watchdog.timer",
    f"{LEGACY_WANTS}/messagebox.target",
    f"{LEGACY_WANTS}/comitup.service",
    f"{LEGACY_WANTS}/messagebox-mode-reconcile.path",
)
MODE_GENERATOR = "/usr/local/lib/systemd/system-generators/messagebox-mode-generator"
MODE_MIGRATION = "/usr/lib/messagebox/messagebox-mode-migrate.py"
UPDATE_LOCK = "/run/lock/messagebox-bounded-update.lock"
BACKUP_FORMAT = 2
UNIT_SETTLE_TIMEOUT = 30.0
UNIT_SETTLE_STABLE = 2.0
UNIT_SETTLE_POLL = 0.5
TRANSITIONAL_ACTIVE_STATES = {"activating", "deactivating", "reloading"}
UNIT_NAME = re.compile(
    r"(?:[A-Za-z0-9:_.@-]|\\x[0-9a-fA-F]{2})+"
    r"\.(?:service|socket|device|mount|automount|swap|target|path|timer|slice|scope)\Z"
)

UNITS = (
    "messagebox.target",
    "messagebox-audio-detect.service",
    "messagebox-button.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
    "messagebox-dash.service",
    "messagebox-nfc.service",
    "messagebox-wifi-change.service",
    "messagebox-wifi-change.path",
    "messagebox-wifi-watchdog.service",
    "messagebox-wifi-watchdog.timer",
    "messagebox-mode-reconcile.service",
    "messagebox-mode-reconcile.path",
    "comitup.service",
    "comitup-web.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-nfc.service",
    "messagebox-onboarding-complete.service",
    "messagebox-onboarding-complete.path",
    "messagebox-onboarding-button.service",
    "messagebox-onboarding-voice-gate.service",
    "messagebox-onboarding-voice.path",
    "messagebox-onboarding-voice.target",
    "messagebox-whatsapp-pairing.service",
    "messagebox-wifi-reset.service",
)
START_ORDER = (
    "messagebox-audio-detect.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
    "messagebox-nfc.service",
    "messagebox-dash.service",
    "messagebox-button.service",
    "comitup.service",
    "messagebox-whatsapp-pairing.service",
    "messagebox-onboarding-nfc.service",
    "comitup-web.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-button.service",
    "messagebox-onboarding-voice-gate.service",
    "messagebox-onboarding-voice.target",
    "messagebox-onboarding-complete.service",
    "messagebox-wifi-reset.service",
    "messagebox-wifi-change.service",
    "messagebox-mode-reconcile.service",
    "messagebox-onboarding-complete.path",
    "messagebox-onboarding-voice.path",
    "messagebox-wifi-change.path",
    "messagebox-wifi-watchdog.service",
    "messagebox-wifi-watchdog.timer",
    "messagebox-mode-reconcile.path",
    "messagebox.target",
)

EXPLICIT_TARGETS = {
    "config/journald.conf.d/messagebox.conf": "/etc/systemd/journald.conf.d/messagebox.conf",
    "scripts/install/audio_config.py": "/usr/lib/messagebox/audio_config.py",
    "scripts/install/messagebox-mode-migrate.py": MODE_MIGRATION,
    "scripts/messageboxctl": "/usr/local/bin/messageboxctl",
    "scripts/commands/messagebox-contact": "/usr/local/bin/messagebox-contact",
    "scripts/commands/messagebox-comitup-state": "/usr/local/sbin/messagebox-comitup-state",
    "scripts/commands/messagebox-init-wifi-onboarding": "/usr/local/sbin/messagebox-init-wifi-onboarding",
    "scripts/dev/onboard.sh": "/usr/local/bin/messagebox-dev-onboard",
    "scripts/dev/hardware-test.sh": "/opt/messagebox/dev/hardware-test.sh",
    "config/requirements-nfc.txt": "/opt/messagebox/config/requirements-nfc.txt",
}
for name in ("hello_piano", "sunshine", "bouncy", "sing_along", "island", "hello", "ukulele"):
    for suffix in (".wav", ".lamp.json"):
        EXPLICIT_TARGETS[f"sounds/ringtones/{name}{suffix}"] = f"/opt/messagebox/ringtones/{name}{suffix}"
EXPLICIT_TARGETS["sounds/ringtones/manifest.json"] = "/opt/messagebox/ringtones/manifest.json"

SOUND_SOURCES = (
    "sounds/cues/README.md",
    "sounds/cues/cue-all_set.wav",
    "sounds/cues/cue-card.wav",
    "sounds/cues/cue-card_saved.wav",
    "sounds/cues/cue-connected.wav",
    "sounds/cues/cue-listened.wav",
    "sounds/cues/cue-msg_end.wav",
    "sounds/cues/cue-msg_start.wav",
    "sounds/cues/cue-offline.wav",
    "sounds/cues/cue-oops.wav",
    "sounds/cues/cue-press.wav",
    "sounds/cues/cue-ready.wav",
    "sounds/cues/cue-rec_go.wav",
    "sounds/cues/cue-rec_limit.wav",
    "sounds/cues/cue-sent.wav",
    "sounds/cues/cue-still_trying.wav",
    "sounds/cues/cues.json",
    "sounds/cues/manifest.json",
    "sounds/voice/README.md",
    "sounds/voice/manifest.json",
    "sounds/voice/voice-all-set.wav",
    "sounds/voice/voice-ask-send-1.wav",
    "sounds/voice/voice-ask-send-2.wav",
    "sounds/voice/voice-ask-send-3.wav",
    "sounds/voice/voice-card-needed.wav",
    "sounds/voice/voice-card-unknown.wav",
    "sounds/voice/voice-count-new.wav",
    "sounds/voice/voice-count-reply.wav",
    "sounds/voice/voice-empty.wav",
    "sounds/voice/voice-fail.wav",
    "sounds/voice/voice-last-chance.wav",
    "sounds/voice/voice-listened.wav",
    "sounds/voice/voice-msg-start.wav",
    "sounds/voice/voice-not-sent.wav",
    "sounds/voice/voice-online.wav",
    "sounds/voice/voice-review.wav",
    "sounds/voice/voice-stuck.wav",
)
SOUND_SOURCES += ("sounds/voices/README.md",) + tuple(
    f"sounds/voices/{pack}/{name}"
    for pack in ("pirate", "alien", "dj", "robot", "french", "charlie")
    for name in ("manifest.json",) + tuple(
        Path(source).name for source in SOUND_SOURCES
        if source.startswith("sounds/voice/") and source.endswith(".wav")
    )
)
for source in SOUND_SOURCES:
    EXPLICIT_TARGETS[source] = "/opt/messagebox/" + source
RETIRED_SOUNDS = tuple(
    "/opt/messagebox/sounds/guided-reply/" + name + ".wav"
    for name in ("reply-countdown", "standalone-countdown", "press-to-send", "delete-warning", "not-sent")
) + ("/opt/messagebox/sounds/feedback/sent-swoosh.wav",)

EXECUTABLE_TARGETS = {
    MODE_GENERATOR,
    "/usr/local/bin/messageboxctl",
    "/usr/local/bin/messagebox-contact",
    "/usr/local/bin/messagebox-dev-onboard",
    "/usr/local/sbin/messagebox-comitup-state",
    "/usr/local/sbin/messagebox-init-wifi-onboarding",
    "/opt/messagebox/dev/hardware-test.sh",
    "/opt/messagebox/messagebox/syncloop.sh",
}
ENABLED_STATES = {"enabled"}
DISABLED_STATES = {"disabled"}
PASSIVE_ENABLED_STATES = {"alias", "generated", "indirect", "not-found", "static", "transient"}


class UpdateError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _expected_target(source):
    if source in EXPLICIT_TARGETS:
        return EXPLICIT_TARGETS[source]
    path = PurePosixPath(source)
    if len(path.parts) >= 2 and path.parts[0] == "messagebox":
        if path.suffix not in {".css", ".html", ".js", ".py", ".sh"}:
            return None
        return "/opt/messagebox/" + source
    if len(path.parts) >= 2 and path.parts[0] == "systemd":
        if path.name == "messagebox.tmpfiles.conf":
            return "/etc/tmpfiles.d/messagebox.conf"
        if path.name == "messagebox-mode-generator":
            return MODE_GENERATOR
        if path.name == "messagebox.conf" and path.parent.name.endswith(".service.d"):
            return f"/etc/systemd/system/{path.parent.name}/messagebox.conf"
        if (
            path.name == "messagebox.target" or path.name.startswith("messagebox-")
        ) and path.suffix in {".path", ".service", ".target", ".timer"}:
            return "/etc/systemd/system/" + path.name
    return None


def _rollback_target_allowed(absolute):
    exact = (
        set(SELECTOR_PATHS)
        | {RELEASE_METADATA, MODE_GENERATOR, "/etc/tmpfiles.d/messagebox.conf"}
        | set(EXPLICIT_TARGETS.values())
        | set(RETIRED_SOUNDS)
        | {
            "/etc/systemd/system/messagebox.target",
            "/etc/systemd/system/comitup.service.d/messagebox.conf",
            "/etc/systemd/system/comitup-web.service.d/messagebox.conf",
        }
    )
    return absolute in exact or (
        absolute.startswith("/opt/messagebox/messagebox/")
        or absolute.startswith("/opt/messagebox/sounds/guided-reply/")
        or absolute.startswith("/etc/systemd/system/messagebox-")
    )


def _install_mode(target):
    return 0o755 if target in EXECUTABLE_TARGETS else 0o644


def _rooted(root, absolute):
    pure = PurePosixPath(absolute)
    if not pure.is_absolute() or ".." in pure.parts:
        raise UpdateError("manifest contains an unsafe installed path")
    return Path(root).joinpath(*pure.parts[1:])


def _check_parents(path, root):
    root = Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UpdateError("path escapes the selected root") from exc
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UpdateError("destination has an unsafe parent")


def _check_absolute_parents(path):
    path = Path(path)
    if not path.is_absolute():
        raise UpdateError("backup path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UpdateError("backup path has an unsafe parent")


def _validate_staged_file(path, source_root, trusted_uid):
    source_root = Path(source_root)
    path = Path(path)
    try:
        relative = path.relative_to(source_root)
    except ValueError as exc:
        raise UpdateError("release input is outside the staged source") from exc
    current = source_root
    directories = [source_root]
    for part in relative.parts[:-1]:
        current /= part
        directories.append(current)
    for directory in directories:
        try:
            metadata = directory.lstat()
        except FileNotFoundError as exc:
            raise UpdateError("release source is incomplete") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_mode & 0o022
        ):
            raise UpdateError("release source directory is not trusted")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise UpdateError("release source is incomplete") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != trusted_uid
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
    ):
        raise UpdateError("release source file is not trusted")
    return metadata


def _load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _candidate_sound_pack(source_root):
    """Load the staged validator with the staged package importable, then forget it.

    The updater runs from the staging tree, where `messagebox` is not on sys.path.
    """
    saved_path = list(sys.path)
    saved = {name: module for name, module in sys.modules.items()
             if name == "messagebox" or name.startswith("messagebox.")}
    sys.path.insert(0, str(source_root))
    try:
        return _load_module("messagebox_candidate_sound_pack", source_root / "messagebox/sound_pack.py")
    finally:
        sys.path[:] = saved_path
        for name in [name for name in sys.modules if name == "messagebox" or name.startswith("messagebox.")]:
            del sys.modules[name]
        sys.modules.update(saved)


@contextlib.contextmanager
def _update_lock(root):
    root = Path(root)
    trusted_uid = 0 if root == Path("/") else os.geteuid()
    path = _rooted(root, UPDATE_LOCK)
    _check_parents(path, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UpdateError("bounded update lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise UpdateError("bounded update lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another bounded update operation is active") from exc
        yield
    finally:
        os.close(descriptor)


def load_candidate(source_root, manifest_path, root):
    source_root = Path(source_root).resolve()
    manifest_path = Path(manifest_path).absolute()
    trusted_uid = 0 if Path(root) == Path("/") else os.geteuid()
    _validate_staged_file(manifest_path, source_root, trusted_uid)
    canonical_script = source_root / "scripts/dev/release-manifest.py"
    _validate_staged_file(canonical_script, source_root, trusted_uid)
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError("release manifest is unavailable or invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise UpdateError("release manifest has an invalid schema")
    version = manifest.get("version")
    commit = manifest.get("commit")
    if not isinstance(version, str) or not version or not isinstance(commit, str):
        raise UpdateError("release manifest lacks release identity")
    if len(version) > 64 or any(
        character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.+_-"
        for character in version
    ):
        raise UpdateError("release manifest version is invalid")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise UpdateError("release manifest commit is invalid")

    entries = []
    sources = set()
    targets = set()
    for raw in manifest["files"]:
        if not isinstance(raw, dict):
            raise UpdateError("release manifest has an invalid file entry")
        source = raw.get("source")
        target = raw.get("installed")
        expected_hash = raw.get("sha256")
        if not all(isinstance(value, str) for value in (source, target, expected_hash)):
            raise UpdateError("release manifest has an invalid file entry")
        source_pure = PurePosixPath(source)
        if source_pure.is_absolute() or ".." in source_pure.parts or str(source_pure) != source:
            raise UpdateError("release manifest contains an unsafe source path")
        if _expected_target(source) != target:
            raise UpdateError("release manifest contains a source outside the install allowlist")
        if source in sources or target in targets:
            raise UpdateError("release manifest contains a duplicate source or destination")
        if len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
            raise UpdateError("release manifest contains an invalid hash")
        source_path = source_root.joinpath(*source_pure.parts)
        _validate_staged_file(source_path, source_root, trusted_uid)
        if _sha256(source_path) != expected_hash:
            raise UpdateError("release source hash does not match the manifest")
        destination = _rooted(root, target)
        _check_parents(destination, root)
        try:
            current = destination.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
            raise UpdateError("installed destination is not a regular file")
        entries.append(
            {
                "source": source,
                "source_path": source_path,
                "target": target,
                "destination": destination,
                "sha256": expected_hash,
                "mode": _install_mode(target),
            }
        )
        sources.add(source)
        targets.add(target)
    sound_entries = {entry["source"] for entry in entries if entry["source"] in SOUND_SOURCES}
    if sound_entries:
        if sound_entries != set(SOUND_SOURCES):
            raise UpdateError("release sound pack is incomplete")
        try:
            _candidate_sound_pack(source_root).validate_sounds(source_root / "sounds")
        except (OSError, ValueError, ImportError) as exc:
            raise UpdateError("release sound assets are invalid") from exc
    for absolute in RETIRED_SOUNDS:
        destination = _rooted(root, absolute)
        _check_parents(destination, root)
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UpdateError("retired sound is not a regular file")
    canonical = _load_module(
        "messagebox_bounded_update_release_manifest", canonical_script
    ).installed_paths(source_root)
    declared = {entry["source"]: entry["target"] for entry in entries}
    if declared != canonical:
        raise UpdateError("release manifest is not the complete installed release")
    if MODE_GENERATOR not in targets or MODE_MIGRATION not in targets:
        raise UpdateError("release manifest lacks the checked mode migration inputs")

    migration_entry = next(item for item in entries if item["target"] == MODE_MIGRATION)
    generator_entry = next(item for item in entries if item["target"] == MODE_GENERATOR)
    if stat.S_IMODE(generator_entry["source_path"].stat().st_mode) != 0o755:
        raise UpdateError("staged mode generator is not executable")
    try:
        migration = _load_module(
            "messagebox_bounded_update_migration_check", migration_entry["source_path"]
        )
        migration.check(
            generator_entry["source_path"],
            marker=_rooted(root, MODE_MARKER),
            legacy_dir=_rooted(root, LEGACY_WANTS),
            trusted_uid=trusted_uid,
        )
        reconcile_link = _rooted(
            root, f"{LEGACY_WANTS}/messagebox-mode-reconcile.path"
        )
        if reconcile_link.is_symlink() and os.readlink(reconcile_link) != (
            "/etc/systemd/system/messagebox-mode-reconcile.path"
        ):
            raise UpdateError("existing mode reconciliation link is unsafe")
        if reconcile_link.exists() and not reconcile_link.is_symlink():
            raise UpdateError("existing mode reconciliation link is unsafe")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise UpdateError("staged mode migration check failed") from exc
    return manifest, entries, hashlib.sha256(manifest_bytes).hexdigest()


def _record_path(path, absolute, backup_files, index, *, allow_symlink=False):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": absolute, "kind": "absent"}
    if stat.S_ISLNK(metadata.st_mode):
        if not allow_symlink:
            raise UpdateError("cannot back up an unexpected symbolic link")
        return {"path": absolute, "kind": "symlink", "target": os.readlink(path)}
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UpdateError("cannot back up an unsafe filesystem object")
    stored = backup_files / f"{index:04d}"
    with open(path, "rb") as source, open(stored, "xb") as destination:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
        os.fchmod(destination.fileno(), 0o600)
    return {
        "path": absolute,
        "kind": "file",
        "backup": stored.name,
        "sha256": _sha256(stored),
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def _run(command, run, **kwargs):
    try:
        return run(command, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"command failed: {command[0]}") from exc


def _unit_states(run, *, allow_transitional=False, deadline=None):
    def query(operation, unit):
        options = {"check": False, "capture_output": True, "text": True}
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpdateError(f"timed out reading managed unit state: {unit}")
            options["timeout"] = remaining
        return _run(["systemctl", operation, unit], run, **options).stdout.strip()

    states = {}
    for unit in UNITS:
        active = query("is-active", unit)
        enabled = query("is-enabled", unit)
        if active in TRANSITIONAL_ACTIVE_STATES and not allow_transitional:
            raise UpdateError(f"managed unit is still transitioning: {unit} ({active})")
        known_active = {"active", "inactive", "failed"}
        if allow_transitional:
            known_active |= TRANSITIONAL_ACTIVE_STATES
        if active not in known_active:
            raise UpdateError(f"managed unit has an unsupported active state: {unit}")
        known_enabled = ENABLED_STATES | DISABLED_STATES | PASSIVE_ENABLED_STATES | {
            "enabled-runtime",
            "masked",
            "masked-runtime",
        }
        if enabled not in known_enabled:
            raise UpdateError(f"managed unit has an unsupported enabled state: {unit}")
        states[unit] = {"active": active, "enabled": enabled}
    return states


def _valid_auxiliary_unit(unit):
    return (
        isinstance(unit, str)
        and not unit.startswith("-")
        and UNIT_NAME.fullmatch(unit) is not None
        and unit not in UNITS
    )


def _active_auxiliary_units(states, run):
    # ConsistsOf is systemd's reverse PartOf relation. Follow only units whose
    # stop can propagate from an active managed unit. An inactive intermediate
    # can still pass a stop to an active descendant.
    pending = [unit for unit in UNITS if states[unit]["active"] == "active"]
    seen = set(pending)
    active = []
    while pending:
        unit = pending.pop(0)
        result = _run(
            ["systemctl", "show", "--property=ConsistsOf", "--value", unit],
            run, check=True, capture_output=True, text=True,
        )
        if not isinstance(result.stdout, str):
            raise UpdateError("cannot read auxiliary unit dependencies")
        for dependent in result.stdout.split():
            if dependent in seen:
                continue
            if dependent not in UNITS and not _valid_auxiliary_unit(dependent):
                raise UpdateError("systemd returned an invalid auxiliary unit name")
            seen.add(dependent)
            if dependent in UNITS:
                pending.append(dependent)
                continue
            result = _run(
                ["systemctl", "is-active", dependent], run,
                check=False, capture_output=True, text=True,
            )
            state = result.stdout.strip() if isinstance(result.stdout, str) else None
            if state in TRANSITIONAL_ACTIVE_STATES:
                raise UpdateError("auxiliary unit is still transitioning")
            if state not in {"active", "inactive", "failed"}:
                raise UpdateError("auxiliary unit has an unsupported active state")
            if state == "active":
                active.append(dependent)
            pending.append(dependent)
    return active


def create_backup(backup_dir, root, entries, unit_states, auxiliary_units=()):
    backup_dir = Path(backup_dir)
    _check_absolute_parents(backup_dir)
    if backup_dir.exists() or backup_dir.is_symlink():
        raise UpdateError("backup directory must not already exist")
    backup_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_metadata = backup_dir.parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise UpdateError("backup parent is unsafe")
    backup_dir.mkdir(mode=0o700)
    backup_files = backup_dir / "files"
    backup_files.mkdir(mode=0o700)
    records = []
    targets = [item["target"] for item in entries]
    targets.extend(RETIRED_SOUNDS)
    targets.append(RELEASE_METADATA)
    for absolute in targets:
        records.append(
            _record_path(_rooted(root, absolute), absolute, backup_files, len(records))
        )
    for absolute in SELECTOR_PATHS:
        records.append(
            _record_path(
                _rooted(root, absolute),
                absolute,
                backup_files,
                len(records),
                allow_symlink=absolute != MODE_MARKER,
            )
        )
    state = {
        "format": BACKUP_FORMAT, "files": records, "units": unit_states,
        "auxiliary_units": list(auxiliary_units),
    }
    state_path = backup_dir / "state.json"
    with open(state_path, "x", encoding="utf-8") as output:
        output.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
        os.fchmod(output.fileno(), 0o600)
    _fsync_directory(backup_files)
    _fsync_directory(backup_dir)
    return state


def _atomic_install(source, destination, mode, *, uid=0, gid=0):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = None
    try:
        with open(source, "rb") as input_file, tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=".messagebox-update.", delete=False
        ) as output:
            temporary = Path(output.name)
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(output.fileno(), uid, gid)
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stop_active(states, run):
    active = [
        unit for unit in reversed(UNITS)
        if states[unit]["active"] in {"active"} | TRANSITIONAL_ACTIVE_STATES
    ]
    if active:
        _run(["systemctl", "stop", *active], run, check=True)


def _restore_enabled(states, run):
    for unit in UNITS:
        enabled = states[unit]["enabled"]
        if enabled in ENABLED_STATES:
            _run(["systemctl", "enable", unit], run, check=True)
        elif enabled == "enabled-runtime":
            _run(["systemctl", "enable", "--runtime", unit], run, check=True)
        elif enabled in DISABLED_STATES:
            _run(["systemctl", "disable", unit], run, check=True)
        elif enabled == "masked":
            _run(["systemctl", "mask", unit], run, check=True)
        elif enabled == "masked-runtime":
            _run(["systemctl", "mask", "--runtime", unit], run, check=True)


def _restore_active(states, run, *, also_active=()):
    if set(START_ORDER) != set(UNITS) or len(START_ORDER) != len(UNITS):
        raise UpdateError("managed unit start order is incomplete")
    # ComItUp replaces portal jobs while moving HOTSPOT -> CONNECTED. It must
    # own those starts; a simultaneous manual start can be canceled by it.
    comitup_owns_portals = states["comitup.service"]["active"] == "active"
    for unit in START_ORDER:
        if comitup_owns_portals and unit in {
            "comitup-web.service", "messagebox-onboarding-home.service"
        }:
            continue
        if states[unit]["active"] == "active" and unit not in also_active:
            _run(
                ["systemctl", "--job-mode=ignore-dependencies", "start", unit],
                run,
                check=True,
            )


def _restore_auxiliary_active(auxiliary_units, run):
    for unit in auxiliary_units:
        _run(
            ["systemctl", "--job-mode=ignore-dependencies", "start", unit],
            run, check=True,
        )


def _verify_active(states, run, *, also_active=(), auxiliary_units=()):
    expected = {
        unit: "active" if unit in also_active else states[unit]["active"]
        for unit in UNITS
    }
    deadline = time.monotonic() + UNIT_SETTLE_TIMEOUT
    stable_since = None
    differences = []
    while True:
        if time.monotonic() >= deadline:
            detail = "; ".join(differences) or "the restored state did not remain stable"
            raise UpdateError(f"managed unit state did not settle within {UNIT_SETTLE_TIMEOUT:g}s: {detail}")
        current = _unit_states(run, allow_transitional=True, deadline=deadline)
        differences = [
            f"{unit} expected {expected[unit]}, observed {current[unit]['active']}"
            for unit in UNITS if current[unit]["active"] != expected[unit]
        ]
        newly_failed = [
            unit for unit in UNITS
            if current[unit]["active"] == "failed" and expected[unit] != "failed"
        ]
        if newly_failed:
            raise UpdateError("restored managed unit failed: " + ", ".join(newly_failed))
        for unit in auxiliary_units:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpdateError("timed out reading auxiliary unit state")
            result = _run(
                ["systemctl", "is-active", unit], run,
                check=False, capture_output=True, text=True, timeout=remaining,
            )
            observed = result.stdout.strip() if isinstance(result.stdout, str) else None
            if observed == "failed":
                raise UpdateError("restored auxiliary unit failed")
            if observed not in {"active", "inactive"} | TRANSITIONAL_ACTIVE_STATES:
                raise UpdateError("auxiliary unit has an unsupported active state")
            if observed != "active":
                differences.append("auxiliary unit is not active")
        now = time.monotonic()
        if now >= deadline:
            detail = "; ".join(differences) or "verification finished after its deadline"
            raise UpdateError(f"managed unit state did not settle within {UNIT_SETTLE_TIMEOUT:g}s: {detail}")
        if differences:
            stable_since = None
        elif stable_since is None:
            stable_since = now
        elif now - stable_since >= UNIT_SETTLE_STABLE:
            return
        time.sleep(min(UNIT_SETTLE_POLL, max(0, deadline - now)))


def _verify_enabled(states, run):
    actual = _unit_states(
        run, allow_transitional=True, deadline=time.monotonic() + UNIT_SETTLE_TIMEOUT
    )
    for unit in UNITS:
        if actual[unit]["enabled"] != states[unit]["enabled"]:
            raise UpdateError(f"managed enabled unit state does not match the recorded state: {unit}")


def _failure_cause(error):
    # Only updater-authored errors are safe to print; OS/library errors can
    # contain private paths or subprocess output. The exception chain is kept.
    return str(error) if isinstance(error, UpdateError) else type(error).__name__


def _validate_backup_state(state, backup_dir, root):
    if not isinstance(state, dict) or state.get("format") not in {1, BACKUP_FORMAT}:
        raise UpdateError("backup state has an unsupported format")
    if not isinstance(state.get("files"), list) or not isinstance(state.get("units"), dict):
        raise UpdateError("backup state is incomplete")
    records = []
    seen = set()
    trusted_uid = 0 if Path(root) == Path("/") else os.geteuid()
    files_metadata = (backup_dir / "files").lstat()
    if (
        not stat.S_ISDIR(files_metadata.st_mode)
        or files_metadata.st_uid != trusted_uid
        or stat.S_IMODE(files_metadata.st_mode) != 0o700
    ):
        raise UpdateError("backup file directory is unsafe")
    for record in state["files"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise UpdateError("backup contains an invalid file record")
        absolute = record["path"]
        if not _rollback_target_allowed(absolute):
            raise UpdateError("backup contains a path outside the rollback allowlist")
        if absolute in seen:
            raise UpdateError("backup contains a duplicate path")
        seen.add(absolute)
        path = _rooted(root, absolute)
        _check_parents(path, root)
        kind = record.get("kind")
        if kind == "file":
            stored_name = record.get("backup")
            if not isinstance(stored_name, str) or PurePosixPath(stored_name).name != stored_name:
                raise UpdateError("backup file reference is unsafe")
            stored = backup_dir / "files" / stored_name
            try:
                stored_metadata = stored.lstat()
            except FileNotFoundError as exc:
                raise UpdateError("backup file is missing") from exc
            if (
                not stat.S_ISREG(stored_metadata.st_mode)
                or stored_metadata.st_uid != trusted_uid
                or stored_metadata.st_nlink != 1
                or stat.S_IMODE(stored_metadata.st_mode) != 0o600
                or _sha256(stored) != record.get("sha256")
            ):
                raise UpdateError("backup file hash does not match its record")
            if (
                not isinstance(record.get("mode"), int)
                or not 0 <= record["mode"] <= 0o777
                or not isinstance(record.get("uid"), int)
                or record["uid"] < 0
                or not isinstance(record.get("gid"), int)
                or record["gid"] < 0
            ):
                raise UpdateError("backup file metadata is invalid")
        elif kind == "symlink":
            if absolute not in SELECTOR_PATHS or absolute == MODE_MARKER:
                raise UpdateError("backup contains an unexpected symbolic link")
            if not isinstance(record.get("target"), str):
                raise UpdateError("backup symbolic link target is invalid")
        elif kind != "absent":
            raise UpdateError("backup contains an invalid file kind")
        records.append((record, path))
    if set(state["units"]) != set(UNITS):
        raise UpdateError("backup unit state is incomplete")
    for unit in UNITS:
        unit_state = state["units"][unit]
        if not isinstance(unit_state, dict):
            raise UpdateError("backup unit state is invalid")
        if unit_state.get("active") not in {"active", "failed", "inactive"}:
            raise UpdateError("backup active unit state is invalid")
        enabled = unit_state.get("enabled")
        if enabled not in (
            ENABLED_STATES
            | DISABLED_STATES
            | PASSIVE_ENABLED_STATES
            | {"enabled-runtime", "masked", "masked-runtime"}
        ):
            raise UpdateError("backup enabled unit state is invalid")
    if state["format"] == 1:
        if "auxiliary_units" in state:
            raise UpdateError("version-one backup has unexpected auxiliary state")
    else:
        auxiliary = state.get("auxiliary_units")
        if (
            not isinstance(auxiliary, list)
            or any(not _valid_auxiliary_unit(unit) for unit in auxiliary)
            or len(set(auxiliary)) != len(auxiliary)
        ):
            raise UpdateError("backup auxiliary unit state is invalid")
    return records


def _restore_record(record, path, backup_dir):
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None:
        if stat.S_ISDIR(current.st_mode):
            raise UpdateError("rollback destination is an unexpected directory")
        path.unlink()
    if record["kind"] == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if record["kind"] == "symlink":
        path.symlink_to(record["target"])
        _fsync_directory(path.parent)
        return
    _atomic_install(
        backup_dir / "files" / record["backup"],
        path,
        record["mode"],
        uid=record["uid"],
        gid=record["gid"],
    )


def _rollback_locked(backup_dir, *, root, run):
    backup_dir = Path(backup_dir)
    _check_absolute_parents(backup_dir)
    try:
        backup_metadata = backup_dir.lstat()
        state_path = backup_dir / "state.json"
        state_metadata = state_path.lstat()
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise UpdateError("backup state is unavailable or invalid") from exc
    if stat.S_ISLNK(backup_metadata.st_mode) or not stat.S_ISDIR(backup_metadata.st_mode):
        raise UpdateError("backup directory is unsafe")
    if stat.S_ISLNK(state_metadata.st_mode) or not stat.S_ISREG(state_metadata.st_mode):
        raise UpdateError("backup state is unsafe")
    trusted_uid = 0 if root == Path("/") else os.geteuid()
    if (
        backup_metadata.st_uid != trusted_uid
        or stat.S_IMODE(backup_metadata.st_mode) != 0o700
        or state_metadata.st_uid != trusted_uid
        or state_metadata.st_nlink != 1
        or stat.S_IMODE(state_metadata.st_mode) != 0o600
    ):
        raise UpdateError("backup permissions or ownership are unsafe")
    records = _validate_backup_state(state, backup_dir, root)
    auxiliary_units = state.get("auxiliary_units", ())

    current = _unit_states(
        run, allow_transitional=True, deadline=time.monotonic() + UNIT_SETTLE_TIMEOUT
    )
    _stop_active(current, run)
    for record, path in records:
        _restore_record(record, path, backup_dir)
    _run(["systemctl", "daemon-reload"], run, check=True)
    _restore_enabled(state["units"], run)
    _restore_active(state["units"], run)
    _restore_auxiliary_active(auxiliary_units, run)
    _verify_enabled(state["units"], run)
    _verify_active(state["units"], run, auxiliary_units=auxiliary_units)
    return state


def rollback(backup_dir, *, root=Path("/"), run=subprocess.run):
    root = Path(root).resolve()
    if root == Path("/") and os.geteuid() != 0:
        raise UpdateError("bounded rollback requires root")
    with _update_lock(root):
        return _rollback_locked(backup_dir, root=root, run=run)


def _apply_locked(source_root, manifest_path, backup_dir, *, root, run):
    manifest, entries, manifest_hash = load_candidate(source_root, manifest_path, root)
    states = _unit_states(run)
    auxiliary_units = _active_auxiliary_units(states, run)
    create_backup(backup_dir, root, entries, states, auxiliary_units)
    try:
        _stop_active(states, run)
        generator_entry = next(item for item in entries if item["target"] == MODE_GENERATOR)
        migration_entry = next(item for item in entries if item["target"] == MODE_MIGRATION)
        for entry in entries:
            if entry is generator_entry:
                continue
            _atomic_install(entry["source_path"], entry["destination"], entry["mode"])

        for absolute in RETIRED_SOUNDS:
            destination = _rooted(root, absolute)
            _check_parents(destination, root)
            try:
                current = destination.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                raise UpdateError("retired sound changed after preflight")
            destination.unlink(missing_ok=True)
            if destination.parent.exists():
                _fsync_directory(destination.parent)

        migration = _load_module("messagebox_bounded_update_migration", migration_entry["source_path"])
        trusted_uid = 0 if root == Path("/") else root.stat().st_uid
        migration.migrate(
            generator_entry["source_path"],
            marker=_rooted(root, MODE_MARKER),
            lock=_rooted(root, "/run/lock/messagebox-mode-transition.lock"),
            destination=_rooted(root, MODE_GENERATOR),
            legacy_dir=_rooted(root, LEGACY_WANTS),
            generated_dir=_rooted(root, "/run/systemd/generator/multi-user.target.wants"),
            target_wants=_rooted(root, "/etc/systemd/system/messagebox.target.wants"),
            run=run,
            trusted_uid=trusted_uid,
        )
        _run(["systemctl", "enable", "messagebox-wifi-watchdog.timer"], run, check=True)
        if root == Path("/"):
            _run(["env", "PYTHONPATH=/opt/messagebox", "python3", "-m",
                  "messagebox.wifi_watchdog", "--configure"], run, check=True)
            Path("/var/log/journal").mkdir(mode=0o755, parents=True, exist_ok=True)
            _run(["systemctl", "restart", "systemd-journald.service"], run, check=True)
            _run(["journalctl", "--flush"], run, check=True)
        metadata = {
            "commit": manifest["commit"],
            "manifest_sha256": manifest_hash,
            "version": manifest["version"],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as output:
            json.dump(metadata, output, indent=2, sort_keys=True)
            output.write("\n")
            metadata_source = Path(output.name)
        try:
            _atomic_install(metadata_source, _rooted(root, RELEASE_METADATA), 0o644)
        finally:
            metadata_source.unlink(missing_ok=True)
        for entry in entries:
            if _sha256(entry["destination"]) != entry["sha256"]:
                raise UpdateError("installed file hash does not match the manifest")
            if stat.S_IMODE(entry["destination"].stat().st_mode) != entry["mode"]:
                raise UpdateError("installed file mode does not match policy")
        _restore_active(
            states, run, also_active={"messagebox-mode-reconcile.path"} | (
                {"messagebox-wifi-watchdog.timer"} if not _rooted(root, MODE_MARKER).exists() else set())
        )
        if not _rooted(root, MODE_MARKER).exists():
            _run(["systemctl", "--job-mode=ignore-dependencies", "start", "messagebox-wifi-watchdog.timer"], run, check=True)
        _restore_auxiliary_active(auxiliary_units, run)
        _verify_active(
            states, run, also_active={"messagebox-mode-reconcile.path"} | (
                {"messagebox-wifi-watchdog.timer"} if not _rooted(root, MODE_MARKER).exists() else set()),
            auxiliary_units=auxiliary_units,
        )
    except BaseException as update_error:
        try:
            _rollback_locked(backup_dir, root=root, run=run)
        except BaseException as rollback_error:
            raise UpdateError(
                f"update failed ({_failure_cause(update_error)}); "
                f"automatic rollback also failed: {_failure_cause(rollback_error)}"
            ) from update_error
        raise UpdateError(
            f"update failed ({_failure_cause(update_error)}); the recorded state was restored"
        ) from update_error
    return manifest


def apply(source_root, manifest_path, backup_dir, *, root=Path("/"), run=subprocess.run):
    root = Path(root).resolve()
    if root == Path("/") and os.geteuid() != 0:
        raise UpdateError("bounded update requires root")
    with _update_lock(root):
        return _apply_locked(
            source_root, manifest_path, backup_dir, root=root, run=run
        )


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply", help="apply a checked release manifest")
    apply_parser.add_argument("--source-root", required=True)
    apply_parser.add_argument("--manifest", required=True)
    apply_parser.add_argument("--backup-dir", required=True)
    rollback_parser = subparsers.add_parser("rollback", help="restore a bounded-update backup")
    rollback_parser.add_argument("--backup-dir", required=True)
    options = parser.parse_args(arguments)
    try:
        if options.command == "apply":
            manifest = apply(
                options.source_root,
                options.manifest,
                options.backup_dir,
                root=options.root,
            )
            print(f"Installed {manifest['version']} ({manifest['commit']}).")
        else:
            rollback(options.backup_dir, root=options.root)
            print("Restored the recorded pre-update state.")
    except UpdateError as exc:
        parser.exit(1, f"bounded update: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
