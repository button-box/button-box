#!/usr/bin/env python3
"""Install or roll back one manifest-bounded Button Box release."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


RELEASE_METADATA = "/opt/messagebox/release.json"
MODE_MARKER = "/etc/messagebox-onboarding/enabled"
LEGACY_WANTS = "/etc/systemd/system/multi-user.target.wants"
SELECTOR_PATHS = (
    MODE_MARKER,
    f"{LEGACY_WANTS}/messagebox.target",
    f"{LEGACY_WANTS}/comitup.service",
    f"{LEGACY_WANTS}/messagebox-mode-reconcile.path",
)
MODE_GENERATOR = "/usr/local/lib/systemd/system-generators/messagebox-mode-generator"
MODE_MIGRATION = "/usr/lib/messagebox/messagebox-mode-migrate.py"
UPDATE_LOCK = "/run/lock/messagebox-bounded-update.lock"
BACKUP_FORMAT = 1

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
    "messagebox-mode-reconcile.path",
    "messagebox.target",
)

EXPLICIT_TARGETS = {
    "sounds/feedback/sent-swoosh.wav": "/opt/messagebox/sounds/feedback/sent-swoosh.wav",
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
    if source.startswith("sounds/guided-reply/") and path.name in {
        "delete-warning.wav",
        "not-sent.wav",
        "press-to-send.wav",
        "reply-countdown.wav",
        "standalone-countdown.wav",
    }:
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
        ) and path.suffix in {".path", ".service", ".target"}:
            return "/etc/systemd/system/" + path.name
    return None


def _rollback_target_allowed(absolute):
    exact = (
        set(SELECTOR_PATHS)
        | {RELEASE_METADATA, MODE_GENERATOR, "/etc/tmpfiles.d/messagebox.conf"}
        | set(EXPLICIT_TARGETS.values())
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


def _unit_states(run):
    states = {}
    for unit in UNITS:
        active = _run(
            ["systemctl", "is-active", unit],
            run,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        enabled = _run(
            ["systemctl", "is-enabled", unit],
            run,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if active not in {"active", "inactive", "failed"}:
            raise UpdateError("a managed unit has an unsupported active state")
        known_enabled = ENABLED_STATES | DISABLED_STATES | PASSIVE_ENABLED_STATES | {
            "enabled-runtime",
            "masked",
            "masked-runtime",
        }
        if enabled not in known_enabled:
            raise UpdateError("a managed unit has an unsupported enabled state")
        states[unit] = {"active": active, "enabled": enabled}
    return states


def create_backup(backup_dir, root, entries, unit_states):
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
    state = {"format": BACKUP_FORMAT, "files": records, "units": unit_states}
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
    active = [unit for unit in reversed(UNITS) if states[unit]["active"] == "active"]
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
    for unit in START_ORDER:
        if states[unit]["active"] == "active" and unit not in also_active:
            _run(
                ["systemctl", "--job-mode=ignore-dependencies", "start", unit],
                run,
                check=True,
            )


def _verify_active(states, run, *, also_active=()):
    expected = {
        unit for unit in UNITS if states[unit]["active"] == "active"
    } | set(also_active)
    current = _unit_states(run)
    actual = {unit for unit in UNITS if current[unit]["active"] == "active"}
    if actual != expected:
        raise UpdateError("managed active unit state does not match the recorded state")


def _verify_enabled(states, run):
    actual = _unit_states(run)
    for unit in UNITS:
        if actual[unit]["enabled"] != states[unit]["enabled"]:
            raise UpdateError("managed enabled unit state does not match the recorded state")


def _validate_backup_state(state, backup_dir, root):
    if not isinstance(state, dict) or state.get("format") != BACKUP_FORMAT:
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

    current = _unit_states(run)
    _stop_active(current, run)
    for record, path in records:
        _restore_record(record, path, backup_dir)
    _run(["systemctl", "daemon-reload"], run, check=True)
    _restore_enabled(state["units"], run)
    _restore_active(state["units"], run)
    _verify_enabled(state["units"], run)
    _verify_active(state["units"], run)
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
    create_backup(backup_dir, root, entries, states)
    try:
        _stop_active(states, run)
        generator_entry = next(item for item in entries if item["target"] == MODE_GENERATOR)
        migration_entry = next(item for item in entries if item["target"] == MODE_MIGRATION)
        for entry in entries:
            if entry is generator_entry:
                continue
            _atomic_install(entry["source_path"], entry["destination"], entry["mode"])

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
            states, run, also_active={"messagebox-mode-reconcile.path"}
        )
        _verify_active(states, run, also_active={"messagebox-mode-reconcile.path"})
    except BaseException as update_error:
        try:
            _rollback_locked(backup_dir, root=root, run=run)
        except BaseException as rollback_error:
            raise UpdateError(
                f"update failed and automatic rollback also failed: {rollback_error}"
            ) from update_error
        raise UpdateError("update failed; the recorded state was restored") from update_error
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
