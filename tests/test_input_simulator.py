import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
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
            stack.enter_context(mock.patch.object(simulator, "Inputs", side_effect=lambda *args: original(*args, clock=lambda: clock[0])))

            def setup():
                clock[0] += 1

            for name in ("make_beeps", "validate_prompts", "apply_master_volume"):
                stack.enter_context(mock.patch.object(button_send, name, side_effect=setup))
            stack.enter_context(mock.patch.object(simulator.threading, "Thread"))
            stack.enter_context(mock.patch.object(simulator, "run_inputs", side_effect=lambda runtime, inputs, manifest: observed.append(inputs.started)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            simulator.execute(plan([{"at": 1, "type": "press"}, {"at": 1.2, "type": "release"}]), {}, False)
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
                job = store.approve(str(source), JID, "standalone", 1, message_id="synthetic-local-job")
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
