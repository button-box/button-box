#!/usr/bin/env python3
"""Run one owner-authorized WACLI standalone recording in an empty namespace.

Validation is the default. Execution uses the unchanged standard input driver,
normal button handler, recorder, review flow, durable outbox, conversion, and
single transport send. It has no inbound-message authorization or receipt.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import multiprocessing
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace


MAX_RUNTIME_SECONDS = 120
TERMINATION_GRACE_SECONDS = 3
MONITOR_INTERVAL_SECONDS = 0.5
HEX = re.compile(r"[0-9a-f]{64}\Z")
JID = re.compile(r"[^\s/]{1,200}\Z")
PROFILES = {
    "tap_review_short": {
        "duration": 15,
        "events": ((1, "press"), (1.25, "release"),
                   (8, "press"), (8.3, "release"),
                   (10, "press"), (10.3, "release")),
        "recording_mode": "tap_review",
    },
    "tap_review_long": {
        "duration": 32,
        "events": ((1, "press"), (1.25, "release"),
                   (20, "press"), (20.3, "release"),
                   (25, "press"), (25.3, "release")),
        "recording_mode": "tap_review",
    },
    "hold_release_short": {
        "duration": 8,
        "events": ((1, "press"), (4, "release")),
        "recording_mode": "hold_release",
    },
}
AUTH_KEYS = {
    "version", "transport", "profile", "contacts", "settings",
    "effective_settings_sha256", "settings_override", "account_sha256",
    "recipient", "route", "assets", "sync_mutable_paths", "services",
    "runtime_identity",
}
FILE_PROOF_KEYS = {"sha256", "document_sha256", "mode", "uid", "gid"}
ASSET_KEYS = {"path", "sha256"}
IDENTITY_KEYS = {"uid", "gid"}
OVERRIDE_KEYS = {"recording_mode", "source_revision"}
SERVICE_BASELINE = {
    "messagebox-button.service": "inactive",
    "messagebox-nfc.service": "inactive",
    "messagebox-poller.service": "inactive",
    "messagebox-dash.service": "inactive",
    "messagebox-sync.service": "active",
}
RECEIPT_NAME = "standalone-run-receipt.json"


def _load(name, filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("required developer driver is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = _load("messagebox_standalone_receiver", "simulate-received-message.py")
shared = receiver.shared
SimulationError = receiver.SimulationError


def _json_sha(value):
    return receiver._json_sha(value)


def _private_json(path):
    return shared.private_json(path)


def _static_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"profile", "duration", "events"}:
        raise SimulationError("standalone plan fields are invalid")
    expected = PROFILES.get(plan["profile"])
    if expected is None or plan["duration"] != expected["duration"]:
        raise SimulationError("standalone plan profile is invalid")
    events = plan["events"]
    if (not isinstance(events, list) or
            [(item.get("at"), item.get("type")) if isinstance(item, dict) else None
             for item in events] != list(expected["events"]) or
            any(set(item) != {"at", "type"} for item in events)):
        raise SimulationError("standalone plan does not match its fixed profile")
    shared.validate_plan({"duration": plan["duration"], "events": events})
    return plan


def validate_authorization(value):
    if not isinstance(value, dict) or set(value) != AUTH_KEYS:
        raise SimulationError("standalone authorization fields are invalid")
    profile = value.get("profile")
    if (value.get("version") != 1 or value.get("transport") != "wacli"
            or profile not in PROFILES or value.get("route") != "default"):
        raise SimulationError("standalone authorization mode is invalid")
    for key in ("contacts", "settings"):
        proof = value[key]
        if (not isinstance(proof, dict) or set(proof) != FILE_PROOF_KEYS
                or any(not isinstance(proof[name], str) or HEX.fullmatch(proof[name]) is None
                       for name in ("sha256", "document_sha256"))
                or type(proof["mode"]) is not int or not 0 <= proof["mode"] <= 0o7777
                or type(proof["uid"]) is not int or proof["uid"] < 0
                or type(proof["gid"]) is not int or proof["gid"] < 0):
            raise SimulationError("standalone file proof is invalid")
    if (not isinstance(value["effective_settings_sha256"], str)
            or HEX.fullmatch(value["effective_settings_sha256"]) is None
            or not isinstance(value["account_sha256"], str)
            or HEX.fullmatch(value["account_sha256"]) is None
            or not isinstance(value["recipient"], str)
            or JID.fullmatch(value["recipient"]) is None):
        raise SimulationError("standalone binding is invalid")
    override = value["settings_override"]
    expected_mode = PROFILES[profile]["recording_mode"]
    if expected_mode == "tap_review":
        if override is not None:
            raise SimulationError("tap-review profile cannot override copied settings")
    elif (not isinstance(override, dict) or set(override) != OVERRIDE_KEYS
          or override.get("recording_mode") != "hold_release"
          or type(override.get("source_revision")) is not int
          or override["source_revision"] < 0):
        raise SimulationError("hold-release settings override is invalid")
    assets = value["assets"]
    if (not isinstance(assets, list) or not assets
            or any(not isinstance(item, dict) or set(item) != ASSET_KEYS
                   or not isinstance(item["path"], str) or not os.path.isabs(item["path"])
                   or not isinstance(item["sha256"], str) or HEX.fullmatch(item["sha256"]) is None
                   for item in assets)
            or [item["path"] for item in assets] != sorted(set(item["path"] for item in assets))):
        raise SimulationError("standalone audio asset proof is invalid")
    identity = value["runtime_identity"]
    if (not isinstance(identity, dict) or set(identity) != IDENTITY_KEYS
            or any(type(identity[key]) is not int or identity[key] < 0 for key in IDENTITY_KEYS)):
        raise SimulationError("standalone runtime identity proof is invalid")
    mutable = value["sync_mutable_paths"]
    if (not isinstance(mutable, list) or mutable != sorted(set(mutable))
            or not set(mutable).issubset(receiver.SYNC_MUTABLE)):
        raise SimulationError("standalone sync allowance is invalid")
    if value["services"] != SERVICE_BASELINE:
        raise SimulationError("standalone service baseline is invalid")
    return value


def _runtime_identity(expected=None):
    try:
        account = pwd.getpwnam("messagebox")
    except KeyError as exc:
        raise SimulationError("normal runtime account is unavailable") from exc
    identity = {"uid": account.pw_uid, "gid": account.pw_gid}
    if os.getuid() != identity["uid"] or os.getgid() != identity["gid"]:
        raise SimulationError("standalone execution must use the normal runtime identity")
    if expected is not None and identity != expected:
        raise SimulationError("normal runtime identity changed from authorization")
    return identity


def _file_snapshot(path):
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SimulationError("protected metadata file is unsafe")
    return {
        "sha256": _regular_sha256(path), "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid, "gid": metadata.st_gid,
    }


def _regular_sha256(path, maximum=32 * 1024 * 1024):
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise SimulationError("standalone audio asset is not a bounded regular file")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise SimulationError("standalone audio asset exceeds its bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise SimulationError("standalone audio asset changed while it was read")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _generated_regular(path, parent, suffix):
    path = Path(path)
    parent = Path(parent).resolve()
    try:
        resolved_parent = path.parent.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise SimulationError("generated standalone audio is unavailable") from exc
    if (resolved_parent != parent or not path.name.endswith(suffix)
            or not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_size <= 0 or metadata.st_size > 32 * 1024 * 1024):
        raise SimulationError("generated standalone audio is unsafe")
    _regular_sha256(path)
    return path


def _verify_file_proof(path, proof, document, *, copied=False):
    snapshot = _file_snapshot(path)
    expected = {key: proof[key] for key in ("sha256", "mode", "uid", "gid")}
    if copied:
        expected = {"sha256": proof["sha256"], "mode": 0o600,
                    "uid": os.getuid(), "gid": os.getgid()}
    if snapshot != expected or _json_sha(document) != proof["document_sha256"]:
        raise SimulationError("standalone contacts or settings proof changed")


def _claim_absent(path=Path("/run/messagebox-cloud/claim.json")):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SimulationError("claim state cannot be verified") from exc
    raise SimulationError("claim confirmation state must remain absent")


def _dashboard_inactive():
    result = subprocess.run(
        ["systemctl", "is-active", "messagebox-dash.service"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 3 or result.stdout.strip() != "inactive":
        raise SimulationError("production dashboard service must be verified inactive")


def _private_empty_directory(path, identity, label):
    path = Path(path)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != identity["uid"] or metadata.st_gid != identity["gid"]
            or any(path.iterdir())):
        raise SimulationError(f"{label} must be empty, private, and runtime-owned")
    return path.resolve()


class ProductionGuard:
    def __init__(self, production, manifest, authorization):
        self.production = production
        self.manifest = manifest
        self.authorization = authorization
        self.baseline = receiver._production_trees(production)
        self.cloud = receiver._tree("/var/lib/messagebox-cloud")
        self.settings = _file_snapshot(production["settings"])
        self.mutable = set(authorization["sync_mutable_paths"])
        self.sync = receiver._tree(production["sync_store"], self.mutable)
        self.runtime = SimpleNamespace(transport_mode=receiver._actual_transport_mode,
                                       WACLI_BIN="/usr/local/bin/wacli")
        self.namespace = None
        self.scratch_contacts = self.scratch_settings = self.scratch_queue = None
        self.application_bound = False
        self.failed = threading.Event()
        self.lock = threading.Lock()
        self.error = None

    def bind_namespace(self, root):
        self.namespace = Path(root)
        self.scratch_contacts = _file_snapshot(self.namespace / "state/contacts.json")
        self.scratch_settings = _file_snapshot(self.namespace / "settings/settings.json")
        self.scratch_queue = receiver._tree(self.namespace / "queue")
        self.application_bound = True

    def fail(self, error=None):
        self.error = error
        self.failed.set()

    def check_latch(self):
        if self.failed.is_set():
            raise SimulationError("standalone preservation guard failed")

    def verify(self):
        with self.lock:
            self.check_latch()
            try:
                _runtime_identity(self.authorization["runtime_identity"])
                receiver._verify_service_state()
                _dashboard_inactive()
                receiver._verify_production_nfc_clear(self.production)
                _claim_absent()
                receiver._actual_transport_mode()
                if (receiver._digest(self.production["contacts"])
                        != self.manifest["contacts_sha256"]
                        or receiver._digest(self.production["settings"])
                        != self.manifest["settings_sha256"]):
                    raise SimulationError("production route or settings changed")
                if self.application_bound:
                    receiver._verify_live_binding(self.production, self.manifest, self.runtime)
                if receiver._production_trees(self.production) != self.baseline:
                    raise SimulationError("production queue, outbox, or state changed")
                if receiver._tree("/var/lib/messagebox-cloud") != self.cloud:
                    raise SimulationError("production Cloud state changed")
                if _file_snapshot(self.production["settings"]) != self.settings:
                    raise SimulationError("production settings changed")
                if self.namespace is not None:
                    if (_file_snapshot(self.namespace / "state/contacts.json")
                            != self.scratch_contacts
                            or _file_snapshot(self.namespace / "settings/settings.json")
                            != self.scratch_settings
                            or receiver._tree(self.namespace / "queue") != self.scratch_queue):
                        raise SimulationError("scratch routing or empty queue changed")
                receiver._verify_sync_store(
                    self.sync, receiver._tree(self.production["sync_store"], self.mutable), self.mutable,
                )
            except BaseException as exc:
                self.fail(exc)
                raise


class Monitor:
    def __init__(self, guard):
        self.guard = guard
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(MONITOR_INTERVAL_SECONDS):
            try:
                self.guard.verify()
            except BaseException:
                return

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()


def _settings_document(store):
    document, warning = store.load()
    if warning:
        raise SimulationError("scratch settings required recovery or defaults")
    return document


def _apply_settings(profile, store, original, authorization):
    override = authorization["settings_override"]
    if override is None:
        effective = original
    else:
        if original["revision"] != override["source_revision"]:
            raise SimulationError("settings override source revision changed")
        candidate = {key: value for key, value in original.items()
                     if key not in {"version", "revision"}}
        candidate["recording_mode"] = override["recording_mode"]
        effective = store.update(candidate, original["revision"])
    if (effective["recording_mode"] != PROFILES[profile]["recording_mode"]
            or effective["master_volume_percent"] != 30
            or _json_sha(effective) != authorization["effective_settings_sha256"]):
        raise SimulationError("effective standalone settings do not match authorization")
    return effective


def _asset_proofs(runtime, authorization):
    required = sorted(os.fspath(path) for path in runtime.PROMPTS.values())
    actual = [item["path"] for item in authorization["assets"]]
    if actual != required:
        raise SimulationError("standalone prompt authorization is incomplete")
    for item in authorization["assets"]:
        if _regular_sha256(item["path"]) != item["sha256"]:
            raise SimulationError("standalone prompt asset changed")


def _prepare_namespace(plan, authorization, root, production, guard):
    paths = receiver.bind_scratch_paths(root)
    receiver._copy_exact(production["contacts"], paths.CONTACTS_FILE,
                         authorization["contacts"]["sha256"])
    receiver._copy_exact(production["settings"], paths.SETTINGS_FILE,
                         authorization["settings"]["sha256"])
    paths.QUEUE_DIR.mkdir(mode=0o700)
    paths.RUNTIME_DIR.mkdir(mode=0o700)
    from messagebox import button_send as runtime, nfc
    from messagebox.contacts import ContactStore
    from messagebox.settings import SettingsStore
    contacts_document = ContactStore(paths.CONTACTS_FILE).load()
    settings_store = SettingsStore(paths.SETTINGS_FILE)
    original_settings = _settings_document(settings_store)
    _verify_file_proof(production["contacts"], authorization["contacts"], contacts_document)
    _verify_file_proof(production["settings"], authorization["settings"], original_settings)
    _verify_file_proof(paths.CONTACTS_FILE, authorization["contacts"], contacts_document,
                       copied=True)
    _verify_file_proof(paths.SETTINGS_FILE, authorization["settings"], original_settings,
                       copied=True)
    effective = _apply_settings(plan["profile"], settings_store, original_settings, authorization)
    contacts = contacts_document["contacts"]
    recipient = authorization["recipient"]
    if contacts_document["default_recipient"] != recipient or recipient not in contacts:
        raise SimulationError("authorized counterpart is not the current default route")
    if any(paths.QUEUE_DIR.iterdir()):
        raise SimulationError("standalone scratch queue is not empty")
    route_state, recent = runtime.recent_reply_recipient(paths.QUEUE_DIR, contacts)
    history_lock = paths.QUEUE_DIR / ".played.lock"
    lock_metadata = history_lock.lstat()
    if (route_state != "fallback" or recent is not None
            or not stat.S_ISREG(lock_metadata.st_mode)
            or stat.S_ISLNK(lock_metadata.st_mode)
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            or lock_metadata.st_uid != os.getuid() or lock_metadata.st_gid != os.getgid()
            or lock_metadata.st_size != 0
            or set(path.name for path in paths.QUEUE_DIR.iterdir()) != {".played.lock"}):
        raise SimulationError("standalone scratch history is not an empty normal baseline")
    _asset_proofs(runtime, authorization)
    guard.bind_namespace(root)
    guard.verify()
    return runtime, nfc, effective


class AllowedRun:
    def __init__(self, original, runtime, root, outbound, authorization, guard, delegation):
        self.original, self.runtime, self.guard = original, runtime, guard
        self.delegation = delegation
        self.root, self.outbound = Path(root).resolve(), Path(outbound).resolve()
        self.recipient = authorization["recipient"]
        self.assets = {item["path"]: item["sha256"] for item in authorization["assets"]}
        self.beeps = {
            os.fspath(value[0]): (value[1], value[2], value[3])
            for value in runtime.BEEPS.values()
        }
        self.send_count = 0
        self.calls = []

    def _inside(self, value, parent):
        return Path(value).resolve().is_relative_to(parent)

    def __call__(self, args, *positional, **kwargs):
        command = tuple(os.fspath(value) for value in args)
        allowed_read = {
            ("systemctl", "is-active", "messagebox-button.service"),
            ("systemctl", "is-active", "messagebox-nfc.service"),
            ("systemctl", "is-active", "messagebox-poller.service"),
            ("systemctl", "is-active", "messagebox-sync.service"),
            ("systemctl", "is-active", "messagebox-dash.service"),
            (self.runtime.WACLI_BIN, "--read-only", "--json", "auth", "status"),
        }
        ok = command in allowed_read
        side_effect = False
        is_send = False
        if command[:1] == ("ffmpeg",) and command[-1:]:
            target = Path(command[-1]).resolve()
            beep_values = self.beeps.get(command[-1])
            beep = (beep_values is not None and command == (
                "ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                f"sine=frequency={beep_values[0]}:duration={beep_values[1]}",
                "-filter:a", f"volume={beep_values[2]}dB", command[-1],
            ) and target.parent == self.root / "runtime")
            conversion = (len(command) == 15 and command[1:4] == ("-loglevel", "error", "-y")
                          and command[4] == "-i" and command[6:14] == (
                              "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1")
                          and self._inside(command[5], self.outbound / "outbox")
                          and self._inside(command[-1], self.outbound / "recording-temp")
                          and command[-1].endswith(".ogg"))
            if conversion:
                source = Path(command[5])
                if source.parent.name.endswith(".job"):
                    _generated_regular(source, source.parent, "audio.wav")
                else:
                    _generated_regular(source, self.outbound / "outbox", ".wav")
            ok, side_effect = beep or conversion, True
        elif command[:1] == ("amixer",):
            ok = command == ("amixer", "-q", "-c", self.runtime.SPEAKER_CARD, "sset",
                             self.runtime.SPEAKER_CONTROL, "30%", "unmute")
            side_effect = True
        elif command[:1] == ("aplay",) and len(command) == 5:
            target = command[-1]
            ok = (command[:4] == ("aplay", "-q", "-D", self.runtime.SPK_DEV)
                  and (target in self.beeps or target in self.assets
                       or self._inside(target, self.outbound / "recording-temp")))
            if target in self.assets and _regular_sha256(target) != self.assets[target]:
                ok = False
            elif target in self.beeps:
                _generated_regular(target, self.root / "runtime", ".wav")
            elif self._inside(target, self.outbound / "recording-temp"):
                _generated_regular(target, self.outbound / "recording-temp", ".wav")
            side_effect = True
        elif command[:4] == (self.runtime.WACLI_BIN, "send", "voice", "--file"):
            expected = tuple(self.runtime.voice_send_command(
                self.runtime.WACLI_BIN, command[4], self.recipient, self.runtime.SEND_LOCK_WAIT,
            ))
            ok = (command == expected and self._inside(command[4], self.outbound / "recording-temp")
                  and command[4].endswith(".ogg") and self.send_count == 0)
            side_effect = True
            if ok:
                _generated_regular(command[4], self.outbound / "recording-temp", ".ogg")
                is_send = True
        if not ok:
            raise SimulationError("standalone child attempted a disallowed subprocess")
        if side_effect:
            self.guard.verify()
        if is_send:
            self.send_count += 1
        self.calls.append(command[0])
        self.delegation.active = True
        try:
            result = self.original(args, *positional, **kwargs)
        finally:
            self.delegation.active = False
        if side_effect:
            self.guard.verify()
        return result


class AllowedPopen:
    def __init__(self, original, runtime, root, outbound, authorization, guard, delegation):
        self.original, self.runtime, self.guard = original, runtime, guard
        self.delegation = delegation
        self.root, self.outbound = Path(root).resolve(), Path(outbound).resolve()
        self.recipient = authorization["recipient"]
        self.assets = {item["path"]: item["sha256"] for item in authorization["assets"]}
        self.capture_count = 0
        self.calls = []

    def _inside(self, value, parent):
        return Path(value).resolve().is_relative_to(parent)

    def __call__(self, args, *positional, **kwargs):
        if getattr(self.delegation, "active", False):
            return self.original(args, *positional, **kwargs)
        command = tuple(os.fspath(value) for value in args)
        is_capture = False
        guided_capture = command == (
            "arecord", "-q", "-D", self.runtime.MIC_DEV, "-t", "raw", "-f", "S16_LE",
            "-r", "16000", "-c", "1",
        )
        legacy_capture = (len(command) == 15 and command[:5] == (
            "arecord", "-q", "-D", self.runtime.MIC_DEV, "-t")
            and command[5:13] == ("wav", "-f", "S16_LE", "-r", "48000", "-c", "1", "-d")
            and command[13] in {"32", "62", "122"}
            and Path(command[-1]).resolve().parent == self.outbound / "outbox"
            and command[-1].endswith(".part"))
        review = (len(command) == 5 and command[:4] == (
            "aplay", "-q", "-D", self.runtime.SPK_DEV)
            and (command[-1] in self.assets
                 or self._inside(command[-1], self.outbound / "recording-temp")))
        presence = (command[:2] == (self.runtime.WACLI_BIN, "presence")
                    and command[-4:] == ("--to", self.recipient, "--lock-wait",
                                         self.runtime.SEND_LOCK_WAIT)
                    and command[2:-4] in (("typing", "--media", "audio"), ("paused",)))
        if not (guided_capture or legacy_capture or review or presence):
            raise SimulationError("standalone child attempted a disallowed child process")
        if guided_capture or legacy_capture:
            if self.capture_count:
                raise SimulationError("standalone run attempted more than one capture")
            is_capture = True
        if review and command[-1] in self.assets:
            if _regular_sha256(command[-1]) != self.assets[command[-1]]:
                raise SimulationError("standalone prompt asset changed")
        elif review:
            _generated_regular(command[-1], self.outbound / "recording-temp", ".wav")
        self.guard.verify()
        if is_capture:
            self.capture_count += 1
        self.calls.append(command[0])
        result = self.original(args, *positional, **kwargs)
        self.guard.verify()
        return result


@contextmanager
def _guard_processes(runtime, root, outbound, authorization, guard):
    original_run, original_popen = subprocess.run, subprocess.Popen
    delegation = threading.local()
    runs = AllowedRun(
        original_run, runtime, root, outbound, authorization, guard, delegation,
    )
    children = AllowedPopen(
        original_popen, runtime, root, outbound, authorization, guard, delegation,
    )
    subprocess.run = runtime.subprocess.run = runs
    subprocess.Popen = runtime.subprocess.Popen = children
    try:
        yield runs, children
    finally:
        runtime.subprocess.run = subprocess.run = original_run
        runtime.subprocess.Popen = subprocess.Popen = original_popen


@contextmanager
def _guard_interactions(runtime, authorization, guard):
    handle = runtime.handle_confirmed_press
    guided = runtime.send_guided_job
    legacy = runtime.send_legacy_outbox_file

    def guarded_handle(closed_at):
        guard.verify()
        result = handle(closed_at)
        guard.verify()
        return result

    def guarded_guided(job):
        guard.verify()
        if (job.recipient != authorization["recipient"] or job.transport != "wacli"
                or job.flow_kind != "standalone" or job.state != "pending"):
            raise SimulationError("generated guided job does not match authorization")
        result = guided(job)
        guard.verify()
        return result

    def guarded_legacy(name):
        guard.verify()
        path = os.path.join(runtime.OUTBOX_DIR, name)
        if (runtime.legacy_job_recipient(path) != authorization["recipient"]
                or runtime.legacy_job_transport(path) != "wacli"):
            raise SimulationError("generated hold-release job does not match authorization")
        result = legacy(name)
        guard.verify()
        return result

    runtime.handle_confirmed_press = guarded_handle
    runtime.send_guided_job = guarded_guided
    runtime.send_legacy_outbox_file = guarded_legacy
    try:
        yield
    finally:
        runtime.handle_confirmed_press = handle
        runtime.send_guided_job = guided
        runtime.send_legacy_outbox_file = legacy


def _safe_empty_directory(path):
    metadata = Path(path).lstat()
    return (stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
            and not any(Path(path).iterdir()))


def _verify_output(root, profile, recipient, run_boundary, child_boundary):
    root = Path(root)
    if run_boundary.send_count != 1 or child_boundary.capture_count != 1:
        raise SimulationError("standalone run did not capture and send exactly once")
    for name in ("outbox", "recording-temp"):
        path = root / name
        if not _safe_empty_directory(path):
            raise SimulationError("standalone generated work did not complete exactly once")
    receipts = root / "listened-receipts"
    expected = {"sent", "pending", "inflight", "seen"}
    children = {path.name: path for path in receipts.iterdir()}
    if (set(children) != expected
            or any(not stat.S_ISDIR(path.lstat().st_mode)
                   or stat.S_ISLNK(path.lstat().st_mode) for path in children.values())):
        raise SimulationError("standalone receipt store is invalid")
    counts = {name: len(list(path.iterdir())) for name, path in children.items()}
    if counts["sent"] != 1 or any(counts[name] for name in ("pending", "inflight", "seen")):
        raise SimulationError("standalone transport accounting is incomplete")
    sent = next(children["sent"].iterdir())
    metadata = sent.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SimulationError("standalone sent receipt is unsafe")
    try:
        document = json.loads(receiver._regular_bytes(sent).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SimulationError("standalone sent receipt is invalid") from exc
    expected_flow = "legacy" if PROFILES[profile]["recording_mode"] == "hold_release" else "standalone"
    if document.get("chat") != recipient or document.get("flow") != expected_flow:
        raise SimulationError("standalone sent receipt route is invalid")
    return {"profile": profile, "capture_count": 1, "send_count": 1,
            "sent_receipt_count": 1}


def _boundary_evidence(root, profile, boundaries):
    sent_count = None
    sent = Path(root) / "listened-receipts/sent"
    try:
        metadata = sent.lstat()
        entries = list(sent.iterdir())
        if (stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
                and all(stat.S_ISREG(path.lstat().st_mode)
                        and not stat.S_ISLNK(path.lstat().st_mode) for path in entries)):
            sent_count = len(entries)
    except FileNotFoundError:
        sent_count = 0
    except OSError:
        pass
    runs, children = boundaries
    return {"profile": profile, "capture_count": children.capture_count,
            "send_count": runs.send_count, "sent_receipt_count": sent_count}


def _write_receipt(root, status, plan, authorization, evidence=None):
    value = {
        "version": 1, "status": status, "profile": plan["profile"],
        "plan_sha256": _json_sha(plan), "authorization_sha256": _json_sha(authorization),
        "capture_count": (evidence or {}).get("capture_count", 0),
        "send_count": (evidence or {}).get("send_count", 0),
        "sent_receipt_count": (evidence or {}).get("sent_receipt_count", 0),
        "counterpart_delivery": "unverified",
    }
    path = Path(root) / RECEIPT_NAME
    if path.exists():
        receiver._replace_private_json(path, value)
    else:
        receiver._new_private_json(path, value)


def execute_standalone(plan, authorization, namespace_root, outbound_root, production):
    identity = _runtime_identity(authorization["runtime_identity"])
    receiver._actual_transport_mode()
    root = receiver._verify_storage_layout(namespace_root, production, outbound=outbound_root)
    root = _private_empty_directory(root, identity, "standalone namespace")
    outbound = _private_empty_directory(outbound_root, identity, "standalone outbound scratch")
    manifest = {
        "transport": "wacli", "contacts_sha256": authorization["contacts"]["sha256"],
        "settings_sha256": authorization["settings"]["sha256"],
        "account_sha256": authorization["account_sha256"],
        "recipients": [authorization["recipient"]],
    }
    guard = ProductionGuard(production, manifest, authorization)
    guard.verify()
    runtime, _nfc, _settings = _prepare_namespace(
        plan, authorization, root, production, guard,
    )
    # The shared route manifest must bind the effective scratch settings bytes.
    route_manifest = {**manifest, "settings_sha256": receiver._digest(root / "settings/settings.json")}
    traces = []
    original_trace = shared.InputTrace

    class CapturingTrace(original_trace):
        def __init__(self, application, verified):
            super().__init__(application, verified, emit=traces.append)

    monitor = Monitor(guard)
    status, evidence, boundaries = "failed", None, None
    try:
        shared.InputTrace = CapturingTrace
        monitor.start()
        with _guard_processes(runtime, root, outbound, authorization, guard) as boundaries:
            with _guard_interactions(runtime, authorization, guard):
                shared.execute(
                    {"duration": plan["duration"], "events": plan["events"]},
                    route_manifest, True, outbound,
                )
        guard.verify()
        route_rows = [item for item in traces if item.get("type") == "route_observation"]
        route_ok = any(item.get("source") == "recording_recipient_context"
                    and item.get("guard_outcome") == "allowed"
                    and item.get("via_card") is False
                    and item.get("via_recent_reply") is False for item in route_rows)
        capture_ok = any(item.get("source") == "capture_recipient_guard"
                         and item.get("guard_outcome") == "allowed" for item in route_rows)
        if (not route_ok
                or (PROFILES[plan["profile"]]["recording_mode"] == "tap_review"
                    and not capture_ok)
                or any(item.get("authorized_recipient") is not True
                       for item in route_rows if item.get("context_returned") is True)):
            raise SimulationError("normal default recording route was not observed")
        evidence = _verify_output(
            outbound, plan["profile"], authorization["recipient"], *boundaries,
        )
        status = "completed"
    finally:
        monitor.close()
        shared.InputTrace = original_trace
        if status != "completed" and boundaries is not None:
            evidence = _boundary_evidence(outbound, plan["profile"], boundaries)
        try:
            guard.verify()
        except BaseException:
            status = "failed"
        _write_receipt(outbound, status, plan, authorization, evidence)
    if status != "completed" or guard.failed.is_set():
        raise SimulationError("standalone recording run failed")


def _child(plan, authorization, namespace_root, outbound_root, production):
    os.setsid()
    os.umask(0o077)
    try:
        execute_standalone(plan, authorization, namespace_root, outbound_root, production)
    except Exception:
        print("Standalone recording run failed; preserve artifacts and reconcile before retry.",
              file=sys.stderr)
        raise SystemExit(1) from None


def _supervise(plan, authorization, namespace_root, outbound_root, production, timeout):
    process = multiprocessing.get_context("spawn").Process(
        target=_child,
        args=(plan, authorization, namespace_root, outbound_root, production),
    )
    process.start()
    try:
        process.join(timeout - TERMINATION_GRACE_SECONDS)
        timed_out = process.is_alive()
    finally:
        cleaned = shared.stop_group(process.pid)
        process.join(TERMINATION_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(TERMINATION_GRACE_SECONDS)
    if timed_out or not cleaned:
        print("Standalone recording child did not stop cleanly; reconcile before retry.",
              file=sys.stderr)
        return 1
    return 0 if process.exitcode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--send-generated", action="store_true")
    parser.add_argument("--namespace-root", type=Path)
    parser.add_argument("--scratch-state-root", type=Path)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    try:
        plan = _static_plan(_private_json(args.plan))
        if not args.execute:
            if any((args.authorization, args.send_generated, args.namespace_root,
                    args.scratch_state_root)):
                raise SimulationError("execution options require --execute")
            print("Standalone plan shape is valid; no application, device, or audio state was accessed.")
            return 0
        if (args.authorization is None or args.namespace_root is None
                or args.scratch_state_root is None or not args.send_generated
                or not math.isfinite(args.timeout)
                or not plan["duration"] + 30 <= args.timeout <= MAX_RUNTIME_SECONDS):
            raise SimulationError("bounded standalone execution inputs are required")
        authorization = validate_authorization(_private_json(args.authorization))
        if authorization["profile"] != plan["profile"]:
            raise SimulationError("standalone plan and authorization profiles differ")
        return _supervise(
            plan, authorization, args.namespace_root, args.scratch_state_root,
            receiver._production_paths(), args.timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError, SimulationError):
        print("Standalone plan or authorization is invalid.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
