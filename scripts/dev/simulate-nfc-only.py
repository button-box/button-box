#!/usr/bin/env python3
"""Prepare a fresh WACLI namespace and run one approved NFC-only plan.

Validation is the default. ``--execute`` is an owner-only path which starts a
fresh child, copies current verified contacts and settings into an empty private
namespace, binds runtime paths before application imports, prepares the normal
generated beeps, and delegates NFC events to the unchanged standard input
simulator. It never creates button edges or enables sending.
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
import re
import stat
import subprocess
import sys
import threading
import wave
from types import SimpleNamespace


MAX_RUNTIME_SECONDS = 120
TERMINATION_GRACE_SECONDS = 3
MONITOR_INTERVAL_SECONDS = 0.5
MAX_CUE_BYTES = 10 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
JID = re.compile(r"[^\s/]{1,200}\Z")
PROFILES = {
    "known_repeat": {
        "duration": 8,
        "events": ((1, "nfc-present"), (2, "nfc-repeat"), (3, "nfc-removed"),
                   (5, "nfc-present"), (6, "nfc-removed")),
        "mapped": 1,
    },
    "unknown_hold": {
        "duration": 7,
        "events": ((1, "nfc-unknown"), (2, "nfc-repeat"), (5, "nfc-removed")),
        "mapped": 0,
    },
    "stale_selection": {
        "duration": 36,
        "events": ((1, "nfc-present"), (2, "nfc-removed")),
        "mapped": 1,
    },
    "assigned_alternation": {
        "duration": 6,
        "events": ((1, "nfc-present"), (2, "nfc-present"),
                   (3, "nfc-present"), (4, "nfc-removed")),
        "mapped": 2,
    },
}
AUTH_KEYS = {
    "version", "transport", "profile", "contacts", "settings", "account_sha256",
    "recipients", "mappings", "cues", "sync_mutable_paths", "services",
}
CUE_KEYS = {"role", "path", "state", "sha256", "duration_seconds", "audio"}
CUE_AUDIO_KEYS = {
    "channels", "sample_rate", "sample_width", "frames", "peak_fraction", "clipped_samples",
}
MAPPING_KEYS = {"uid", "recipient"}
FILE_PROOF_KEYS = {"sha256", "document_sha256", "mode", "uid", "gid"}
SERVICE_BASELINE = {
    "messagebox-button.service": "inactive",
    "messagebox-nfc.service": "inactive",
    "messagebox-poller.service": "inactive",
    "messagebox-sync.service": "active",
}
NAMESPACE_RECEIPT = "nfc-namespace-receipt.json"


def _load(name, filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("required developer driver is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = _load("messagebox_nfc_scratch_receiver", "simulate-received-message.py")
shared = receiver.shared
SimulationError = receiver.SimulationError


def _private_json(path):
    return shared.private_json(path)


def _json_sha(value):
    return receiver._json_sha(value)


def _static_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"duration", "events", "profile"}:
        raise SimulationError("NFC plan fields are invalid")
    profile = plan["profile"]
    expected = PROFILES.get(profile)
    if expected is None or plan["duration"] != expected["duration"]:
        raise SimulationError("NFC plan profile is invalid")
    events = plan["events"]
    if not isinstance(events, list) or len(events) != len(expected["events"]):
        raise SimulationError("NFC plan events are invalid")
    for event, (at, kind) in zip(events, expected["events"]):
        keys = {"at", "type", "uid"} if kind in {"nfc-present", "nfc-unknown"} else {"at", "type"}
        if (not isinstance(event, dict) or set(event) != keys
                or event.get("at") != at or event.get("type") != kind
                or ("uid" in keys and (not isinstance(event.get("uid"), str) or not event["uid"]))):
            raise SimulationError("NFC plan does not match the approved profile")
    if profile == "assigned_alternation":
        values = [event["uid"] for event in events if "uid" in event]
        if values[0] != values[2] or values[0] == values[1]:
            raise SimulationError("assigned alternation requires two alternating cards")
    return plan


def validate_authorization(value):
    if not isinstance(value, dict) or set(value) != AUTH_KEYS:
        raise SimulationError("NFC authorization fields are invalid")
    if value["version"] != 1 or value["transport"] != "wacli" or value["profile"] not in PROFILES:
        raise SimulationError("NFC authorization transport or profile is invalid")
    if not isinstance(value["account_sha256"], str) or not HEX.fullmatch(value["account_sha256"]):
        raise SimulationError("NFC authorization account fingerprint is invalid")
    for key in ("contacts", "settings"):
        proof = value[key]
        if (not isinstance(proof, dict) or set(proof) != FILE_PROOF_KEYS
                or any(not isinstance(proof[name], str) or not HEX.fullmatch(proof[name])
                       for name in ("sha256", "document_sha256"))
                or type(proof["mode"]) is not int or not 0 <= proof["mode"] <= 0o7777
                or type(proof["uid"]) is not int or proof["uid"] < 0
                or type(proof["gid"]) is not int or proof["gid"] < 0):
            raise SimulationError("NFC authorization file proof is invalid")
    if value["services"] != SERVICE_BASELINE:
        raise SimulationError("NFC authorization service baseline is invalid")
    recipients = value["recipients"]
    if (not isinstance(recipients, list) or recipients != sorted(set(recipients))
            or not recipients or any(not isinstance(item, str) or not JID.fullmatch(item) for item in recipients)):
        raise SimulationError("NFC authorization recipients are invalid")
    mappings = value["mappings"]
    if not isinstance(mappings, list) or len(mappings) != PROFILES[value["profile"]]["mapped"]:
        raise SimulationError("NFC authorization mappings are invalid")
    seen_uids = set()
    for mapping in mappings:
        if (not isinstance(mapping, dict) or set(mapping) != MAPPING_KEYS
                or not isinstance(mapping["uid"], str) or not mapping["uid"]
                or mapping["uid"] in seen_uids or mapping["recipient"] not in recipients):
            raise SimulationError("NFC authorization mapping is invalid")
        seen_uids.add(mapping["uid"])
    if value["profile"] == "assigned_alternation" and len({m["recipient"] for m in mappings}) != 2:
        raise SimulationError("assigned alternation requires two authorized contacts")
    cues = value["cues"]
    if not isinstance(cues, list) or len(cues) > 3:
        raise SimulationError("NFC cue authorization is invalid")
    identities = set()
    for cue in cues:
        if not isinstance(cue, dict) or set(cue) != CUE_KEYS:
            raise SimulationError("NFC cue authorization fields are invalid")
        identity = (cue["role"], cue["path"])
        if (cue["role"] not in {"known", "unknown"} or identity in identities
                or not isinstance(cue["path"], str) or cue["state"] not in {"regular", "missing"}):
            raise SimulationError("NFC cue authorization is invalid")
        identities.add(identity)
        if cue["state"] == "regular":
            if (not isinstance(cue["sha256"], str) or not HEX.fullmatch(cue["sha256"])
                    or type(cue["duration_seconds"]) not in (int, float)
                    or not math.isfinite(cue["duration_seconds"])
                    or not 0 < cue["duration_seconds"] <= 120
                    or not isinstance(cue["audio"], dict)
                    or set(cue["audio"]) != CUE_AUDIO_KEYS):
                raise SimulationError("NFC cue proof is invalid")
            audio = cue["audio"]
            if (type(audio["channels"]) is not int or audio["channels"] not in {1, 2}
                    or type(audio["sample_rate"]) is not int or audio["sample_rate"] <= 0
                    or type(audio["sample_width"]) is not int
                    or audio["sample_width"] not in {1, 2, 3, 4}
                    or type(audio["frames"]) is not int or audio["frames"] <= 0
                    or type(audio["peak_fraction"]) not in (int, float)
                    or not math.isfinite(audio["peak_fraction"])
                    or not 0 <= audio["peak_fraction"] <= 1
                    or type(audio["clipped_samples"]) is not int
                    or audio["clipped_samples"] < 0):
                raise SimulationError("NFC cue audio proof is invalid")
        elif (cue["sha256"] is not None or cue["duration_seconds"] is not None
              or cue["audio"] is not None):
            raise SimulationError("missing NFC cue proof is invalid")
    mutable = value["sync_mutable_paths"]
    if (not isinstance(mutable, list) or mutable != sorted(set(mutable))
            or not set(mutable).issubset(receiver.SYNC_MUTABLE)):
        raise SimulationError("sync mutable paths are invalid")
    return value


def _file_snapshot(path):
    path = Path(os.path.expanduser(path))
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SimulationError("protected settings must be a regular file")
    return {"sha256": receiver._digest(path), "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid, "gid": metadata.st_gid}


def _verify_file_proof(path, proof, document, *, copied=False):
    snapshot = _file_snapshot(path)
    expected = {"sha256": proof["sha256"]}
    if not copied:
        expected.update({key: proof[key] for key in ("mode", "uid", "gid")})
    if (any(snapshot[key] != value for key, value in expected.items())
            or _json_sha(document) != proof["document_sha256"]):
        raise SimulationError("authorized source file or document changed")


def _claim_absent(path=Path("/var/lib/messagebox-cloud/claim.json")):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SimulationError("claim state cannot be verified") from exc
    raise SimulationError("claim confirmation state must remain absent")


def _pcm_metrics(raw, width):
    maximum = (1 << (8 * width - 1)) - 1
    peak, clipped = 0, 0
    for index in range(0, len(raw), width):
        sample = raw[index:index + width]
        if len(sample) != width:
            raise SimulationError("approved NFC cue PCM is truncated")
        if width == 1:
            value = sample[0] - 128
            limit = 127
        else:
            value = int.from_bytes(sample, "little", signed=True)
            limit = maximum
        magnitude = abs(value)
        peak = max(peak, magnitude)
        clipped += magnitude >= limit
    return round(min(1.0, peak / maximum), 6), clipped


def _wav_metrics(handle):
    with wave.open(handle, "rb") as source:
        channels, width = source.getnchannels(), source.getsampwidth()
        rate, frames = source.getframerate(), source.getnframes()
        if channels not in {1, 2} or width not in {1, 2, 3, 4} or rate <= 0 or frames <= 0:
            raise SimulationError("approved NFC cue format is invalid")
        raw = source.readframes(frames)
        if len(raw) != frames * channels * width:
            raise SimulationError("approved NFC cue PCM is truncated")
        if source.readframes(1):
            raise SimulationError("approved NFC cue frame count is invalid")
    peak, clipped = _pcm_metrics(raw, width)
    return {
        "channels": channels, "sample_rate": rate, "sample_width": width, "frames": frames,
        "peak_fraction": peak, "clipped_samples": clipped,
    }


def _safe_wav(path, cue):
    if not path and cue["state"] == "missing":
        return
    path = Path(os.path.expanduser(path))
    if cue["state"] == "missing":
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SimulationError("approved missing cue cannot be verified") from exc
        raise SimulationError("approved missing cue now exists")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_CUE_BYTES:
            raise SimulationError("approved NFC cue is unsafe")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            audio = _wav_metrics(handle)
        duration = audio["frames"] / audio["sample_rate"]
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if (digest.hexdigest() != cue["sha256"]
                or abs(duration - cue["duration_seconds"]) > 0.01
                or audio != cue["audio"]):
            raise SimulationError("approved NFC cue changed")
    except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
        raise SimulationError("approved NFC cue is invalid") from exc
    finally:
        os.close(descriptor)


def _audit_cues(authorization, contacts, nfc, mappings):
    required = []
    for mapping in mappings:
        required.append(("known", contacts[mapping["recipient"]].get("card_clip", "")))
    if authorization["profile"] == "unknown_hold":
        required.append(("unknown", nfc.UNKNOWN_TOKEN_WAV))
    required = sorted(set(required))
    actual = sorted((cue["role"], cue["path"]) for cue in authorization["cues"])
    if actual != required:
        raise SimulationError("NFC cue authorization does not match reachable assets")
    by_identity = {(cue["role"], cue["path"]): cue for cue in authorization["cues"]}
    for identity in required:
        _safe_wav(identity[1], by_identity[identity])


def _settings_document(store):
    document, warning = store.load()
    if warning:
        raise SimulationError("scratch settings required recovery or defaults")
    if document["master_volume_percent"] != 30:
        raise SimulationError("NFC scratch requires the verified owner volume of 30 percent")
    return document


def _dashboard_inactive():
    result = subprocess.run(
        ["systemctl", "is-active", "messagebox-dash.service"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 3 or result.stdout.strip() != "inactive":
        raise SimulationError("production dashboard service must be verified inactive")


class ProductionGuard:
    def __init__(self, production, manifest, authorization):
        self.production = production
        self.manifest = manifest
        self.authorization = authorization
        self.namespace_root = None
        self.scratch_contacts = None
        self.scratch_settings = None
        self.scratch_queue = None
        self.application_bound = False
        self.baseline = receiver._production_trees(production)
        self.cloud = receiver._tree("/var/lib/messagebox-cloud")
        self.settings = _file_snapshot(production["settings"])
        self.mutable = set(authorization["sync_mutable_paths"])
        self.sync = receiver._tree(production["sync_store"], self.mutable)
        self.runtime = SimpleNamespace(transport_mode=receiver._actual_transport_mode,
                                       WACLI_BIN="/usr/local/bin/wacli")
        self.failed = threading.Event()
        self.lock = threading.Lock()
        self.error = None

    def bind_namespace(self, root):
        self.namespace_root = Path(root)
        self.scratch_contacts = _file_snapshot(self.namespace_root / "state" / "contacts.json")
        self.scratch_settings = _file_snapshot(self.namespace_root / "settings" / "settings.json")
        self.scratch_queue = receiver._tree(self.namespace_root / "queue")
        self.application_bound = True

    def fail(self, error=None):
        self.error = error
        self.failed.set()

    def check_latch(self):
        if self.failed.is_set():
            raise SimulationError("NFC scratch preservation guard failed")

    def verify(self):
        with self.lock:
            self.check_latch()
            try:
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
                if self.namespace_root is not None:
                    if (_file_snapshot(self.namespace_root / "state" / "contacts.json")
                            != self.scratch_contacts
                            or _file_snapshot(self.namespace_root / "settings" / "settings.json")
                            != self.scratch_settings):
                        raise SimulationError("scratch contacts or settings changed")
                    if receiver._tree(self.namespace_root / "queue") != self.scratch_queue:
                        raise SimulationError("scratch inbound queue changed")
                receiver._verify_sync_store(
                    self.sync, receiver._tree(self.production["sync_store"], self.mutable), self.mutable,
                )
            except BaseException as exc:
                self.fail(exc)
                raise


class Monitor:
    def __init__(self, guard, interval=MONITOR_INTERVAL_SECONDS):
        self.guard = guard
        self.interval = interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                self.guard.verify()
            except BaseException:
                return

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        # A running preservation check owns the guard lock and may be inside a
        # subprocess with its own timeout. Never restore patched boundaries or
        # write a success receipt while that check is still active; the outer
        # supervisor remains the hard deadline.
        self.thread.join()


class AllowedSubprocess:
    def __init__(self, original, runtime, cues, scratch_root, guard=None):
        self.original, self.runtime = original, runtime
        self.guard = guard
        self.cues = {str(Path(cue["path"]).expanduser()): cue
                     for cue in cues if cue["state"] == "regular"}
        self.scratch_root = Path(scratch_root).resolve()
        self.beeps = {os.fspath(value[0]): value[1:] for value in runtime.BEEPS.values()}
        self.calls = []

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
        if command[:1] == ("ffmpeg",) and command[-1:]:
            values = self.beeps.get(command[-1])
            ok = (values is not None and len(command) == 11
                  and Path(command[-1]).resolve().is_relative_to(self.scratch_root)
                  and command == ("ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                  f"sine=frequency={values[0]}:duration={values[1]}",
                                  "-filter:a", f"volume={values[2]}dB", command[-1]))
        elif command[:1] == ("amixer",):
            ok = command == ("amixer", "-q", "-c", self.runtime.SPEAKER_CARD, "sset",
                             self.runtime.SPEAKER_CONTROL, "30%", "unmute")
        elif command[:1] == ("aplay",) and command[-1:]:
            target = str(Path(command[-1]).expanduser())
            ok = (command == ("aplay", "-q", "-D", self.runtime.SPK_DEV, command[-1])
                  and (target in self.cues or command[-1] in self.beeps))
            if ok and target in self.cues:
                _safe_wav(target, self.cues[target])
        if not ok:
            raise SimulationError("NFC scratch child attempted a disallowed subprocess")
        if self.guard is not None and command[0] in {"ffmpeg", "amixer", "aplay"}:
            self.guard.verify()
        self.calls.append(command[0])
        if command[0] in {"ffmpeg", "aplay"}:
            kwargs.setdefault("stdout", subprocess.DEVNULL)
            kwargs.setdefault("stderr", subprocess.DEVNULL)
        return self.original(args, *positional, **kwargs)


@contextmanager
def _guard_subprocesses(runtime, cues, audio_scratch_root, guard):
    original = subprocess.run
    guarded = AllowedSubprocess(original, runtime, cues, audio_scratch_root, guard)
    subprocess.run = guarded
    runtime.subprocess.run = guarded
    try:
        yield guarded
    finally:
        runtime.subprocess.run = original
        subprocess.run = original


def _production_manifest(authorization):
    return {
        "transport": "wacli", "contacts_sha256": authorization["contacts"]["sha256"],
        "settings_sha256": authorization["settings"]["sha256"],
        "account_sha256": authorization["account_sha256"],
        "recipients": authorization["recipients"],
    }


def _verify_bindings(root, paths, runtime, nfc):
    root = Path(root).resolve()
    values = {
        paths.QUEUE_DIR, paths.OUTBOX_DIR, paths.STATE_DIR, paths.CONTACTS_FILE,
        paths.SETTINGS_DIR, paths.SETTINGS_FILE, paths.RUNTIME_DIR,
        runtime.QUEUE_DIR, runtime.OUTBOX_DIR, runtime.STATE_DIR, runtime.CONTACTS_FILE,
        runtime.TEMP_DIR, runtime.EVENTS_FILE, runtime.LISTENED_DIR,
        nfc.CONTACTS_FILE, nfc.NFC_SELECTION_FILE, nfc.NFC_ANNOUNCEMENT_FILE,
        nfc.NFC_ENROLLMENT_FILE, nfc.NFC_HEALTH_FILE,
        runtime.nfc_announcement_store.path,
        *(value[0] for value in runtime.BEEPS.values()),
    }
    if any(not Path(value).resolve().is_relative_to(root) for value in values):
        raise SimulationError("NFC scratch path audit failed")


def _removal_observed(traces, event_record, next_present, removal_grace,
                      tolerance=shared.MAX_EVENT_LATENESS_S + 0.02):
    try:
        start = traces.index(event_record)
        event_time = event_record["elapsed_seconds"]
    except (ValueError, KeyError, TypeError):
        return False
    if type(event_time) not in (int, float) or not math.isfinite(event_time):
        return False
    earliest = event_time + max(0, removal_grace - tolerance)
    latest = next_present.get("elapsed_seconds") if next_present is not None else None
    for item in traces[start + 1:]:
        elapsed = item.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed):
            continue
        if latest is not None and elapsed >= latest:
            return False
        if item.get("handler_action") == "removed" and elapsed >= earliest:
            return True
    return False


def _trace_assertions(profile, plan, traces, contact_count, removal_grace=0.8,
                      expected_routes=None):
    if not traces or any(item.get("type") != "nfc_observation" for item in traces):
        raise SimulationError("NFC trace contains an unexpected application observation")
    event_records = [item for item in traces if item.get("input_event") is not None]
    if [item.get("input_event") for item in event_records] != [event["type"] for event in plan["events"]]:
        raise SimulationError("NFC trace did not process every planned event")
    if any(item.get("handler_authorized_recipient") is False
           or item.get("selection_authorized_recipient") is False for item in traces):
        raise SimulationError("NFC trace observed an unauthorized recipient")
    removal_events = [item for item in event_records if item.get("input_event") == "nfc-removed"]
    present_events = [item for item in event_records if item.get("input_event") == "nfc-present"]

    def removed(index):
        event = removal_events[index]
        following = next((item for item in present_events
                          if traces.index(item) > traces.index(event)), None)
        return _removal_observed(traces, event, following, removal_grace)

    if profile == "unknown_hold":
        unknown_records = [item for item in traces if item.get("handler_action") == "unknown"
                           and item.get("unknown_marker_present") is True]
        if not unknown_records:
            raise SimulationError("unknown NFC result was not observed")
        unknown_index = traces.index(unknown_records[0])
        cleared = next((item for item in traces[unknown_index + 1:]
                        if item.get("handler_action") == "removed"), None)
        cleared_index = traces.index(cleared) if cleared is not None else len(traces)
        held = [item for item in traces[unknown_index:cleared_index]
                if item.get("reader_present") is True]
        if (not held or any(item.get("unknown_marker_present") is not True for item in held)
                or not any(item.get("announcement_present") is False for item in held)):
            raise SimulationError("unknown NFC block did not persist after announcement consumption")
        if len(removal_events) != 1 or not removed(0) or not any(
                item.get("unknown_marker_present") is False
                and item.get("reader_present") is False
                and item.get("handler_action") == "removed" for item in traces):
            raise SimulationError("unknown marker did not clear after removal grace")
    elif profile == "stale_selection":
        if contact_count < 2:
            raise SimulationError("stale selection requires multi-contact routing")
        presentations = [item for item in event_records if item.get("input_event") == "nfc-present"]
        if (len(presentations) != 1 or presentations[0].get("handler_action") != "selected"
                or presentations[0].get("handler_authorized_recipient") is not True):
            raise SimulationError("stale NFC presentation did not select its authorized recipient")
        known_results = [item for item in traces
                         if item.get("handler_action") in {"selected", "recognized", "refreshed"}]
        if expected_routes is not None and any(
                expected_routes.get(item.get("reader_card_sha256"))
                != item.get("handler_recipient_sha256") for item in known_results):
            raise SimulationError("observed NFC result does not match its authorized card route")
        if not any(item.get("selection_present") and item.get("selection_within_ttl") is False
                   and item.get("reader_present") is False
                   and type(item.get("elapsed_seconds")) in (int, float)
                   and item["elapsed_seconds"] >= 32 for item in traces):
            raise SimulationError("stale NFC selection was not observed")
        if len(removal_events) != 1 or not removed(0):
            raise SimulationError("stale profile removal was not observed")
    else:
        expected = {"selected"} if contact_count >= 2 else {"recognized"}
        presentations = [item for item in event_records if item.get("input_event") == "nfc-present"]
        if any(item.get("handler_action") not in expected
               or item.get("handler_authorized_recipient") is not True for item in presentations):
            raise SimulationError("every known NFC presentation must return the expected result")
        known_results = [item for item in traces
                         if item.get("handler_action") in {"selected", "recognized", "refreshed"}]
        if expected_routes is not None and any(
                expected_routes.get(item.get("reader_card_sha256"))
                != item.get("handler_recipient_sha256") for item in known_results):
            raise SimulationError("observed NFC result does not match its authorized card route")
        if profile == "known_repeat":
            expected_repeat = "refreshed" if contact_count >= 2 else "recognized"
            first_present_time = presentations[0].get("elapsed_seconds")
            first_removal_time = removal_events[0].get("elapsed_seconds") if removal_events else None
            refreshes = [item for item in traces
                         if item.get("handler_action") == expected_repeat
                         and item.get("handler_announce") is False
                         and item.get("handler_authorized_recipient") is True
                         and type(item.get("elapsed_seconds")) in (int, float)
                         and type(first_present_time) in (int, float)
                         and type(first_removal_time) in (int, float)
                         and first_present_time < item["elapsed_seconds"] < first_removal_time]
            if not refreshes:
                raise SimulationError("known NFC refresh was not observed")
            if len(removal_events) != 2 or not all(removed(index) for index in range(2)):
                raise SimulationError("known NFC removals were not observed")
        if profile == "assigned_alternation":
            recipients = [item.get("handler_recipient_sha256") for item in presentations]
            cards = [item.get("reader_card_sha256") for item in presentations]
            if (None in recipients or None in cards or recipients[0] != recipients[2]
                    or recipients[0] == recipients[1] or cards[0] != cards[2]
                    or cards[0] == cards[1]):
                raise SimulationError("two assigned NFC identities were not observed")
            if len(removal_events) != 1 or not removed(0):
                raise SimulationError("assigned NFC removal was not observed")


def _write_run_receipt(outbound_root, status, profile, plan, authorization, traces, calls,
                       beep_proofs):
    root = Path(outbound_root)
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    value = {
        "version": 1, "status": status, "profile": profile,
        "plan_sha256": _json_sha(plan), "authorization_sha256": _json_sha(authorization),
        "trace_records": traces, "subprocess_kinds": sorted(set(calls)),
        "generated_beeps": beep_proofs,
        "case_evidence": "live_app_simulated_tag_events",
        "mixer_application": "requires_separate_observation",
        "delivery_acceptance": "unverified",
    }
    receiver._new_private_json(root / "nfc-run-receipt.json", value)


def _verify_no_generated_work(root):
    root = Path(root)
    expected = {"outbox", "recording-temp", "listened-receipts"}
    entries = {path.name: path for path in root.iterdir()}
    if set(entries) != expected:
        raise SimulationError("NFC-only execution created unexpected scratch work")
    for name in ("outbox", "recording-temp"):
        path = entries[name]
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or any(path.iterdir())):
            raise SimulationError("NFC-only execution created outbound, capture, or receipt work")
    receipts = entries["listened-receipts"]
    metadata = receipts.lstat()
    children = {path.name: path for path in receipts.iterdir()}
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or set(children) != {"sent", "pending", "inflight", "seen"}):
        raise SimulationError("NFC-only execution created outbound, capture, or receipt work")
    for path in children.values():
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or any(path.iterdir())):
            raise SimulationError("NFC-only execution created outbound, capture, or receipt work")


def _private_empty_directory(path, label):
    path = Path(path)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()
            or any(path.iterdir())):
        raise SimulationError(f"{label} must be empty, private, and owned by the runtime user")
    return path.resolve()


def _beep_proofs(runtime):
    proofs = []
    for name, (path, _frequency, _duration, _gain) in sorted(runtime.BEEPS.items()):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_CUE_BYTES:
                raise SimulationError("generated NFC feedback asset is unsafe")
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                audio = _wav_metrics(handle)
            duration = audio["frames"] / audio["sample_rate"]
            proofs.append({"name": name, "sha256": receiver._digest(path),
                           "duration_seconds": round(duration, 4), "audio": audio})
        except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
            raise SimulationError("generated NFC feedback asset is invalid") from exc
        finally:
            os.close(descriptor)
    return proofs


def _validate_routes(plan, authorization, contacts, nfc):
    if not set(authorization["recipients"]).issubset(contacts):
        raise SimulationError("approved NFC recipients changed")
    mappings = authorization["mappings"]
    mapped = {mapping["uid"]: mapping["recipient"] for mapping in mappings}
    for mapping in mappings:
        contact = contacts.get(mapping["recipient"])
        if contact is None or mapping["uid"] not in contact["card_uids"]:
            raise SimulationError("approved NFC mapping changed")
    planned = [event["uid"] for event in plan["events"] if event["type"] == "nfc-present"]
    if set(planned) != set(mapped):
        raise SimulationError("planned known cards do not match authorization")
    if plan["profile"] == "unknown_hold":
        unknown = plan["events"][0]["uid"]
        if any(unknown in contact["card_uids"] for contact in contacts.values()):
            raise SimulationError("planned unknown card is currently assigned")
    if plan["profile"] == "stale_selection" and len(contacts) < 2:
        raise SimulationError("stale selection requires selection-capable multi-contact routing")
    _audit_cues(authorization, contacts, nfc, mappings)


def _prepare_namespace(plan, authorization, root, production, guard):
    paths = receiver.bind_scratch_paths(root)
    standard_plan = {"duration": plan["duration"], "events": plan["events"]}
    shared.validate_plan(standard_plan)
    receiver._copy_exact(production["contacts"], paths.CONTACTS_FILE,
                         authorization["contacts"]["sha256"])
    receiver._copy_exact(production["settings"], paths.SETTINGS_FILE,
                         authorization["settings"]["sha256"])
    paths.QUEUE_DIR.mkdir(mode=0o700)
    paths.RUNTIME_DIR.mkdir(mode=0o700)
    from messagebox import button_send as runtime, nfc
    from messagebox.contacts import ContactStore
    from messagebox.settings import SettingsStore
    _verify_bindings(root, paths, runtime, nfc)
    contacts_document = ContactStore(paths.CONTACTS_FILE).load()
    settings_document = _settings_document(SettingsStore(paths.SETTINGS_FILE))
    _verify_file_proof(production["contacts"], authorization["contacts"], contacts_document)
    _verify_file_proof(production["settings"], authorization["settings"], settings_document)
    _verify_file_proof(paths.CONTACTS_FILE, authorization["contacts"], contacts_document,
                       copied=True)
    _verify_file_proof(paths.SETTINGS_FILE, authorization["settings"], settings_document,
                       copied=True)
    contacts = contacts_document["contacts"]
    _validate_routes(plan, authorization, contacts, nfc)
    guard.bind_namespace(root)
    guard.verify()
    return standard_plan, runtime, nfc, contacts


def execute_nfc(plan, authorization, namespace_root, outbound_root, production):
    receiver._actual_transport_mode()
    root = receiver._verify_storage_layout(namespace_root, production, outbound=outbound_root)
    root = _private_empty_directory(root, "NFC namespace")
    outbound = _private_empty_directory(outbound_root, "NFC outbound scratch")
    manifest = _production_manifest(authorization)
    guard = ProductionGuard(production, manifest, authorization)
    guard.verify()
    standard_plan, runtime, nfc, contacts = _prepare_namespace(
        plan, authorization, root, production, guard,
    )
    traces, calls, trace_instance = [], [], [None]
    original_trace, original_inputs, original_log = shared.InputTrace, shared.Inputs, runtime.log

    class CapturingTrace(original_trace):
        def __init__(self, application, verified):
            super().__init__(application, verified, emit=self.capture)
            trace_instance[0] = self

        def capture(self, record):
            traces.append(record)
            self.print_record(record)

    class GuardedInputs(original_inputs):
        def tick(self):
            guard.check_latch()
            result = super().tick()
            guard.check_latch()
            return result

    monitor = Monitor(guard)
    status = "failed"
    namespace_receipt = {
        "version": 1, "status": "preparing", "profile": plan["profile"],
        "authorization_sha256": _json_sha(authorization),
        "storage_sha256": receiver._storage_sha(production),
        "services": SERVICE_BASELINE, "generated_beeps": [],
    }
    receipt_path = root / NAMESPACE_RECEIPT
    receiver._new_private_json(receipt_path, namespace_receipt)
    try:
        shared.InputTrace, shared.Inputs = CapturingTrace, GuardedInputs
        runtime.log = lambda _message: print(json.dumps({"type": "nfc_runtime", "status": "redacted"}), flush=True)
        monitor.start()
        with _guard_subprocesses(runtime, authorization["cues"], root, guard) as subprocesses:
            try:
                runtime.make_beeps()
                namespace_receipt["generated_beeps"] = _beep_proofs(runtime)
                namespace_receipt["status"] = "prepared"
                receiver._replace_private_json(receipt_path, namespace_receipt)
                shared.execute(standard_plan, manifest, False, outbound)
            finally:
                calls[:] = subprocesses.calls
        _verify_no_generated_work(outbound)
        guard.verify()
        if trace_instance[0] is None:
            raise SimulationError("NFC trace was not initialized")
        expected_routes = {
            trace_instance[0].identifier(mapping["uid"], "card"):
            trace_instance[0].identifier(mapping["recipient"], "recipient")
            for mapping in authorization["mappings"]
        }
        _trace_assertions(plan["profile"], plan, traces, len(contacts),
                          nfc.REMOVAL_GRACE_S, expected_routes)
        final_beeps = _beep_proofs(runtime)
        status = "completed"
    finally:
        monitor.close()
        shared.InputTrace, shared.Inputs, runtime.log = original_trace, original_inputs, original_log
        if status == "completed":
            try:
                guard.verify()
            except BaseException:
                status = "failed"
        namespace_receipt["status"] = status
        try:
            receiver._replace_private_json(receipt_path, namespace_receipt)
            _write_run_receipt(outbound, status, plan["profile"], plan, authorization,
                               traces, calls, final_beeps if status == "completed" else [])
        except Exception:
            guard.fail()
            raise
    if guard.failed.is_set():
        raise SimulationError("NFC scratch preservation guard failed")


def _child(plan, authorization, namespace_root, outbound_root, production):
    os.setsid()
    os.umask(0o077)
    try:
        execute_nfc(plan, authorization, namespace_root, outbound_root, production)
    except Exception:
        print("NFC scratch run failed; preserve artifacts and reconcile before retry.", file=sys.stderr)
        raise SystemExit(1) from None


def _supervise(plan, authorization, namespace_root, outbound_root, production, timeout):
    process = multiprocessing.get_context("spawn").Process(
        target=_child, args=(plan, authorization, namespace_root, outbound_root, production),
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
        print("NFC scratch child did not stop cleanly; reconcile before retry.", file=sys.stderr)
        return 1
    return 0 if process.exitcode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--namespace-root", type=Path)
    parser.add_argument("--scratch-state-root", type=Path)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    try:
        plan = _static_plan(_private_json(args.plan))
        if not args.execute:
            if any((args.authorization, args.namespace_root, args.scratch_state_root)):
                raise SimulationError("execution options require --execute")
            print("NFC scratch plan shape is valid; no application, device, or audio state was accessed.")
            return 0
        if (args.authorization is None or args.namespace_root is None or args.scratch_state_root is None
                or not math.isfinite(args.timeout)
                or not plan["duration"] + 45 <= args.timeout <= MAX_RUNTIME_SECONDS):
            raise SimulationError("bounded NFC execution inputs are required")
        authorization = validate_authorization(_private_json(args.authorization))
        if authorization["profile"] != plan["profile"]:
            raise SimulationError("NFC plan and authorization profiles differ")
        production = receiver._production_paths()
        return _supervise(plan, authorization, args.namespace_root, args.scratch_state_root,
                          production, args.timeout)
    except (OSError, ValueError, json.JSONDecodeError, SimulationError):
        print("NFC scratch plan or authorization is invalid.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
