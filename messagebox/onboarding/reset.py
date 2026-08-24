"""Wi-Fi onboarding reset for the operator command and physical button.

This module deliberately knows only about onboarding state and NetworkManager.
It must not read or modify Message Box contact or runtime data.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path

from messagebox.onboarding.paths import (
    ONBOARDING_COMPLETION_REQUEST_PATH,
    ONBOARDING_CONFIGURED_PATH,
    ONBOARDING_ENABLED_PATH,
    ONBOARDING_STATE_PATH,
)
from messagebox.onboarding.state import StateStore


DEFAULT_GPIO_PIN = 17
HOLD_SECONDS = 10.0
POLL_SECONDS = 0.05

ENABLED_PATH = ONBOARDING_ENABLED_PATH
CONFIGURED_PATH = ONBOARDING_CONFIGURED_PATH
STATE_PATH = ONBOARDING_STATE_PATH

RUNTIME_UNITS = (
    "messagebox-onboarding-voice.target",
    "messagebox-onboarding-button.service",
    "messagebox-onboarding-nfc.service",
    "messagebox-onboarding-complete.service",
    "messagebox.target",
    "messagebox-button.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
    "messagebox-dash.service",
    "messagebox-nfc.service",
)
SETUP_UNITS = (
    "messagebox-wifi-reset.service",
    "messagebox-onboarding-complete.path",
    "comitup.service",
)
ONBOARDING_START_UNITS = (
    "messagebox-onboarding-voice.path",
    "messagebox-onboarding-complete.path",
    "comitup.service",
)
ONBOARDING_UNITS = (
    "comitup-web.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-nfc.service",
    "messagebox-onboarding-complete.service",
    "messagebox-whatsapp-pairing.service",
    "comitup.service",
)
WIFI_TYPES = frozenset({"802-11-wireless", "wifi"})

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_ROOT = 2
EXIT_GPIO_UNAVAILABLE = 3


class ResetStatus(Enum):
    """Non-error outcomes from checking the physical reset gesture."""

    NOT_PRESSED = "not-pressed"
    RELEASED_EARLY = "released-early"
    RESET_COMPLETE = "reset-complete"


def _run(runner, arguments):
    return runner(arguments, check=True, capture_output=True, text=True, timeout=20)


def _stdout(result):
    output = result.stdout or ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="strict")
    return output


def wait_for_hold(
    button,
    *,
    clock=time.monotonic,
    sleeper=time.sleep,
    hold_seconds=HOLD_SECONDS,
    poll_seconds=POLL_SECONDS,
):
    """Return the gesture outcome without touching disk or running commands."""

    if not button.is_pressed:
        return ResetStatus.NOT_PRESSED

    deadline = clock() + hold_seconds
    while True:
        if not button.is_pressed:
            return ResetStatus.RELEASED_EARLY
        remaining = deadline - clock()
        if remaining <= 0:
            return ResetStatus.RESET_COMPLETE
        sleeper(min(poll_seconds, remaining))


def _atomic_write(path, content, *, mode, preserve_owner=False, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if owner is None and preserve_owner:
        try:
            info = path.lstat()
            owner = (info.st_uid, info.st_gid)
        except FileNotFoundError:
            pass

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            if owner is not None:
                try:
                    os.fchown(handle.fileno(), *owner)
                except PermissionError:
                    # Off-device callers may not be able to restore foreign ownership.
                    pass
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def list_infrastructure_wifi_profiles(runner):
    """Return UUIDs for Wi-Fi infrastructure profiles, excluding AP profiles."""

    result = _run(
        runner,
        ["nmcli", "--terse", "--fields", "UUID,TYPE", "connection", "show"],
    )
    profiles = []
    for line in _stdout(result).splitlines():
        uuid, separator, profile_type = line.partition(":")
        if not separator or not uuid or profile_type.strip().lower() not in WIFI_TYPES:
            continue
        mode_result = _run(
            runner,
            [
                "nmcli",
                "--get-values",
                "802-11-wireless.mode",
                "connection",
                "show",
                "uuid",
                uuid,
            ],
        )
        mode = _stdout(mode_result).strip().lower()
        # NetworkManager treats an unset Wi-Fi mode as infrastructure.
        if mode in {"", "infrastructure"}:
            profiles.append(uuid)
    return profiles


def perform_reset(
    *,
    runner,
    enabled_path=ENABLED_PATH,
    configured_path=CONFIGURED_PATH,
    state_path=STATE_PATH,
    completion_request_path=ONBOARDING_COMPLETION_REQUEST_PATH,
    state_clock=time.time,
):
    """Perform the confirmed reset transaction and restart Wi-Fi onboarding."""

    configured_info = Path(configured_path).lstat()
    if not stat.S_ISREG(configured_info.st_mode):
        raise RuntimeError("Wi-Fi onboarding is not configured")

    state_path = Path(state_path)
    state_directory = state_path.parent.stat()
    state_owner = (state_directory.st_uid, state_directory.st_gid)
    _run(runner, ["systemctl", "enable", *SETUP_UNITS])
    _atomic_write(enabled_path, b"enabled\n", mode=0o600, preserve_owner=True)
    _run(runner, ["systemctl", "stop", *RUNTIME_UNITS])
    _run(runner, ["systemctl", "stop", *ONBOARDING_UNITS])
    _run(runner, ["rm", "-f", "--", "/var/lib/comitup/dhcpleaseinfo"])
    StateStore(state_path, clock=state_clock, owner=state_owner).reset(recreate=True)
    Path(completion_request_path).unlink(missing_ok=True)

    for uuid in list_infrastructure_wifi_profiles(runner):
        _run(runner, ["nmcli", "connection", "delete", "uuid", uuid])

    _run(runner, ["nmcli", "radio", "wifi", "on"])
    # During the boot-button service this is queued until that service exits.
    _run(runner, ["systemctl", "--no-block", "start", *ONBOARDING_START_UNITS])


def reset_if_held(
    button,
    *,
    clock=time.monotonic,
    sleeper=time.sleep,
    runner=subprocess.run,
    enabled_path=ENABLED_PATH,
    configured_path=CONFIGURED_PATH,
    state_path=STATE_PATH,
    completion_request_path=ONBOARDING_COMPLETION_REQUEST_PATH,
    state_clock=time.time,
    hold_seconds=HOLD_SECONDS,
    poll_seconds=POLL_SECONDS,
):
    """Check the gesture and perform a reset only after a continuous hold."""

    status = wait_for_hold(
        button,
        clock=clock,
        sleeper=sleeper,
        hold_seconds=hold_seconds,
        poll_seconds=poll_seconds,
    )
    if status is not ResetStatus.RESET_COMPLETE:
        return status
    perform_reset(
        runner=runner,
        enabled_path=enabled_path,
        configured_path=configured_path,
        state_path=state_path,
        completion_request_path=completion_request_path,
        state_clock=state_clock,
    )
    return ResetStatus.RESET_COMPLETE


def _gpio_button():
    from gpiozero import Button  # Lazy so tests and non-Pi imports need no GPIO stack.

    pin = int(os.environ.get("MSGBOX_BUTTON_PIN", str(DEFAULT_GPIO_PIN)))
    return Button(pin, pull_up=True)


def main(
    *,
    button=None,
    clock=None,
    sleeper=None,
    runner=None,
    enabled_path=ENABLED_PATH,
    configured_path=CONFIGURED_PATH,
    state_path=STATE_PATH,
    geteuid=None,
    stdout=None,
    stderr=None,
    force=False,
):
    """Run the root-only production reset check and return a process status."""

    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    runner = runner or subprocess.run
    geteuid = geteuid or os.geteuid
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    if geteuid() != 0:
        print("Wi-Fi reset must run as root.", file=stderr)
        return EXIT_NOT_ROOT

    if force:
        try:
            perform_reset(
                runner=runner,
                enabled_path=enabled_path,
                configured_path=configured_path,
                state_path=state_path,
            )
        except Exception:
            print("Wi-Fi reset failed; onboarding may need a manual restart.", file=stderr)
            return EXIT_FAILED
        print("Wi-Fi reset complete; setup hotspot start requested.", file=stdout)
        return EXIT_OK

    owns_button = button is None
    if owns_button:
        try:
            button = _gpio_button()
        except Exception:
            print("Wi-Fi reset button is unavailable; no reset was attempted.", file=stderr)
            return EXIT_GPIO_UNAVAILABLE

    try:
        status = reset_if_held(
            button,
            clock=clock,
            sleeper=sleeper,
            runner=runner,
            enabled_path=enabled_path,
            configured_path=configured_path,
            state_path=state_path,
        )
    except Exception:
        print("Wi-Fi reset failed; onboarding may need a manual restart.", file=stderr)
        return EXIT_FAILED
    finally:
        if owns_button:
            button.close()

    if status is ResetStatus.NOT_PRESSED:
        print("Wi-Fi reset button is not held; continuing startup.", file=stdout)
    elif status is ResetStatus.RELEASED_EARLY:
        print("Wi-Fi reset cancelled after the button was released.", file=stdout)
    else:
        print("Wi-Fi reset complete; setup hotspot start requested.", file=stdout)
    return EXIT_OK


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments not in ([], ["--force"]):
        print("Usage: reset.py [--force]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(force=arguments == ["--force"]))
