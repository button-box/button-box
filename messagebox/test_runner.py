"""Explicit operator workflow for reproducible Button Box test runs."""

from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from messagebox.test_report import (
    RUN_STATE_PATH,
    SCHEMA_VERSION,
    TEST_RIG_CONFIG_PATH,
    TestRigError,
    build_report,
    report_to_markdown,
)


SCENARIOS = {
    "smoke": (
        "portal-opened",
        "home-wifi-connected",
        "whatsapp-linked",
        "button-press-detected",
        "speaker-played",
    ),
    "hardware": (
        "button-press-detected",
        "led-activated",
        "speaker-played",
        "microphone-recorded",
        "nfc-read",
    ),
    "onboarding": (
        "hotspot-visible",
        "phone-connected",
        "portal-opened",
        "wrong-password-recovered",
        "home-wifi-connected",
        "whatsapp-linked",
        "recipient-selected",
        "nfc-routed",
        "cold-reboot-resumed",
    ),
    "message-loop": (
        "message-received",
        "message-played",
        "reply-recorded",
        "reply-played-back",
        "reply-sent",
        "recipient-received",
    ),
}
SCENARIOS["full"] = tuple(
    dict.fromkeys(
        SCENARIOS["smoke"]
        + SCENARIOS["onboarding"]
        + SCENARIOS["hardware"]
        + SCENARIOS["message-loop"]
    )
)
RESULTS = {"pass", "fail", "skip"}


def _timestamp(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path, document, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _require_root(geteuid=os.geteuid):
    if geteuid() != 0:
        raise TestRigError("run this command with sudo")


def enable(device, *, config_path=TEST_RIG_CONFIG_PATH, hostname=None, geteuid=os.geteuid):
    _require_root(geteuid)
    actual = (hostname or socket.gethostname()).split(".", 1)[0].lower()
    expected = device.lower()
    if expected != actual:
        raise TestRigError(f"target mismatch: expected {expected}, running on {actual}")
    _atomic_json(config_path, {"version": SCHEMA_VERSION, "device": actual})


def disable(*, config_path=TEST_RIG_CONFIG_PATH, geteuid=os.geteuid):
    _require_root(geteuid)
    try:
        Path(config_path).unlink()
    except FileNotFoundError:
        pass


def start_run(scenario, *, state_path=RUN_STATE_PATH, clock=time.time, geteuid=os.geteuid):
    _require_root(geteuid)
    if scenario not in SCENARIOS:
        raise TestRigError("unknown test scenario")
    now = _timestamp(clock)
    document = {
        "version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "scenario": scenario,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "steps": {step: "pending" for step in SCENARIOS[scenario]},
    }
    _atomic_json(state_path, document)
    return document


def _load_run(state_path):
    try:
        document = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TestRigError("no readable test run is active") from exc
    if not isinstance(document, dict) or document.get("status") != "running":
        raise TestRigError("no test run is active")
    scenario = document.get("scenario")
    if scenario not in SCENARIOS or set(document.get("steps", {})) != set(SCENARIOS[scenario]):
        raise TestRigError("test run state is invalid")
    return document


def record_step(step, result, *, state_path=RUN_STATE_PATH, clock=time.time, geteuid=os.geteuid):
    _require_root(geteuid)
    document = _load_run(state_path)
    if step not in document["steps"]:
        raise TestRigError("step is not part of the active scenario")
    if result not in RESULTS:
        raise TestRigError("result must be pass, fail, or skip")
    document["steps"][step] = result
    document["updated_at"] = _timestamp(clock)
    _atomic_json(state_path, document)
    return document


def finish_run(*, state_path=RUN_STATE_PATH, clock=time.time, geteuid=os.geteuid):
    _require_root(geteuid)
    document = _load_run(state_path)
    results = set(document["steps"].values())
    if "fail" in results:
        document["status"] = "failed"
    elif "pending" in results:
        document["status"] = "incomplete"
    else:
        document["status"] = "passed"
    document["updated_at"] = _timestamp(clock)
    _atomic_json(state_path, document)
    return document


def _parser():
    parser = argparse.ArgumentParser(prog="messagebox-test")
    commands = parser.add_subparsers(dest="command", required=True)
    enable_parser = commands.add_parser("enable", help="mark this exact device as a test rig")
    enable_parser.add_argument("--device", required=True)
    commands.add_parser("disable", help="remove the test-rig marker")
    start_parser = commands.add_parser("start", help="start a guided physical scenario")
    start_parser.add_argument("scenario", choices=sorted(SCENARIOS))
    record_parser = commands.add_parser("record", help="record one physical step")
    record_parser.add_argument("step")
    record_parser.add_argument("result", choices=sorted(RESULTS))
    commands.add_parser("finish", help="finish the active scenario")
    report_parser = commands.add_parser("report", help="print a sanitized report")
    report_parser.add_argument("--surface", choices=("onboarding", "dashboard"), default="dashboard")
    report_parser.add_argument("--note", default="")
    report_parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "enable":
            enable(args.device)
            print(f"Test rig enabled for {args.device}.")
            return 0
        if args.command == "disable":
            disable()
            print("Test rig disabled.")
            return 0
        if args.command == "start":
            document = start_run(args.scenario)
            print(f"Started {document['scenario']} run {document['id']}")
            for step in document["steps"]:
                print(f"  sudo messagebox-test record {step} pass|fail|skip")
            return 0
        if args.command == "record":
            document = record_step(args.step, args.result)
            print(f"{args.step}: {document['steps'][args.step]}")
            return 0
        if args.command == "finish":
            document = finish_run()
            print(f"Run {document['id']}: {document['status']}")
            return 0 if document["status"] == "passed" else 1
        report = build_report(args.surface, note=args.note)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else report_to_markdown(report), end="")
        return 0
    except (OSError, TestRigError) as exc:
        print(f"messagebox-test: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
