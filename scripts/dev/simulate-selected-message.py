#!/usr/bin/env python3
"""Run one privately authorized new inbound message through the real guided loop.

This is an owner-only test driver.  It selects one exact receiver-created queue
item while preserving every pre-existing queued item.  It is not a FIFO test or
a substitute for physical button, speaker, microphone, or delivery acceptance.
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
import sys
import threading
import time


def _load_shared_driver():
    path = Path(__file__).with_name("simulate-inputs.py")
    spec = importlib.util.spec_from_file_location("messagebox_input_simulator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared input simulator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = _load_shared_driver()
SimulationError = shared.SimulationError
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TARGET_KEYS = {
    "name", "wav_sha256", "sidecar_sha256", "sidecar_document",
    "not_before", "max_age_seconds",
}
MANIFEST_KEYS = {
    "version", "transport", "contacts_sha256", "settings_sha256",
    "account_sha256", "recipient", "baseline_entries", "target",
}
STRUCTURAL_QUEUE_ENTRIES = {".inflight", ".played", ".played.lock"}
MAX_RUNTIME_SECONDS = 300
TERMINATION_GRACE_SECONDS = 1


def _digest(value):
    return isinstance(value, str) and HEX_SHA256.fullmatch(value) is not None


def _safe_relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def validate_selected_plan(plan):
    plan = shared.validate_plan(plan)
    if any(event["type"] not in {"press", "release"} for event in plan["events"]):
        raise SimulationError("selected-message plans support button events only")
    if plan["events"][0]["type"] != "press":
        raise SimulationError("selected-message plan must begin with a press")
    return plan


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise SimulationError("selected-message authorization fields are invalid")
    if manifest["version"] != 1 or manifest["transport"] not in {"cloud", "wacli"}:
        raise SimulationError("selected-message authorization version or transport is invalid")
    if not all(_digest(manifest[key]) for key in (
            "contacts_sha256", "settings_sha256", "account_sha256")):
        raise SimulationError("selected-message authorization fingerprints are invalid")
    if not isinstance(manifest["recipient"], str) or not manifest["recipient"]:
        raise SimulationError("selected-message authorization recipient is invalid")

    baseline = manifest["baseline_entries"]
    if not isinstance(baseline, list):
        raise SimulationError("selected-message baseline must be a list")
    names = []
    for entry in baseline:
        if not isinstance(entry, dict) or entry.get("kind") not in {"file", "directory"}:
            raise SimulationError("selected-message baseline entry is invalid")
        expected = {"name", "kind", "sha256"} if entry["kind"] == "file" else {"name", "kind"}
        if set(entry) != expected or not _safe_relative_path(entry["name"]):
            raise SimulationError("selected-message baseline entry fields are invalid")
        if entry["kind"] == "file" and not _digest(entry["sha256"]):
            raise SimulationError("selected-message baseline hash is invalid")
        names.append(entry["name"])
    if names != sorted(names) or len(names) != len(set(names)):
        raise SimulationError("selected-message baseline must be complete, unique, and sorted")

    target = manifest["target"]
    if not isinstance(target, dict) or set(target) != TARGET_KEYS:
        raise SimulationError("selected-message target fields are invalid")
    if (not _safe_relative_path(target["name"]) or Path(target["name"]).name != target["name"]
            or not target["name"].endswith(".wav") or target["name"].startswith(".")):
        raise SimulationError("selected-message target name is invalid")
    if target["name"] in names or target["name"] + ".json" in names:
        raise SimulationError("selected-message target overlaps the protected baseline")
    if not _digest(target["wav_sha256"]) or not _digest(target["sidecar_sha256"]):
        raise SimulationError("selected-message target hashes are invalid")
    if not isinstance(target["sidecar_document"], dict):
        raise SimulationError("selected-message target sidecar document is invalid")
    try:
        json.dumps(target["sidecar_document"], sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise SimulationError("selected-message target sidecar document is invalid") from None
    not_before, max_age = target["not_before"], target["max_age_seconds"]
    if (type(not_before) not in (int, float) or not math.isfinite(not_before)
            or type(max_age) not in (int, float) or not math.isfinite(max_age)
            or not 1 <= max_age <= 600):
        raise SimulationError("selected-message target freshness is invalid")
    return manifest


def _regular_sha256(path):
    try:
        before = path.lstat()
    except OSError as exc:
        raise SimulationError("authorized queue entry is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise SimulationError("queue entries must be regular files or directories")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise SimulationError("queue entry changed while it was verified")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise SimulationError("queue entry changed while it was verified")
        return digest.hexdigest(), opened
    except OSError as exc:
        raise SimulationError("queue entry could not be verified safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_queue_tree_types(queue):
    pending = [Path(queue)]
    seen = 0
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise SimulationError("incoming queue tree could not be inspected safely") from exc
        for child in children:
            seen += 1
            if seen > 10000:
                raise SimulationError("incoming queue tree exceeds the selected-test bound")
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise SimulationError("incoming queue tree changed while it was inspected") from exc
            if stat.S_ISLNK(metadata.st_mode) or not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise SimulationError("incoming queue contains a link or special entry")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(child.path))


def snapshot_queue_root(queue):
    queue = Path(queue)
    try:
        metadata = queue.lstat()
    except OSError as exc:
        raise SimulationError("incoming queue is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SimulationError("incoming queue must be a real directory")
    _verify_queue_tree_types(queue)
    entries = []
    pending = [queue]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise SimulationError("incoming queue could not be listed") from exc
        for path in children:
            name = path.relative_to(queue).as_posix()
            try:
                item = path.lstat()
            except OSError as exc:
                raise SimulationError("incoming queue changed while it was listed") from exc
            if stat.S_ISREG(item.st_mode):
                digest, _metadata = _regular_sha256(path)
                entries.append({"name": name, "kind": "file", "sha256": digest})
            elif stat.S_ISDIR(item.st_mode):
                entries.append({"name": name, "kind": "directory"})
                pending.append(path)
            else:
                raise SimulationError("incoming queue contains a link or special entry")
    return sorted(entries, key=lambda entry: entry["name"])


def _read_target_sidecar(path, expected_hash):
    descriptor = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
            raise SimulationError("selected-message sidecar does not match authorization")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode)):
            raise SimulationError("selected-message sidecar changed while it was verified")
        raw = os.read(descriptor, 64 * 1024 + 1)
        if len(raw) > 64 * 1024 or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise SimulationError("selected-message sidecar does not match authorization")
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise SimulationError("selected-message sidecar changed while it was verified")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SimulationError("selected-message sidecar is malformed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise SimulationError("selected-message sidecar is malformed")
    return value, opened


def _target_metadata(manifest, sidecar_path, *, archived=False):
    expected_hash = manifest["target"]["sidecar_sha256"]
    if archived:
        archive_hash, _archive_stat = _regular_sha256(sidecar_path)
        value, metadata = _read_target_sidecar(sidecar_path, archive_hash)
    else:
        value, metadata = _read_target_sidecar(sidecar_path, expected_hash)
        if value != manifest["target"]["sidecar_document"]:
            raise SimulationError("selected-message sidecar document does not match authorization")
    mode = manifest["transport"]
    required_strings = ("chat", "msgid", "sender_jid", "media_type")
    if (value.get("version") != 1 or value.get("chat") != manifest["recipient"]
            or any(not isinstance(value.get(key), str) or not value[key] for key in required_strings)
            or value.get("media_type") != "audio"
            or (value.get("cloud") is True) != (mode == "cloud")
            or (not archived and any(key in value for key in (
                "replay_name", "replay_history_file", "played_at", "duration_s",
            )))):
        raise SimulationError("selected-message sidecar is not bound to a normal authorized receiver item")
    if mode == "cloud" and (not isinstance(value.get("cloud_message_id"), str)
                            or not value["cloud_message_id"]):
        raise SimulationError("selected cloud message binding is incomplete")
    if archived:
        original = manifest["target"]["sidecar_document"]
        additions = set(value) - set(original)
        if (not set(original).issubset(value)
                or any(value[key] != expected for key, expected in original.items())
                or additions - {"played_at", "duration_s"}
                or "played_at" not in additions):
            raise SimulationError("archived selected-message binding changed")
        played_at = value["played_at"]
        if (type(played_at) not in (int, float) or not math.isfinite(played_at)
                or played_at < manifest["target"]["not_before"]
                or played_at > time.time() + 5):
            raise SimulationError("archived selected-message played time is invalid")
        if "duration_s" in additions:
            duration = value["duration_s"]
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                raise SimulationError("archived selected-message duration is invalid")
    return value, metadata


def _baseline_without_target(manifest, entries, stage):
    target = manifest["target"]["name"]
    by_name = {entry["name"]: entry for entry in entries}
    for directory in (".inflight", ".hold", ".trash", ".played"):
        entry = by_name.get(directory)
        if entry is not None and entry != {"name": directory, "kind": "directory"}:
            raise SimulationError("queue lifecycle path has an unsafe type")
    lock = by_name.get(".played.lock")
    if lock is not None and lock.get("kind") != "file":
        raise SimulationError("queue history lock path has an unsafe type")
    if stage == "waiting":
        target_entry = by_name.pop(target, None)
        sidecar_entry = by_name.pop(target + ".json", None)
        if target_entry is None or sidecar_entry is None:
            raise SimulationError("selected-message target is no longer waiting")
    else:
        if target in by_name or target + ".json" in by_name:
            raise SimulationError("selected-message target returned to the waiting queue")
    lifecycle_targets = {
        f"{directory}/{target}" for directory in (".inflight", ".hold", ".trash", ".played")
    }
    lifecycle_targets.update(path + ".json" for path in tuple(lifecycle_targets))
    # archive_played_file writes this exact temporary metadata pathname before
    # replacing the final sidecar. It must never collide with a baseline file.
    lifecycle_targets.add(f".played/{target}.json.part")
    found = lifecycle_targets.intersection(by_name)
    if stage == "waiting" and found:
        raise SimulationError("selected-message target already exists in another queue state")
    if stage != "waiting":
        expected_archive = {f".played/{target}", f".played/{target}.json"}
        if not expected_archive.issubset(by_name) or found != expected_archive:
            raise SimulationError("selected-message target did not reach the exact played archive")
        for name in expected_archive:
            by_name.pop(name)
    baseline_names = {entry["name"] for entry in manifest["baseline_entries"]}
    if stage != "waiting":
        for name in STRUCTURAL_QUEUE_ENTRIES - baseline_names:
            entry = by_name.get(name)
            if entry is None:
                continue
            if name == ".played.lock" and entry != {
                    "name": name, "kind": "file", "sha256": hashlib.sha256(b"").hexdigest()}:
                raise SimulationError("queue lock state changed unexpectedly")
            if name != ".played.lock" and entry != {"name": name, "kind": "directory"}:
                raise SimulationError("queue lifecycle state changed unexpectedly")
            by_name.pop(name)
    return [by_name[name] for name in sorted(by_name)]


def verify_queue(runtime, manifest, stage, now=None):
    queue = Path(runtime.QUEUE_DIR)
    entries = snapshot_queue_root(queue)
    if _baseline_without_target(manifest, entries, stage) != manifest["baseline_entries"]:
        raise SimulationError("protected incoming queue changed from the authorized baseline")
    baseline_wavs = sorted(entry["name"] for entry in manifest["baseline_entries"]
                           if entry["kind"] == "file" and "/" not in entry["name"]
                           and entry["name"].endswith(".wav"))
    expected = baseline_wavs + ([manifest["target"]["name"]] if stage == "waiting" else [])
    if runtime.queued() != sorted(expected):
        raise SimulationError("incoming queue order or membership changed")

    target = manifest["target"]
    if stage == "waiting":
        wav = queue / target["name"]
        sidecar = Path(str(wav) + ".json")
        wav_hash, wav_stat = _regular_sha256(wav)
        metadata, sidecar_stat = _target_metadata(manifest, sidecar)
        current = time.time() if now is None else now
        if wav_hash != target["wav_sha256"]:
            raise SimulationError("selected-message audio does not match authorization")
        oldest = min(wav_stat.st_mtime, sidecar_stat.st_mtime)
        newest = max(wav_stat.st_mtime, sidecar_stat.st_mtime)
        if (oldest < target["not_before"] or newest > current + 5
                or current - oldest > target["max_age_seconds"]):
            raise SimulationError("selected-message target is not fresh")
        return metadata

    archive = queue / ".played" / target["name"]
    archive_hash, _archive_stat = _regular_sha256(archive)
    if archive_hash != target["wav_sha256"]:
        raise SimulationError("selected-message archived audio changed")
    return _target_metadata(manifest, Path(str(archive) + ".json"), archived=True)[0]


def verify_archive_preserves_history(
    runtime, manifest, now=None, runtime_bound_s=MAX_RUNTIME_SECONDS,
):
    """Reject a run if normal played-history pruning would alter an old file."""
    from messagebox import played_history
    observed = time.time() if now is None else now
    if (type(runtime_bound_s) not in (int, float) or not math.isfinite(runtime_bound_s)
            or not 0 <= runtime_bound_s <= MAX_RUNTIME_SECONDS):
        raise SimulationError("selected-message runtime bound is invalid")
    # The production archive uses its actual completion time. Model the latest
    # permitted completion so no protected history can cross a retention edge
    # anywhere in this bounded run.
    current = observed + runtime_bound_s + TERMINATION_GRACE_SECONDS
    cloud = manifest["transport"] == "cloud"
    metadata_limit = 1000000 if cloud else played_history.METADATA_LIMIT
    media_limit = 1000000 if cloud else played_history.MEDIA_LIMIT
    retention = 91 * 86400 if cloud else played_history.RETENTION_SECONDS
    media_bytes_limit = 1 << 60 if cloud else played_history.MEDIA_BYTES_LIMIT
    directory = Path(runtime.QUEUE_DIR) / ".played"
    records = []
    try:
        metadata_paths = list(directory.glob("*.wav.json"))
    except OSError as exc:
        raise SimulationError("played history could not be checked safely") from exc
    for metadata_path in metadata_paths:
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            value = {}
        value = value if isinstance(value, dict) else {}
        if value.get("cloud") is not True:
            # Production retention is wall-clock based and archive_played_file
            # has no validation-only boundary. With only the selector replaced,
            # an arbitrary forward clock step after claim could otherwise prune
            # this baseline record before the adapter can stop it.
            raise SimulationError(
                "preexisting local played history cannot be protected by the selected-message adapter"
            )
        played_at = value.get("played_at")
        if not isinstance(played_at, (int, float)) or not math.isfinite(played_at):
            played_at = metadata_path.stat().st_mtime
        records.append((float(played_at), metadata_path, value, False))
    target = Path(runtime.QUEUE_DIR) / manifest["target"]["name"]
    records.append((current, target, {"cloud": cloud}, True))
    records.sort(key=lambda item: item[0], reverse=True)

    retained = []
    local_index = 0
    for played_at, path, value, is_target in records:
        if value.get("cloud") is True:
            continue
        if local_index >= metadata_limit or played_at < current - retention:
            if not is_target:
                raise SimulationError("normal played-history pruning would alter protected history")
        else:
            wav = path if is_target else Path(os.fspath(path)[:-5])
            retained.append((played_at, wav, is_target))
        local_index += 1

    media_bytes = 0
    media_count = 0
    for _played_at, wav, is_target in retained:
        try:
            size = wav.stat().st_size
        except OSError:
            if is_target:
                raise SimulationError("selected-message target disappeared before history preflight")
            continue
        keep = media_count < media_limit and media_bytes + size <= media_bytes_limit
        if not keep:
            raise SimulationError("normal played-history pruning would alter protected media")
        media_count += 1
        media_bytes += size


def _routing_manifest(manifest):
    return {
        "transport": manifest["transport"],
        "account_sha256": manifest["account_sha256"],
        "recipients": [manifest["recipient"]],
    }


def verify_services():
    for unit in ("messagebox-button.service", "messagebox-nfc.service", "messagebox-poller.service"):
        result = __import__("subprocess").run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 3 or result.stdout.strip() != "inactive":
            raise SimulationError("input and queue producer services must be verified inactive")


def verify_nfc_clear(runtime, nfc):
    from messagebox.nfc_state import SelectionStore
    for path in (nfc.NFC_ENROLLMENT_FILE, nfc.NFC_SELECTION_FILE, nfc.NFC_ANNOUNCEMENT_FILE,
                 nfc.NFC_HEALTH_FILE, SelectionStore(nfc.NFC_SELECTION_FILE).unknown_path):
        if shared.trace_present(path) is not False:
            raise SimulationError("existing or unknown NFC state must remain untouched")


def verify_claim_absent(runtime):
    if os.environ.get("MSGBOX_CLAIM_ONLY") == "1" or runtime.cloud_claim.CLAIM_FILE.exists():
        raise SimulationError("claim confirmation cannot be simulated")


@contextmanager
def block_claim_confirmation(runtime):
    """Close the post-preflight claim race without invoking claim side effects."""
    original = runtime.cloud_claim.consume_claim_press

    def guarded():
        verify_claim_absent(runtime)
        return False

    runtime.cloud_claim.consume_claim_press = guarded
    try:
        yield
    finally:
        runtime.cloud_claim.consume_claim_press = original


def verify_environment(
    runtime, nfc, manifest, stage, runtime_bound_s=MAX_RUNTIME_SECONDS,
):
    from messagebox.contacts import ContactStore
    from messagebox.runtime_paths import SETTINGS_FILE
    verify_claim_absent(runtime)
    if runtime.transport_mode() != manifest["transport"]:
        raise SimulationError("transport changed from selected-message authorization")
    if shared.account_hash(runtime) != manifest["account_sha256"]:
        raise SimulationError("account changed from selected-message authorization")
    for path, key in ((runtime.CONTACTS_FILE, "contacts_sha256"),
                      (SETTINGS_FILE, "settings_sha256")):
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != manifest[key]:
            raise SimulationError("contacts or settings changed from selected-message authorization")
    if manifest["recipient"] not in ContactStore(runtime.CONTACTS_FILE).allowed_jids():
        raise SimulationError("selected-message recipient is no longer allowed")
    settings = runtime.caregiver_settings()
    if settings.get("recording_mode") != "tap_review" or settings.get("after_listening") != "invite_reply":
        raise SimulationError("selected-message run requires the existing guided reply settings")
    verify_services()
    verify_nfc_clear(runtime, nfc)
    metadata = verify_queue(runtime, manifest, stage)
    if stage == "waiting":
        verify_archive_preserves_history(
            runtime, manifest, runtime_bound_s=runtime_bound_s,
        )
    playable = runtime.inbound_audio_authorized(metadata)
    if not shared.check_inbound(runtime, _routing_manifest(manifest), metadata, playable):
        raise SimulationError("production playback authorization rejected the selected message")
    return metadata


def preflight(runtime, nfc, manifest, runtime_bound_s=MAX_RUNTIME_SECONDS):
    verify_claim_absent(runtime)
    for directory in (runtime.OUTBOX_DIR, runtime.TEMP_DIR, runtime.LISTENED_DIR):
        path = Path(directory)
        if path.exists() and any(path.iterdir()):
            raise SimulationError("existing outbound or temporary work must remain untouched")
    verify_environment(runtime, nfc, manifest, "waiting", runtime_bound_s)


class SelectedClaim:
    def __init__(self):
        self.calls = 0
        self.claimed = None


@contextmanager
def select_exact_target(runtime, nfc, manifest, runtime_bound_s=MAX_RUNTIME_SECONDS):
    original = runtime.claim_oldest
    state = SelectedClaim()

    def claim_selected():
        if state.calls:
            raise SimulationError("selected-message target may be claimed only once")
        state.calls += 1
        metadata = verify_environment(runtime, nfc, manifest, "waiting", runtime_bound_s)
        source = Path(runtime.QUEUE_DIR) / manifest["target"]["name"]
        if runtime.queue_metadata(source) != metadata:
            raise SimulationError("production metadata lookup changed the selected-message binding")
        claimed = runtime.claim_inbox_file(runtime.QUEUE_DIR, source.name)
        claimed_hash, _stat = _regular_sha256(claimed)
        if claimed_hash != manifest["target"]["wav_sha256"]:
            raise SimulationError("selected-message audio changed during atomic claim")
        _read_target_sidecar(Path(str(claimed) + ".json"), manifest["target"]["sidecar_sha256"])
        state.claimed = claimed
        return {"path": claimed, "meta": metadata}

    runtime.claim_oldest = claim_selected
    try:
        yield state
    finally:
        runtime.claim_oldest = original


def run_inputs(runtime, inputs, before_interaction):
    handled = False
    while not inputs.done:
        if not inputs.is_pressed:
            runtime.play_pending_nfc_announcement()
            time.sleep(runtime.POLL_S)
            continue
        if handled:
            raise SimulationError("selected-message run permits one top-level interaction")
        if runtime.wait_for_confirmed_press(timeout=runtime.CONFIRM_PRESS_S + 2 * runtime.POLL_S):
            before_interaction()
            if not runtime.handle_confirmed_press(inputs.pressed_at):
                raise SimulationError("application input handler failed")
            handled = True
            runtime.wait_for_stable_open()
    if inputs.error:
        raise inputs.error
    if (not handled or inputs.index != len(inputs.plan["events"])
            or inputs.is_pressed or inputs.uid is not None):
        raise SimulationError("selected-message input schedule did not complete")


def _one_generated_job(runtime, manifest):
    jobs = runtime.outbox_store.jobs()
    if len(jobs) != 1 or runtime.legacy_outbox_files():
        raise SimulationError("selected-message run did not create exactly one isolated guided job")
    job = jobs[0]
    if (job.recipient != manifest["recipient"] or job.transport != manifest["transport"]
            or job.flow_kind != "reply" or job.state != "pending"):
        raise SimulationError("selected-message generated job does not match authorization")
    entries = list(Path(runtime.OUTBOX_DIR).iterdir())
    if len(entries) != 1 or entries[0] != job.path:
        raise SimulationError("scratch outbox contains work outside the selected run")
    return job


def send_selected(runtime, nfc, manifest):
    metadata = verify_environment(runtime, nfc, manifest, "archived")
    if not runtime.inbound_audio_authorized(metadata):
        raise SimulationError("selected message lost production authorization before send")
    job = _one_generated_job(runtime, manifest)
    handled = runtime.send_guided_job(job)
    progress = "failed_or_uncertain"
    if handled and manifest["transport"] == "wacli" and not job.path.exists():
        progress = "transport_completed"
    elif handled and manifest["transport"] == "cloud" and job.path.exists():
        durable = json.loads((job.path / "job.json").read_text(encoding="utf-8"))
        state = durable.get("cloud_state")
        if (durable.get("message_id") == job.message_id
                and durable.get("transport") == manifest["transport"]
                and durable.get("recipient") == manifest["recipient"]
                and durable.get("state") == "cloud_retained"
                and isinstance(durable.get("cloud_message_id"), str)
                and durable["cloud_message_id"]
                and state in {"queued", "waiting_for_reply", "accepted", "delivered", "read"}):
            progress = "cloud_" + state
    shared.send_outcome(job.message_id, manifest["transport"], manifest["account_sha256"], progress)
    if progress == "failed_or_uncertain":
        raise SimulationError("selected-message send did not reach verified progress; retain and reconcile")


def execute(plan, manifest, scratch, runtime_bound_s):
    from messagebox import button_send as runtime, nfc
    shared.use_scratch_state(runtime, scratch)
    preflight(runtime, nfc, manifest, runtime_bound_s)
    route_manifest = _routing_manifest(manifest)
    runtime.led = shared.StatusLamp()
    runtime.outbox_store = runtime.OutboxStore(runtime.OUTBOX_DIR, transport=runtime.transport_mode())
    runtime.receipt_store = runtime.ReceiptStore(runtime.LISTENED_DIR)
    Path(runtime.TEMP_DIR).mkdir(parents=True, exist_ok=True)
    runtime.validate_prompts()
    invalid_beeps = runtime.invalid_prompt_files(value[0] for value in ((path,) for path in runtime.CUES.values()))
    if invalid_beeps:
        raise SimulationError("installed runtime feedback audio is unavailable")
    trace = shared.InputTrace(runtime, route_manifest)
    announcement = nfc.AnnouncementStore(nfc.NFC_ANNOUNCEMENT_FILE)
    inputs = shared.Inputs(
        plan, nfc.NfcRuntime(nfc.router(announcement), nfc.Announcer(announcement)),
        lambda: None, trace=trace,
    )
    runtime.button = inputs
    stop = threading.Event()
    scheduler = threading.Thread(target=inputs.run, args=(stop,), daemon=True)
    scheduler.start()
    try:
        with shared.validated_routing(runtime, route_manifest, trace):
            with block_claim_confirmation(runtime):
                with select_exact_target(runtime, nfc, manifest, runtime_bound_s) as selected:
                    run_inputs(runtime, inputs, lambda: verify_environment(
                        runtime, nfc, manifest, "waiting", runtime_bound_s,
                    ))
                    if selected.calls != 1:
                        raise SimulationError("application did not claim the selected message exactly once")
        send_selected(runtime, nfc, manifest)
        verify_queue(runtime, manifest, "archived")
        print(json.dumps({
            "type": "selected_message_simulation", "status": "completed",
            "events_planned": len(plan["events"]), "events_processed": inputs.index,
            "max_event_lateness_seconds": round(inputs.max_lateness, 4),
            "delivery_acceptance": "unverified", "trace_records": trace.count,
            "trace_truncated": trace.truncated,
        }, sort_keys=True), flush=True)
    finally:
        stop.set()
        scheduler.join(timeout=2)


def child(plan, manifest, scratch, runtime_bound_s):
    os.setsid()
    os.umask(0o077)
    try:
        execute(plan, manifest, scratch, runtime_bound_s)
    except Exception:
        print("Selected-message simulation failed; preserve artifacts and reconcile before retry.",
              file=sys.stderr)
        raise SystemExit(1) from None


def supervise(plan, manifest, scratch, timeout):
    process = multiprocessing.get_context("fork").Process(
        target=child, args=(plan, manifest, scratch, timeout),
    )
    process.start()
    try:
        process.join(timeout)
        timed_out = process.is_alive()
    finally:
        cleaned = shared.stop_group(process.pid)
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    if timed_out or not cleaned:
        print("Selected-message simulation did not stop cleanly; reconcile before retry.", file=sys.stderr)
        return 1
    return 0 if process.exitcode == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--send-generated", action="store_true")
    parser.add_argument("--scratch-state-root", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    try:
        plan = validate_selected_plan(shared.private_json(args.plan))
        if not math.isfinite(args.timeout) or not 1 <= args.timeout <= MAX_RUNTIME_SECONDS:
            raise SimulationError(f"timeout must be between 1 and {MAX_RUNTIME_SECONDS} seconds")
        if not args.execute:
            if args.authorization or args.send_generated or args.scratch_state_root:
                raise SimulationError("execution options require --execute")
            print("Selected-message plan is valid; no application or hardware state was accessed.")
            return 0
        if not args.authorization or not args.scratch_state_root or not args.send_generated:
            raise SimulationError(
                "selected-message execution requires private authorization, scratch state, and --send-generated"
            )
        manifest = validate_manifest(shared.private_json(args.authorization))
        return supervise(plan, manifest, args.scratch_state_root, args.timeout)
    except (OSError, ValueError, json.JSONDecodeError, SimulationError):
        print("Selected-message plan or authorization is invalid.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
