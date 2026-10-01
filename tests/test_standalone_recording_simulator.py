import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dev" / "simulate-standalone-recording.py"
spec = importlib.util.spec_from_file_location("standalone_recording_simulator", SCRIPT)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)

RECIPIENT = "15551234567@s.whatsapp.net"
HEX = "a" * 64


def plan(profile="tap_review_short"):
    shape = simulator.PROFILES[profile]
    return {
        "profile": profile,
        "duration": shape["duration"],
        "events": [{"at": at, "type": kind} for at, kind in shape["events"]],
    }


def authorization(profile="tap_review_short"):
    override = None
    if profile == "hold_release_short":
        override = {"recording_mode": "hold_release", "source_revision": 3}
    return {
        "version": 1,
        "transport": "wacli",
        "profile": profile,
        "contacts": {"sha256": HEX, "document_sha256": HEX, "mode": 0o600,
                     "uid": 1000, "gid": 1000},
        "settings": {"sha256": HEX, "document_sha256": HEX, "mode": 0o600,
                     "uid": 1000, "gid": 1000},
        "effective_settings_sha256": HEX,
        "settings_override": override,
        "account_sha256": HEX,
        "recipient": RECIPIENT,
        "route": "default",
        "assets": [{"path": "/opt/messagebox/test.wav", "sha256": HEX}],
        "sync_mutable_paths": ["wacli.db"],
        "services": dict(simulator.SERVICE_BASELINE),
        "runtime_identity": {"uid": 1000, "gid": 1000},
    }


class StandaloneContractTests(unittest.TestCase):
    def test_only_fixed_button_profiles_are_accepted(self):
        for profile in simulator.PROFILES:
            self.assertEqual(simulator._static_plan(plan(profile))["profile"], profile)
        changed = plan()
        changed["events"][0]["at"] = 1.1
        with self.assertRaises(simulator.SimulationError):
            simulator._static_plan(changed)
        card = plan()
        card["events"][0] = {"at": 1, "type": "nfc-present", "uid": "04:A1:02:03"}
        with self.assertRaises(simulator.SimulationError):
            simulator._static_plan(card)

    def test_authorization_has_no_inbound_message_or_receiver_receipt_fields(self):
        value = authorization()
        self.assertIs(simulator.validate_authorization(value), value)
        forbidden = {"target", "msgid", "sender_jid", "not_before", "source_timestamp",
                     "receiver_receipt", "max_age_seconds"}
        self.assertFalse(forbidden & set(value))
        for key, invalid in (("transport", "cloud"), ("route", "recent"),
                             ("sync_mutable_paths", ["foreign.db"])):
            changed = dict(value)
            changed[key] = invalid
            with self.subTest(key=key), self.assertRaises(simulator.SimulationError):
                simulator.validate_authorization(changed)

    def test_hold_release_requires_explicit_revision_bound_scratch_override(self):
        value = authorization("hold_release_short")
        self.assertIs(simulator.validate_authorization(value), value)
        value["settings_override"] = None
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_authorization(value)
        tap = authorization()
        tap["settings_override"] = {
            "recording_mode": "hold_release", "source_revision": 3,
        }
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_authorization(tap)

    def test_runtime_identity_derives_uid_and_gid_from_messagebox_account(self):
        account = types.SimpleNamespace(pw_uid=985, pw_gid=991)
        with mock.patch.object(simulator.pwd, "getpwnam", return_value=account), \
                mock.patch.object(simulator.os, "getuid", return_value=985), \
                mock.patch.object(simulator.os, "getgid", return_value=991):
            self.assertEqual(simulator._runtime_identity({"uid": 985, "gid": 991}),
                             {"uid": 985, "gid": 991})
            with self.assertRaises(simulator.SimulationError):
                simulator._runtime_identity({"uid": 985, "gid": 984})

    def test_default_validation_does_not_read_device_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan()))
            path.chmod(0o600)
            with mock.patch.object(simulator.receiver, "_production_paths") as production:
                self.assertEqual(simulator.main([str(path)]), 0)
                production.assert_not_called()


class StandaloneSettingsAndFileTests(unittest.TestCase):
    def test_hold_override_uses_real_settings_store_and_leaves_source_unchanged(self):
        from messagebox.settings import defaults, SettingsStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            scratch = root / "scratch.json"
            document = defaults({"TZ": "UTC", "MSGBOX_SPEAKER_VOLUME": "30"})
            document["revision"] = 3
            source.write_text(json.dumps(document, sort_keys=True))
            scratch.write_bytes(source.read_bytes())
            source_before = source.read_bytes()
            auth = authorization("hold_release_short")
            expected = dict(document)
            expected.update({"revision": 4, "recording_mode": "hold_release"})
            auth["effective_settings_sha256"] = simulator._json_sha(expected)
            effective = simulator._apply_settings(
                "hold_release_short", SettingsStore(scratch), document, auth,
            )
            self.assertEqual(effective, expected)
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(json.loads(scratch.read_text()), expected)

    def test_prompt_and_generated_audio_reject_symlinks_and_empty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.wav"
            target.write_bytes(b"audio")
            linked = root / "linked.wav"
            linked.symlink_to(target)
            with self.assertRaises((OSError, simulator.SimulationError)):
                simulator._regular_sha256(linked)
            empty = root / "empty.wav"
            empty.write_bytes(b"")
            with self.assertRaises(simulator.SimulationError):
                simulator._generated_regular(empty, root, ".wav")


class StandaloneBoundaryTests(unittest.TestCase):
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

    def test_production_guard_latches_transient_failure(self):
        guard = simulator.ProductionGuard.__new__(simulator.ProductionGuard)
        guard.production = {"settings": Path("/unused"), "sync_store": Path("/unused")}
        guard.manifest = {}
        guard.authorization = {"runtime_identity": {"uid": 1, "gid": 1}}
        guard.baseline = guard.cloud = guard.settings = guard.sync = {}
        guard.mutable = set()
        guard.runtime = object()
        guard.namespace = None
        guard.application_bound = False
        guard.failed = threading.Event()
        guard.lock = threading.Lock()
        guard.error = None
        with mock.patch.object(simulator, "_runtime_identity"), \
                mock.patch.object(simulator.receiver, "_verify_service_state",
                                  side_effect=simulator.SimulationError("changed")):
            with self.assertRaises(simulator.SimulationError):
                guard.verify()
        with mock.patch.object(simulator.receiver, "_verify_service_state"):
            with self.assertRaises(simulator.SimulationError):
                guard.verify()

    def test_audio_and_send_boundary_is_exact_single_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace, outbound = root / "namespace", root / "outbound"
            (namespace / "runtime").mkdir(parents=True)
            (outbound / "recording-temp").mkdir(parents=True)
            (outbound / "outbox/job.job").mkdir(parents=True)
            beep = namespace / "runtime/beep.wav"
            beep.write_bytes(b"beep")
            wav = outbound / "outbox/job.job/audio.wav"
            wav.write_bytes(b"wav")
            ogg = outbound / "recording-temp/job.ogg"
            ogg.write_bytes(b"ogg")
            calls = []

            def original(args, *_positional, **_kwargs):
                calls.append(tuple(args))
                return subprocess.CompletedProcess(args, 0)

            runtime = types.SimpleNamespace(
                WACLI_BIN="/usr/local/bin/wacli", SPEAKER_CARD="0",
                SPEAKER_CONTROL="Master", SPK_DEV="speaker", SEND_LOCK_WAIT="30s",
                BEEPS={"press": (str(beep), "880", "0.40", "12")},
                voice_send_command=lambda binary, path, recipient, wait: [
                    binary, "send", "voice", "--file", path, "--to", recipient,
                    "--lock-wait", wait, "--json",
                ],
            )
            auth = authorization()
            auth["assets"] = []
            guard = mock.Mock()
            boundary = simulator.AllowedRun(
                original, runtime, namespace, outbound, auth, guard, threading.local(),
            )
            boundary(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav),
                      "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
                      str(ogg)])
            send = runtime.voice_send_command(
                runtime.WACLI_BIN, str(ogg), RECIPIENT, runtime.SEND_LOCK_WAIT,
            )
            boundary(send)
            with self.assertRaises(simulator.SimulationError):
                boundary(send)
            with self.assertRaises(simulator.SimulationError):
                boundary([runtime.WACLI_BIN, "send", "text", "--to", RECIPIENT])
            self.assertEqual(boundary.send_count, 1)
            self.assertEqual(len(calls), 2)

    def test_failed_transport_invocation_cannot_be_retried_in_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "namespace/runtime").mkdir(parents=True)
            (root / "outbound/recording-temp").mkdir(parents=True)
            ogg = root / "outbound/recording-temp/generated.ogg"
            ogg.write_bytes(b"generated")
            runtime = types.SimpleNamespace(
                WACLI_BIN="/usr/local/bin/wacli", SPEAKER_CARD="0",
                SPEAKER_CONTROL="Master", SPK_DEV="speaker", SEND_LOCK_WAIT="30s",
                BEEPS={}, voice_send_command=lambda binary, path, recipient, wait: [
                    binary, "send", "voice", "--file", path, "--to", recipient,
                    "--lock-wait", wait, "--json",
                ],
            )
            calls = []

            def failed(args, *_positional, **_kwargs):
                calls.append(tuple(args))
                return subprocess.CompletedProcess(args, 1)

            auth = authorization()
            auth["assets"] = []
            boundary = simulator.AllowedRun(
                failed, runtime, root / "namespace", root / "outbound", auth, mock.Mock(),
                threading.local(),
            )
            command = runtime.voice_send_command(
                runtime.WACLI_BIN, str(ogg), RECIPIENT, runtime.SEND_LOCK_WAIT,
            )
            self.assertEqual(boundary(command).returncode, 1)
            with self.assertRaises(simulator.SimulationError):
                boundary(command)
            self.assertEqual(len(calls), 1)

    def test_nested_read_guard_cannot_count_a_refused_voice_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "namespace/runtime").mkdir(parents=True)
            (root / "outbound/recording-temp").mkdir(parents=True)
            ogg = root / "outbound/recording-temp/generated.ogg"
            ogg.write_bytes(b"generated")
            runtime = types.SimpleNamespace(
                WACLI_BIN="/usr/local/bin/wacli", SPEAKER_CARD="0",
                SPEAKER_CONTROL="Master", SPK_DEV="speaker", SEND_LOCK_WAIT="30s",
                BEEPS={}, voice_send_command=lambda binary, path, recipient, wait: [
                    binary, "send", "voice", "--file", path, "--to", recipient,
                    "--lock-wait", wait, "--json",
                ],
            )
            calls = []

            def original(args, *_positional, **_kwargs):
                calls.append(tuple(args))
                return subprocess.CompletedProcess(args, 0)

            class NestedGuard:
                boundary = None

                def verify(self):
                    self.boundary(
                        ["systemctl", "is-active", "messagebox-dash.service"],
                        capture_output=True, text=True,
                    )
                    raise simulator.SimulationError("production changed")

            auth = authorization()
            auth["assets"] = []
            guard = NestedGuard()
            boundary = simulator.AllowedRun(
                original, runtime, root / "namespace", root / "outbound", auth, guard,
                threading.local(),
            )
            guard.boundary = boundary
            command = runtime.voice_send_command(
                runtime.WACLI_BIN, str(ogg), RECIPIENT, runtime.SEND_LOCK_WAIT,
            )
            with self.assertRaises(simulator.SimulationError):
                boundary(command)
            self.assertEqual(boundary.send_count, 0)
            self.assertEqual(calls, [
                ("systemctl", "is-active", "messagebox-dash.service"),
            ])

    def test_validated_run_can_use_its_internal_popen_without_a_second_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "namespace/runtime").mkdir(parents=True)
            (root / "outbound").mkdir()
            runtime = types.SimpleNamespace(
                WACLI_BIN="/usr/local/bin/wacli", SPEAKER_CARD="0",
                SPEAKER_CONTROL="Master", SPK_DEV="speaker", SEND_LOCK_WAIT="30s",
                BEEPS={}, subprocess=simulator.subprocess,
            )
            calls = []

            class FakePopen:
                def __init__(self, args, *_positional, **_kwargs):
                    calls.append(tuple(args))
                    self.args = args
                    self.returncode = 0

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def communicate(self, input=None, timeout=None):
                    return "inactive\n", ""

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

                def kill(self):
                    self.returncode = -9

            auth = authorization()
            auth["assets"] = []
            with mock.patch.object(simulator.subprocess, "Popen", FakePopen):
                with simulator._guard_processes(
                        runtime, root / "namespace", root / "outbound", auth,
                        mock.Mock()) as (runs, children):
                    result = runs(
                        ["systemctl", "is-active", "messagebox-dash.service"],
                        capture_output=True, text=True,
                    )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(calls, [("systemctl", "is-active", "messagebox-dash.service")])
            self.assertEqual(children.calls, [])

    def test_output_requires_exactly_one_sent_receipt_and_no_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("outbox", "recording-temp"):
                (root / name).mkdir()
            from messagebox.listened_receipts import ReceiptStore
            receipts = ReceiptStore(root / "listened-receipts")
            receipts.track_sent("synthetic-message", RECIPIENT, flow="standalone")
            run = types.SimpleNamespace(send_count=1)
            child = types.SimpleNamespace(capture_count=1)
            simulator._verify_output(root, "tap_review_short", RECIPIENT, run, child)
            (root / "outbox/foreign.job").mkdir()
            with self.assertRaises(simulator.SimulationError):
                simulator._verify_output(root, "tap_review_short", RECIPIENT, run, child)

    def test_failure_receipt_reports_attempted_boundaries_and_durable_receipt_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = root / "listened-receipts/sent"
            sent.mkdir(parents=True)
            (sent / "tracked.json").write_text("{}")
            run = types.SimpleNamespace(send_count=1)
            child = types.SimpleNamespace(capture_count=1)
            evidence = simulator._boundary_evidence(
                root, "tap_review_short", (run, child),
            )
            self.assertEqual(evidence, {
                "profile": "tap_review_short", "capture_count": 1,
                "send_count": 1, "sent_receipt_count": 1,
            })
            auth = authorization()
            simulator._write_receipt(root, "failed", plan(), auth, evidence)
            receipt = json.loads((root / simulator.RECEIPT_NAME).read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["capture_count"], 1)
            self.assertEqual(receipt["send_count"], 1)
            self.assertEqual(receipt["sent_receipt_count"], 1)
            self.assertEqual(receipt["counterpart_delivery"], "unverified")

    def test_actual_standard_tap_review_lifecycle_uses_empty_scratch_and_one_send(self):
        code = r'''
import hashlib, importlib.util, json, os, pathlib, stat, subprocess, sys, threading, time, types, wave
gpiozero = types.ModuleType("gpiozero"); gpiozero.Button = object; gpiozero.LED = object
sys.modules["gpiozero"] = gpiozero
repo, base = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
os.environ["MSGBOX_TRANSPORT"] = "wacli"
os.environ["MSGBOX_PROMPT_DIR"] = str(repo / "sounds/guided-reply")
production, namespace, outbound = base / "production", base / "namespace", base / "outbound"
for path in (production / "queue", production / "outbox", production / "state",
             production / "settings", production / "sync", production / "runtime",
             namespace, outbound):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
(production / "state/seen.json").write_text("[]")
(production / "sync/wacli.db").write_bytes(b"db")
os.environ["WACLI_STORE_DIR"] = str((production / "sync").resolve())
spec = importlib.util.spec_from_file_location(
    "standalone_lifecycle", repo / "scripts/dev/simulate-standalone-recording.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
recipient, account = "15551234567@s.whatsapp.net", "15557654321@s.whatsapp.net"
contacts = {"version": 2, "revision": 1, "default_recipient": recipient,
            "contacts": {recipient: {"label": "Test", "kind": "person", "receive_after": 0,
                                      "card_uids": [], "card_clip": ""}}, "listeners": {}}
settings = {"version": 1, "revision": 3, "timezone": "UTC", "recording_mode": "tap_review",
            "after_listening": "play_only", "max_recording_seconds": 30,
            "ringtone_id": "ding_dong", "master_volume_percent": 30,
            "arrival_signal": "silent", "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "nfc_confirmation_beep": True}
contacts_path = production / "state/contacts.json"
settings_path = production / "settings/settings.json"
contacts_path.write_text(json.dumps(contacts, sort_keys=True)); contacts_path.chmod(0o600)
settings_path.write_text(json.dumps(settings, sort_keys=True)); settings_path.chmod(0o600)
def proof(path, document):
    metadata = path.lstat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_sha256": module._json_sha(document), "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid, "gid": metadata.st_gid}
assets = []
for path in sorted((repo / "sounds/guided-reply").glob("*.wav")):
    assets.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
account_hash = hashlib.sha256(json.dumps(
    [str((production / "sync").resolve()), account], separators=(",", ":"),
).encode()).hexdigest()
authorization = {"version": 1, "transport": "wacli", "profile": "tap_review_short",
    "contacts": proof(contacts_path, contacts), "settings": proof(settings_path, settings),
    "effective_settings_sha256": module._json_sha(settings), "settings_override": None,
    "account_sha256": account_hash, "recipient": recipient, "route": "default",
    "assets": assets, "sync_mutable_paths": ["wacli.db"],
    "services": dict(module.SERVICE_BASELINE),
    "runtime_identity": {"uid": os.getuid(), "gid": os.getgid()}}
plan = {"profile": "tap_review_short", "duration": 15,
        "events": [{"at": at, "type": kind}
                   for at, kind in module.PROFILES["tap_review_short"]["events"]]}
production_paths = {"queue": production / "queue", "outbox": production / "outbox",
    "state": production / "state", "seen": production / "state/seen.json",
    "contacts": contacts_path, "settings": settings_path, "sync_store": production / "sync",
    "nfc_enrollment": production / "runtime/nfc-enrollment.json",
    "nfc_selection": production / "runtime/nfc-selection.json",
    "nfc_selection_claimed": production / "runtime/.nfc-selection.json.claimed",
    "nfc_unknown": production / "runtime/.nfc-selection.json.unknown",
    "nfc_announcement": production / "runtime/nfc-announcement.json",
    "nfc_announcement_claimed": production / "runtime/.nfc-announcement.json.claimed",
    "nfc_announcement_acknowledged": production / "runtime/.nfc-announcement.json.acknowledged",
    "nfc_health": production / "runtime/nfc-health"}
baseline = {path: path.read_bytes() for path in (contacts_path, settings_path, production / "state/seen.json", production / "sync/wacli.db")}
class Guard:
    def __init__(self, *_args): self.failed = threading.Event(); self.error = None; self.queue = None
    def bind_namespace(self, root): self.queue = module.receiver._tree(pathlib.Path(root) / "queue")
    def verify(self):
        if self.failed.is_set(): raise module.SimulationError("guard failed")
        if self.queue is not None and module.receiver._tree(namespace / "queue") != self.queue:
            self.fail(module.SimulationError("scratch queue changed")); raise self.error
    def fail(self, error=None): self.error = error; self.failed.set()
module.ProductionGuard = Guard
module._runtime_identity = lambda expected=None: {"uid": os.getuid(), "gid": os.getgid()}
module.receiver._verify_storage_layout = lambda root, _production, outbound=None: pathlib.Path(root).resolve()
class Process:
    def __init__(self, command):
        self.command = tuple(command); self.done = False; self.stdout = None; self.writer = None
        if self.command[:1] == ("arecord",) and self.command[5] == "raw":
            read_fd, write_fd = os.pipe(); self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.writer = os.fdopen(write_fd, "wb", buffering=0)
            def write_voice():
                try:
                    for _ in range(24):
                        if self.done: break
                        self.writer.write((12000).to_bytes(2, "little", signed=True) * 320)
                        time.sleep(0.01)
                except (BrokenPipeError, ValueError): pass
            threading.Thread(target=write_voice, daemon=True).start()
    def poll(self): return 0 if self.done else None
    def send_signal(self, _signal): self.done = True
    def communicate(self, timeout=None):
        self.done = True
        if self.writer is not None:
            try: self.writer.close()
            except OSError: pass
        return b"", b""
    def terminate(self): self.done = True
    def kill(self): self.done = True
    def wait(self, timeout=None): self.done = True; return 0
def fake_popen(args, *positional, **kwargs): return Process(args)
send_calls = []
def fake_run(args, *positional, **kwargs):
    command = tuple(os.fspath(value) for value in args)
    if command[:1] == ("ffmpeg",) and command[4:6] == ("-f", "lavfi"):
        duration = float(command[7].split("duration=", 1)[1])
        with wave.open(command[-1], "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(b"\0\0" * round(8000 * duration))
        return subprocess.CompletedProcess(args, 0, "", "")
    if command[:1] == ("ffmpeg",) and command[4] == "-i":
        pathlib.Path(command[-1]).write_bytes(b"synthetic-ogg")
        return subprocess.CompletedProcess(args, 0, "", "")
    if command[:1] in {("amixer",), ("aplay",)}:
        return subprocess.CompletedProcess(args, 0, "", "")
    if command[:3] == ("systemctl", "is-active", "messagebox-sync.service"):
        return subprocess.CompletedProcess(args, 0, "active\n", "")
    if command[:2] == ("systemctl", "is-active"):
        return subprocess.CompletedProcess(args, 3, "inactive\n", "")
    if command == ("/usr/local/bin/wacli", "--read-only", "--json", "auth", "status"):
        return subprocess.CompletedProcess(args, 0, json.dumps({"authenticated": True, "linked_jid": account}), "")
    if command[:3] == ("/usr/local/bin/wacli", "send", "voice"):
        send_calls.append(command)
        return subprocess.CompletedProcess(args, 0, '{"sent":true,"id":"synthetic-message"}', "")
    raise AssertionError(command)
module.subprocess.run = fake_run; module.subprocess.Popen = fake_popen
module.execute_standalone(plan, authorization, namespace, outbound, production_paths)
receipt = json.loads((outbound / module.RECEIPT_NAME).read_text())
assert receipt["status"] == "completed" and receipt["capture_count"] == 1 and receipt["send_count"] == 1
assert receipt["sent_receipt_count"] == 1 and receipt["counterpart_delivery"] == "unverified"
assert len(send_calls) == 1
assert all(path.read_bytes() == content for path, content in baseline.items())
assert not list((production / "queue").iterdir()) and not list((production / "outbox").iterdir())
assert {path.name for path in (namespace / "queue").iterdir()} == {".played.lock"}
assert not list((outbound / "outbox").iterdir()) and not list((outbound / "recording-temp").iterdir())
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), directory],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_real_guard_baseline_includes_normal_history_lock_before_default_route(self):
        code = r'''
import hashlib, importlib.util, json, os, pathlib, stat, sys, types
gpiozero = types.ModuleType("gpiozero"); gpiozero.Button = object; gpiozero.LED = object
sys.modules["gpiozero"] = gpiozero
repo, base = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
os.environ["MSGBOX_TRANSPORT"] = "wacli"
os.environ["MSGBOX_PROMPT_DIR"] = str(repo / "sounds/guided-reply")
spec = importlib.util.spec_from_file_location(
    "standalone_guard_lock", repo / "scripts/dev/simulate-standalone-recording.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
production_root, namespace = base / "production", base / "namespace"
for path in (production_root / "queue", production_root / "outbox", production_root / "state",
             production_root / "settings", production_root / "sync", production_root / "runtime",
             namespace):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
recipient = "15551234567@s.whatsapp.net"
contacts = {"version": 2, "revision": 1, "default_recipient": recipient,
            "contacts": {recipient: {"label": "Test", "kind": "person", "receive_after": 0,
                                      "card_uids": [], "card_clip": ""}}, "listeners": {}}
settings = {"version": 1, "revision": 3, "timezone": "UTC", "recording_mode": "tap_review",
            "after_listening": "play_only", "max_recording_seconds": 30,
            "ringtone_id": "ding_dong", "master_volume_percent": 30,
            "arrival_signal": "silent", "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "nfc_confirmation_beep": True}
contacts_path, settings_path = production_root / "state/contacts.json", production_root / "settings/settings.json"
contacts_path.write_text(json.dumps(contacts, sort_keys=True)); contacts_path.chmod(0o600)
settings_path.write_text(json.dumps(settings, sort_keys=True)); settings_path.chmod(0o600)
(production_root / "state/seen.json").write_text("[]")
(production_root / "sync/wacli.db").write_bytes(b"db")
def proof(path, document):
    metadata = path.lstat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_sha256": module._json_sha(document), "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid, "gid": metadata.st_gid}
assets = [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
          for path in sorted((repo / "sounds/guided-reply").glob("*.wav"))]
authorization = {"version": 1, "transport": "wacli", "profile": "tap_review_short",
    "contacts": proof(contacts_path, contacts), "settings": proof(settings_path, settings),
    "effective_settings_sha256": module._json_sha(settings), "settings_override": None,
    "account_sha256": "a" * 64, "recipient": recipient, "route": "default", "assets": assets,
    "sync_mutable_paths": ["wacli.db"], "services": dict(module.SERVICE_BASELINE),
    "runtime_identity": {"uid": os.getuid(), "gid": os.getgid()}}
production = {"queue": production_root / "queue", "outbox": production_root / "outbox",
    "state": production_root / "state", "seen": production_root / "state/seen.json",
    "contacts": contacts_path, "settings": settings_path, "sync_store": production_root / "sync",
    "nfc_enrollment": production_root / "runtime/nfc-enrollment.json",
    "nfc_selection": production_root / "runtime/nfc-selection.json",
    "nfc_selection_claimed": production_root / "runtime/.nfc-selection.json.claimed",
    "nfc_unknown": production_root / "runtime/.nfc-selection.json.unknown",
    "nfc_announcement": production_root / "runtime/nfc-announcement.json",
    "nfc_announcement_claimed": production_root / "runtime/.nfc-announcement.json.claimed",
    "nfc_announcement_acknowledged": production_root / "runtime/.nfc-announcement.json.acknowledged",
    "nfc_health": production_root / "runtime/nfc-health"}
original_tree = module.receiver._tree
module.receiver._tree = lambda path, *args, **kwargs: (
    {"exists": False, "entries": []} if os.fspath(path) == "/var/lib/messagebox-cloud"
    else original_tree(path, *args, **kwargs))
module._runtime_identity = lambda expected=None: {"uid": os.getuid(), "gid": os.getgid()}
module.receiver._verify_service_state = lambda: None
module._dashboard_inactive = lambda: None
module.receiver._verify_production_nfc_clear = lambda _production: None
module.receiver._actual_transport_mode = lambda: "wacli"
module.receiver._verify_live_binding = lambda *_args: None
manifest = {"transport": "wacli", "contacts_sha256": authorization["contacts"]["sha256"],
            "settings_sha256": authorization["settings"]["sha256"],
            "account_sha256": authorization["account_sha256"], "recipients": [recipient]}
guard = module.ProductionGuard(production, manifest, authorization)
runtime, _nfc, _settings = module._prepare_namespace(
    {"profile": "tap_review_short"}, authorization, namespace, production, guard)
context = runtime.recording_recipient_context()
assert context["contact"]["jid"] == recipient and context["via_card"] is False
guard.verify()
lock = namespace / "queue/.played.lock"; metadata = lock.lstat()
assert stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600
assert metadata.st_size == 0 and {path.name for path in lock.parent.iterdir()} == {".played.lock"}
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), directory],
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
