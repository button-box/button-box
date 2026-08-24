"""Privileged, interactive Wi-Fi onboarding initializer."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import grp
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from messagebox.onboarding.paths import INITIALIZER_PATHS
from messagebox.onboarding.state import StateStore


ONBOARDING_ACCOUNT = "messagebox-onboarding"
ONBOARDING_SERVICES = (
    "comitup.service",
    "comitup-web.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-nfc.service",
    "messagebox-onboarding-complete.service",
    "messagebox-whatsapp-pairing.service",
)
INTERRUPT_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
COMITUP_BIN = "/usr/sbin/comitup"
UNAMBIGUOUS_CHARACTERS = "23456789abcdefghjkmnpqrstuvwxyz"
PASSWORD_PLACEHOLDER = "@HOTSPOT_PASSWORD@"
_HOSTNAME = re.compile(r"message-box-([a-z0-9-]{1,32})\Z")
_PLACEHOLDER = re.compile(r"@[A-Z0-9_]+@")

EXIT_OK = 0
EXIT_FAILED = 1


class InitializationError(RuntimeError):
    """A safe initializer error suitable for operator output."""


class _SignalAbort(Exception):
    pass


@dataclass(frozen=True)
class Dependencies:
    """Privileged and nondeterministic boundaries used by the initializer."""

    geteuid: object = os.geteuid
    gethostname: object = socket.gethostname
    getpwnam: object = pwd.getpwnam
    getgrnam: object = grp.getgrnam
    choice: object = secrets.choice
    run: object = subprocess.run
    access: object = os.access
    chown: object = os.chown
    fchown: object = os.fchown
    replace: object = os.replace
    copy2: object = shutil.copy2
    mkdtemp: object = tempfile.mkdtemp
    rmtree: object = shutil.rmtree
    set_signal: object = signal.signal
    state_store: object = StateStore
    fstat: object = os.fstat


def _parser():
    return argparse.ArgumentParser(
        prog="messagebox-init-wifi-onboarding",
        description="Generate this device's private Wi-Fi onboarding identity"
    )


def _exists_or_symlink(path):
    path = Path(path)
    return path.exists() or path.is_symlink()


def _preflight(paths, dependencies):
    for marker, message in (
        (paths.enabled, "Wi-Fi onboarding is already armed."),
        (paths.configured, "Wi-Fi onboarding is already configured."),
    ):
        if _exists_or_symlink(marker):
            raise InitializationError(message)

    for path in (paths.comitup_boot_config, paths.comitup_firmware_config):
        if _exists_or_symlink(path):
            raise InitializationError(
                "Remove boot Comitup configuration overrides before configuring."
            )

    for unit in ONBOARDING_SERVICES:
        result = dependencies.run(
            ["systemctl", "is-active", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            raise InitializationError(f"Stop {unit} before configuring.")

    for path in (paths.config, paths.state, paths.obsolete_session_key):
        if Path(path).is_symlink():
            raise InitializationError(
                f"Refusing symlinked onboarding credential: {path}"
            )
    if Path(paths.comitup_config).is_symlink():
        raise InitializationError("Refusing symlink at /etc/comitup.conf.")
    if not dependencies.access(paths.template, os.R_OK):
        raise InitializationError("Missing Comitup configuration template.")
    try:
        account = dependencies.getpwnam(ONBOARDING_ACCOUNT)
        group = dependencies.getgrnam(ONBOARDING_ACCOUNT)
    except KeyError as exc:
        raise InitializationError("Missing messagebox-onboarding account.") from exc
    return account.pw_uid, group.gr_gid


@contextlib.contextmanager
def _initializer_lock(path, dependencies):
    descriptor = None
    created = False
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if Path(path).is_symlink():
            raise InitializationError("Initializer lock path is unsafe.")
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, flags)

        metadata = dependencies.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_nlink != 1
            or (not created and mode != 0o600)
        ):
            raise InitializationError("Initializer lock path is unsafe.")
        if created:
            os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except InitializationError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise InitializationError(
                "Wi-Fi onboarding initialization is already running."
            ) from exc
        raise InitializationError("Initializer lock path is unavailable or unsafe.") from exc

    try:
        yield
    finally:
        os.close(descriptor)


def _write_private(path, content):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_install(source, destination, *, mode, owner, dependencies):
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), mode)
            dependencies.fchown(output.fileno(), *owner)
            with open(source, "rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        dependencies.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore_copy(backup, destination, metadata, dependencies):
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.restore.", delete=False
        ) as handle:
            temporary = Path(handle.name)
        dependencies.copy2(backup, temporary)
        dependencies.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.chmod(temporary, metadata.st_mode)
        os.utime(
            temporary,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
        )
        dependencies.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _ComitupTransaction:
    def __init__(self, paths, temporary_dir, dependencies):
        self.paths = paths
        self.backup = Path(temporary_dir) / "comitup.conf.previous"
        self.dependencies = dependencies
        self.previous_metadata = None
        self.replaced = False
        self.committed = False

    def install(self, candidate):
        target = Path(self.paths.comitup_config)
        if target.exists():
            self.previous_metadata = target.stat()
            self.dependencies.copy2(target, self.backup)
        # From this point onward, any failure must restore the original target.
        # Set the flag before replace so a signal cannot land in an unsafe gap.
        self.replaced = True
        _atomic_install(
            candidate,
            target,
            mode=0o600,
            owner=(0, 0),
            dependencies=self.dependencies,
        )

    def rollback(self):
        if not self.replaced or self.committed:
            return
        target = Path(self.paths.comitup_config)
        if self.previous_metadata is None:
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)
        else:
            _restore_copy(
                self.backup,
                target,
                self.previous_metadata,
                self.dependencies,
            )
        self.replaced = False


def _make_candidates(paths, temporary_dir, dependencies):
    hostname = dependencies.gethostname().lower()
    match = _HOSTNAME.fullmatch(hostname)
    if match is None:
        raise InitializationError("hostname must match message-box-ID")
    device_id = match.group(1)
    password_raw = "".join(
        dependencies.choice(UNAMBIGUOUS_CHARACTERS) for _ in range(8)
    )
    password = f"{password_raw[:4]}-{password_raw[4:]}"

    metadata = {
        "version": 1,
        "device_id": device_id,
        "canonical_host": f"{hostname}.local",
    }
    output = Path(temporary_dir)
    _write_private(
        output / "config.json",
        (
            json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii"),
    )
    dependencies.state_store(output / "state.json").initialize()

    template = Path(paths.template).read_text(encoding="ascii")
    if template.count(PASSWORD_PLACEHOLDER) != 1:
        raise InitializationError(
            "Comitup template must contain exactly one hotspot password placeholder."
        )
    rendered = template.replace(PASSWORD_PLACEHOLDER, password)
    if _PLACEHOLDER.search(rendered):
        raise InitializationError("unresolved Comitup template placeholder")
    _write_private(output / "comitup.conf", rendered.encode("ascii"))
    _write_private(
        output / "card", f"{device_id}\n{hostname}\n{password}\n".encode("ascii")
    )
    return device_id, hostname, password


def _display_and_confirm(device_id, hostname, password, stdin, stdout):
    print(
        f"\nDevice ID:        {device_id}\n"
        f"Hotspot:          {hostname}\n"
        f"Hotspot password: {password}\n"
        f"Setup URL:        http://{hostname}.local/\n",
        file=stdout,
    )
    print(
        "Wi-Fi password recorded? [y/N] ",
        end="",
        file=stdout,
        flush=True,
    )
    confirmation = stdin.readline().strip().casefold()
    if confirmation not in {"y", "yes"}:
        raise InitializationError(
            "Confirmation declined; no credentials were installed."
        )


def _install_directory(path, *, mode, owner, dependencies):
    Path(path).mkdir(parents=True, exist_ok=True)
    dependencies.chown(path, *owner)
    os.chmod(path, mode)


@contextlib.contextmanager
def _ignore_interrupts(dependencies):
    previous_handlers = {}
    try:
        for signum in INTERRUPT_SIGNALS:
            previous_handlers[signum] = dependencies.set_signal(
                signum, signal.SIG_IGN
            )
        yield
    finally:
        for signum, handler in previous_handlers.items():
            dependencies.set_signal(signum, handler)


def _commit_marker(path, onboarding_gid, dependencies, transaction):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=".configured.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o640)
            dependencies.fchown(handle.fileno(), 0, onboarding_gid)
            handle.write(b"configured\n")
            handle.flush()
            os.fsync(handle.fileno())
        with _ignore_interrupts(dependencies):
            try:
                dependencies.replace(temporary, path)
            except BaseException:
                if path.exists() and not path.is_symlink():
                    path.unlink()
                    _fsync_directory(path.parent)
                raise
            temporary = None
            try:
                _fsync_directory(path.parent)
            except BaseException:
                path.unlink(missing_ok=True)
                _fsync_directory(path.parent)
                raise
            transaction.committed = True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _initialize_locked(paths, dependencies, stdin, stdout):
    onboarding_uid, onboarding_gid = _preflight(paths, dependencies)
    temporary_dir = None
    transaction = None
    previous_handlers = {}
    interrupted_once = False
    cleaning_up = False

    def interrupted(signum, frame):
        nonlocal interrupted_once
        if cleaning_up or (transaction is not None and transaction.committed):
            return
        if interrupted_once:
            return
        interrupted_once = True
        raise _SignalAbort

    try:
        for signum in INTERRUPT_SIGNALS:
            previous_handlers[signum] = dependencies.set_signal(signum, interrupted)

        for path in (paths.config, paths.state, paths.obsolete_session_key):
            Path(path).unlink(missing_ok=True)

        temporary_dir = Path(
            dependencies.mkdtemp(
                prefix="messagebox-onboarding-configure.", dir=paths.run_dir
            )
        )
        os.chmod(temporary_dir, 0o700)
        transaction = _ComitupTransaction(paths, temporary_dir, dependencies)

        device_id, hostname, password = _make_candidates(
            paths, temporary_dir, dependencies
        )
        _display_and_confirm(device_id, hostname, password, stdin, stdout)
        comitup_candidate = temporary_dir / "comitup.conf"
        transaction.install(comitup_candidate)
        dependencies.run(
            [COMITUP_BIN, "--check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        if Path(paths.comitup_config).read_bytes() != comitup_candidate.read_bytes():
            raise InitializationError("Comitup changed /etc/comitup.conf during validation.")

        _install_directory(
            paths.config_dir,
            mode=0o750,
            owner=(0, onboarding_gid),
            dependencies=dependencies,
        )
        _install_directory(
            paths.state_dir,
            mode=0o700,
            owner=(onboarding_uid, onboarding_gid),
            dependencies=dependencies,
        )
        _atomic_install(
            temporary_dir / "config.json",
            paths.config,
            mode=0o640,
            owner=(0, onboarding_gid),
            dependencies=dependencies,
        )
        _atomic_install(
            temporary_dir / "state.json",
            paths.state,
            mode=0o600,
            owner=(onboarding_uid, onboarding_gid),
            dependencies=dependencies,
        )
        _commit_marker(
            paths.configured, onboarding_gid, dependencies, transaction
        )
    except _SignalAbort:
        if transaction is None or not transaction.committed:
            raise
    finally:
        cleaning_up = True
        try:
            if transaction is not None:
                transaction.rollback()
        finally:
            try:
                if temporary_dir is not None:
                    dependencies.rmtree(temporary_dir, ignore_errors=True)
            finally:
                for signum, handler in previous_handlers.items():
                    dependencies.set_signal(signum, handler)


def _initialize(paths, dependencies, stdin, stdout):
    with _initializer_lock(paths.lock, dependencies):
        _initialize_locked(paths, dependencies, stdin, stdout)


def main(
    argv=None,
    *,
    paths=INITIALIZER_PATHS,
    dependencies=None,
    stdin=None,
    stdout=None,
    stderr=None,
):
    """Initialize Wi-Fi onboarding and return a process exit status."""

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        _parser().parse_args(argv)
    dependencies = Dependencies() if dependencies is None else dependencies

    if dependencies.geteuid() != 0:
        print("Run with sudo.", file=stderr)
        return EXIT_FAILED
    if not stdin.isatty() or not stdout.isatty():
        print("Configuration requires an interactive terminal.", file=stderr)
        return EXIT_FAILED

    try:
        _initialize(paths, dependencies, stdin, stdout)
    except InitializationError as exc:
        print(str(exc), file=stderr)
        return EXIT_FAILED
    except _SignalAbort:
        print("Configuration interrupted; no credentials were installed.", file=stderr)
        return EXIT_FAILED
    except Exception:
        print(
            "Wi-Fi onboarding configuration failed. Verify /etc/comitup.conf before retrying.",
            file=stderr,
        )
        return EXIT_FAILED

    print(
        "\nWi-Fi onboarding is configured but not started. The hotspot password "
        "is stored root-only in /etc/comitup.conf.\n"
        "Run: sudo messageboxctl reset-wifi",
        file=stdout,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
