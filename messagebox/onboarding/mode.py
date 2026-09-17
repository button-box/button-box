"""Boot-mode authority and crash-safe current-boot reconciliation."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
import subprocess
import tempfile
from enum import Enum
from pathlib import Path

from messagebox.onboarding.paths import (
    MODE_RECONCILE_PENDING_PATH,
    MODE_TRANSITION_LOCK_PATH,
    ONBOARDING_ENABLED_PATH,
)


class Mode(Enum):
    RUNTIME = "runtime"
    SETUP = "setup"


class ModeError(RuntimeError):
    """The persistent mode authority or transition lock is unsafe."""


def read_mode(path=ONBOARDING_ENABLED_PATH, *, trusted_uid=0):
    """Read the marker without following links; absence selects runtime."""

    path = Path(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise ModeError("onboarding marker directory is unavailable or unsafe") from exc
    try:
        parent = os.fstat(directory)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != trusted_uid
            or parent.st_mode & 0o022
        ):
            raise ModeError("onboarding marker directory is unavailable or unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path.name, flags, dir_fd=directory)
        except FileNotFoundError:
            return Mode.RUNTIME
        except OSError as exc:
            raise ModeError("onboarding marker is unavailable or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != trusted_uid
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ModeError("onboarding marker is unavailable or unsafe")
            content = os.read(descriptor, 9)
            if content != b"enabled\n" or os.read(descriptor, 1):
                raise ModeError("onboarding marker is unavailable or unsafe")
            return Mode.SETUP
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_setup_marker(path=ONBOARDING_ENABLED_PATH):
    """Atomically commit setup mode using the existing marker format."""

    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(b"enabled\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def remove_setup_marker(path=ONBOARDING_ENABLED_PATH):
    Path(path).unlink()
    _fsync_directory(Path(path).parent)


@contextlib.contextmanager
def transition_lock(path=MODE_TRANSITION_LOCK_PATH, *, trusted_uid=None):
    """Serialize completion, reset, migration, and reconciliation."""

    path = Path(path)
    if trusted_uid is None:
        trusted_uid = os.geteuid()
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ModeError("mode transition lock is unavailable or unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except ModeError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EACCES):
            raise ModeError("mode transition lock is unavailable or unsafe") from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def queue_reconcile(
    *,
    run=subprocess.run,
    pending_path=MODE_RECONCILE_PENDING_PATH,
    reason="transition",
):
    """Persist a transient request before queuing the nonblocking worker."""

    pending_path = Path(pending_path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=pending_path.parent, prefix=".messagebox-mode-reconcile.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            if reason not in {"transition", "completion", "reset"}:
                raise ValueError("invalid reconciliation reason")
            handle.write(f"{reason}\n".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pending_path)
        temporary = None
        _fsync_directory(pending_path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    try:
        run(
            ["systemctl", "start", "--no-block", "messagebox-mode-reconcile.service"],
            check=True,
        )
    except BaseException:
        pending_path.unlink(missing_ok=True)
        _fsync_directory(pending_path.parent)
        raise


def _read_pending_reason(path, *, trusted_uid):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return ""
        content = os.read(descriptor, 12)
        if os.read(descriptor, 1):
            return ""
        reason = content.decode("ascii", errors="strict").strip()
        return reason if reason in {"transition", "completion", "reset"} else ""
    except (OSError, UnicodeError):
        return ""
    finally:
        os.close(descriptor)


def reconcile(
    *,
    enabled_path=ONBOARDING_ENABLED_PATH,
    lock_path=MODE_TRANSITION_LOCK_PATH,
    pending_path=MODE_RECONCILE_PENDING_PATH,
    run=subprocess.run,
    trusted_uid=0,
):
    """Converge only process state from the marker; never repeat reset work."""

    with transition_lock(lock_path, trusted_uid=trusted_uid):
        # Consume one transient request under the lock. A command failure is
        # bounded; a later actor or marker event can queue a fresh attempt.
        pending_reason = _read_pending_reason(pending_path, trusted_uid=trusted_uid)
        Path(pending_path).unlink(missing_ok=True)
        _fsync_directory(Path(pending_path).parent)
        mode = read_mode(enabled_path, trusted_uid=trusted_uid)
        setup_active = run(
            ["systemctl", "is-active", "--quiet", "comitup.service"],
            check=False,
            timeout=10,
        ).returncode == 0
        runtime_active = run(
            ["systemctl", "is-active", "--quiet", "messagebox.target"],
            check=False,
            timeout=10,
        ).returncode == 0
        if mode is Mode.SETUP:
            voice_active = run(
                [
                    "systemctl",
                    "is-active",
                    "--quiet",
                    "messagebox-onboarding-voice.target",
                ],
                check=False,
                timeout=10,
            ).returncode == 0
            run(["systemctl", "stop", "messagebox.target"], check=True, timeout=30)
            run(["systemctl", "start", "comitup.service"], check=True, timeout=30)
            if voice_active:
                run(
                    ["systemctl", "restart", "messagebox-onboarding-voice.target"],
                    check=True,
                    timeout=30,
                )
        elif mode is Mode.RUNTIME:
            run(["systemctl", "stop", "comitup.service"], check=True, timeout=30)
            if setup_active or (pending_reason == "completion" and not runtime_active):
                run(["systemctl", "restart", "avahi-daemon.service"], check=True, timeout=30)
            # Requeue the target transaction so any missing enabled Wants are
            # started even when the target itself was already active.
            run(["systemctl", "start", "messagebox.target"], check=True, timeout=30)
    return mode


def main():
    if os.geteuid() != 0:
        raise ModeError("mode reconciliation requires root")
    reconcile()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ModeError, OSError, subprocess.SubprocessError) as exc:
        print(f"mode reconciliation: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
