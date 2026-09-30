#!/usr/bin/env python3
"""Validate or explicitly execute a private, bounded application-input plan."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time


class SimulationError(ValueError):
    pass


MAX_EVENT_LATENESS_S = 0.05


def private_json(path):
    with open(path, encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SimulationError("plan and authorization files must be private regular files")
        return json.load(handle)


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"duration", "events"}:
        raise SimulationError("plan requires duration and events")
    duration = plan["duration"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 1 <= duration <= 120:
        raise SimulationError("duration must be between 1 and 120 seconds")
    events = plan["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 256:
        raise SimulationError("plan requires 1 to 256 events")
    pressed, card, previous = False, None, -1
    for event in events:
        if not isinstance(event, dict):
            raise SimulationError("invalid event")
        kind, at = event.get("type"), event.get("at")
        keys = {"at", "type", "uid"} if kind in {"nfc-present", "nfc-unknown"} else {"at", "type"}
        if set(event) != keys or type(at) not in (int, float) or not math.isfinite(at):
            raise SimulationError("invalid event fields")
        if not 0.8 <= at <= duration - 0.8 or at <= previous:
            raise SimulationError("events must increase and leave startup/release settling time")
        previous = at
        if kind == "press" and not pressed:
            pressed = True
        elif kind == "release" and pressed:
            pressed = False
        elif kind in {"nfc-present", "nfc-unknown"}:
            from messagebox.nfc_state import normalize_uid
            try:
                card = normalize_uid(event["uid"])
            except ValueError:
                raise SimulationError("invalid card event") from None
        elif kind == "nfc-repeat" and card is not None:
            pass
        elif kind == "nfc-removed" and card is not None:
            card = None
        else:
            raise SimulationError("invalid input transition")
    if pressed or card is not None:
        raise SimulationError("plan must end released with no card present")
    return plan


def account_hash(runtime):
    """Reuse local identity proof until its authoritative source changes."""
    mode = runtime.transport_mode()
    if mode == "cloud":
        from messagebox.cloud_device import IDENTITY_FILE
        raw = IDENTITY_FILE.read_bytes()  # Never create an identity for a test.
        url = os.environ.get("MSGBOX_CLOUD_API_URL", "https://button.box/cloud-api/v1")
        fingerprint = (mode, url, hashlib.sha256(raw).hexdigest())
    else:
        root = Path(os.environ.get("WACLI_STORE_DIR", "/var/lib/messagebox/wacli")).resolve()
        fingerprint = (mode, str(root), tuple(sorted(
            (path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in root.iterdir()
        )))
    cached = getattr(runtime, "_simulation_account", None)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    if mode == "cloud":
        client = runtime.CloudDeviceClient(url, json.loads(raw))
        binding = [client.api_url, client.identity["device_id"]]
    else:
        from messagebox.onboarding.whatsapp import _linked_account_jid
        status = subprocess.run([runtime.WACLI_BIN, "--read-only", "--json", "auth", "status"],
                                capture_output=True, text=True, timeout=15)
        jid = _linked_account_jid(json.loads(status.stdout)) if status.returncode == 0 else None
        if jid is None:
            raise SimulationError("runtime account identity could not be verified")
        binding = [str(root), jid]
    digest = hashlib.sha256(json.dumps(binding, separators=(",", ":")).encode()).hexdigest()
    runtime._simulation_account = (fingerprint, digest)
    return digest


def verify_routes(runtime, manifest):
    from messagebox.contacts import ContactStore
    from messagebox.runtime_paths import SETTINGS_FILE
    if not isinstance(manifest, dict) or set(manifest) != {
        "transport", "contacts_sha256", "settings_sha256", "recipients", "account_sha256"
    }:
        raise SimulationError("authorization manifest fields are invalid")
    if manifest["transport"] != runtime.transport_mode() or manifest["transport"] not in {"cloud", "wacli"}:
        raise SimulationError("transport does not match authorization")
    if account_hash(runtime) != manifest["account_sha256"]:
        raise SimulationError("account does not match verified test authorization")
    recipients = manifest["recipients"]
    if not isinstance(recipients, list) or not recipients or not all(isinstance(jid, str) for jid in recipients):
        raise SimulationError("authorized test recipients are required")
    for path, key in [(runtime.CONTACTS_FILE, "contacts_sha256"), (SETTINGS_FILE, "settings_sha256")]:
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != manifest[key]:
            raise SimulationError("routing or settings changed from the verified test manifest")
    if not set(recipients).issubset(ContactStore(runtime.CONTACTS_FILE).load()["contacts"]):
        raise SimulationError("authorized test routes must exist in the authoritative contacts")
    for name in runtime.queued():
        metadata = runtime.queue_metadata(Path(runtime.QUEUE_DIR) / name)
        if not isinstance(metadata, dict) or metadata.get("chat") not in recipients:
            raise SimulationError("queued audio has an unverified route; preserve it before testing")


def preflight(runtime, nfc, manifest):
    verify_routes(runtime, manifest)
    if os.environ.get("MSGBOX_CLAIM_ONLY") == "1" or runtime.cloud_claim.CLAIM_FILE.exists():
        raise SimulationError("claim confirmation cannot be simulated")
    for unit in ("messagebox-button.service", "messagebox-nfc.service", "messagebox-poller.service"):
        result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
        if result.returncode != 3 or result.stdout.strip() != "inactive":
            raise SimulationError("input and queue producer services must be verified inactive")
    for directory in (runtime.OUTBOX_DIR, runtime.TEMP_DIR, runtime.LISTENED_DIR):
        path = Path(directory)
        if path.exists() and any(path.iterdir()):
            raise SimulationError("existing outbound or temporary work must remain untouched")
    for path in (nfc.NFC_ENROLLMENT_FILE, nfc.NFC_SELECTION_FILE, nfc.NFC_ANNOUNCEMENT_FILE, nfc.NFC_HEALTH_FILE):
        if Path(path).exists():
            raise SimulationError("existing NFC state must remain untouched")


class Inputs:
    """Only input state changes; application handlers own every resulting action."""

    def __init__(self, plan, nfc_runtime, health, clock=time.monotonic):
        self.plan, self.nfc, self.health, self.clock = plan, nfc_runtime, health, clock
        self.started = clock()
        self.index = 0
        self.is_pressed = False
        self.pressed_at = None
        self.uid = None
        self.done = False
        self.error = None
        self.last_health = None
        self.max_lateness = 0.0

    def tick(self):
        now = self.clock()
        elapsed = now - self.started
        due = [event for event in self.plan["events"][self.index:] if event["at"] <= elapsed]
        if sum(event["type"] in {"press", "release"} for event in due) > 1:
            raise SimulationError("input scheduling collapsed button transitions")
        if due:
            lateness = elapsed - due[0]["at"]
            self.max_lateness = max(self.max_lateness, lateness)
            if lateness > MAX_EVENT_LATENESS_S:
                raise SimulationError("input scheduling exceeded its timing tolerance")
        while self.index < len(self.plan["events"]) and self.plan["events"][self.index]["at"] <= elapsed:
            event = self.plan["events"][self.index]
            self.index += 1
            kind = event["type"]
            if kind == "press":
                self.is_pressed, self.pressed_at = True, now
            elif kind == "release":
                self.is_pressed = False
            elif kind in {"nfc-present", "nfc-unknown"}:
                self.uid = event["uid"]
            elif kind == "nfc-removed":
                self.uid = None
            # Repeated samples use the same production refresh/debounce path.
            self.nfc.observe(self.uid, now)
        self.nfc.observe(self.uid, now)
        if self.last_health is None or now - self.last_health >= 2:
            self.health()
            self.last_health = now
        self.done = elapsed >= self.plan["duration"]

    def run(self, stop):
        try:
            while not stop.wait(0.005):
                self.tick()
        except Exception:
            self.error = SimulationError("input scheduler failed")
            self.done = True
            self.is_pressed = False


class StatusLamp:
    def on(self):
        pass

    def off(self):
        pass


def send_outcome(key, transport, account, progress):
    outcome = {"type": "generated_send", "job_sha256": hashlib.sha256(key.encode()).hexdigest(),
               "transport": transport, "account_sha256": account,
               "progress": progress, "counterpart_delivery": "unverified"}
    print(json.dumps(outcome, sort_keys=True), flush=True)
    return outcome


def send_generated(runtime, manifest):
    verify_routes(runtime, manifest)
    mode = runtime.transport_mode()
    outcomes = []
    if mode == "cloud":
        for filename in runtime.compatible_legacy_outbox_files():
            path = Path(runtime.OUTBOX_DIR) / filename
            if runtime.legacy_job_recipient(str(path)) not in manifest["recipients"]:
                raise SimulationError("generated recording route does not match authorization")
            if not runtime.stage_hold_release_business_job(filename):
                raise SimulationError("generated recording staging failed")
    for job in runtime.outbox_store.jobs():
        verify_routes(runtime, manifest)
        if job.recipient not in manifest["recipients"] or job.transport != manifest["transport"]:
            raise SimulationError("generated job route does not match authorization")
        handled = runtime.send_guided_job(job)
        progress = "failed_or_uncertain"
        if handled and mode == "wacli" and not job.path.exists():
            progress = "transport_completed"
        elif handled and mode == "cloud":
            metadata = json.loads((job.path / "job.json").read_text(encoding="utf-8"))
            state = metadata.get("cloud_state")
            if (metadata.get("message_id") == job.message_id
                    and metadata.get("transport") == mode
                    and metadata.get("recipient") == job.recipient
                    and metadata.get("state") == "cloud_retained"
                    and isinstance(metadata.get("cloud_message_id"), str)
                    and metadata["cloud_message_id"]
                    and state in {"queued", "waiting_for_reply", "accepted", "delivered", "read"}):
                progress = "cloud_" + state
        outcomes.append(send_outcome(job.message_id, mode, manifest["account_sha256"], progress))
        if progress == "failed_or_uncertain":
            raise SimulationError("generated job did not reach verified send progress; retain and reconcile")
    if mode == "wacli":
        for filename in runtime.compatible_legacy_outbox_files():
            verify_routes(runtime, manifest)
            path = Path(runtime.OUTBOX_DIR) / filename
            if runtime.legacy_job_recipient(str(path)) not in manifest["recipients"]:
                raise SimulationError("generated recording route does not match authorization")
            handled = runtime.send_legacy_outbox_file(filename)
            completed = (handled and not path.exists() and not Path(str(path) + ".json").exists()
                         and not (Path(runtime.OUTBOX_DIR) / ".bad" / filename).exists())
            progress = "transport_completed" if completed else "failed_or_uncertain"
            outcomes.append(send_outcome(filename, mode, manifest["account_sha256"], progress))
            if not completed:
                raise SimulationError("generated recording failed, is retained or quarantined; reconcile")
    return outcomes


def use_scratch_state(runtime, root):
    """Rebind only outbound/temp/receipt storage; never migrate household work."""
    root = Path(root)
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
        raise SimulationError("scratch root must be a private directory owned by the runtime user")
    root = root.resolve()
    protected = [runtime.OUTBOX_DIR, runtime.QUEUE_DIR, runtime.STATE_DIR,
                 runtime.TEMP_DIR, runtime.LISTENED_DIR, "/var/lib/messagebox-cloud",
                 "/etc/messagebox", "/run/messagebox", "/opt/messagebox"]
    for path in protected:
        path = Path(path).resolve()
        if root == path or root in path.parents or path in root.parents:
            raise SimulationError("scratch root must be separate from production storage")
    if any(root.iterdir()):
        raise SimulationError("scratch root must be empty; retain prior run evidence")
    runtime.OUTBOX_DIR = str(root / "outbox")
    runtime.TEMP_DIR = str(root / "recording-temp")
    runtime.LISTENED_DIR = str(root / "listened-receipts")


@contextmanager
def validated_routing(runtime, manifest):
    """Validate actual production decisions without selecting a replacement route."""
    recording = runtime.recording_recipient_context
    claim = runtime.claim_fresh_card_intent
    capture = runtime.capture_guided_recording

    def check(context):
        if context is not None and context["contact"]["jid"] not in manifest["recipients"]:
            raise SimulationError("application selected an unauthorized test route")
        return context

    def guarded_recording():
        return check(recording())

    def guarded_claim():
        state, context = claim()
        return state, check(context)

    def guarded_capture(recipient, *args, **kwargs):
        if recipient not in manifest["recipients"]:
            raise SimulationError("capture route is outside test authorization")
        return capture(recipient, *args, **kwargs)

    runtime.recording_recipient_context = guarded_recording
    runtime.claim_fresh_card_intent = guarded_claim
    runtime.capture_guided_recording = guarded_capture
    try:
        yield
    finally:
        runtime.recording_recipient_context = recording
        runtime.claim_fresh_card_intent = claim
        runtime.capture_guided_recording = capture


def run_inputs(runtime, inputs, manifest):
    while not inputs.done:
        if not inputs.is_pressed:
            runtime.play_pending_nfc_announcement()
            time.sleep(runtime.POLL_S)
            continue
        # Begin the shared debounce window on the observed edge, never midway
        # through an unrelated idle timeout window.
        if runtime.wait_for_confirmed_press(timeout=runtime.CONFIRM_PRESS_S + 2 * runtime.POLL_S):
            verify_routes(runtime, manifest)
            if not runtime.handle_confirmed_press(inputs.pressed_at):
                raise SimulationError("application input handler failed")
            runtime.wait_for_stable_open()
    if inputs.error:
        raise inputs.error
    if inputs.index != len(inputs.plan["events"]) or inputs.is_pressed or inputs.uid is not None:
        raise SimulationError("input schedule did not complete")


def execute(plan, manifest, send, scratch=None):
    from messagebox import button_send as runtime, nfc
    from messagebox.contacts import ContactStore
    from messagebox.nfc_state import normalize_uid
    if scratch is not None:
        use_scratch_state(runtime, scratch)
    preflight(runtime, nfc, manifest)
    known = {uid: jid for jid, contact in ContactStore(runtime.CONTACTS_FILE).load()["contacts"].items() for uid in contact["card_uids"]}
    for event in plan["events"]:
        if event["type"] in {"nfc-present", "nfc-unknown"}:
            recognized = normalize_uid(event["uid"]) in known
            if recognized and known[normalize_uid(event["uid"])] not in manifest["recipients"]:
                raise SimulationError("planned card is outside test authorization")
            if recognized != (event["type"] == "nfc-present"):
                raise SimulationError("card event does not match the verified routing state")
    announcement = nfc.AnnouncementStore(nfc.NFC_ANNOUNCEMENT_FILE)
    runtime.led = StatusLamp()
    runtime.outbox_store = runtime.OutboxStore(runtime.OUTBOX_DIR, transport=runtime.transport_mode())
    runtime.receipt_store = runtime.ReceiptStore(runtime.LISTENED_DIR)
    Path(runtime.TEMP_DIR).mkdir(parents=True, exist_ok=True)
    runtime.make_beeps()
    runtime.validate_prompts()
    runtime.apply_master_volume()
    inputs = Inputs(plan, nfc.NfcRuntime(nfc.router(announcement), nfc.Announcer(announcement)), nfc.mark_healthy)
    runtime.button = inputs
    stop = threading.Event()
    scheduler = threading.Thread(target=inputs.run, args=(stop,), daemon=True)
    scheduler.start()
    try:
        with validated_routing(runtime, manifest):
            run_inputs(runtime, inputs, manifest)
        if send:
            send_generated(runtime, manifest)
        print(json.dumps({"type": "input_simulation", "status": "completed",
                          "events_planned": len(plan["events"]), "events_processed": inputs.index,
                          "max_event_lateness_seconds": round(inputs.max_lateness, 4),
                          "delivery_acceptance": "unverified"}, sort_keys=True), flush=True)
    finally:
        stop.set()
        scheduler.join(timeout=2)
        for path in (nfc.NFC_SELECTION_FILE, nfc.NFC_ANNOUNCEMENT_FILE, nfc.NFC_HEALTH_FILE):
            Path(path).unlink(missing_ok=True)


def child(plan, manifest, send, scratch=None):
    os.setsid()
    os.umask(0o077)
    try:
        execute(plan, manifest, send, scratch)
    except Exception:
        # Runtime output stays local/private. Never print exceptions with paths or IDs.
        print("Simulation failed; preserve artifacts and reconcile before retry.", file=sys.stderr)
        raise SystemExit(1) from None


def stop_group(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def supervise(plan, manifest, send, timeout, scratch=None):
    process = multiprocessing.get_context("fork").Process(target=child, args=(plan, manifest, send, scratch))
    process.start()
    try:
        process.join(timeout)
        timed_out = process.is_alive()
    finally:
        # Also reap presence/audio children after a normal exit. No PID is a shell target.
        cleaned = stop_group(process.pid)
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    if timed_out:
        print("Simulation timed out; owner must reconcile queue/outbox/NFC state before retry.", file=sys.stderr)
        return 1
    if not cleaned:
        print("Process cleanup could not be verified; owner must reconcile before retry.", file=sys.stderr)
        return 1
    return 0 if process.exitcode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--execute", action="store_true", help="run real application/audio/queue handlers locally")
    parser.add_argument("--authorization", type=Path, help="private manifest of already verified test routing")
    parser.add_argument("--send-generated", action="store_true", help="also send only this run's new approved audio")
    parser.add_argument("--scratch-state-root", type=Path, help="private empty separate outbox/temp/receipt root; preserves household jobs")
    parser.add_argument("--timeout", type=float, default=180, help="hard process-group limit, 1–300 seconds")
    args = parser.parse_args(argv)
    try:
        plan = validate_plan(private_json(args.plan))
        if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300:
            raise SimulationError("timeout must be between 1 and 300 seconds")
        if args.execute:
            if args.authorization is None or args.timeout < plan["duration"] + 1:
                raise SimulationError("execution requires authorization and time for the normal workflow")
            return supervise(plan, private_json(args.authorization), args.send_generated, args.timeout, args.scratch_state_root)
        if args.send_generated or args.authorization is not None or args.scratch_state_root is not None:
            raise SimulationError("execution options require --execute")
        print("Plan valid; no application, audio, queue or transport actions executed.")
        return 0
    except (OSError, ValueError):
        print("Invalid/private input or unmet execution prerequisites; no retry performed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
