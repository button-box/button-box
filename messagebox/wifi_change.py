"""Transactional, root-isolated Wi-Fi changes requested by the dashboard."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
import unicodedata
from pathlib import Path

from messagebox.onboarding.connectivity import ConnectivityChecker
from messagebox.onboarding.reset import perform_reset
from messagebox.runtime_paths import SETTINGS_DIR


REQUEST_FILE = SETTINGS_DIR / "wifi-change.json"
STATUS_FILE = SETTINGS_DIR / "wifi-change-status.json"
PROOFS = {
    "wlan0_nm_active",
    "wlan0_non_ap",
    "wlan0_ipv4",
    "wlan0_default_route",
    "wlan0_dns",
    "wlan0_https",
}


class WifiChangeError(ValueError):
    """A safe caregiver-facing Wi-Fi transaction error."""


def validate_request(document):
    if not isinstance(document, dict) or set(document) != {"ssid", "password", "security"}:
        raise WifiChangeError("Wi-Fi request is invalid")
    ssid = document["ssid"]
    password = document["password"]
    security = document["security"]
    if not isinstance(ssid, str) or not ssid:
        raise WifiChangeError("Wi-Fi name is required")
    try:
        ssid_bytes = ssid.encode("utf-8")
    except UnicodeError as exc:
        raise WifiChangeError("Wi-Fi name is invalid") from exc
    if len(ssid_bytes) > 32 or any(unicodedata.category(character) == "Cc" for character in ssid):
        raise WifiChangeError("Wi-Fi name is invalid")
    if not isinstance(password, str) or security not in {"protected", "open"}:
        raise WifiChangeError("Wi-Fi security is invalid")
    password_bytes = password.encode("utf-8")
    if security == "open" and password:
        raise WifiChangeError("Open networks do not use a password")
    if security == "protected" and not 8 <= len(password_bytes) <= 63:
        raise WifiChangeError("Wi-Fi password must contain 8 to 63 UTF-8 bytes")
    return {"ssid": ssid, "password": password, "security": security}


def request_change(document, path=REQUEST_FILE):
    document = validate_request(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise WifiChangeError("Another Wi-Fi change is already running") from exc
    try:
        payload = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        os.write(descriptor, payload.encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_status(path, status, message, *, network=None):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    document = {"version": 1, "status": status, "message": message, "network": network}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    try:
        os.write(
            descriptor,
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def load_status(path=STATUS_FILE):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "status": "idle", "message": "No Wi-Fi change is running", "network": None}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise WifiChangeError("Wi-Fi change status is unavailable") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "status", "message", "network"}
        or document["version"] != 1
        or document["status"] not in {"connecting", "connected", "rolled_back", "hotspot", "failed"}
        or not isinstance(document["message"], str)
        or (document["network"] is not None and not isinstance(document["network"], str))
    ):
        raise WifiChangeError("Wi-Fi change status is unavailable")
    return document


def _run(runner, command, *, input_text=None):
    return runner(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=40,
        input=input_text,
    )


def _active_uuid(runner):
    result = _run(
        runner,
        ["nmcli", "--get-values", "GENERAL.CON-UUID", "device", "show", "wlan0"],
    )
    value = (result.stdout or "").strip()
    if not value or len(value) > 64 or any(character not in "0123456789abcdefABCDEF-" for character in value):
        raise WifiChangeError("The current Wi-Fi profile is unavailable")
    return value


def execute_change(
    *,
    request_path=REQUEST_FILE,
    status_path=STATUS_FILE,
    runner=subprocess.run,
    checker=None,
    fallback=perform_reset,
    clock=time.time,
):
    request_path = Path(request_path)
    try:
        metadata = request_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise WifiChangeError("Wi-Fi change request is unsafe")
        if metadata.st_size > 8192:
            raise WifiChangeError("Wi-Fi change request is too large")
        request = validate_request(json.loads(request_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        request_path.unlink(missing_ok=True)
        raise WifiChangeError("Wi-Fi change request is unavailable") from exc
    try:
        old_uuid = _active_uuid(runner)
    except (OSError, subprocess.SubprocessError, WifiChangeError):
        request_path.unlink(missing_ok=True)
        raise
    profile_name = f"button-box-candidate-{int(clock())}"
    _write_status(status_path, "connecting", "Checking the new Wi-Fi connection", network=request["ssid"])
    command = ["nmcli"]
    password_input = None
    if request["security"] == "protected":
        command.append("--ask")
        password_input = request["password"] + "\n"
    command.extend(["device", "wifi", "connect", request["ssid"]])
    command.extend(["ifname", "wlan0", "name", profile_name])
    candidate_uuid = None
    try:
        _run(runner, command, input_text=password_input)
        candidate_uuid = _active_uuid(runner)
        result = (checker or ConnectivityChecker()).check()
        if result.get("ok") is not True or set(result.get("proof", ())) != PROOFS:
            raise WifiChangeError("The new Wi-Fi did not pass connectivity checks")
        if old_uuid != candidate_uuid:
            _run(runner, ["nmcli", "connection", "delete", "uuid", old_uuid])
        _write_status(status_path, "connected", "Button Box is connected", network=request["ssid"])
        return "connected"
    except (OSError, subprocess.SubprocessError, WifiChangeError):
        rollback_ok = False
        try:
            _run(runner, ["nmcli", "connection", "up", "uuid", old_uuid, "ifname", "wlan0"])
            rollback = (checker or ConnectivityChecker()).check()
            rollback_ok = rollback.get("ok") is True and set(rollback.get("proof", ())) == PROOFS
            if candidate_uuid and candidate_uuid != old_uuid:
                _run(runner, ["nmcli", "connection", "delete", "uuid", candidate_uuid])
        except (OSError, subprocess.SubprocessError, WifiChangeError):
            rollback_ok = False
        if rollback_ok:
            _write_status(status_path, "rolled_back", "The new network failed; the old Wi-Fi was restored")
            return "rolled_back"
        _write_status(status_path, "hotspot", "Wi-Fi recovery failed; reopening the setup hotspot")
        fallback(runner=runner, enable_units=False)
        return "hotspot"
    finally:
        request_path.unlink(missing_ok=True)


def main():
    if os.geteuid() != 0:
        raise WifiChangeError("Wi-Fi change requires root")
    outcome = execute_change()
    print(f"wifi change: {outcome}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, WifiChangeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"wifi change: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
