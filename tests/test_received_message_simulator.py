import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock
import wave


spec = importlib.util.spec_from_file_location(
    "received_message_simulator",
    Path(__file__).resolve().parents[1] / "scripts/dev/simulate-received-message.py",
)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)

JID = "15551234567@s.whatsapp.net"
OLD_ID = "already-seen"
TARGET_ID = "fresh-target"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contact_document():
    return {
        "version": 2,
        "revision": 1,
        "default_recipient": JID,
        "contacts": {
            JID: {
                "label": "Approved tester",
                "kind": "person",
                "receive_after": 0,
                "card_uids": [],
                "card_clip": "",
            },
        },
        "listeners": {},
    }


def write_wav(path):
    with wave.open(os.fspath(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 80)


class ReceiveFixture:
    def __init__(self, parent):
        self.parent = Path(parent)
        self.production = {}
        for name in ("queue", "outbox", "state", "sync_store"):
            path = self.parent / ("production-" + name)
            path.mkdir(mode=0o700)
            self.production[name] = path
        self.contacts = self.production["state"] / "contacts.json"
        self.settings = self.parent / "production-settings.json"
        self.seen = self.production["state"] / "seen.json"
        self.contacts.write_text(json.dumps(contact_document()))
        self.settings.write_text(json.dumps({
            "recording_mode": "tap_review", "after_listening": "invite_reply",
        }))
        self.seen.write_text(json.dumps([OLD_ID]))
        (self.production["sync_store"] / "wacli.db").write_bytes(b"database")
        self.production.update({
            "seen": self.seen, "contacts": self.contacts, "settings": self.settings,
        })
        runtime = self.parent / "production-runtime"
        self.production.update({
            "nfc_enrollment": runtime / "nfc-enrollment.json",
            "nfc_selection": runtime / "nfc-selection.json",
            "nfc_selection_claimed": runtime / ".nfc-selection.json.claimed",
            "nfc_unknown": runtime / ".nfc-selection.json.unknown",
            "nfc_announcement": runtime / "nfc-announcement.json",
            "nfc_announcement_claimed": runtime / ".nfc-announcement.json.claimed",
            "nfc_announcement_acknowledged": runtime / ".nfc-announcement.json.acknowledged",
            "nfc_health": runtime / "nfc-health",
        })
        self.receiver_root = self.parent / "receiver"
        self.receiver_root.mkdir(mode=0o700)
        self.output = self.parent / "candidate.json"
        self.now = time.time()
        self.authorization = receiver.validate_receive_authorization({
            "version": 1,
            "transport": "wacli",
            "recipient": JID,
            "sender_jid": JID,
            "not_before": self.now - 5,
            "max_age_seconds": 60,
            "contacts_sha256": digest(self.contacts),
            "settings_sha256": digest(self.settings),
            "account_sha256": "a" * 64,
            "sync_mutable_paths": ["wacli.db", "wacli.db-shm", "wacli.db-wal"],
        })
        self.message = {
            "MsgID": TARGET_ID,
            "ChatJID": JID,
            "SenderJID": JID,
            "Timestamp": self.now,
            "MediaType": "audio",
            "FromMe": False,
        }

    def paths(self, root=None):
        root = self.receiver_root if root is None else Path(root)
        return types.SimpleNamespace(
            QUEUE_DIR=root / "queue",
            OUTBOX_DIR=root / "unused-outbox",
            STATE_DIR=root / "state",
            CONTACTS_FILE=root / "state" / "contacts.json",
            SETTINGS_DIR=root / "settings",
            SETTINGS_FILE=root / "settings" / "settings.json",
            RUNTIME_DIR=root / "runtime",
            NFC_SELECTION_FILE=root / "runtime" / "nfc-selection.json",
            NFC_ENROLLMENT_FILE=root / "runtime" / "nfc-enrollment.json",
            NFC_ANNOUNCEMENT_FILE=root / "runtime" / "nfc-announcement.json",
            NFC_HEALTH_FILE=root / "runtime" / "nfc-health",
        )

    @contextlib.contextmanager
    def production_receiver(self, messages=None, root=None):
        from messagebox import voicepoll

        paths = self.paths(root)
        old = (voicepoll.QUEUE_DIR, voicepoll.STATE_FILE, voicepoll.EVENTS_FILE,
               voicepoll._last_queue_ms)
        voicepoll.QUEUE_DIR = os.fspath(paths.QUEUE_DIR)
        voicepoll.STATE_FILE = os.fspath(paths.STATE_DIR / "seen.json")
        voicepoll.EVENTS_FILE = os.fspath(paths.STATE_DIR / "events.jsonl")
        voicepoll._last_queue_ms = 0
        listing = list(messages if messages is not None else [self.message])

        def wacli(*args):
            if args[1:3] == ("messages", "list"):
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    "data": {"messages": listing},
                }), "")
            if args[1:3] == ("media", "download"):
                output = Path(args[args.index("--output") + 1])
                write_wav(output)
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    "success": True, "data": {"path": os.fspath(output)},
                }), "")
            raise AssertionError(args)

        def transcode(command, **_kwargs):
            self.assert_ffmpeg(command)
            write_wav(command[-1])
            return subprocess.CompletedProcess(command, 0, "", "")

        try:
            with mock.patch.object(receiver, "bind_scratch_paths", return_value=paths), \
                    mock.patch.object(receiver, "_verify_service_state"), \
                    mock.patch.object(receiver, "_account_hash", return_value="a" * 64), \
                    mock.patch.object(voicepoll, "wacli", side_effect=wacli), \
                    mock.patch.object(voicepoll.subprocess, "run", side_effect=transcode):
                yield paths
        finally:
            voicepoll.QUEUE_DIR, voicepoll.STATE_FILE, voicepoll.EVENTS_FILE, \
                voicepoll._last_queue_ms = old

    @staticmethod
    def assert_ffmpeg(command):
        if command[0] != "ffmpeg":
            raise AssertionError(command)


class ReceivedMessageSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"})
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def test_authorization_is_exact_wacli_and_narrowly_mutable(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            for key, value in (
                    ("transport", "cloud"),
                    ("max_age_seconds", 601),
                    ("sync_mutable_paths", ["wacli.db", "foreign"]),
                    ("account_sha256", "bad")):
                invalid = dict(fixture.authorization)
                invalid[key] = value
                with self.subTest(key=key), self.assertRaises(receiver.SimulationError):
                    receiver.validate_receive_authorization(invalid)

    def test_binding_refuses_preimport_and_storage_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "receiver"
            root.mkdir(mode=0o700)
            with self.assertRaises(receiver.SimulationError):
                receiver.bind_scratch_paths(root, loaded={"messagebox.voicepoll": object()})
            production = {
                "queue": root / "queue", "outbox": Path(directory) / "outbox",
                "state": Path(directory) / "state", "seen": Path(directory) / "state/seen.json",
                "contacts": Path(directory) / "state/contacts.json",
                "settings": Path(directory) / "settings.json",
                "sync_store": Path(directory) / "wacli",
            }
            with self.assertRaises(receiver.SimulationError):
                receiver._verify_storage_layout(root, production)

    def test_sync_allowlist_permits_database_bytes_not_metadata_or_missing_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "wacli.db"
            database.write_bytes(b"before")
            database.chmod(0o600)
            mutable = {"wacli.db", "wacli.db-wal", "wacli.db-shm"}
            before = receiver._tree(root, mutable)
            database.write_bytes(b"after")
            wal = root / "wacli.db-wal"
            wal.write_bytes(b"normal new wal")
            after = receiver._tree(root, mutable)
            receiver._verify_sync_store(before, after, mutable)

            database.chmod(0o000)
            with self.assertRaises(receiver.SimulationError):
                receiver._verify_sync_store(before, receiver._tree(root, mutable), mutable)
            database.chmod(0o600)
            database.unlink()
            with self.assertRaises(receiver.SimulationError):
                receiver._verify_sync_store(before, receiver._tree(root, mutable), mutable)

    def test_foreign_actual_mode_and_live_nfc_marker_refuse_before_receive(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            with mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"}), \
                    self.assertRaises(receiver.SimulationError):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            self.assertEqual(list(fixture.receiver_root.iterdir()), [])
            self.assertFalse(fixture.output.exists())

            marker = fixture.production["nfc_unknown"]
            marker.parent.mkdir(mode=0o700)
            marker.write_text("{}")
            with fixture.production_receiver(), self.assertRaises(receiver.SimulationError):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            self.assertEqual(list(fixture.receiver_root.iterdir()), [])
            self.assertFalse(fixture.output.exists())

    def test_real_poll_once_download_queue_and_seen_commit_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            fifo = fixture.production["state"] / ".lgd-nfy0"
            os.mkfifo(fifo, 0o600)
            seen_before = fixture.seen.lstat()
            fixture.seen.chmod(0o640)
            seen_before = fixture.seen.lstat()
            queue_before = receiver._tree(fixture.production["queue"])
            outbox_before = receiver._tree(fixture.production["outbox"])
            with fixture.production_receiver() as paths, contextlib.redirect_stdout(io.StringIO()):
                receiver.receive_one(
                    fixture.authorization, fixture.receiver_root, fixture.output,
                    fixture.production, clock=lambda: fixture.now,
                )
            self.assertEqual(receiver._read_seen(fixture.seen), {OLD_ID, TARGET_ID})
            self.assertEqual(receiver._tree(fixture.production["queue"]), queue_before)
            self.assertEqual(receiver._tree(fixture.production["outbox"]), outbox_before)
            seen_after = fixture.seen.lstat()
            self.assertEqual(
                (stat.S_IMODE(seen_after.st_mode), seen_after.st_uid, seen_after.st_gid),
                (stat.S_IMODE(seen_before.st_mode), seen_before.st_uid, seen_before.st_gid),
            )
            self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
            self.assertEqual(len(list(paths.QUEUE_DIR.glob("*.wav"))), 1)
            manifest = receiver.selected.validate_manifest(json.loads(fixture.output.read_text()))
            self.assertEqual(manifest["target"]["sidecar_document"]["msgid"], TARGET_ID)
            receipt = json.loads((fixture.receiver_root / receiver.RECEIPT_NAME).read_text())
            self.assertEqual(receipt["status"], "received")
            self.assertEqual(receipt["source_timestamp"], fixture.message["Timestamp"])
            self.assertEqual(receipt["source_timestamp_unix"], fixture.now)

    def test_seen_copy_makes_second_receive_idempotently_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            with fixture.production_receiver(), contextlib.redirect_stdout(io.StringIO()):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            second = Path(directory) / "receiver-2"
            second.mkdir(mode=0o700)
            second_output = Path(directory) / "candidate-2.json"
            with fixture.production_receiver(root=second), self.assertRaises(receiver.SimulationError):
                receiver.receive_one(fixture.authorization, second, second_output,
                                     fixture.production, clock=lambda: fixture.now)
            self.assertEqual(receiver._read_seen(fixture.seen), {OLD_ID, TARGET_ID})
            self.assertFalse(second_output.exists())

    def test_target_race_and_additional_playable_work_refuse_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            foreign = dict(fixture.message, MsgID="other-fresh")
            with fixture.production_receiver([fixture.message, foreign]), \
                    self.assertRaises(receiver.SimulationError):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            self.assertEqual(receiver._read_seen(fixture.seen), {OLD_ID})
            self.assertFalse(fixture.output.exists())

    def test_failed_seen_commit_leaves_media_manifest_and_pending_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            synced = []
            original_fsync = receiver._fsync_directory

            def fsync_directory(path):
                synced.append(Path(path).resolve())
                original_fsync(path)

            def fail_commit(*_args):
                self.assertIn(fixture.output.parent.resolve(), synced)
                self.assertIn(fixture.receiver_root.resolve(), synced)
                raise receiver.SimulationError("race")

            with fixture.production_receiver(), \
                    mock.patch.object(receiver, "_fsync_directory", side_effect=fsync_directory), \
                    mock.patch.object(receiver, "_add_seen", side_effect=fail_commit), \
                    self.assertRaises(receiver.SimulationError):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            self.assertEqual(receiver._read_seen(fixture.seen), {OLD_ID})
            self.assertTrue(fixture.output.exists())
            receipt = json.loads((fixture.receiver_root / receiver.RECEIPT_NAME).read_text())
            self.assertEqual(receipt["status"], "seen_commit_pending")
            with self.assertRaises(receiver.SimulationError):
                receiver._receipt(fixture.receiver_root)

    def test_original_server_timestamp_can_expire_before_execute_or_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            fixture.message["Timestamp"] = fixture.now - 590
            fixture.authorization["not_before"] = fixture.now - 600
            fixture.authorization["max_age_seconds"] = 600
            with fixture.production_receiver(), contextlib.redirect_stdout(io.StringIO()):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            manifest = json.loads(fixture.output.read_text())
            outbound = Path(directory) / "outbound"
            outbound.mkdir(mode=0o700)
            fake_paths = fixture.paths()
            with mock.patch.object(receiver, "bind_scratch_paths", return_value=fake_paths), \
                    mock.patch.object(receiver, "_account_hash", return_value="a" * 64), \
                    mock.patch.object(receiver, "_verify_service_state"), \
                    mock.patch.object(receiver.selected, "execute") as execute, \
                    self.assertRaises(receiver.SimulationError):
                receiver.execute_selected({}, manifest, fixture.receiver_root, outbound,
                                          fixture.production, 30,
                                          clock=lambda: fixture.now + 20)
            execute.assert_not_called()

            current = [fixture.now]

            def age_then_guard(*_args):
                current[0] = fixture.now + 20
                receiver.selected.verify_environment(None, None, None, None)

            with mock.patch.object(receiver, "bind_scratch_paths", return_value=fake_paths), \
                    mock.patch.object(receiver, "_account_hash", return_value="a" * 64), \
                    mock.patch.object(receiver, "_verify_service_state"), \
                    mock.patch.object(receiver.selected, "verify_environment", return_value={}), \
                    mock.patch.object(receiver.selected, "execute", side_effect=age_then_guard), \
                    self.assertRaises(receiver.SimulationError):
                receiver.execute_selected({}, manifest, fixture.receiver_root, outbound,
                                          fixture.production, 30, clock=lambda: current[0])

    def test_execute_requires_exact_receive_receipt_and_delegates_unchanged_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            with fixture.production_receiver(), contextlib.redirect_stdout(io.StringIO()):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            manifest = json.loads(fixture.output.read_text())
            outbound = Path(directory) / "outbound"
            outbound.mkdir(mode=0o700)
            fake_paths = fixture.paths()
            with mock.patch.object(receiver, "bind_scratch_paths", return_value=fake_paths), \
                    mock.patch.object(receiver, "_account_hash", return_value="a" * 64), \
                    mock.patch.object(receiver, "_verify_service_state"), \
                    mock.patch.object(receiver.selected, "execute") as execute:
                receiver.execute_selected({"duration": 1, "events": []}, manifest,
                                          fixture.receiver_root, outbound,
                                          fixture.production, 30)
            execute.assert_called_once_with(
                {"duration": 1, "events": []}, manifest, outbound, 30,
            )
            receipt_path = fixture.receiver_root / receiver.RECEIPT_NAME
            receipt = json.loads(receipt_path.read_text())
            receipt["target_msgid"] = "changed"
            receipt_path.write_text(json.dumps(receipt))
            with self.assertRaises(receiver.SimulationError):
                receiver.execute_selected({}, manifest, fixture.receiver_root, outbound,
                                          fixture.production, 30)

    def test_execute_rechecks_live_binding_before_delegation_and_at_driver_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReceiveFixture(directory)
            with fixture.production_receiver(), contextlib.redirect_stdout(io.StringIO()):
                receiver.receive_one(fixture.authorization, fixture.receiver_root, fixture.output,
                                     fixture.production, clock=lambda: fixture.now)
            manifest = json.loads(fixture.output.read_text())
            outbound = Path(directory) / "outbound"
            outbound.mkdir(mode=0o700)
            fake_paths = fixture.paths()
            original_settings = fixture.settings.read_bytes()
            fixture.settings.write_bytes(b"changed before execution")
            with mock.patch.object(receiver, "bind_scratch_paths", return_value=fake_paths), \
                    mock.patch.object(receiver, "_account_hash", return_value="a" * 64), \
                    mock.patch.object(receiver, "_verify_service_state"), \
                    mock.patch.object(receiver.selected, "execute") as execute, \
                    self.assertRaises(receiver.SimulationError):
                receiver.execute_selected({}, manifest, fixture.receiver_root, outbound,
                                          fixture.production, 30)
            execute.assert_not_called()

            fixture.settings.write_bytes(original_settings)

            def drift_then_guard(*_args):
                fixture.settings.write_bytes(b"changed before selected claim")
                receiver.selected.verify_environment(None, None, None, None)

            with mock.patch.object(receiver, "bind_scratch_paths", return_value=fake_paths), \
                    mock.patch.object(receiver, "_account_hash", return_value="a" * 64), \
                    mock.patch.object(receiver, "_verify_service_state"), \
                    mock.patch.object(receiver.selected, "verify_environment", return_value={}), \
                    mock.patch.object(receiver.selected, "execute", side_effect=drift_then_guard), \
                    self.assertRaises(receiver.SimulationError):
                receiver.execute_selected({}, manifest, fixture.receiver_root, outbound,
                                          fixture.production, 30)


if __name__ == "__main__":
    unittest.main()
