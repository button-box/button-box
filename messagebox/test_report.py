"""Sanitized, test-rig-only evidence bundles for Button Box failures."""

from __future__ import annotations

import json
import math
import re
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
TEST_RIG_CONFIG_PATH = Path("/etc/messagebox-test-rig.json")
REVISION_PATH = Path("/opt/messagebox/REVISION")
RUN_STATE_PATH = Path("/var/lib/messagebox-test/run.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

SERVICES = (
    "messagebox-button.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
    "messagebox-dash.service",
    "messagebox-nfc.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-button.service",
    "messagebox-whatsapp-pairing.service",
    "messagebox-onboarding-nfc.service",
    "messagebox-onboarding-voice-gate.service",
    "messagebox-onboarding-complete.service",
    "messagebox-wifi-change.service",
)
RUN_STEPS = frozenset(
    {
        "hotspot-visible",
        "phone-connected",
        "portal-opened",
        "wrong-password-recovered",
        "home-wifi-connected",
        "whatsapp-linked",
        "recipient-selected",
        "nfc-routed",
        "cold-reboot-resumed",
        "button-press-detected",
        "led-activated",
        "speaker-played",
        "microphone-recorded",
        "nfc-read",
        "message-received",
        "message-played",
        "reply-recorded",
        "reply-played-back",
        "reply-sent",
        "recipient-received",
    }
)
TRANSITION_EVENTS = frozenset(
    {
        "received",
        "played",
        "guided_press",
        "guided_session_started",
        "guided_inbound_played",
        "guided_playback_only",
        "guided_review_played",
        "guided_approved",
        "guided_recording_empty",
        "guided_deleted",
        "guided_session_interrupted",
        "button_flow_error",
    }
)

_DEVICE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_REVISION = re.compile(r"[0-9a-f]{7,40}")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_ -]?key)\s*[:=]\s*\S+"
)
_WHATSAPP_ID = re.compile(r"\b[^\s@]+@(s\.whatsapp\.net|g\.us)\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)\+?(?:\d[\s().-]?){7,}\d(?!\w)")
_IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


class TestRigError(RuntimeError):
    """A safe test-rig configuration or collection failure."""


class TestRigDisabled(TestRigError):
    """The device is not explicitly configured as a test rig."""


def _short_hostname(hostname=None):
    value = hostname if hostname is not None else socket.gethostname()
    return value.split(".", 1)[0].lower()


def _regular_file(path):
    path = Path(path)
    return path.is_file() and not path.is_symlink()


def test_rig_available(config_path=TEST_RIG_CONFIG_PATH, *, hostname=None):
    """Return whether a valid marker explicitly names this exact device."""
    try:
        load_test_rig_config(config_path, hostname=hostname)
    except TestRigError:
        return False
    return True


def load_test_rig_config(config_path=TEST_RIG_CONFIG_PATH, *, hostname=None):
    path = Path(config_path)
    if not _regular_file(path):
        raise TestRigDisabled("test reporting is disabled")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TestRigError("test rig configuration is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "device"}
        or document.get("version") != SCHEMA_VERSION
        or not isinstance(document.get("device"), str)
        or not _DEVICE.fullmatch(document["device"])
    ):
        raise TestRigError("test rig configuration is invalid")
    actual = _short_hostname(hostname)
    if document["device"].lower() != actual:
        raise TestRigError("test rig marker does not match this device")
    return {"version": SCHEMA_VERSION, "device": actual}


def redact_text(value, limit=400):
    """Conservatively scrub common private identifiers from a short tester note."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\x00", " ").split())[:limit]
    text = _SECRET_ASSIGNMENT.sub("[redacted secret]", text)
    text = _WHATSAPP_ID.sub("[redacted WhatsApp ID]", text)
    text = _PHONE.sub("[redacted number]", text)
    text = _IPV4.sub("[redacted address]", text)
    return text


def _safe_token(value, default="unknown"):
    return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else default


def _safe_count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else 0


def _safe_bool(value):
    return value if type(value) is bool else False


def _read_token(path, pattern, default="unknown"):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return default
    return value if pattern.fullmatch(value) else default


def _onboarding_snapshot(state):
    state = state if isinstance(state, dict) else {}
    whatsapp = state.get("whatsapp") if isinstance(state.get("whatsapp"), dict) else {}
    recipient = (
        state.get("recipient_setup")
        if isinstance(state.get("recipient_setup"), dict)
        else {}
    )
    proof = recipient.get("proof") if isinstance(recipient.get("proof"), dict) else {}
    nfc = state.get("nfc_setup") if isinstance(state.get("nfc_setup"), dict) else {}
    return {
        "mode": _safe_token(state.get("mode")),
        "phase": _safe_token(state.get("phase")),
        "error": _safe_token(state.get("safe_error"), "none"),
        "whatsapp": {
            "status": _safe_token(whatsapp.get("status")),
            "error": _safe_token(whatsapp.get("safe_error"), "none"),
            "eligible_count": _safe_count(whatsapp.get("eligible_count")),
        },
        "recipient": {
            "status": _safe_token(recipient.get("status")),
            "configured_count": _safe_count(recipient.get("configured_count")),
            "available_count": _safe_count(recipient.get("available_count")),
            "proof": {
                "received": _safe_bool(proof.get("received")),
                "played": _safe_bool(proof.get("played")),
                "replied": _safe_bool(proof.get("replied")),
            },
        },
        "nfc": {
            "status": _safe_token(nfc.get("status")),
            "mapped_count": _safe_count(nfc.get("mapped_count")),
        },
    }


def _dashboard_snapshot(state):
    state = state if isinstance(state, dict) else {}
    transitions = state.get("transitions")
    transitions = transitions if isinstance(transitions, dict) else {}
    return {
        "queue_count": _safe_count(state.get("queue_count")),
        "hold_count": _safe_count(state.get("hold_count")),
        "trash_count": _safe_count(state.get("trash_count")),
        "event_count": _safe_count(state.get("event_count")),
        "button": {
            "raw_edge_at": "unavailable",
            "confirmed_press_at": _safe_token(
                transitions.get("guided_press"), "unavailable"
            ),
        },
        "transitions": {
            kind: _safe_token(transitions.get(kind), "unavailable")
            for kind in sorted(TRANSITION_EVENTS)
            if kind in transitions
        },
    }


def event_transition_snapshot(events):
    """Return only latest timestamps for fixed, content-free runtime transitions."""
    latest = {}
    for event in events if isinstance(events, list) else ():
        if not isinstance(event, dict) or event.get("type") not in TRANSITION_EVENTS:
            continue
        timestamp = event.get("ts")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            continue
        if not math.isfinite(timestamp) or timestamp < 0:
            continue
        kind = event["type"]
        latest[kind] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    return latest


def surface_snapshot(surface, state):
    if surface == "onboarding":
        return _onboarding_snapshot(state)
    if surface == "dashboard":
        return _dashboard_snapshot(state)
    return {}


def _service_snapshot(unit, runner=subprocess.run):
    try:
        result = runner(
            [
                "systemctl",
                "show",
                unit,
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--property=NRestarts",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"active": "unknown", "sub": "unknown", "result": "unknown", "restarts": 0}
    values = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
    try:
        restarts = int(values.get("NRestarts", "0"))
    except ValueError:
        restarts = 0
    return {
        "active": _safe_token(values.get("ActiveState")),
        "sub": _safe_token(values.get("SubState")),
        "result": _safe_token(values.get("Result")),
        "restarts": max(0, min(restarts, 1_000_000)),
    }


def _run_snapshot(run_state_path):
    path = Path(run_state_path)
    if not _regular_file(path):
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("version") != SCHEMA_VERSION:
        return None
    steps = document.get("steps") if isinstance(document.get("steps"), dict) else {}
    safe_steps = {
        key: _safe_token(value)
        for key, value in steps.items()
        if key in RUN_STEPS
    }
    return {
        "id": _safe_token(document.get("id")),
        "scenario": _safe_token(document.get("scenario")),
        "status": _safe_token(document.get("status")),
        "started_at": _safe_token(document.get("started_at")),
        "updated_at": _safe_token(document.get("updated_at")),
        "steps": safe_steps,
    }


def build_report(
    surface,
    *,
    note="",
    surface_state=None,
    config_path=TEST_RIG_CONFIG_PATH,
    revision_path=REVISION_PATH,
    run_state_path=RUN_STATE_PATH,
    boot_id_path=BOOT_ID_PATH,
    hostname=None,
    runner=subprocess.run,
    clock=time.time,
    path_exists=None,
):
    """Build one bounded report from fixed, allowlisted facts only."""
    config = load_test_rig_config(config_path, hostname=hostname)
    now = datetime.fromtimestamp(clock(), timezone.utc).isoformat().replace("+00:00", "Z")
    exists = path_exists or (lambda path: Path(path).exists())
    report = {
        "schema_version": SCHEMA_VERSION,
        "report_id": str(uuid.uuid4()),
        "generated_at": now,
        "device": {
            "name": config["device"],
            "boot_id": _read_token(boot_id_path, re.compile(r"[0-9a-f-]{36}")),
        },
        "software": {"revision": _read_token(revision_path, _REVISION)},
        "surface": _safe_token(surface),
        "state": surface_snapshot(surface, surface_state),
        "test_run": _run_snapshot(run_state_path),
        "services": {unit: _service_snapshot(unit, runner) for unit in SERVICES},
        "hardware": {
            "gpio": bool(exists("/dev/gpiochip0")),
            "i2c": bool(exists("/dev/i2c-1")),
            "audio": bool(exists("/proc/asound/cards")),
        },
        "note": redact_text(note),
        "privacy": "automatic fields are allowlisted metadata only; the optional tester note is user supplied and redacted for common identifiers",
    }
    return report


def report_to_markdown(report):
    device = report["device"]
    software = report["software"]
    lines = [
        "# Button Box test report",
        "",
        f"- Report: `{report['report_id']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Device: `{device['name']}`",
        f"- Boot: `{device['boot_id']}`",
        f"- Revision: `{software['revision']}`",
        f"- Surface: `{report['surface']}`",
    ]
    if report.get("note"):
        lines.extend((f"- Tester note: {report['note']}",))
    run = report.get("test_run")
    if run:
        lines.extend(("", "## Test run", "", f"- `{run['scenario']}` — `{run['status']}` — `{run['id']}`"))
        for step, status in run["steps"].items():
            lines.append(f"- `{step}`: `{status}`")
    lines.extend(("", "## App state", "", "```json", json.dumps(report["state"], indent=2, sort_keys=True), "```"))
    lines.extend(("", "## Services", ""))
    for unit, status in report["services"].items():
        lines.append(
            f"- `{unit}`: `{status['active']}/{status['sub']}`; result `{status['result']}`; restarts `{status['restarts']}`"
        )
    hardware = report["hardware"]
    lines.extend(
        (
            "",
            "## Hardware visibility",
            "",
            f"- GPIO: `{str(hardware['gpio']).lower()}`",
            f"- I2C: `{str(hardware['i2c']).lower()}`",
            f"- Audio: `{str(hardware['audio']).lower()}`",
            "",
            f"Privacy: {report['privacy']}",
        )
    )
    return "\n".join(lines) + "\n"
