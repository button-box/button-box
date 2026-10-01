import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from messagebox.contacts import ContactStore
from messagebox.guided_reply import OutboxStore, claim_inbox_file


spec = importlib.util.spec_from_file_location(
    "selected_message_simulator",
    Path(__file__).resolve().parents[1] / "scripts/dev/simulate-selected-message.py",
)
selected = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selected)

JID = "15551234567@s.whatsapp.net"
OTHER = "15551234568@s.whatsapp.net"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan():
    return {"duration": 3, "events": [
        {"at": 1, "type": "press"}, {"at": 1.2, "type": "release"},
    ]}


class QueueFixture:
    def __init__(self, root, mode="wacli"):
        self.root = Path(root)
        self.queue = self.root / "queue"
        self.queue.mkdir()
        (self.queue / ".hold").mkdir()
        self.old = self.queue / "1700000000000-old.wav"
        self.old.write_bytes(b"protected old audio")
        Path(str(self.old) + ".json").write_text(json.dumps({
            "version": 1, "chat": OTHER, "msgid": "old-message",
            "sender_jid": OTHER, "media_type": "audio",
        }))
        baseline = selected.snapshot_queue_root(self.queue)
        self.target = self.queue / "1800000000000-new.wav"
        self.target.write_bytes(b"new authorized audio")
        metadata = {
            "version": 1, "chat": JID, "msgid": "new-message",
            "sender_jid": JID, "media_type": "audio",
        }
        if mode == "cloud":
            metadata.update({
                "cloud": True, "cloud_message_id": "cloud-message",
                "cloud_operation_id": "cloud-operation", "sender_id": "person",
                "expires_at": time.time() + 300,
            })
        self.sidecar = Path(str(self.target) + ".json")
        self.sidecar.write_text(json.dumps(metadata, sort_keys=True))
        self.started = time.time() - 1
        self.manifest = selected.validate_manifest({
            "version": 1,
            "transport": mode,
            "contacts_sha256": EMPTY_SHA,
            "settings_sha256": EMPTY_SHA,
            "account_sha256": EMPTY_SHA,
            "recipient": JID,
            "baseline_entries": baseline,
            "target": {
                "name": self.target.name,
                "wav_sha256": sha(self.target),
                "sidecar_sha256": sha(self.sidecar),
                "sidecar_document": metadata,
                "not_before": self.started,
                "max_age_seconds": 60,
            },
        })

    def runtime(self):
        return types.SimpleNamespace(
            QUEUE_DIR=str(self.queue),
            queued=lambda: sorted(path.name for path in self.queue.glob("*.wav")),
        )


class SelectedMessageSimulatorTests(unittest.TestCase):
    def test_validation_is_default_and_execution_requires_all_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan()))
            path.chmod(0o600)
            with mock.patch.object(selected, "supervise") as supervise, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(selected.main([str(path)]), 0)
                self.assertEqual(selected.main([str(path), "--execute"]), 2)
            supervise.assert_not_called()
            candidate = plan()
            candidate["events"].insert(1, {"at": 1.1, "type": "nfc-unknown", "uid": "04:A1:00:FF"})
            with self.assertRaises(selected.SimulationError):
                selected.validate_selected_plan(candidate)

    def test_manifest_requires_sorted_complete_hashed_baseline_and_fresh_target(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            invalid = json.loads(json.dumps(fixture.manifest))
            invalid["baseline_entries"].reverse()
            with self.assertRaises(selected.SimulationError):
                selected.validate_manifest(invalid)
            invalid = json.loads(json.dumps(fixture.manifest))
            invalid["target"]["name"] = fixture.old.name
            with self.assertRaises(selected.SimulationError):
                selected.validate_manifest(invalid)
            invalid = json.loads(json.dumps(fixture.manifest))
            invalid["target"]["max_age_seconds"] = 601
            with self.assertRaises(selected.SimulationError):
                selected.validate_manifest(invalid)

    def test_queue_guard_preserves_every_old_entry_and_rejects_change_or_link(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            metadata = selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            self.assertEqual(metadata["chat"], JID)
            before = fixture.old.read_bytes()
            fixture.old.write_bytes(b"changed old audio")
            with self.assertRaises(selected.SimulationError):
                selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            fixture.old.write_bytes(before)
            link = fixture.queue / "unknown.wav"
            link.symlink_to(fixture.old)
            with self.assertRaises(selected.SimulationError):
                selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            self.assertEqual(fixture.old.read_bytes(), before)
            self.assertTrue(fixture.target.exists())

    def test_queue_guard_rejects_nested_symlink_without_reading_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            outside = Path(directory) / "outside"
            outside.write_bytes(b"private bytes")
            link = fixture.queue / ".hold" / "linked.wav"
            link.symlink_to(outside)
            with mock.patch.object(selected, "_regular_sha256", wraps=selected._regular_sha256) as digest:
                with self.assertRaises(selected.SimulationError):
                    selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            self.assertTrue(link.is_symlink())
            self.assertNotIn(outside, [call.args[0] for call in digest.call_args_list])

    def test_recursive_baseline_detects_changed_held_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            held = fixture.queue / ".hold" / "1699999999999-held.wav"
            held.write_bytes(b"protected held audio")
            held_sidecar = Path(str(held) + ".json")
            held_sidecar.write_text("{}")
            target_names = {fixture.target.name, fixture.target.name + ".json"}
            fixture.manifest["baseline_entries"] = [
                entry for entry in selected.snapshot_queue_root(fixture.queue)
                if entry["name"] not in target_names
            ]
            selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            held.write_bytes(b"changed held audio")
            with self.assertRaises(selected.SimulationError):
                selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")

    def test_preflight_rejects_protected_played_archive_temporary_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            played = fixture.queue / ".played"
            played.mkdir()
            collision = played / f"{fixture.target.name}.json.part"
            collision.write_bytes(b"protected temporary evidence")
            target_names = {fixture.target.name, fixture.target.name + ".json"}
            fixture.manifest["baseline_entries"] = [
                entry for entry in selected.snapshot_queue_root(fixture.queue)
                if entry["name"] not in target_names
            ]
            before = collision.read_bytes()
            with self.assertRaises(selected.SimulationError):
                selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            self.assertEqual(collision.read_bytes(), before)
            self.assertTrue(fixture.target.exists())
            self.assertFalse((fixture.queue / ".inflight" / fixture.target.name).exists())

    def test_preflight_rejects_non_directory_lifecycle_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            inflight = fixture.queue / ".inflight"
            inflight.write_bytes(b"protected lifecycle evidence")
            target_names = {fixture.target.name, fixture.target.name + ".json"}
            fixture.manifest["baseline_entries"] = [
                entry for entry in selected.snapshot_queue_root(fixture.queue)
                if entry["name"] not in target_names
            ]
            before = inflight.read_bytes()
            with self.assertRaises(selected.SimulationError):
                selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            self.assertEqual(inflight.read_bytes(), before)
            self.assertTrue(fixture.target.exists())

    def test_queue_guard_rejects_stale_malformed_and_foreign_mode_target(self):
        for case in ("stale", "malformed", "foreign"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = QueueFixture(directory)
                if case == "stale":
                    old = time.time() - 120
                    os.utime(fixture.target, (old, old))
                    os.utime(fixture.sidecar, (old, old))
                    fixture.manifest["target"]["not_before"] = old - 1
                elif case == "malformed":
                    fixture.sidecar.write_text("[]")
                    fixture.manifest["target"]["sidecar_sha256"] = sha(fixture.sidecar)
                else:
                    metadata = json.loads(fixture.sidecar.read_text())
                    metadata["cloud"] = True
                    metadata["cloud_message_id"] = "foreign-message"
                    fixture.sidecar.write_text(json.dumps(metadata))
                    fixture.manifest["target"]["sidecar_sha256"] = sha(fixture.sidecar)
                with self.assertRaises(selected.SimulationError):
                    selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")

    def test_exact_selector_claims_new_target_and_leaves_older_item_in_place(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                fixture = QueueFixture(directory, mode)
                before = fixture.old.read_bytes()
                runtime = types.SimpleNamespace(
                    QUEUE_DIR=str(fixture.queue),
                    claim_oldest=lambda: None,
                    claim_inbox_file=claim_inbox_file,
                    queue_metadata=lambda path: json.loads(Path(str(path) + ".json").read_text()),
                )
                nfc = mock.Mock()
                with mock.patch.object(selected, "verify_environment", return_value=json.loads(
                        fixture.sidecar.read_text())), \
                        selected.select_exact_target(runtime, nfc, fixture.manifest, 30) as state:
                    claimed = runtime.claim_oldest()
                    self.assertEqual(claimed["path"].name, fixture.target.name)
                    with self.assertRaises(selected.SimulationError):
                        runtime.claim_oldest()
                self.assertEqual(state.calls, 1)
                self.assertFalse(fixture.target.exists())
                self.assertTrue((fixture.queue / ".inflight" / fixture.target.name).exists())
                self.assertTrue(fixture.old.exists())
                self.assertEqual(fixture.old.read_bytes(), before)

    def test_production_gate_and_contact_membership_are_rechecked(self):
        from messagebox import runtime_paths
        settings = {
            "recording_mode": "tap_review", "after_listening": "invite_reply",
        }
        for playable, allowed in ((False, True), (True, False)):
            with self.subTest(playable=playable, allowed=allowed), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = QueueFixture(directory, "cloud")
                contacts_path = Path(directory) / "contacts.json"
                settings_path = Path(directory) / "settings.json"
                contacts_path.write_bytes(b"contacts")
                settings_path.write_bytes(b"settings")
                fixture.manifest["contacts_sha256"] = sha(contacts_path)
                fixture.manifest["settings_sha256"] = sha(settings_path)
                runtime = types.SimpleNamespace(
                    QUEUE_DIR=str(fixture.queue), CONTACTS_FILE=str(contacts_path),
                    queued=fixture.runtime().queued, transport_mode=lambda: "cloud",
                    caregiver_settings=lambda: settings,
                    inbound_audio_authorized=lambda metadata: playable,
                    cloud_claim=types.SimpleNamespace(CLAIM_FILE=Path(directory) / "claim.json"),
                )
                allowed_jids = (JID,) if allowed else ()
                with mock.patch.object(selected.shared, "account_hash", return_value=EMPTY_SHA), \
                        mock.patch.object(selected, "verify_services"), \
                        mock.patch.object(selected, "verify_nfc_clear"), \
                        mock.patch.object(runtime_paths, "SETTINGS_FILE", settings_path), \
                        mock.patch.object(ContactStore, "allowed_jids", return_value=allowed_jids):
                    with self.assertRaises(selected.SimulationError):
                        selected.verify_environment(runtime, mock.Mock(), fixture.manifest, "waiting")

    def test_before_send_guard_blocks_queue_change_and_preexisting_outbound(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            runtime = mock.Mock()
            runtime.legacy_outbox_files.return_value = ["preexisting.wav"]
            runtime.outbox_store.jobs.return_value = []
            with self.assertRaises(selected.SimulationError):
                selected._one_generated_job(runtime, fixture.manifest)
            runtime.send_guided_job.assert_not_called()

            with mock.patch.object(selected, "verify_environment",
                                   side_effect=selected.SimulationError("changed")):
                with self.assertRaises(selected.SimulationError):
                    selected.send_selected(runtime, mock.Mock(), fixture.manifest)
            runtime.send_guided_job.assert_not_called()

    def test_send_boundary_dispatches_only_the_one_run_owned_job(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            outbox = Path(directory) / "outbox"
            source = Path(directory) / "reply.wav"
            source.write_bytes(b"synthetic reply")
            store = OutboxStore(str(outbox), transport="wacli")
            job = store.approve(str(source), JID, "reply", 1, message_id="run-owned-job")

            def completed(sent):
                self.assertEqual(sent, job)
                shutil.rmtree(sent.path)
                return True

            runtime = types.SimpleNamespace(
                outbox_store=store, OUTBOX_DIR=str(outbox),
                legacy_outbox_files=lambda: [],
                inbound_audio_authorized=lambda metadata: True,
                send_guided_job=mock.Mock(side_effect=completed),
            )
            with mock.patch.object(selected, "verify_environment", return_value={"chat": JID}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                selected.send_selected(runtime, mock.Mock(), fixture.manifest)
            runtime.send_guided_job.assert_called_once_with(job)
            self.assertFalse(job.path.exists())
            self.assertNotIn(JID, output.getvalue())
            self.assertNotIn(job.message_id, output.getvalue())

    def test_archived_target_allows_only_production_metadata_additions(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            played = fixture.queue / ".played"
            played.mkdir()
            archived = played / fixture.target.name
            fixture.target.replace(archived)
            metadata = json.loads(fixture.sidecar.read_text())
            metadata.update({"played_at": time.time(), "duration_s": 1.0})
            fixture.sidecar.unlink()
            Path(str(archived) + ".json").write_text(json.dumps(metadata))
            result = selected.verify_queue(fixture.runtime(), fixture.manifest, "archived")
            self.assertEqual(result["chat"], JID)
            self.assertTrue(fixture.old.exists())

    def test_preflight_rejects_normal_history_pruning_before_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            played = fixture.queue / ".played"
            played.mkdir()
            for index in range(10):
                wav = played / f"{index:013d}-history.wav"
                wav.write_bytes(b"old played audio")
                Path(str(wav) + ".json").write_text(json.dumps({
                    "version": 1, "chat": OTHER, "msgid": f"history-{index}",
                    "sender_jid": OTHER, "media_type": "audio",
                    "played_at": time.time() - index - 1,
                }))
            target_names = {fixture.target.name, fixture.target.name + ".json"}
            fixture.manifest["baseline_entries"] = [
                entry for entry in selected.snapshot_queue_root(fixture.queue)
                if entry["name"] not in target_names
            ]
            selected.verify_queue(fixture.runtime(), fixture.manifest, "waiting")
            with self.assertRaises(selected.SimulationError):
                selected.verify_archive_preserves_history(fixture.runtime(), fixture.manifest)
            self.assertTrue(all(path.exists() for path in played.glob("*.wav")))

    def test_retention_preflight_projects_through_the_full_runtime_bound(self):
        from messagebox import played_history
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory)
            now = time.time()
            played = fixture.queue / ".played"
            played.mkdir()
            wav = played / "1600000000000-protected.wav"
            wav.write_bytes(b"protected history")
            sidecar = Path(str(wav) + ".json")
            sidecar.write_text(json.dumps({
                "version": 1, "chat": OTHER, "msgid": "protected-history",
                "sender_jid": OTHER, "media_type": "audio",
                "played_at": now - played_history.RETENTION_SECONDS + 1,
            }))
            with self.assertRaises(selected.SimulationError):
                selected.verify_archive_preserves_history(
                    fixture.runtime(), fixture.manifest, now=now, runtime_bound_s=2,
                )
            self.assertTrue(wav.exists())
            self.assertTrue(sidecar.exists())

    def test_archived_metadata_must_preserve_every_original_binding_field(self):
        fields = ("msgid", "sender_jid", "cloud_message_id", "expires_at")
        for field in fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = QueueFixture(directory, "cloud")
                original = fixture.manifest["target"]["sidecar_document"]
                played = fixture.queue / ".played"
                played.mkdir()
                archived = played / fixture.target.name
                fixture.target.replace(archived)
                fixture.sidecar.unlink()
                changed = dict(original)
                changed.update({"played_at": time.time(), "duration_s": 1.0})
                changed[field] = (changed[field] + "-replacement"
                                  if isinstance(changed[field], str) else changed[field] + 1)
                Path(str(archived) + ".json").write_text(json.dumps(changed))
                with self.assertRaises(selected.SimulationError):
                    selected.verify_queue(fixture.runtime(), fixture.manifest, "archived")
                self.assertTrue(fixture.old.exists())

    def test_claim_appearing_after_preflight_cannot_reach_confirmation_side_effect(self):
        from tests.test_input_simulator import button_send
        with tempfile.TemporaryDirectory() as directory:
            claim_file = Path(directory) / "claim.json"
            original = mock.Mock(return_value=True)
            with mock.patch.object(button_send.cloud_claim, "CLAIM_FILE", claim_file), \
                    mock.patch.object(button_send.cloud_claim, "consume_claim_press", original), \
                    mock.patch.object(button_send, "transport_mode", return_value="cloud"), \
                    mock.patch.object(button_send, "run_guided_once") as guided, \
                    selected.block_claim_confirmation(button_send):
                claim_file.write_text("{}")
                with self.assertRaises(selected.SimulationError):
                    button_send.handle_confirmed_press(time.monotonic())
            original.assert_not_called()
            guided.assert_not_called()

    def test_archived_metadata_cannot_delete_an_original_null_valued_field(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = QueueFixture(directory, "cloud")
            original = dict(fixture.manifest["target"]["sidecar_document"])
            original["receiver_optional"] = None
            fixture.sidecar.write_text(json.dumps(original, sort_keys=True))
            fixture.manifest["target"]["sidecar_document"] = original
            fixture.manifest["target"]["sidecar_sha256"] = sha(fixture.sidecar)
            played = fixture.queue / ".played"
            played.mkdir()
            archived = played / fixture.target.name
            fixture.target.replace(archived)
            fixture.sidecar.unlink()
            changed = dict(original)
            del changed["receiver_optional"]
            changed.update({"played_at": time.time(), "duration_s": 1.0})
            Path(str(archived) + ".json").write_text(json.dumps(changed))
            with self.assertRaises(selected.SimulationError):
                selected.verify_queue(fixture.runtime(), fixture.manifest, "archived")


if __name__ == "__main__":
    unittest.main()
