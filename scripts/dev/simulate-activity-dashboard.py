#!/usr/bin/env python3
"""Serve the real Activity dashboard against one completed scratch audio run.

Validation is the default.  ``--serve`` is an owner-only, loopback-only child
that uses the production dashboard Handler with all mutable paths bound to the
receiver scratch namespace before application imports.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import ipaddress
import json
import math
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from types import SimpleNamespace


MAX_RUNTIME_SECONDS = 599
TERMINATION_GRACE_SECONDS = 3
STATIC_GETS = frozenset({"/", "/static/app.js", "/static/styles.css", "/static/clipboard.js"})
API_GETS = frozenset({"/api/state", "/api/data"})
ACTIVITY_POSTS = frozenset({
    "/api/requeue", "/api/hold", "/api/resume", "/api/delete", "/api/reinstate",
})


def _load_receiver():
    path = Path(__file__).with_name("simulate-received-message.py")
    spec = importlib.util.spec_from_file_location("messagebox_activity_receiver", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("scratch receiver is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = _load_receiver()
selected = receiver.selected
SimulationError = receiver.SimulationError


def _file_snapshot(path):
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SimulationError("protected settings must be a regular file")
    return {
        "sha256": receiver._digest(path), "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid, "gid": metadata.st_gid,
    }


def _inside(root, value):
    root, value = Path(root).resolve(), Path(value).resolve()
    return value == root or root in value.parents


def import_scratch_dashboard(root):
    """Bind runtime_paths before importing any application module."""
    paths = receiver.bind_scratch_paths(root)
    from messagebox import contacts, nfc, nfc_state, settings
    from messagebox.dashboard import app as dashboard
    from messagebox.onboarding import recipients

    expected = {
        "dashboard.QUEUE_DIR": dashboard.QUEUE_DIR,
        "dashboard.HOLD_DIR": dashboard.HOLD_DIR,
        "dashboard.TRASH_DIR": dashboard.TRASH_DIR,
        "dashboard.PLAYED_DIR": dashboard.PLAYED_DIR,
        "dashboard.OUTBOX_DIR": dashboard.OUTBOX_DIR,
        "dashboard.EVENTS_FILE": dashboard.EVENTS_FILE,
        "dashboard.LISTENED_DIR": dashboard.LISTENED_DIR,
        "dashboard.RING_REQUEST_FILE": dashboard.RING_REQUEST_FILE,
        "contacts.CONTACTS_FILE": contacts.CONTACTS_FILE,
        "contacts.NFC_ENROLLMENT_FILE": contacts.NFC_ENROLLMENT_FILE,
        "nfc.CONTACTS_FILE": nfc.CONTACTS_FILE,
        "nfc.NFC_ANNOUNCEMENT_FILE": nfc.NFC_ANNOUNCEMENT_FILE,
        "nfc.NFC_ENROLLMENT_FILE": nfc.NFC_ENROLLMENT_FILE,
        "nfc.NFC_HEALTH_FILE": nfc.NFC_HEALTH_FILE,
        "nfc.NFC_SELECTION_FILE": nfc.NFC_SELECTION_FILE,
        "nfc_state.NFC_ANNOUNCEMENT_FILE": nfc_state.NFC_ANNOUNCEMENT_FILE,
        "recipients.CONTACTS_FILE": recipients.CONTACTS_FILE,
        "recipients.RECIPIENT_STATE_FILE": recipients.RECIPIENT_STATE_FILE,
        "recipients.EVENTS_FILE": recipients.EVENTS_FILE,
        "recipients.VOICE_REQUEST_FILE": recipients.VOICE_REQUEST_FILE,
        "recipients.NFC_ANNOUNCEMENT_FILE": recipients.NFC_ANNOUNCEMENT_FILE,
        "recipients.NFC_ENROLLMENT_FILE": recipients.NFC_ENROLLMENT_FILE,
        "recipients.NFC_SELECTION_FILE": recipients.NFC_SELECTION_FILE,
        "settings.SETTINGS_FILE": settings.SETTINGS_FILE,
    }
    outside = sorted(name for name, value in expected.items() if not _inside(root, value))
    if outside:
        raise SimulationError("scratch path audit failed: " + ", ".join(outside))
    if Path(paths.CONTACTS_FILE) != Path(dashboard.CONTACTS_FILE):
        raise SimulationError("dashboard contacts path was not scratch-bound")
    return dashboard


class ReadOnlySubprocess:
    """Allow only the exact read-only probes used by state and safety checks."""

    def __init__(self, original, wacli_bin="/usr/local/bin/wacli"):
        self.original = original
        self.wacli_bin = wacli_bin
        self.calls = []

    def __call__(self, args, *positional, **kwargs):
        command = tuple(os.fspath(value) for value in args)
        allowed = {
            ("systemctl", "is-active", "messagebox-button.service"),
            ("systemctl", "is-active", "messagebox-nfc.service"),
            ("systemctl", "is-active", "messagebox-poller.service"),
            ("systemctl", "is-active", "messagebox-sync.service"),
            ("systemctl", "is-active", "messagebox-dash.service"),
            ("systemctl", "is-active", "--quiet", "messagebox-button.service"),
            ("nmcli", "--get-values", "GENERAL.CONNECTION", "device", "show", "wlan0"),
            (self.wacli_bin, "--read-only", "--json", "auth", "status"),
        }
        if command not in allowed:
            raise SimulationError("dashboard child attempted a disallowed subprocess")
        self.calls.append(command)
        return self.original(args, *positional, **kwargs)


def _dashboard_inactive():
    result = subprocess.run(
        ["systemctl", "is-active", "messagebox-dash.service"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 3 or result.stdout.strip() != "inactive":
        raise SimulationError("production dashboard service must be verified inactive")


def _validate_completed_run(root, manifest, production, clock=time.time):
    receipt = receiver._receipt(root)
    authorization = receipt["authorization"]
    observation_sha = receiver._observation_sha(
        manifest["recipient"], manifest["target"]["sidecar_document"].get("sender_jid"),
        manifest["target"]["sidecar_document"].get("msgid"),
        manifest["target"]["sidecar_document"].get("media_type"),
        receipt["source_timestamp"], receipt["source_timestamp_unix"],
    )
    if (receipt["manifest_sha256"] != receiver._json_sha(manifest)
            or receipt["storage_sha256"] != receiver._storage_sha(production)
            or receipt["target_msgid"] != manifest["target"]["sidecar_document"].get("msgid")
            or receipt["target_msgid"] not in receiver._read_seen(production["seen"])
            or manifest["recipient"] != authorization["recipient"]
            or manifest["contacts_sha256"] != authorization["contacts_sha256"]
            or manifest["settings_sha256"] != authorization["settings_sha256"]
            or manifest["account_sha256"] != authorization["account_sha256"]
            or manifest["target"]["sidecar_document"].get("sender_jid")
               != authorization["sender_jid"]
            or receipt["target_observation_sha256"] != observation_sha):
        raise SimulationError("receiver receipt does not bind this Activity run")
    receiver._verify_source_fresh(receipt, clock)
    runtime = SimpleNamespace(
        QUEUE_DIR=str(Path(root) / "queue"),
        queued=lambda: sorted(path.name for path in (Path(root) / "queue").glob("*.wav")),
    )
    selected.verify_queue(runtime, manifest, "archived")
    return receipt


class ProductionGuard:
    def __init__(self, production, manifest, receipt, scratch_root=None):
        self.production = production
        self.manifest = manifest
        self.receipt = receipt
        self.baseline = receiver._production_trees(production)
        self.mutable = set(receipt["sync_mutable_paths"])
        self.sync = receiver._tree(production["sync_store"], self.mutable)
        self.settings = _file_snapshot(production["settings"])
        self.scratch_root = Path(scratch_root).resolve() if scratch_root is not None else None
        self.runtime = SimpleNamespace(
            transport_mode=receiver._actual_transport_mode,
            WACLI_BIN="/usr/local/bin/wacli",
        )

    def verify(self):
        receiver._verify_service_state()
        _dashboard_inactive()
        receiver._verify_production_nfc_clear(self.production)
        receiver._verify_live_binding(self.production, self.manifest, self.runtime)
        if receiver._production_trees(self.production) != self.baseline:
            raise SimulationError("production queue, outbox, or state changed")
        if _file_snapshot(self.production["settings"]) != self.settings:
            raise SimulationError("production settings changed")
        if self.scratch_root is not None:
            receiver._tree(self.scratch_root)
            if (receiver._digest(self.scratch_root / "state" / "contacts.json")
                    != self.manifest["contacts_sha256"]
                    or receiver._digest(self.scratch_root / "settings" / "settings.json")
                    != self.manifest["settings_sha256"]):
                raise SimulationError("scratch contacts or settings changed")
        receiver._verify_sync_store(
            self.sync, receiver._tree(self.production["sync_store"], self.mutable), self.mutable,
        )


def _request_parts(raw):
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    return parsed


def _valid_activity_request(method, raw):
    parsed = _request_parts(raw)
    if parsed is None:
        return False
    if method == "GET":
        return parsed.path in STATIC_GETS | API_GETS and not parsed.query
    if method == "POST" and parsed.path in ACTIVITY_POSTS:
        try:
            values = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=True,
            )
        except ValueError:
            return False
        return set(values) == {"f"} and len(values["f"]) == 1 and 1 <= len(values["f"][0]) <= 64
    return False


class ControllerState:
    def __init__(self):
        self.failed = threading.Event()
        self.lock = threading.Lock()

    def fail(self):
        self.failed.set()


def guarded_handler(dashboard, guard, host, ledger, state=None):
    state = state or ControllerState()

    class ActivityHandler(dashboard.Handler):
        local_host = host
        tailscale_host = None
        controller_state = state

        def _trusted_origin(self):
            headers = getattr(self, "headers", {})
            forwarded = (
                "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
            )
            try:
                address = ipaddress.ip_address(getattr(self, "client_address", ("", 0))[0])
            except ValueError:
                return None
            if (address != ipaddress.ip_address("127.0.0.1")
                    or headers.get("Host") != host
                    or any(headers.get(name) for name in forwarded)):
                return None
            expected = "http://" + host
            origin = headers.get("Origin")
            if (getattr(self, "command", "") == "POST" and origin != expected) or (
                    origin is not None and origin.rstrip("/") != expected):
                return None
            return expected

        def _run(self, method, callback):
            if not _valid_activity_request(method, self.path):
                return self._send(404, "{}")
            with state.lock:
                if state.failed.is_set():
                    return self._send(
                        503, json.dumps({"ok": False, "error": "scratch safety check failed"}),
                    )
                try:
                    guard.verify()
                except (OSError, ValueError, json.JSONDecodeError, SimulationError):
                    state.fail()
                    return self._send(
                        503, json.dumps({"ok": False, "error": "scratch safety check failed"}),
                    )
                try:
                    result = callback()
                except BaseException:
                    state.fail()
                    raise
                try:
                    guard.verify()
                except (OSError, ValueError, json.JSONDecodeError, SimulationError):
                    # The original Handler may already have committed success.
                    # Keep that wire response valid, retain the scratch move,
                    # and make the child failure irreversible for reconciliation.
                    state.fail()
                    self.close_connection = True
                return result

        def do_GET(self):
            return self._run("GET", super().do_GET)

        def do_POST(self):
            return self._run("POST", super().do_POST)

        def do_PUT(self):
            return self._send(404, "{}")

        def _send(self, code, body, ctype="application/json"):
            parsed = _request_parts(getattr(self, "path", ""))
            route = (
                parsed.path
                if parsed and parsed.path in STATIC_GETS | API_GETS | ACTIVITY_POSTS
                else "blocked"
            )
            ledger.append({"method": getattr(self, "command", "?"), "path": route, "status": code})
            return super()._send(code, body, ctype)

    return ActivityHandler


class ActivityServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, state):
        self.controller_state = state
        super().__init__(address, handler)

    def handle_error(self, request, client_address):
        self.controller_state.fail()


def _serve_loop(server, state, guard, timeout):
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not state.failed.is_set():
            server.handle_request()
    finally:
        server.server_close()
        try:
            guard.verify()
        except (OSError, ValueError, json.JSONDecodeError, SimulationError):
            state.fail()


@contextmanager
def _guard_subprocesses():
    original = subprocess.run
    guarded = ReadOnlySubprocess(original)
    subprocess.run = guarded
    try:
        yield guarded
    finally:
        subprocess.run = original


def serve_activity(manifest, root, production, port, timeout):
    receiver._actual_transport_mode()
    receiver._verify_storage_layout(root, production)
    receipt = _validate_completed_run(root, manifest, production)
    with _guard_subprocesses():
        dashboard = import_scratch_dashboard(root)
        guard = ProductionGuard(production, manifest, receipt, root)
        guard.verify()
        ledger = []
        host = f"127.0.0.1:{port}"
        state = ControllerState()
        handler = guarded_handler(dashboard, guard, host, ledger, state)
        server = ActivityServer(("127.0.0.1", port), handler, state)
        server.timeout = 0.25
        print(json.dumps({
            "type": "activity_scratch", "status": "ready",
            "url": f"http://{host}/#activity",
        }), flush=True)
        _serve_loop(server, state, guard, timeout)
        if state.failed.is_set():
            print(json.dumps({
                "type": "activity_scratch", "status": "failed", "requests": ledger,
            }), flush=True)
            raise SimulationError("Activity scratch safety check failed")
        print(json.dumps({"type": "activity_scratch", "status": "stopped", "requests": ledger}), flush=True)


def _child(manifest, root, production, port, timeout):
    os.setsid()
    os.umask(0o077)
    try:
        serve_activity(manifest, root, production, port, timeout)
    except Exception:
        print("Activity scratch controller failed; preserve scratch artifacts and reconcile.", file=sys.stderr)
        raise SystemExit(1) from None


def _supervise(manifest, root, production, port, timeout):
    serve_timeout = timeout - TERMINATION_GRACE_SECONDS
    process = multiprocessing.get_context("spawn").Process(
        target=_child, args=(manifest, root, production, port, serve_timeout),
    )
    process.start()
    try:
        process.join(timeout)
        timed_out = process.is_alive()
    finally:
        cleaned = receiver.shared.stop_group(process.pid)
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    if timed_out or not cleaned:
        print("Activity scratch controller did not stop cleanly; reconcile before retry.", file=sys.stderr)
        return 1
    return 0 if process.exitcode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("authorization", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--receiver-root", type=Path)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        manifest = selected.validate_manifest(receiver.shared.private_json(args.authorization))
        if manifest["transport"] != "wacli":
            raise SimulationError("Activity scratch controller supports wacli only")
        if not args.serve:
            if args.receiver_root:
                raise SimulationError("receiver root requires --serve")
            print("Activity scratch authorization is valid; no device or application state was accessed.")
            return 0
        if (args.receiver_root is None or type(args.port) is not int or not 1024 <= args.port <= 65535
                or not math.isfinite(args.timeout)
                or not TERMINATION_GRACE_SECONDS + 1 <= args.timeout <= MAX_RUNTIME_SECONDS):
            raise SimulationError("bounded Activity execution inputs are required")
        production = receiver._production_paths()
        return _supervise(
            manifest, args.receiver_root, production, args.port, args.timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError, SimulationError):
        print("Activity scratch plan or authorization is invalid.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
