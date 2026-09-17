#!/usr/bin/python3
"""Install the boot-mode generator without crossing an unsafe link state."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import importlib.machinery
import os
import stat
import subprocess
import tempfile
from pathlib import Path


MARKER = Path("/etc/messagebox-onboarding/enabled")
LOCK = Path("/run/lock/messagebox-mode-transition.lock")
DESTINATION = Path("/usr/local/lib/systemd/system-generators/messagebox-mode-generator")
LEGACY_DIR = Path("/etc/systemd/system/multi-user.target.wants")
GENERATED_DIR = Path("/run/systemd/generator/multi-user.target.wants")
TARGET_WANTS = Path("/etc/systemd/system/messagebox.target.wants")
RECONCILE_PATH = "messagebox-mode-reconcile.path"
LEGACY = {
    "runtime": "messagebox.target",
    "setup": "comitup.service",
}
LEGACY_TARGETS = {
    "runtime": "/etc/systemd/system/messagebox.target",
    "setup": "/usr/lib/systemd/system/comitup.service",
}


class MigrationError(RuntimeError):
    pass


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_generator(path, *, trusted_uid=0):
    path = Path(path)
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != trusted_uid
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
    ):
        raise MigrationError("staged mode generator is unavailable or unsafe")
    loader = importlib.machinery.SourceFileLoader("messagebox_mode_generator", str(path))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _snapshot_directory(path):
    path = Path(path)
    if not path.exists():
        return {}
    return {
        item.name: os.readlink(item)
        for item in path.iterdir()
        if item.is_symlink()
    }


def _marker_fingerprint(path):
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError("onboarding marker is unavailable or unsafe")
    return (
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@contextlib.contextmanager
def _lock(path, *, trusted_uid):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise MigrationError("mode transition lock is unavailable or unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _validate_legacy_link(path, unit, expected_target):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if not path.is_symlink() or os.readlink(path) != expected_target:
        raise MigrationError(f"legacy {unit} enablement link is unsafe")


def _remove_link(path):
    Path(path).unlink(missing_ok=True)
    _fsync_directory(Path(path).parent)


def _atomic_install(source, destination, *, trusted_uid):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with open(source, "rb") as input_file, tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=".messagebox-mode-generator.", delete=False
        ) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o755)
            if os.geteuid() == 0:
                os.fchown(output.fileno(), trusted_uid, 0)
            output.write(input_file.read())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def migrate(
    generator_source,
    *,
    marker=MARKER,
    lock=LOCK,
    destination=DESTINATION,
    legacy_dir=LEGACY_DIR,
    generated_dir=GENERATED_DIR,
    target_wants=TARGET_WANTS,
    run=subprocess.run,
    trusted_uid=0,
):
    generator = _load_generator(generator_source, trusted_uid=trusted_uid)
    marker_before = _marker_fingerprint(marker)
    mode = generator.read_mode(marker, trusted_uid=trusted_uid)
    with tempfile.TemporaryDirectory(prefix="messagebox-mode-generator-check.") as output:
        generated_mode = generator.generate(output, marker, trusted_uid=trusted_uid)
        entries = _snapshot_directory(Path(output) / "multi-user.target.wants")
        expected = LEGACY[mode]
        if generated_mode != mode or set(entries) != {expected}:
            raise MigrationError("staged mode generator produced an invalid dependency")

    legacy_dir = Path(legacy_dir)
    legacy_paths = {name: legacy_dir / unit for name, unit in LEGACY.items()}
    for name, path in legacy_paths.items():
        _validate_legacy_link(path, LEGACY[name], LEGACY_TARGETS[name])
    target_wants_before = _snapshot_directory(target_wants)
    all_links_before = _snapshot_directory(legacy_dir)

    with _lock(lock, trusted_uid=trusted_uid):
        if generator.read_mode(marker, trusted_uid=trusted_uid) != mode:
            raise MigrationError("onboarding marker changed during migration preflight")
        _remove_link(legacy_paths["setup" if mode == "runtime" else "runtime"])
        _atomic_install(generator_source, destination, trusted_uid=trusted_uid)
        _remove_link(legacy_paths[mode])
        run(["systemctl", "daemon-reload"], check=True)
        run(["systemctl", "enable", "--now", RECONCILE_PATH], check=True)

        expected = LEGACY[mode]
        generated = _snapshot_directory(generated_dir)
        selected = set(generated).intersection(LEGACY.values())
        if selected != {expected}:
            raise MigrationError("installed mode generator did not select exactly one dependency")
        if _marker_fingerprint(marker) != marker_before:
            raise MigrationError("onboarding marker changed during migration")
        if _snapshot_directory(target_wants) != target_wants_before:
            raise MigrationError("runtime component selection changed during migration")
        after = _snapshot_directory(legacy_dir)
        expected_after = {
            name: target
            for name, target in all_links_before.items()
            if name not in set(LEGACY.values())
        }
        expected_after[RECONCILE_PATH] = str(
            Path("/etc/systemd/system") / RECONCILE_PATH
        )
        if after != expected_after:
            raise MigrationError("support-unit enablement changed during migration")
        active = run(
            ["systemctl", "is-active", "--quiet", RECONCILE_PATH],
            check=False,
        )
        if active.returncode != 0:
            raise MigrationError("mode reconciliation path is not active")
    return mode


def check(generator_source, *, marker=MARKER, legacy_dir=LEGACY_DIR, trusted_uid=0):
    """Validate marker, staged output, and any legacy links without mutation."""

    generator = _load_generator(generator_source, trusted_uid=trusted_uid)
    _marker_fingerprint(marker)
    mode = generator.read_mode(marker, trusted_uid=trusted_uid)
    with tempfile.TemporaryDirectory(prefix="messagebox-mode-generator-check.") as output:
        generator.generate(output, marker, trusted_uid=trusted_uid)
        entries = _snapshot_directory(Path(output) / "multi-user.target.wants")
        if set(entries) != {LEGACY[mode]}:
            raise MigrationError("staged mode generator produced an invalid dependency")
    legacy_dir = Path(legacy_dir)
    for name, unit in LEGACY.items():
        _validate_legacy_link(legacy_dir / unit, unit, LEGACY_TARGETS[name])
    return mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("generator_source")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise MigrationError("mode selector migration requires root")
    if arguments.check:
        check(arguments.generator_source)
    else:
        migrate(arguments.generator_source)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MigrationError, OSError, subprocess.SubprocessError) as exc:
        print(f"mode selector migration: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
