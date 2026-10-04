import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from messagebox.contacts import ContactStore
from messagebox.nfc import Announcer, NfcRuntime
from messagebox.nfc_state import AnnouncementStore, EnrollmentStore, NfcRouter, SelectionStore

spec = importlib.util.spec_from_file_location(
    "input_simulator", Path(__file__).resolve().parents[1] / "scripts/dev/simulate-inputs.py"
)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)
gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


JID = "15551234567@s.whatsapp.net"
OTHER = "15551234568@s.whatsapp.net"
CARD = "04:A1:00:FF"
UNKNOWN = "04:FF:00:FF"


def plan(events, duration=5):
    return {"duration": duration, "events": events}


class InputSimulatorTests(unittest.TestCase):
    def test_validate_is_the_default_and_never_starts_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan([
                {"at": 1, "type": "press"}, {"at": 2, "type": "release"}
            ])))
            path.chmod(0o600)
            with mock.patch.object(simulator, "supervise") as supervise, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(simulator.main([str(path)]), 0)
                supervise.assert_not_called()
                self.assertEqual(simulator.main([str(path), "--send-generated"]), 2)
            path.chmod(0o644)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(simulator.main([str(path)]), 2)

    def test_plan_rejects_unbounded_or_unreleased_and_invalid_transitions(self):
        invalid = [
            plan([{"at": 1, "type": "press"}]),
            plan([{"at": 1, "type": "release"}]),
            plan([{"at": 1, "type": "nfc-repeat"}]),
            plan([{"at": 1, "type": "nfc-present", "uid": CARD}]),
            plan([{"at": 1, "type": "press"}, {"at": 1, "type": "release"}]),
            plan([{"at": 1, "type": "press"}, {"at": 2, "type": "release"}], float("nan")),
            plan([{"at": 1, "type": "press"}, {"at": 2, "type": "release"}], 121),
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(simulator.SimulationError):
                simulator.validate_plan(candidate)

    def test_real_nfc_debounce_consumed_selection_unknown_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contacts = ContactStore(root / "contacts.json")
            contacts.add_contact(JID, "Test A")
            contacts.add_contact(OTHER, "Test B")
            contacts.assign_card(JID, CARD)
            selection = SelectionStore(root / "selection.json")
            announcements = AnnouncementStore(root / "announcements.json")
            router = NfcRouter(contacts, selection, EnrollmentStore(root / "enrollment.json"), announcements)
            runtime = NfcRuntime(router, Announcer(announcements))
            clock = [0]
            health = mock.Mock()
            inputs = simulator.Inputs(simulator.validate_plan(plan([
                {"at": 1, "type": "nfc-present", "uid": CARD},
                {"at": 2, "type": "nfc-repeat"},
                {"at": 2.9, "type": "nfc-unknown", "uid": UNKNOWN},
                {"at": 4, "type": "nfc-removed"},
            ])), runtime, health, clock=lambda: clock[0])
            clock[0] = 1
            inputs.tick()
            self.assertEqual(router.active_contact()["jid"], JID)
            self.assertEqual(announcements.pending_action(), "selected")
            selection.claim()
            clock[0] = 2
            inputs.tick()
            self.assertIsNone(router.active_contact())  # Same held card never re-arms a claimed intent.
            clock[0] = 2.9
            inputs.tick()
            self.assertEqual(announcements.pending_action(), "unknown")
            self.assertIsNone(router.active_contact())
            clock[0] = 3.9
            inputs.tick()
            clock[0] = 4
            inputs.tick()
            self.assertEqual(runtime.uid, UNKNOWN)
            clock[0] = 5
            inputs.tick()
            self.assertIsNone(runtime.uid)
            self.assertEqual(announcements.pending_action(), "unknown")
            self.assertTrue(inputs.done)
            self.assertEqual(health.call_count, 2)

    def test_real_button_hold_classifier_uses_timeline_release(self):
        for release_at, expected in [(1.1, "play"), (2, "record")]:
            clock = [0]
            inputs = simulator.Inputs(plan([
                {"at": 1, "type": "press"}, {"at": release_at, "type": "release"}
            ]), mock.Mock(), mock.Mock(), clock=lambda: clock[0])
            clock[0] = 1
            inputs.tick()

            def advance(seconds):
                clock[0] += seconds
                inputs.tick()

            result = button_send.wait_for_hold_intent(
                lambda: inputs.is_pressed, 0.4, 0.005, started_at=1,
                monotonic=lambda: clock[0], sleeper=advance,
            )
            self.assertEqual(result, expected)

    def test_shared_dispatch_preserves_cloud_claim_and_guided_release_order(self):
        order = []
        with mock.patch.object(button_send, "transport_mode", return_value="cloud"), \
             mock.patch.object(button_send.cloud_claim, "consume_claim_press", return_value=True), \
             mock.patch.object(button_send, "log_event"), \
             mock.patch.object(button_send, "wait_for_stable_open") as released, \
             mock.patch.object(button_send, "caregiver_settings") as settings:
            self.assertTrue(button_send.handle_confirmed_press(1))
            released.assert_called_once()
            settings.assert_not_called()
        with mock.patch.object(button_send, "transport_mode", return_value="wacli"), \
             mock.patch.object(button_send, "caregiver_settings", return_value={"recording_mode": "tap_review"}), \
             mock.patch.object(button_send, "acknowledge_guided_press", side_effect=lambda *args: order.append("ack")), \
             mock.patch.object(button_send, "wait_for_stable_open", side_effect=lambda: order.append("released")), \
             mock.patch.object(button_send, "run_guided_once", side_effect=lambda settings: order.append("session")), \
             mock.patch.object(button_send, "refresh_led", side_effect=lambda **kwargs: order.append("lamp")):
            self.assertTrue(button_send.handle_confirmed_press(1))
            self.assertEqual(order, ["ack", "released", "session", "lamp"])

    def test_shared_dispatch_reports_failure_and_still_refreshes_lamp(self):
        with mock.patch.object(button_send, "transport_mode", return_value="wacli"), \
             mock.patch.object(button_send, "caregiver_settings", return_value={"recording_mode": "hold_release"}), \
             mock.patch.object(button_send, "record_and_send_legacy", side_effect=OSError), \
             mock.patch.object(button_send, "log"), mock.patch.object(button_send, "log_event"), \
             mock.patch.object(button_send, "beep") as beep, \
             mock.patch.object(button_send, "refresh_led") as refresh:
            self.assertFalse(button_send.handle_confirmed_press(1))
            beep.assert_called_once_with("fail")
            refresh.assert_called_once_with(force=True)

    def test_preflight_preserves_unknown_jobs_and_rejects_service_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "outbox"
            pending.mkdir()
            foreign = pending / "foreign.job"
            foreign.write_bytes(b"retained unknown job")
            runtime = types.SimpleNamespace(
                OUTBOX_DIR=pending, TEMP_DIR=root / "temp", LISTENED_DIR=root / "receipts",
                cloud_claim=types.SimpleNamespace(CLAIM_FILE=root / "claim"),
            )
            nfc = types.SimpleNamespace(**{key: root / key for key in (
                "NFC_ENROLLMENT_FILE", "NFC_SELECTION_FILE", "NFC_ANNOUNCEMENT_FILE", "NFC_HEALTH_FILE"
            )})
            with mock.patch.object(simulator, "verify_routes"), \
                 mock.patch.object(simulator.subprocess, "run", return_value=types.SimpleNamespace(returncode=3, stdout="inactive\n")) as services:
                with self.assertRaises(simulator.SimulationError):
                    simulator.preflight(runtime, nfc, {})
                self.assertEqual(foreign.read_bytes(), b"retained unknown job")
                self.assertEqual([call.args[0][-1] for call in services.call_args_list],
                                 ["messagebox-button.service", "messagebox-nfc.service", "messagebox-poller.service"])
            with mock.patch.object(simulator, "verify_routes"), \
                 mock.patch.object(simulator.subprocess, "run", return_value=types.SimpleNamespace(returncode=4, stdout="unknown\n")):
                with self.assertRaises(simulator.SimulationError):
                    simulator.preflight(runtime, nfc, {})

    def test_scratch_state_rebinds_only_new_outputs_and_preserves_household_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            household = root / "household"
            household.mkdir()
            outbox = household / "outbox"
            outbox.mkdir()
            retained = outbox / "unknown.job"
            retained.write_bytes(b"retained unknown job")
            scratch = root / "scratch"
            scratch.mkdir(mode=0o700)
            runtime = types.SimpleNamespace(
                OUTBOX_DIR=str(outbox), QUEUE_DIR=str(household / "queue"),
                STATE_DIR=str(household / "state"), TEMP_DIR=str(household / "temp"),
                LISTENED_DIR=str(household / "receipts"), CONTACTS_FILE="unchanged",
            )
            simulator.use_scratch_state(runtime, scratch)
            self.assertEqual(runtime.OUTBOX_DIR, str(scratch.resolve() / "outbox"))
            self.assertEqual(runtime.CONTACTS_FILE, "unchanged")
            self.assertEqual(runtime.QUEUE_DIR, str(household / "queue"))
            self.assertEqual(retained.read_bytes(), b"retained unknown job")
            (scratch / "evidence").write_text("retain")
            with self.assertRaises(simulator.SimulationError):
                simulator.use_scratch_state(runtime, scratch)

    def test_route_verification_rejects_changes_and_foreign_queue(self):
        from messagebox import runtime_paths
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contacts_path, settings_path = root / "contacts.json", root / "settings.json"
            contacts = ContactStore(contacts_path)
            contacts.add_contact(JID, "Test")
            contacts.add_contact(OTHER, "Household")
            settings_path.write_text("{}")
            runtime = types.SimpleNamespace(
                CONTACTS_FILE=contacts_path, QUEUE_DIR=root, transport_mode=lambda: "wacli",
                queued=lambda: [], queue_metadata=lambda path: {"chat": OTHER},
                inbound_audio_authorized=lambda metadata: True,
            )
            manifest = {"transport": "wacli", "recipients": [JID], "account_sha256": "account",
                        "contacts_sha256": hashlib.sha256(contacts_path.read_bytes()).hexdigest(),
                        "settings_sha256": hashlib.sha256(settings_path.read_bytes()).hexdigest()}
            with mock.patch.object(runtime_paths, "SETTINGS_FILE", settings_path), mock.patch.object(simulator, "account_hash", return_value="account"):
                simulator.verify_routes(runtime, manifest)
                runtime.queued = lambda: ["test.wav"]
                with self.assertRaises(simulator.SimulationError):
                    simulator.verify_routes(runtime, manifest)
                runtime.queued = lambda: []
                settings_path.write_text('{"changed":true}')
                with self.assertRaises(simulator.SimulationError):
                    simulator.verify_routes(runtime, manifest)

    @contextlib.contextmanager
    def mixed_queue_runtime(self, mode):
        from messagebox import cloud_runtime, runtime_paths
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            queue = root / "queue"
            queue.mkdir()
            contacts = ContactStore(root / "contacts.json")
            contacts.add_contact(JID, "Synthetic test")
            contacts.add_contact(OTHER, "Synthetic other")
            settings = root / "settings.json"
            settings.write_text("{}")
            for key, value in [("QUEUE_DIR", str(queue)), ("CONTACTS_FILE", str(contacts.path)),
                               ("EVENTS_FILE", str(root / "events.jsonl"))]:
                stack.enter_context(mock.patch.object(button_send, key, value))
            stack.enter_context(mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": mode}))
            stack.enter_context(mock.patch.object(runtime_paths, "SETTINGS_FILE", settings))
            stack.enter_context(mock.patch.object(simulator, "account_hash", return_value="synthetic-account"))
            now = 2_000_000_000
            cloud = cloud_runtime.CloudRuntime(client=mock.Mock(), state_path=root / "cloud-state.json",
                contacts_path=contacts.path, settings_path=settings, queue_dir=queue,
                clock=lambda: now, monotonic=lambda: 100, boot_id="synthetic-boot")
            cloud.state["snapshot"] = {"box_id": "synthetic-box-identifier", "server_time": now,
                "verified_at": now, "verified_mono": 100, "boot_id": "synthetic-boot",
                "entitlement": {"deliver": True}, "queue_hold": False, "retention_days": 30,
                "people": [{"id": "synthetic-person"}]}
            stack.enter_context(mock.patch.object(cloud_runtime, "CloudRuntime", return_value=cloud))
            manifest = {"transport": mode, "recipients": [JID], "account_sha256": "synthetic-account",
                "contacts_sha256": hashlib.sha256(contacts.path.read_bytes()).hexdigest(),
                "settings_sha256": hashlib.sha256(settings.read_bytes()).hexdigest()}

            def enqueue(name, jid, cloud=False, expired=False):
                path = queue / name
                path.write_bytes(b"synthetic retained audio")
                metadata = {"chat": jid, "msgid": "synthetic-message"}
                if cloud:
                    metadata.update(cloud=True, sender_id="synthetic-person",
                        cloud_message_id="synthetic-cloud-message", expires_at=now - 1 if expired else now + 3600)
                Path(str(path) + ".json").write_text(json.dumps(metadata))
                return path

            yield queue, manifest, enqueue

    def test_cloud_guard_preserves_six_legacy_items_and_unknown_metadata_using_real_gate(self):
        with self.mixed_queue_runtime("cloud") as (queue, manifest, enqueue):
            for index in range(6):
                enqueue(f"{index:04}-legacy.wav", JID if index < 3 else OTHER)
            enqueue("0006-expired-cloud.wav", OTHER, cloud=True, expired=True)
            missing = queue / "0007-missing.wav"
            missing.write_bytes(b"retained missing metadata")
            malformed = queue / "0008-malformed.wav"
            malformed.write_bytes(b"retained malformed metadata")
            Path(str(malformed) + ".json").write_text("{malformed")
            before = {path.name: path.read_bytes() for path in queue.iterdir()}
            simulator.verify_routes(button_send, manifest)
            records = []
            trace = simulator.InputTrace(button_send, manifest, emit=records.append)
            original = button_send.inbound_audio_authorized
            with mock.patch.object(button_send, "claim_inbox_file") as claim, \
                 mock.patch.object(button_send, "play_pending_listened"), \
                 mock.patch.object(button_send.subprocess, "run") as playback, \
                 simulator.validated_routing(button_send, manifest, trace):
                self.assertIsNone(button_send.claim_oldest())
                button_send.play_next_legacy()
                claim.assert_not_called()
                playback.assert_not_called()
            self.assertIs(button_send.inbound_audio_authorized, original)
            self.assertEqual({path.name: path.read_bytes() for path in queue.iterdir()}, before)
            self.assertTrue(records)
            self.assertTrue(all(record["production_playable"] is False for record in records))
            self.assertTrue(all(record["guard_outcome"] == "unavailable" for record in records))
            self.assertTrue(any(record["authorized_recipient"] is False for record in records))
            self.assertNotIn(OTHER, json.dumps(records))
            self.assertNotIn(JID, json.dumps(records))
            self.assertNotIn("legacy.wav", json.dumps(records))

    def test_mixed_mode_guard_restricts_actual_playable_routes_and_preserves_skipped_files(self):
        for mode in ("cloud", "wacli"):
            with self.subTest(mode=mode), self.mixed_queue_runtime(mode) as (queue, manifest, enqueue):
                skipped = enqueue("0000-other-mode.wav", OTHER, cloud=mode != "cloud")
                allowed = enqueue("0001-test.wav", JID, cloud=mode == "cloud")
                simulator.verify_routes(button_send, manifest)
                records = []
                trace = simulator.InputTrace(button_send, manifest, emit=records.append)
                with mock.patch.object(button_send, "claim_inbox_file", return_value=allowed) as claim, \
                     simulator.validated_routing(button_send, manifest, trace):
                    actual = button_send.claim_oldest()
                    self.assertEqual(actual["meta"]["chat"], JID)
                    claim.assert_called_once_with(button_send.QUEUE_DIR, allowed.name)
                    self.assertEqual(records[0]["guard_outcome"], "unavailable")
                    self.assertEqual(records[1]["guard_outcome"], "allowed")
                self.assertTrue(skipped.exists())
                self.assertTrue(Path(str(skipped) + ".json").exists())
                foreign = enqueue("0002-foreign.wav", OTHER, cloud=mode == "cloud")
                with self.assertRaises(simulator.SimulationError):
                    simulator.verify_routes(button_send, manifest)
                with simulator.validated_routing(button_send, manifest, trace), self.assertRaises(simulator.SimulationError):
                    button_send.inbound_audio_authorized(button_send.queue_metadata(foreign))
                self.assertEqual(records[-1]["guard_outcome"], "rejected")
                self.assertTrue(records[-1]["production_playable"])
                self.assertTrue(foreign.exists())

    def test_faulty_true_gate_cannot_claim_or_play_cross_mode_audio_even_for_test_recipient(self):
        for mode in ("cloud", "wacli"):
            with self.subTest(mode=mode), self.mixed_queue_runtime(mode) as (queue, manifest, enqueue):
                enqueue("0000-other-mode.wav", JID, cloud=mode != "cloud")
                before = {path.name: path.read_bytes() for path in queue.iterdir()}
                records = []
                trace = simulator.InputTrace(button_send, manifest, emit=records.append)
                with mock.patch.object(button_send, "inbound_audio_authorized", return_value=True), \
                     mock.patch.object(button_send, "claim_inbox_file") as claim, \
                     mock.patch.object(button_send, "play_pending_listened"), \
                     mock.patch.object(button_send.subprocess, "run") as playback:
                    with self.assertRaises(simulator.SimulationError):
                        simulator.verify_routes(button_send, manifest)
                    with simulator.validated_routing(button_send, manifest, trace):
                        with self.assertRaises(simulator.SimulationError):
                            button_send.claim_oldest()
                        with self.assertRaises(simulator.SimulationError):
                            button_send.play_next_legacy()
                    claim.assert_not_called()
                    playback.assert_not_called()
                self.assertEqual({path.name: path.read_bytes() for path in queue.iterdir()}, before)
                self.assertTrue(all(record["production_playable"] for record in records))
                self.assertTrue(all(record["authorized_recipient"] for record in records))
                self.assertTrue(all(record["transport_compatible"] is False for record in records))
                self.assertTrue(all(record["guard_outcome"] == "rejected" for record in records))

    def test_playable_missing_or_malformed_metadata_fails_before_claim_and_playback(self):
        for sidecar in (None, "{malformed", "[]", '{"chat":null}'):
            with self.subTest(sidecar=sidecar), self.mixed_queue_runtime("wacli") as (queue, manifest, enqueue):
                path = queue / "0000-unknown.wav"
                path.write_bytes(b"retained unknown audio")
                if sidecar is not None:
                    Path(str(path) + ".json").write_text(sidecar)
                before = {entry.name: entry.read_bytes() for entry in queue.iterdir()}
                with self.assertRaises(simulator.SimulationError):
                    simulator.verify_routes(button_send, manifest)
                records = []
                trace = simulator.InputTrace(button_send, manifest, emit=records.append)
                with mock.patch.object(button_send, "claim_inbox_file") as claim, \
                     mock.patch.object(button_send, "play_pending_listened"), \
                     mock.patch.object(button_send.subprocess, "run") as playback, \
                     simulator.validated_routing(button_send, manifest, trace):
                    with self.assertRaises(simulator.SimulationError):
                        button_send.claim_oldest()
                    with self.assertRaises(simulator.SimulationError):
                        button_send.play_next_legacy()
                    claim.assert_not_called()
                    playback.assert_not_called()
                self.assertEqual({entry.name: entry.read_bytes() for entry in queue.iterdir()}, before)
                self.assertTrue(all(record["production_playable"] for record in records))
                self.assertTrue(all(record["guard_outcome"] == "rejected" for record in records))

    def test_edge_started_debounce_observes_short_press_across_old_timeout_boundary(self):
        clock = [0]
        inputs = simulator.Inputs(plan([
            {"at": 0.05, "type": "press"}, {"at": 0.15, "type": "release"}
        ], 0.3), mock.Mock(), mock.Mock(), clock=lambda: clock[0])

        def advance(seconds):
            clock[0] += seconds
            inputs.tick()

        with mock.patch.object(button_send, "button", inputs, create=True), \
             mock.patch.object(button_send.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(button_send.time, "sleep", side_effect=advance), \
             mock.patch.object(button_send, "play_pending_nfc_announcement"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send, "handle_confirmed_press", return_value=True) as handler, \
             mock.patch.object(simulator, "verify_routes"):
            simulator.run_inputs(button_send, inputs, {})
        handler.assert_called_once()
        self.assertEqual(inputs.index, 2)

    def test_late_or_collapsed_events_and_incomplete_schedule_cannot_succeed(self):
        candidate = plan([{"at": 0.8, "type": "press"}, {"at": 1, "type": "release"}], 2)
        clock = [0]
        inputs = simulator.Inputs(candidate, mock.Mock(), mock.Mock(), clock=lambda: clock[0])
        clock[0] = 2.1
        with self.assertRaises(simulator.SimulationError):
            inputs.tick()
        self.assertEqual(inputs.index, 0)
        clock[0] = 0.9
        with self.assertRaises(simulator.SimulationError):
            inputs.tick()
        inputs.done = True
        with self.assertRaises(simulator.SimulationError):
            simulator.run_inputs(mock.Mock(), inputs, {})

    def test_execute_starts_timeline_after_slow_setup(self):
        from messagebox import nfc
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root, clock, observed = Path(directory), [0], []
            for name in ("button", "led", "outbox_store", "receipt_store"):
                stack.enter_context(mock.patch.object(button_send, name, None, create=True))
            for name in ("CONTACTS_FILE", "OUTBOX_DIR", "TEMP_DIR", "LISTENED_DIR"):
                stack.enter_context(mock.patch.object(button_send, name, str(root / name)))
            for name in ("NFC_SELECTION_FILE", "NFC_ANNOUNCEMENT_FILE", "NFC_HEALTH_FILE", "NFC_ENROLLMENT_FILE"):
                stack.enter_context(mock.patch.object(nfc, name, root / name))
            stack.enter_context(mock.patch.object(simulator, "preflight"))
            original = simulator.Inputs
            stack.enter_context(mock.patch.object(simulator, "Inputs", side_effect=lambda *args, **kwargs: original(*args, clock=lambda: clock[0], **kwargs)))

            def setup():
                clock[0] += 1

            for name in ("make_beeps", "validate_prompts", "apply_master_volume"):
                stack.enter_context(mock.patch.object(button_send, name, side_effect=setup))
            stack.enter_context(mock.patch.object(simulator.threading, "Thread"))
            stack.enter_context(mock.patch.object(simulator, "run_inputs", side_effect=lambda runtime, inputs, manifest: observed.append(inputs.started)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            simulator.execute(plan([{"at": 1, "type": "press"}, {"at": 1.2, "type": "release"}]), {"recipients": []}, False)
            self.assertEqual(observed, [3])

    def test_authorized_subset_blocks_household_default_before_capture_or_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contacts_path = root / "contacts.json"
            contacts = ContactStore(contacts_path)
            contacts.add_contact(OTHER, "Household default")
            contacts.add_contact(JID, "Authorized test")
            queue = root / "queue"
            queue.mkdir()
            manifest = {"recipients": [JID]}
            with mock.patch.object(button_send, "CONTACTS_FILE", str(contacts_path)), \
                 mock.patch.object(button_send, "QUEUE_DIR", str(queue)), \
                 mock.patch.object(button_send, "NFC_SELECTION_FILE", root / "selection"), \
                 mock.patch.object(button_send, "acknowledge_and_classify_legacy_press", return_value="record"), \
                 mock.patch.object(button_send, "presence") as presence, \
                 mock.patch.object(button_send.subprocess, "Popen") as capture:
                original = button_send.recording_recipient_context
                with simulator.validated_routing(button_send, manifest):
                    with self.assertRaises(simulator.SimulationError):
                        button_send.record_and_send_legacy({"max_recording_seconds": 1}, pressed_at=1)
                    with self.assertRaises(simulator.SimulationError):
                        button_send.run_guided_once({"max_recording_seconds": 1})
                    with self.assertRaises(simulator.SimulationError):
                        button_send.capture_guided_recording(OTHER)
                self.assertIs(button_send.recording_recipient_context, original)
                capture.assert_not_called()
                presence.assert_not_called()

    def test_mapped_household_card_is_rejected_before_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contacts_path = root / "contacts.json"
            contacts = ContactStore(contacts_path)
            contacts.add_contact(OTHER, "Household")
            contacts.assign_card(OTHER, CARD)
            with mock.patch.object(button_send, "CONTACTS_FILE", str(contacts_path)), \
                 mock.patch.object(simulator, "preflight"), \
                 mock.patch.object(button_send, "make_beeps") as setup:
                with self.assertRaises(simulator.SimulationError):
                    simulator.execute(plan([
                        {"at": 1, "type": "nfc-present", "uid": CARD},
                        {"at": 2, "type": "nfc-removed"}
                    ]), {"recipients": [JID]}, False)
                setup.assert_not_called()

    def test_cloud_account_uses_existing_identity_and_detects_drift_without_creation(self):
        from messagebox import cloud_device
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            path.write_text(json.dumps({"device_id": "a" * 16, "credential": "c" * 43}))
            runtime = types.SimpleNamespace(transport_mode=lambda: "cloud", CloudDeviceClient=cloud_device.CloudDeviceClient)
            with mock.patch.object(cloud_device, "IDENTITY_FILE", path), \
                 mock.patch.object(cloud_device.DeviceIdentityStore, "load_or_create") as create, \
                 mock.patch.object(cloud_device, "_OPENER") as network:
                first = simulator.account_hash(runtime)
                self.assertEqual(simulator.account_hash(runtime), first)
                path.write_text(json.dumps({"device_id": "b" * 16, "credential": "c" * 43}))
                self.assertNotEqual(simulator.account_hash(runtime), first)
                create.assert_not_called()
                network.assert_not_called()

    def test_wacli_account_reuses_read_only_identity_until_store_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session.db"
            session.write_bytes(b"session")
            runtime = types.SimpleNamespace(transport_mode=lambda: "wacli", WACLI_BIN="wacli")
            result = types.SimpleNamespace(returncode=0, stdout=json.dumps({"authenticated": True, "linked_jid": JID}))
            with mock.patch.dict(os.environ, {"WACLI_STORE_DIR": str(root)}), \
                 mock.patch.object(simulator.subprocess, "run", return_value=result) as status:
                first = simulator.account_hash(runtime)
                self.assertEqual(simulator.account_hash(runtime), first)
                status.assert_called_once_with(["wacli", "--read-only", "--json", "auth", "status"], capture_output=True, text=True, timeout=15)
                session.write_bytes(b"changed session")
                result.stdout = json.dumps({"authenticated": True, "linked_jid": OTHER})
                self.assertNotEqual(simulator.account_hash(runtime), first)
                self.assertEqual(status.call_count, 2)

    @contextlib.contextmanager
    def trace_runtime(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            contacts = ContactStore(root / "contacts.json")
            contacts.add_contact(OTHER, "Private household label")
            contacts.add_contact(JID, "Private test label")
            contacts.assign_card(JID, CARD)
            contacts.assign_card(JID, "04:B2:00:FF")
            selection = SelectionStore(root / "selection.json")
            announcements = AnnouncementStore(root / "announcements.json")
            router = NfcRouter(contacts, selection, EnrollmentStore(root / "enrollment.json"), announcements)
            reader = NfcRuntime(router, Announcer(announcements))
            queue = root / "queue"
            queue.mkdir()
            health = root / "health"
            health.touch()
            for key, value in [("CONTACTS_FILE", str(contacts.path)), ("NFC_SELECTION_FILE", str(selection.path)),
                               ("NFC_HEALTH_FILE", health), ("nfc_announcement_store", announcements),
                               ("QUEUE_DIR", str(queue))]:
                stack.enter_context(mock.patch.object(button_send, key, value))
            stack.enter_context(mock.patch.object(button_send, "log"))
            records = []
            trace = simulator.InputTrace(button_send, {"recipients": [JID]}, emit=records.append)
            yield reader, selection, announcements, trace, records

    def test_trace_reports_real_repeat_claim_alternation_removal_and_staleness_without_probes(self):
        with self.trace_runtime() as (reader, selection, announcements, trace, records):
            clock = [0]
            candidate = plan([
                {"at": 1, "type": "nfc-present", "uid": CARD},
                {"at": 1.2, "type": "nfc-repeat"},
                {"at": 3, "type": "nfc-present", "uid": "04:B2:00:FF"},
                {"at": 4, "type": "nfc-removed"},
            ], 6)
            inputs = simulator.Inputs(candidate, reader, mock.Mock(), clock=lambda: clock[0], trace=trace)
            with mock.patch.object(button_send, "current_recipient_context") as route_probe, \
                 mock.patch.object(reader.router, "active_contact") as status_probe:
                inputs.tick()
                clock[0] = 1
                inputs.tick()
                first = records[-1]
                self.assertEqual(first["handler_action"], "selected")
                self.assertTrue(first["handler_authorized_recipient"])
                self.assertTrue(first["selection_present"])
                clock[0] = 1.2
                inputs.tick()
                repeated = records[-1]
                self.assertEqual(repeated["input_event"], "nfc-repeat")
                self.assertFalse(repeated["handler_returned"])
                self.assertEqual(repeated["selection_card_sha256"], first["selection_card_sha256"])
                selection.claim()
                clock[0] = 2
                inputs.tick()
                consumed = records[-1]
                self.assertEqual(consumed["handler_action"], "refreshed")
                self.assertFalse(consumed["handler_announce"])
                self.assertFalse(consumed["selection_present"])
                self.assertTrue(consumed["claimed_marker_present"])
                self.assertTrue(selection.claimed_path.exists())
                clock[0] = 3
                inputs.tick()
                alternate = records[-1]
                self.assertEqual(alternate["handler_action"], "selected")
                self.assertNotEqual(alternate["reader_card_sha256"], first["reader_card_sha256"])
                self.assertEqual(alternate["handler_recipient_sha256"], first["handler_recipient_sha256"])
                clock[0] = 3.9
                inputs.tick()
                clock[0] = 4
                inputs.tick()
                removal_sample = records[-1]
                self.assertEqual(removal_sample["input_event"], "nfc-removed")
                self.assertFalse(removal_sample["handler_returned"])
                self.assertTrue(removal_sample["reader_present"])
                clock[0] = 4.81
                inputs.tick()
                removed = records[-1]
                self.assertEqual(removed["handler_action"], "removed")
                self.assertFalse(removed["reader_present"])
                self.assertTrue(removed["selection_present"])
                document = json.loads(selection.path.read_text())
                document["last_seen_at"] = time.time() - 31
                selection.path.write_text(json.dumps(document))
                clock[0] = 5
                inputs.tick()
                stale = records[-1]
                self.assertFalse(stale["selection_within_ttl"])
                self.assertGreater(stale["selection_age_seconds"], 30)
                self.assertTrue(selection.path.exists())  # Observation never clears stale state.
                route_probe.assert_not_called()
                status_probe.assert_not_called()
            encoded = json.dumps(records)
            for private in (CARD, "04:B2:00:FF", JID, OTHER, "Private household label", "Private test label"):
                self.assertNotIn(private, encoded)
            self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))

    def test_trace_uses_actual_authorized_unavailable_and_rejected_routing_returns(self):
        with self.trace_runtime() as (reader, selection, announcements, trace, records):
            reader.observe(CARD, 1)
            with mock.patch.object(button_send.subprocess, "Popen") as capture, \
                 mock.patch.object(button_send, "presence") as presence, \
                 simulator.validated_routing(button_send, {"recipients": [JID]}, trace):
                state, context = button_send.claim_fresh_card_intent()
                self.assertEqual(state, "claimed")
                allowed = records[-1]
                self.assertEqual(allowed["claim_state"], "claimed")
                self.assertEqual(allowed["guard_outcome"], "allowed")
                self.assertTrue(allowed["authorized_recipient"])
                self.assertTrue(allowed["via_card"])
                with self.assertRaises(simulator.SimulationError):
                    button_send.recording_recipient_context()  # Real household default after consumption.
                rejected = records[-1]
                self.assertEqual(rejected["guard_outcome"], "rejected")
                self.assertFalse(rejected["authorized_recipient"])
                reader.observe(UNKNOWN, 2)
                self.assertIsNone(button_send.recording_recipient_context())
                unavailable = records[-1]
                self.assertEqual(unavailable["guard_outcome"], "unavailable")
                self.assertFalse(unavailable["context_returned"])
                self.assertIsNone(unavailable["authorized_recipient"])
                with self.assertRaises(simulator.SimulationError):
                    button_send.capture_guided_recording(OTHER)
                self.assertEqual(records[-1]["source"], "capture_recipient_guard")
                self.assertEqual(records[-1]["guard_outcome"], "rejected")
                capture.assert_not_called()
                presence.assert_not_called()
            encoded = json.dumps(records)
            for private in (CARD, UNKNOWN, JID, OTHER):
                self.assertNotIn(private, encoded)

    def test_trace_observes_unknown_latch_after_announcement_and_grace_removal(self):
        with self.trace_runtime() as (reader, selection, announcements, trace, records):
            clock = [0]
            inputs = simulator.Inputs(plan([
                {"at": 1, "type": "nfc-unknown", "uid": UNKNOWN},
                {"at": 2, "type": "nfc-repeat"},
                {"at": 3.8, "type": "nfc-removed"},
            ], 5), reader, mock.Mock(), clock=lambda: clock[0], trace=trace)
            clock[0] = 1
            inputs.tick()
            unknown = records[-1]
            self.assertEqual(unknown["handler_action"], "unknown")
            self.assertTrue(unknown["unknown_marker_present"])
            self.assertFalse(unknown["selection_present"])
            announcements.take()  # Actual notification consumption must not be mistaken for absence.
            clock[0] = 2
            inputs.tick()
            repeated = records[-1]
            self.assertEqual(repeated["input_event"], "nfc-repeat")
            self.assertFalse(repeated["handler_returned"])
            self.assertTrue(repeated["unknown_marker_present"])
            self.assertFalse(repeated["announcement_present"])
            with simulator.validated_routing(button_send, {"recipients": [JID]}, trace):
                self.assertIsNone(button_send.recording_recipient_context())
                self.assertEqual(records[-1]["guard_outcome"], "unavailable")
                clock[0] = 3.7
                inputs.tick()
                clock[0] = 3.8
                inputs.tick()
                self.assertTrue(records[-1]["unknown_marker_present"])
                self.assertTrue(records[-1]["reader_present"])
                clock[0] = 4.6
                inputs.tick()
                removed = records[-1]
                self.assertEqual(removed["handler_action"], "removed")
                self.assertFalse(removed["unknown_marker_present"])
                self.assertFalse(removed["reader_present"])
                with self.assertRaises(simulator.SimulationError):
                    button_send.recording_recipient_context()
                self.assertEqual(records[-1]["guard_outcome"], "rejected")
            self.assertNotIn(UNKNOWN, json.dumps(records))
            self.assertNotIn(OTHER, json.dumps(records))

    def test_trace_and_preflight_preserve_preexisting_dangling_unknown_marker(self):
        with self.trace_runtime() as (reader, selection, announcements, trace, records):
            selection.unknown_path.symlink_to(selection.path.parent / "missing")
            self.assertTrue(simulator.trace_present(selection.unknown_path))
            inputs = simulator.Inputs(plan([{ "at": 1, "type": "nfc-unknown", "uid": UNKNOWN},
                                           {"at": 2, "type": "nfc-removed"}]), reader, mock.Mock(), trace=trace)
            inputs.tick()
            self.assertTrue(records[-1]["unknown_marker_present"])
            self.assertTrue(selection.unknown_path.is_symlink())
            root = selection.path.parent
            runtime = types.SimpleNamespace(OUTBOX_DIR=root / "outbox", TEMP_DIR=root / "temp",
                                            LISTENED_DIR=root / "receipts",
                                            cloud_claim=types.SimpleNamespace(CLAIM_FILE=root / "claim"))
            nfc = types.SimpleNamespace(NFC_SELECTION_FILE=selection.path,
                                       NFC_ENROLLMENT_FILE=root / "enrollment.json",
                                       NFC_ANNOUNCEMENT_FILE=announcements.path, NFC_HEALTH_FILE=root / "health")
            (root / "health").unlink()
            with mock.patch.object(simulator, "verify_routes"), \
                 mock.patch.object(simulator.subprocess, "run", return_value=types.SimpleNamespace(returncode=3, stdout="inactive\n")):
                with self.assertRaises(simulator.SimulationError):
                    simulator.preflight(runtime, nfc, {})
            self.assertTrue(selection.unknown_path.is_symlink())

    def test_trace_file_never_reads_fifo_symlink_target_or_oversized_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text(json.dumps({"jid": OTHER, "uid": UNKNOWN}))
            link = root / "linked-state"
            link.symlink_to(outside)
            pipe = root / "pipe-state"
            os.mkfifo(pipe)
            with mock.patch.object(simulator.os, "open") as opened:
                self.assertEqual(simulator.trace_file(link), (True, None))
                self.assertEqual(simulator.trace_file(pipe), (True, None))
                opened.assert_not_called()
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * 16385)
            self.assertEqual(simulator.trace_file(oversized), (True, None))
            with mock.patch.object(simulator.os, "fstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFIFO)), \
                 mock.patch.object(simulator.os, "read") as read:
                self.assertEqual(simulator.trace_file(outside), (True, None))
                read.assert_not_called()

    def test_trace_deduplicates_polling_and_marks_truncation_without_affecting_handlers(self):
        with self.trace_runtime() as (reader, selection, announcements, trace, records):
            clock = [0]
            inputs = simulator.Inputs(plan([{ "at": 1, "type": "nfc-present", "uid": CARD},
                                           {"at": 2, "type": "nfc-removed"}]),
                                      reader, mock.Mock(), clock=lambda: clock[0], trace=trace)
            inputs.tick()
            clock[0] = 0.2
            inputs.tick()
            self.assertEqual(len(records), 1)
            trace.count = 2048
            clock[0] = 1
            inputs.tick()
            self.assertTrue(selection.path.exists())
            self.assertTrue(trace.truncated)
            self.assertEqual(records[-1], {"type": "input_trace", "status": "truncated"})
            trace.route("recording_recipient_context", None)
            self.assertEqual(len(records), 2)

    @contextlib.contextmanager
    def send_runtime(self, mode):
        from messagebox.guided_reply import OutboxStore
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            store = OutboxStore(str(root / "outbox"), transport=mode)
            stack.enter_context(mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": mode}))
            stack.enter_context(mock.patch.object(button_send, "outbox_store", store))
            stack.enter_context(mock.patch.object(button_send, "OUTBOX_DIR", str(root / "outbox")))
            stack.enter_context(mock.patch.object(button_send, "TEMP_DIR", str(root)))
            stack.enter_context(mock.patch.object(button_send, "log_event"))
            stack.enter_context(mock.patch.object(button_send, "log"))
            stack.enter_context(mock.patch.object(button_send, "track_sent_for_receipts", return_value="synthetic-send-id"))
            stack.enter_context(mock.patch.object(simulator, "verify_routes"))
            yield root, store

    def test_real_guided_handled_return_requires_durable_nonfailed_progress(self):
        cases = ["conversion", "preflight", "rejected", "uncertain", "delivery_uncertain", "failed",
                 "accepted", "queued", "waiting_for_reply"]
        for case in cases:
            with self.subTest(case=case), self.send_runtime("cloud") as (root, store):
                source = root / "source.wav"
                source.write_bytes(b"synthetic audio")
                job = store.approve(str(source), JID, "standalone", 1, message_id="synthetic-local-job", account_scope="a" * 64)
                client = mock.Mock()
                client.send_voice.return_value = {"message_id": "synthetic-cloud-job", "state": case,
                                                 "expires_at": 1800000000, "server_time": 1700000000}
                if case == "rejected":
                    client.send_voice.side_effect = button_send.CloudSendRejected("rejected")
                if case == "uncertain":
                    client.send_voice.side_effect = button_send.CloudSendUncertain("uncertain")

                def convert(command, **kwargs):
                    if case != "conversion":
                        Path(command[-1]).write_bytes(b"OggSsynthetic")
                    return types.SimpleNamespace(returncode=1 if case == "conversion" else 0)

                manifest = {"transport": "cloud", "recipients": [JID], "account_sha256": "account"}
                with mock.patch.object(button_send.subprocess, "run", side_effect=convert), \
                     mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
                     mock.patch.object(button_send.cloud_runtime, "outbox_retry_until", return_value=1_800_604_800), \
                     mock.patch.object(button_send.cloud_runtime, "outbox_now", return_value=1_800_000_000), \
                     mock.patch.object(button_send.cloud_runtime, "recipient_id", side_effect=button_send.CloudRuntimeError("preflight") if case == "preflight" else None, return_value="synthetic-person"), \
                     mock.patch.object(button_send.CloudDeviceClient, "from_environment", return_value=client), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    if case in {"accepted", "queued", "waiting_for_reply"}:
                        outcomes = simulator.send_generated(button_send, manifest)
                        self.assertEqual(outcomes[0]["progress"], "cloud_" + case)
                        self.assertEqual(outcomes[0]["counterpart_delivery"], "unverified")
                    else:
                        with self.assertRaises(simulator.SimulationError):
                            simulator.send_generated(button_send, manifest)
                self.assertTrue(job.path.exists())
                self.assertTrue(job.audio_path.exists())
                self.assertNotIn(JID, output.getvalue())
                self.assertNotIn(job.message_id, output.getvalue())

    def test_real_legacy_full_sidecar_path_and_conversion_quarantine(self):
        for converted in (False, True):
            with self.subTest(converted=converted), self.send_runtime("wacli") as (root, store):
                name = "1700000000000-1.0.wav"
                path = Path(button_send.OUTBOX_DIR) / name
                path.write_bytes(b"synthetic audio")
                button_send.bind_legacy_job_recipient(str(path), JID)
                manifest = {"transport": "wacli", "recipients": [JID], "account_sha256": "account"}

                def send(command, **kwargs):
                    if command[0] == "ffmpeg":
                        if converted:
                            Path(command[-1]).write_bytes(b"OggSsynthetic")
                        return types.SimpleNamespace(returncode=0 if converted else 1)
                    return types.SimpleNamespace(returncode=0, stdout='{"sent":true,"id":"synthetic-send"}')

                with mock.patch.object(button_send.subprocess, "run", side_effect=send), contextlib.redirect_stdout(io.StringIO()):
                    if converted:
                        outcomes = simulator.send_generated(button_send, manifest)
                        self.assertEqual(outcomes[0]["progress"], "transport_completed")
                    else:
                        with self.assertRaises(simulator.SimulationError):
                            simulator.send_generated(button_send, manifest)
                        self.assertTrue((Path(button_send.OUTBOX_DIR) / ".bad" / name).exists())
                        self.assertTrue((Path(button_send.OUTBOX_DIR) / ".bad" / (name + ".json")).exists())
                self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "linux" or sys.platform == "darwin", "requires POSIX process groups")
    def test_deadline_stops_application_and_audio_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late-output"

            def hanging_child(*args):
                os.setsid()
                subprocess.Popen([sys.executable, "-c", "import time,pathlib,sys; time.sleep(1); pathlib.Path(sys.argv[1]).write_text('late')", str(marker)])
                time.sleep(10)

            with mock.patch.object(simulator, "child", hanging_child), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(simulator.supervise({}, {}, False, 0.2), 1)
            time.sleep(1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
