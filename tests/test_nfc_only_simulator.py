import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import wave


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dev" / "simulate-nfc-only.py"
spec = importlib.util.spec_from_file_location("nfc_only_simulator", SCRIPT)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)


CARD_A = "04:A1:02:03"
CARD_B = "04:B1:02:03"
RECIPIENT_A = "test-a@example.invalid"
RECIPIENT_B = "test-b@example.invalid"
HEX = "a" * 64


def plan(profile="known_repeat"):
    shapes = {
        "known_repeat": (8, [
            {"at": 1, "type": "nfc-present", "uid": CARD_A},
            {"at": 2, "type": "nfc-repeat"},
            {"at": 3, "type": "nfc-removed"},
            {"at": 5, "type": "nfc-present", "uid": CARD_A},
            {"at": 6, "type": "nfc-removed"},
        ]),
        "unknown_hold": (7, [
            {"at": 1, "type": "nfc-unknown", "uid": CARD_A},
            {"at": 2, "type": "nfc-repeat"},
            {"at": 5, "type": "nfc-removed"},
        ]),
        "stale_selection": (36, [
            {"at": 1, "type": "nfc-present", "uid": CARD_A},
            {"at": 2, "type": "nfc-removed"},
        ]),
        "assigned_alternation": (6, [
            {"at": 1, "type": "nfc-present", "uid": CARD_A},
            {"at": 2, "type": "nfc-present", "uid": CARD_B},
            {"at": 3, "type": "nfc-present", "uid": CARD_A},
            {"at": 4, "type": "nfc-removed"},
        ]),
    }
    duration, events = shapes[profile]
    return {"profile": profile, "duration": duration, "events": events}


def authorization(profile="known_repeat", cues=None):
    mappings = []
    if profile != "unknown_hold":
        mappings.append({"uid": CARD_A, "recipient": RECIPIENT_A})
    recipients = [RECIPIENT_A]
    if profile == "assigned_alternation":
        mappings.append({"uid": CARD_B, "recipient": RECIPIENT_B})
        recipients.append(RECIPIENT_B)
    return {
        "version": 1, "transport": "wacli", "profile": profile,
        "contacts": {"sha256": HEX, "document_sha256": HEX, "mode": 0o600,
                     "uid": 0, "gid": 0},
        "settings": {"sha256": HEX, "document_sha256": HEX, "mode": 0o600,
                     "uid": 0, "gid": 0},
        "account_sha256": HEX,
        "recipients": recipients, "mappings": mappings, "cues": cues or [],
        "sync_mutable_paths": ["wacli.db"],
        "services": dict(simulator.SERVICE_BASELINE),
    }


class NfcScratchPlanTests(unittest.TestCase):
    def test_only_four_exact_nfc_profiles_are_accepted(self):
        for profile in simulator.PROFILES:
            self.assertEqual(simulator._static_plan(plan(profile))["profile"], profile)

        changed = plan()
        changed["events"][0]["at"] = 1.1
        with self.assertRaises(simulator.SimulationError):
            simulator._static_plan(changed)

        button = plan()
        button["events"][0] = {"at": 1, "type": "press"}
        with self.assertRaises(simulator.SimulationError):
            simulator._static_plan(button)

    def test_alternation_requires_two_distinct_cards_and_contacts(self):
        invalid = plan("assigned_alternation")
        invalid["events"][1]["uid"] = CARD_A
        with self.assertRaises(simulator.SimulationError):
            simulator._static_plan(invalid)

        value = authorization("assigned_alternation")
        value["mappings"][1]["recipient"] = RECIPIENT_A
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_authorization(value)

    def test_authorization_is_wacli_exact_and_private_identifiers_are_not_logged(self):
        value = authorization()
        self.assertIs(simulator.validate_authorization(value), value)
        value["transport"] = "cloud"
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_authorization(value)

    def test_main_default_only_checks_static_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan()))
            path.chmod(0o600)
            with mock.patch.object(simulator.receiver, "_production_paths") as production:
                self.assertEqual(simulator.main([str(path)]), 0)
                production.assert_not_called()


class NfcScratchCueTests(unittest.TestCase):
    def _wav(self, root):
        path = Path(root) / "cue.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 800)
        return path

    def test_regular_cue_requires_exact_hash_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._wav(directory)
            cue = {"state": "regular", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "duration_seconds": 0.1,
                   "audio": {"channels": 1, "sample_rate": 8000, "sample_width": 2,
                             "frames": 800, "peak_fraction": 0.0, "clipped_samples": 0}}
            simulator._safe_wav(path, cue)
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaises(simulator.SimulationError):
                simulator._safe_wav(path, cue)

    def test_cue_audio_proof_records_actual_peak_and_clipping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clipped.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes((32767).to_bytes(2, "little", signed=True) * 8)
            with path.open("rb") as handle:
                metrics = simulator._wav_metrics(handle)
            self.assertEqual(metrics["peak_fraction"], 1.0)
            self.assertEqual(metrics["clipped_samples"], 8)

    def test_declared_frames_require_complete_pcm_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._wav(directory)
            path.write_bytes(path.read_bytes()[:44 + 8 * 2])
            with path.open("rb") as handle, self.assertRaises(simulator.SimulationError):
                simulator._wav_metrics(handle)

    def test_cue_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self._wav(directory)
            linked = Path(directory) / "linked.wav"
            linked.symlink_to(target)
            cue = {"state": "regular", "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                   "duration_seconds": 0.1,
                   "audio": {"channels": 1, "sample_rate": 8000, "sample_width": 2,
                             "frames": 800, "peak_fraction": 0.0, "clipped_samples": 0}}
            with self.assertRaises((OSError, simulator.SimulationError)):
                simulator._safe_wav(linked, cue)

    def test_empty_or_absent_cue_is_explicit_and_presence_fails(self):
        cue = {"state": "missing", "sha256": None, "duration_seconds": None, "audio": None}
        simulator._safe_wav("", cue)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.wav"
            simulator._safe_wav(path, cue)
            path.write_bytes(b"not approved")
            with self.assertRaises(simulator.SimulationError):
                simulator._safe_wav(path, cue)

    def test_reachable_cues_must_match_exact_contact_and_unknown_paths(self):
        contacts = {RECIPIENT_A: {"card_clip": "/private/approved.wav"}}
        mappings = [{"uid": CARD_A, "recipient": RECIPIENT_A}]
        auth = authorization(cues=[])
        nfc = types.SimpleNamespace(UNKNOWN_TOKEN_WAV="/private/unknown.wav")
        with self.assertRaises(simulator.SimulationError):
            simulator._audit_cues(auth, contacts, nfc, mappings)


class NfcScratchBoundaryTests(unittest.TestCase):
    def test_full_known_repeat_uses_actual_standard_runtime_and_empty_receipt_store(self):
        code = r'''
import hashlib, importlib.util, json, os, pathlib, stat, subprocess, sys, threading, types, wave
gpiozero = types.ModuleType("gpiozero"); gpiozero.Button = object; gpiozero.LED = object
sys.modules["gpiozero"] = gpiozero
repo, base = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
os.environ["MSGBOX_TRANSPORT"] = "wacli"
os.environ["MSGBOX_PROMPT_DIR"] = str(repo / "sounds/guided-reply")
spec = importlib.util.spec_from_file_location("nfc_only_lifecycle", repo / "scripts/dev/simulate-nfc-only.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
production, namespace, outbound = base / "production", base / "namespace", base / "outbound"
for path in (production / "state", production / "settings", production / "sync", namespace, outbound):
    path.mkdir(parents=True, mode=0o700)
recipient, account, uid = "15551234567@s.whatsapp.net", "15557654321@s.whatsapp.net", "04:A1:02:03"
contacts = {"version": 2, "revision": 1, "default_recipient": recipient,
            "contacts": {recipient: {"label": "Test", "kind": "person", "receive_after": 0,
                                        "card_uids": [uid], "card_clip": ""}}, "listeners": {}}
settings = {"version": 1, "revision": 1, "timezone": "UTC", "recording_mode": "tap_review",
            "after_listening": "play_only", "max_recording_seconds": 30,
            "ringtone_id": "ding_dong", "master_volume_percent": 30,
            "arrival_signal": "silent", "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "nfc_confirmation_beep": True}
contacts_path, settings_path = production / "state/contacts.json", production / "settings/settings.json"
contacts_path.write_text(json.dumps(contacts, sort_keys=True)); settings_path.write_text(json.dumps(settings, sort_keys=True))
contacts_path.chmod(0o600); settings_path.chmod(0o600)
(production / "sync/wacli.db").write_bytes(b"db")
os.environ["WACLI_STORE_DIR"] = str((production / "sync").resolve())
def proof(path, document):
    metadata = path.lstat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_sha256": module._json_sha(document), "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid, "gid": metadata.st_gid}
account_hash = hashlib.sha256(json.dumps(
    [str((production / "sync").resolve()), account], separators=(",", ":"),
).encode()).hexdigest()
authorization = {"version": 1, "transport": "wacli", "profile": "known_repeat",
                 "contacts": proof(contacts_path, contacts), "settings": proof(settings_path, settings),
                 "account_sha256": account_hash, "recipients": [recipient],
                 "mappings": [{"uid": uid, "recipient": recipient}],
                 "cues": [{"role": "known", "path": "", "state": "missing",
                            "sha256": None, "duration_seconds": None, "audio": None}],
                 "sync_mutable_paths": ["wacli.db"], "services": dict(module.SERVICE_BASELINE)}
plan = {"profile": "known_repeat", "duration": 8,
        "events": [{"at": 1, "type": "nfc-present", "uid": uid},
                   {"at": 2, "type": "nfc-repeat"}, {"at": 3, "type": "nfc-removed"},
                   {"at": 5, "type": "nfc-present", "uid": uid},
                   {"at": 6, "type": "nfc-removed"}]}
class Guard:
    def __init__(self, *_args): self.failed = threading.Event(); self.error = None
    def verify(self): self.check_latch()
    def bind_namespace(self, _root): pass
    def check_latch(self):
        if self.failed.is_set(): raise module.SimulationError("guard failed")
    def fail(self, error=None): self.error = error; self.failed.set()
module.ProductionGuard = Guard
module.receiver._verify_storage_layout = lambda root, _production, outbound=None: pathlib.Path(root).resolve()
def fake_run(args, *positional, **kwargs):
    command = tuple(os.fspath(value) for value in args)
    if command[:1] == ("ffmpeg",):
        duration = float(command[7].split("duration=", 1)[1])
        with wave.open(command[-1], "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(b"\0\0" * round(8000 * duration))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if command[:1] in {("amixer",), ("aplay",)}:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if command[:3] == ("systemctl", "is-active", "messagebox-sync.service"):
        return subprocess.CompletedProcess(args, 0, stdout="active\n", stderr="")
    if command[:2] == ("systemctl", "is-active"):
        return subprocess.CompletedProcess(args, 3, stdout="inactive\n", stderr="")
    if command == ("/usr/local/bin/wacli", "--read-only", "--json", "auth", "status"):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(
            {"authenticated": True, "linked_jid": account}), stderr="")
    raise AssertionError(command)
module.subprocess.run = fake_run
module.execute_nfc(plan, authorization, namespace, outbound,
                   {"contacts": contacts_path, "settings": settings_path})
receipt = json.loads((outbound / "nfc-run-receipt.json").read_text())
assert receipt["status"] == "completed"
assert set(path.name for path in (outbound / "listened-receipts").iterdir()) == {
    "sent", "pending", "inflight", "seen"}
assert all(not list(path.iterdir()) for path in (outbound / "listened-receipts").iterdir())
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), directory],
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fresh_process_prepares_empty_namespace_from_exact_current_documents(self):
        code = r'''
import hashlib, importlib.util, json, os, pathlib, stat, sys, types
gpiozero = types.ModuleType("gpiozero"); gpiozero.Button = object; gpiozero.LED = object
sys.modules["gpiozero"] = gpiozero
repo, base = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("nfc_only_child", repo / "scripts/dev/simulate-nfc-only.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
production_root, namespace = base / "production", base / "namespace"
for path in (production_root / "queue", production_root / "outbox", production_root / "state",
             production_root / "settings", production_root / "sync", production_root / "runtime", namespace):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
recipient, uid = "15551234567@s.whatsapp.net", "04:A1:02:03"
contacts = {"version": 2, "revision": 1, "default_recipient": recipient,
            "contacts": {recipient: {"label": "Test", "kind": "person", "receive_after": 0,
                                        "card_uids": [uid], "card_clip": ""}}, "listeners": {}}
settings = {"version": 1, "revision": 1, "timezone": "UTC", "recording_mode": "tap_review",
            "after_listening": "play_only", "max_recording_seconds": 30,
            "ringtone_id": "ding_dong", "master_volume_percent": 30,
            "arrival_signal": "silent", "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "nfc_confirmation_beep": True}
contacts_path, settings_path = production_root / "state/contacts.json", production_root / "settings/settings.json"
contacts_path.write_text(json.dumps(contacts, sort_keys=True)); settings_path.write_text(json.dumps(settings, sort_keys=True))
contacts_path.chmod(0o600); settings_path.chmod(0o600)
(production_root / "sync/wacli.db").write_bytes(b"db")
def proof(path, document):
    metadata = path.lstat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_sha256": module._json_sha(document), "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid, "gid": metadata.st_gid}
authorization = {"version": 1, "transport": "wacli", "profile": "known_repeat",
                 "contacts": proof(contacts_path, contacts), "settings": proof(settings_path, settings),
                 "account_sha256": "a" * 64, "recipients": [recipient],
                 "mappings": [{"uid": uid, "recipient": recipient}],
                 "cues": [{"role": "known", "path": "", "state": "missing",
                            "sha256": None, "duration_seconds": None, "audio": None}],
                 "sync_mutable_paths": ["wacli.db"], "services": dict(module.SERVICE_BASELINE)}
plan = {"profile": "known_repeat", "duration": 8,
        "events": [{"at": 1, "type": "nfc-present", "uid": uid},
                   {"at": 2, "type": "nfc-repeat"}, {"at": 3, "type": "nfc-removed"},
                   {"at": 5, "type": "nfc-present", "uid": uid},
                   {"at": 6, "type": "nfc-removed"}]}
production = {"contacts": contacts_path, "settings": settings_path,
              "queue": production_root / "queue", "outbox": production_root / "outbox",
              "state": production_root / "state", "sync_store": production_root / "sync"}
module.receiver._verify_service_state = lambda: None
module._dashboard_inactive = lambda: None
module.receiver._verify_production_nfc_clear = lambda _production: None
prebind = module.ProductionGuard(production, module._production_manifest(authorization), authorization)
prebind.verify()
assert "messagebox.onboarding.whatsapp" not in sys.modules
class Guard:
    def __init__(self): self.bound = None; self.calls = 0
    def bind_namespace(self, root): self.bound = pathlib.Path(root)
    def verify(self): self.calls += 1
guard = Guard()
standard, runtime, _nfc, loaded = module._prepare_namespace(plan, authorization, namespace, production, guard)
assert standard == {"duration": 8, "events": plan["events"]}
assert loaded[recipient]["card_uids"] == [uid]
assert guard.bound == namespace and guard.calls == 1
assert (namespace / "queue").is_dir() and not list((namespace / "queue").iterdir())
assert (namespace / "runtime").is_dir() and not list((namespace / "runtime").iterdir())
assert (namespace / "state/contacts.json").read_bytes() == contacts_path.read_bytes()
assert (namespace / "settings/settings.json").read_bytes() == settings_path.read_bytes()
assert all(pathlib.Path(value[0]).parent.resolve() == (namespace / "runtime").resolve()
           for value in runtime.BEEPS.values())
assert "target" not in authorization and "source_timestamp" not in authorization
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), directory],
                env={**os.environ, "MSGBOX_TRANSPORT": "wacli"},
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_settings_store_contract_requires_exact_volume_without_recovery(self):
        from messagebox.settings import defaults, SettingsStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            document = defaults({"TZ": "UTC", "MSGBOX_SPEAKER_VOLUME": "30"})
            path.write_text(json.dumps(document))
            self.assertEqual(
                simulator._settings_document(SettingsStore(path))["master_volume_percent"], 30,
            )
            path.write_text("not-json")
            with self.assertRaises(simulator.SimulationError):
                simulator._settings_document(SettingsStore(path))

    def test_claim_check_uses_lstat_and_rejects_any_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claim.json"
            simulator._claim_absent(path)
            path.symlink_to(Path(directory) / "missing")
            with self.assertRaises(simulator.SimulationError):
                simulator._claim_absent(path)

    def test_subprocess_boundary_allows_only_audio_generation_playback_volume_and_readonly_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cue = root / "cue.wav"
            cue.write_bytes(b"x")
            calls = []

            def original(args, *positional, **kwargs):
                calls.append(tuple(args))
                return subprocess.CompletedProcess(args, 0)

            runtime = types.SimpleNamespace(
                WACLI_BIN="/usr/local/bin/wacli", SPEAKER_CARD="0",
                SPEAKER_CONTROL="Master", SPK_DEV="speaker",
                BEEPS={"nfc": (str(root / "beep.wav"), 440, 0.1, -1)},
            )
            guard = mock.Mock()
            guarded = simulator.AllowedSubprocess(
                original, runtime, [{"path": str(cue), "state": "regular"}], root, guard,
            )
            guarded(["amixer", "-q", "-c", "0", "sset", "Master", "30%", "unmute"])
            with mock.patch.object(simulator, "_safe_wav") as checked:
                guarded(["aplay", "-q", "-D", "speaker", str(cue)])
                checked.assert_called_once()
            guarded(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=0.1", "-filter:a", "volume=-1dB",
                     str(root / "beep.wav")])
            guarded([runtime.WACLI_BIN, "--read-only", "--json", "auth", "status"])
            guarded(["systemctl", "is-active", "messagebox-dash.service"])
            with self.assertRaises(simulator.SimulationError):
                guarded([runtime.WACLI_BIN, "send", "text", "--to", RECIPIENT_A])
            with self.assertRaises(simulator.SimulationError):
                guarded(["arecord", str(root / "recording.wav")])
            self.assertEqual(guard.verify.call_count, 3)

    def test_dashboard_must_be_exactly_inactive(self):
        with mock.patch.object(simulator.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["systemctl"], 3, stdout="inactive\n")):
            simulator._dashboard_inactive()
        for result in (
            subprocess.CompletedProcess(["systemctl"], 0, stdout="active\n"),
            subprocess.CompletedProcess(["systemctl"], 3, stdout="unknown\n"),
        ):
            with self.subTest(result=result), mock.patch.object(
                    simulator.subprocess, "run", return_value=result):
                with self.assertRaises(simulator.SimulationError):
                    simulator._dashboard_inactive()

    def test_path_audit_rejects_one_unbound_application_constant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = types.SimpleNamespace(**{name: root / name.lower() for name in (
                "QUEUE_DIR", "OUTBOX_DIR", "STATE_DIR", "CONTACTS_FILE", "SETTINGS_DIR",
                "SETTINGS_FILE", "RUNTIME_DIR")})
            runtime = types.SimpleNamespace(
                QUEUE_DIR=root / "queue", OUTBOX_DIR=root / "outbox", STATE_DIR=root / "state",
                CONTACTS_FILE=root / "contacts", TEMP_DIR=root / "temp",
                EVENTS_FILE=root / "events", LISTENED_DIR=root / "listened",
                nfc_announcement_store=types.SimpleNamespace(path=root / "announcement"),
                BEEPS={"nfc": (str(root / "beep.wav"), 440, 0.1, -1)},
            )
            nfc = types.SimpleNamespace(CONTACTS_FILE=root / "contacts",
                                        NFC_SELECTION_FILE=root / "selection",
                                        NFC_ANNOUNCEMENT_FILE=root / "announcement",
                                        NFC_ENROLLMENT_FILE=root / "enrollment",
                                        NFC_HEALTH_FILE=Path("/run/messagebox/nfc-health"))
            with self.assertRaises(simulator.SimulationError):
                simulator._verify_bindings(root, paths, runtime, nfc)

    def test_production_guard_latches_a_transient_failure(self):
        guard = simulator.ProductionGuard.__new__(simulator.ProductionGuard)
        guard.production = {"settings": Path("/unused"), "sync_store": Path("/unused")}
        guard.manifest = {}
        guard.receipt = {}
        guard.baseline = {}
        guard.cloud = {}
        guard.settings = {}
        guard.mutable = set()
        guard.sync = {}
        guard.runtime = object()
        guard.failed = __import__("threading").Event()
        guard.lock = __import__("threading").Lock()
        guard.error = None
        with mock.patch.object(simulator.receiver, "_verify_service_state", side_effect=simulator.SimulationError("boom")):
            with self.assertRaises(simulator.SimulationError):
                guard.verify()
        with mock.patch.object(simulator.receiver, "_verify_service_state"):
            with self.assertRaises(simulator.SimulationError):
                guard.verify()

    def test_trace_requires_all_events_and_no_route_or_capture_observation(self):
        test_plan = plan()
        traces = []
        for index, event in enumerate(test_plan["events"], 1):
            traces.append({"type": "nfc_observation", "input_event": event["type"],
                           "elapsed_seconds": event["at"] + 0.0001,
                           "handler_action": ("recognized" if index in {1, 2, 4} else None),
                           "handler_announce": False if index == 2 else True,
                           "handler_authorized_recipient": True,
                           "selection_authorized_recipient": True})
        traces.extend([
            {"type": "nfc_observation", "input_event": None,
             "handler_action": "removed", "elapsed_seconds": 3.7501},
            {"type": "nfc_observation", "input_event": None,
             "handler_action": "removed", "elapsed_seconds": 6.7501},
        ])
        traces.sort(key=lambda item: item.get("elapsed_seconds", 999))
        simulator._trace_assertions("known_repeat", test_plan, traces, 1)
        second_present = next(item for item in traces
                              if item.get("input_event") == "nfc-present"
                              and item.get("elapsed_seconds") > 4)
        second_present["handler_action"] = None
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions("known_repeat", test_plan, traces, 1)
        second_present["handler_action"] = "recognized"
        traces.append({"type": "route_observation"})
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions("known_repeat", test_plan, traces, 1)

    def test_unknown_and_stale_assertions_use_observed_state(self):
        unknown = [{
            "type": "nfc_observation", "input_event": event["type"],
            "handler_action": "unknown" if index == 0 else None,
            "handler_authorized_recipient": None, "selection_authorized_recipient": None,
            "unknown_marker_present": True, "announcement_present": index == 0,
            "reader_present": index < 2, "elapsed_seconds": event["at"],
        } for index, event in enumerate(plan("unknown_hold")["events"])]
        unknown.append({
            "type": "nfc_observation", "input_event": None, "handler_action": "removed",
            "handler_authorized_recipient": None, "selection_authorized_recipient": None,
            "unknown_marker_present": False, "announcement_present": False,
            "reader_present": False, "elapsed_seconds": 5.75,
        })
        simulator._trace_assertions("unknown_hold", plan("unknown_hold"), unknown, 1)
        without_persistence = [item for item in unknown if item.get("elapsed_seconds") != 2]
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions(
                "unknown_hold", plan("unknown_hold"), without_persistence, 1,
            )
        cleared_while_held = [dict(item) for item in unknown]
        cleared_while_held.insert(2, {
            "type": "nfc_observation", "input_event": None, "handler_action": None,
            "unknown_marker_present": False, "announcement_present": False,
            "reader_present": True, "elapsed_seconds": 3,
        })
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions(
                "unknown_hold", plan("unknown_hold"), cleared_while_held, 1,
            )
        cleared_during_grace = [dict(item) for item in unknown]
        cleared_during_grace.insert(-1, {
            "type": "nfc_observation", "input_event": None, "handler_action": None,
            "unknown_marker_present": False, "announcement_present": False,
            "reader_present": True, "elapsed_seconds": 5.4,
        })
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions(
                "unknown_hold", plan("unknown_hold"), cleared_during_grace, 1,
            )
        stale = [
            {"type": "nfc_observation", "input_event": event["type"],
             "handler_action": "selected" if index == 0 else None,
             "reader_card_sha256": "card-a" if index == 0 else None,
             "handler_recipient_sha256": "recipient-a" if index == 0 else None,
             "handler_authorized_recipient": True, "selection_authorized_recipient": True,
             "selection_present": True, "selection_within_ttl": index == 0,
             "reader_present": index == 0, "elapsed_seconds": event["at"]}
            for index, event in enumerate(plan("stale_selection")["events"])
        ]
        stale.extend([
            {"type": "nfc_observation", "input_event": None,
             "handler_action": "removed", "elapsed_seconds": 2.75},
            {"type": "nfc_observation", "input_event": None,
             "handler_action": None, "selection_present": True,
             "selection_within_ttl": False, "reader_present": False,
             "elapsed_seconds": 32.1},
        ])
        simulator._trace_assertions("stale_selection", plan("stale_selection"), stale, 2)
        simulator._trace_assertions(
            "stale_selection", plan("stale_selection"), stale, 2,
            expected_routes={"card-a": "recipient-a"},
        )
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions(
                "stale_selection", plan("stale_selection"), stale, 2,
                expected_routes={"card-a": "recipient-b"},
            )
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions("stale_selection", plan("stale_selection"), stale, 1)

    def test_assigned_trace_requires_exact_a_b_a_card_and_recipient_correspondence(self):
        test_plan = plan("assigned_alternation")
        traces = []
        cards = ["card-a", "card-b", "card-a"]
        recipients = ["recipient-a", "recipient-b", "recipient-a"]
        present = 0
        for event in test_plan["events"]:
            record = {"type": "nfc_observation", "input_event": event["type"],
                      "elapsed_seconds": event["at"] + 0.0001,
                      "handler_action": None, "handler_authorized_recipient": True,
                      "selection_authorized_recipient": True}
            if event["type"] == "nfc-present":
                record.update({"handler_action": "selected",
                               "reader_card_sha256": cards[present],
                               "handler_recipient_sha256": recipients[present]})
                present += 1
            traces.append(record)
        traces.append({"type": "nfc_observation", "input_event": None,
                       "handler_action": "removed", "elapsed_seconds": 4.7501})
        simulator._trace_assertions("assigned_alternation", test_plan, traces, 2)
        expected_routes = {"card-a": "recipient-a", "card-b": "recipient-b"}
        simulator._trace_assertions(
            "assigned_alternation", test_plan, traces, 2, expected_routes=expected_routes,
        )
        traces[2]["handler_recipient_sha256"] = "recipient-b"
        with self.assertRaises(simulator.SimulationError):
            simulator._trace_assertions("assigned_alternation", test_plan, traces, 2)

    def test_stale_selection_refuses_one_contact_before_cue_or_audio_work(self):
        auth = authorization("stale_selection")
        contacts = {RECIPIENT_A: {"card_uids": [CARD_A], "card_clip": ""}}
        nfc = types.SimpleNamespace(UNKNOWN_TOKEN_WAV="")
        with mock.patch.object(simulator, "_audit_cues") as cues:
            with self.assertRaises(simulator.SimulationError):
                simulator._validate_routes(plan("stale_selection"), auth, contacts, nfc)
            cues.assert_not_called()

    def test_run_receipt_contains_only_hashed_bindings_and_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outbound"
            auth = authorization()
            simulator._write_run_receipt(root, "failed", "known_repeat", plan(), auth,
                                         [], ["aplay"], [])
            output = json.loads((root / "nfc-run-receipt.json").read_text())
            self.assertEqual(output["status"], "failed")
            self.assertNotIn(RECIPIENT_A, json.dumps(output))
            self.assertNotIn(CARD_A, json.dumps(output))

    def test_nfc_only_outbound_namespace_must_contain_no_generated_work(self):
        from messagebox.listened_receipts import ReceiptStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("outbox", "recording-temp"):
                (root / name).mkdir()
            ReceiptStore(root / "listened-receipts")
            simulator._verify_no_generated_work(root)
            (root / "recording-temp" / "capture.wav").write_bytes(b"unexpected")
            with self.assertRaises(simulator.SimulationError):
                simulator._verify_no_generated_work(root)
            (root / "recording-temp" / "capture.wav").unlink()
            (root / "listened-receipts" / "pending" / "receipt.json").write_text("{}")
            with self.assertRaises(simulator.SimulationError):
                simulator._verify_no_generated_work(root)
            (root / "listened-receipts" / "pending" / "receipt.json").unlink()
            (root / "listened-receipts" / "seen").rmdir()
            (root / "listened-receipts" / "seen").symlink_to(
                root / "listened-receipts" / "pending", target_is_directory=True,
            )
            with self.assertRaises(simulator.SimulationError):
                simulator._verify_no_generated_work(root)
            (root / "listened-receipts" / "seen").unlink()
            (root / "listened-receipts" / "seen").mkdir()
            (root / "listened-receipts" / "extra").mkdir()
            with self.assertRaises(simulator.SimulationError):
                simulator._verify_no_generated_work(root)


class FrozenDriverTests(unittest.TestCase):
    def test_existing_helpers_remain_at_reviewed_hashes(self):
        expected = {
            "simulate-inputs.py": "d5458e236fd335f2ff8cc8d3a2183e12bcd82836be00a3f7804d8078ae86f9ac",
            "simulate-selected-message.py": "093a27bb20999f2aab44246e3bb787f2dcd6602059fad81107245b2215523f1a",
            "simulate-received-message.py": "5aa83f67c4c2c89ce7c69c0630b0330f5876d2edf6269bab9214bc92a4d23324",
        }
        for name, digest in expected.items():
            data = (ROOT / "scripts" / "dev" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
