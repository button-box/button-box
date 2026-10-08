"""Self-hosted contracts across contacts, NFC, queue, settings and receipts.

Only subprocess/audio boundaries are faked; routing and durable state are real.
These contracts always select wacli, including in the Cloud matrix job.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_button_settings_behavior import FakeLed, button_send
from test_cloud_runtime import write_pcm_wav
from messagebox import nfc, voicepoll
from messagebox.contacts import ContactStore
from messagebox.listened_receipts import ReceiptStore, load_listener_profiles
from messagebox.nfc_state import AnnouncementStore, SelectionStore
from messagebox.settings import SettingsReader, SettingsStore, in_quiet_hours


PERSON = "15551234567@s.whatsapp.net"
OTHER = "15557654321@s.whatsapp.net"
CARD = "04:A1:00:FF"


class WacliContractTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("queue", "outbox", "tmp"):
            (self.root / name).mkdir()
        self.contacts_path = self.root / "contacts.json"
        self.contacts = ContactStore(self.contacts_path)
        self.contacts.add_contact(OTHER, "Default person", receive_after=100, make_default=True)
        self.contacts.add_contact(PERSON, "Card person", receive_after=100)
        self.receipts = ReceiptStore(str(self.root / "receipts"))
        self.settings = SettingsStore(self.root / "settings.json", environ={"TZ": "UTC"})
        self.set_quiet(False)
        self.announcements = AnnouncementStore(self.root / "announcement.json")
        self.now = datetime(2026, 10, 8, 23, tzinfo=timezone.utc)
        patches = [mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "wacli"})]
        for module, values in (
            (button_send, {
                "CONTACTS_FILE": str(self.contacts_path), "QUEUE_DIR": str(self.root / "queue"),
                "OUTBOX_DIR": str(self.root / "outbox"), "TEMP_DIR": str(self.root / "tmp"),
                "NFC_SELECTION_FILE": self.root / "selection.json",
                "NFC_HEALTH_FILE": self.root / "nfc-health",
                "nfc_announcement_store": self.announcements, "receipt_store": self.receipts,
                "settings_reader": SettingsReader(self.settings, refresh_seconds=0),
                "button": SimpleNamespace(is_pressed=False), "led": FakeLed(),
                "_recording": False, "_guided_active": False,
                "_known": set(), "_seen_ever": set(), "_ring_last": 0,
                "send_success_notices": button_send.queue.SimpleQueue(),
            }),
            (nfc, {
                "CONTACTS_FILE": self.contacts_path, "NFC_SELECTION_FILE": self.root / "selection.json",
                "NFC_ENROLLMENT_FILE": self.root / "enrollment.json",
            }),
            (voicepoll, {
                "CONTACTS_FILE": self.contacts_path, "QUEUE_DIR": str(self.root / "queue"),
                "STATE_FILE": str(self.root / "seen.json"),
            }),
        ):
            patches.extend(mock.patch.object(module, name, value, create=True)
                           for name, value in values.items())
        for name in ("log", "log_event", "play_moment", "wait_for_stable_open", "refresh_led"):
            patches.append(mock.patch.object(button_send, name))
        patches.extend([
            mock.patch.object(voicepoll, "log_event"),
            mock.patch.object(button_send, "in_quiet_hours",
                              side_effect=lambda settings, now=None:
                              in_quiet_hours(settings, now or self.now)),
        ])
        # Any attempted Cloud authorization or network use is a contract failure.
        for name in ("account_scope", "playable", "record_played", "listened_status"):
            patches.append(mock.patch.object(button_send.cloud_runtime, name,
                                             side_effect=AssertionError("Cloud path used by wacli")))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_quiet(self, enabled):
        current, _ = self.settings.load()
        candidate = {key: value for key, value in current.items() if key not in {"version", "revision"}}
        candidate["quiet_hours"] = {"enabled": enabled, "start": "22:00", "end": "07:00"}
        self.settings.update(candidate, current["revision"])

    def send_to(self, recipient):
        wav = self.root / "outbox" / "1000-2.0.wav"
        write_pcm_wav(wav, 2)
        button_send.bind_legacy_job_recipient(str(wav), recipient)
        results = [SimpleNamespace(returncode=0),
                   SimpleNamespace(returncode=0, stdout='{"sent":true,"id":"synthetic-sent"}', stderr="")]
        with mock.patch.object(button_send.subprocess, "run", side_effect=results) as run:
            self.assertTrue(button_send.send_legacy_outbox_file(wav.name))
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [button_send.WACLI_BIN, "send", "voice"])
        self.assertEqual(command[command.index("--to") + 1], recipient)
        self.assertFalse(wav.exists())
        self.assertFalse(Path(str(wav) + ".json").exists())

    def test_family_card_selects_person_and_sends_exact_jid(self):
        self.contacts.assign_card(PERSON, CARD)
        button_send.NFC_HEALTH_FILE.touch()
        reader = nfc.NfcRuntime(nfc.router(self.announcements), nfc.Announcer(self.announcements))
        self.assertEqual(reader.observe(bytes.fromhex("04a100ff"), 0).action, "selected")
        context = button_send.current_recipient_context(claim=True)
        self.assertTrue(context["via_card"])
        self.assertEqual(context["contact"]["jid"], PERSON)
        self.assertIsNone(SelectionStore(button_send.NFC_SELECTION_FILE).load())
        self.send_to(context["contact"]["jid"])

    def test_receive_authorized_person_play_and_preserve_reply_route(self):
        message = {"MsgID": "synthetic-incoming", "ChatJID": PERSON, "SenderJID": PERSON,
                   "Timestamp": 101, "MediaType": "audio", "FromMe": False}
        denied = {**message, "MsgID": "synthetic-denied", "ChatJID": "15550000000@s.whatsapp.net"}

        def wacli(*arguments):
            if "messages" in arguments:
                return SimpleNamespace(stdout=json.dumps({"data": {"messages": [denied, message]}}))
            self.assertIn("--read-only", arguments)
            self.assertEqual(arguments[arguments.index("--chat") + 1], PERSON)
            self.assertEqual(arguments[arguments.index("--id") + 1], message["MsgID"])
            output = Path(arguments[arguments.index("--output") + 1])
            output.write_bytes(b"synthetic download")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {"path": str(output)}}))

        def convert(command, **kwargs):
            write_pcm_wav(command[-1], 1)
            return SimpleNamespace(returncode=0)

        seen = set()
        with mock.patch.object(voicepoll, "wacli", side_effect=wacli), \
             mock.patch.object(voicepoll.subprocess, "run", side_effect=convert):
            authorizations = voicepoll.load_contact_authorizations(self.contacts_path)
            self.assertEqual(voicepoll.poll_once(seen, authorizations), 1)
            self.assertEqual(voicepoll.poll_once(seen, authorizations), 0)
        self.assertEqual(seen, {message["MsgID"]})
        queued = self.root / "queue" / button_send.queued()[0]
        metadata = button_send.queue_metadata(queued)
        self.assertEqual((metadata["chat"], metadata["msgid"]), (PERSON, message["MsgID"]))
        with mock.patch.object(button_send.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as play, \
             mock.patch.object(button_send.subprocess, "Popen") as react:
            button_send.play_next_legacy()
        self.assertEqual(Path(play.call_args.args[0][-1]), queued)
        self.assertEqual(react.call_args.args[0][4:8], [PERSON, "--id", message["MsgID"], "--reaction"])
        archived = self.root / "queue" / ".played" / queued.name
        self.assertTrue(archived.exists())
        self.assertEqual(button_send.queue_metadata(archived)["chat"], PERSON)
        self.assertEqual(button_send.queued(), [])

    def enqueue_listened(self):
        self.send_to(PERSON)
        clip = self.root / "listened.wav"
        write_pcm_wav(clip, 1)
        self.contacts.upsert_listener(PERSON, "Card person")
        payload = {"EventType": "receipt", "Chat": PERSON, "Sender": "",
                   "MessageIDs": ["synthetic-sent"], "Type": "played", "IsFromMe": False}
        profiles = load_listener_profiles(str(self.contacts_path))
        self.assertEqual(len(self.receipts.ingest_played(payload, profiles, str(clip))), 1)
        self.assertEqual(self.receipts.ingest_played(payload, profiles, str(clip)), [])
        return payload, profiles, clip

    def test_listened_notice_survives_restart_and_announces_once(self):
        payload, profiles, clip = self.enqueue_listened()
        self.receipts.claim_next()  # Crash after claim, before playback.
        restarted = ReceiptStore(str(self.root / "receipts"))
        self.assertEqual(restarted.recover_inflight(), 1)
        with mock.patch.object(button_send, "receipt_store", restarted), \
             mock.patch.object(button_send.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as play:
            self.assertEqual(button_send.play_pending_listened(), 1)
            self.assertEqual(button_send.play_pending_listened(), 0)
        self.assertEqual(Path(play.call_args.args[0][-1]), clip)
        self.assertEqual(play.call_count, 1)
        self.assertEqual(restarted.ingest_played(payload, profiles, str(clip)), [])

    def test_quiet_hours_block_arrival_and_listened_sounds_without_losing_notice(self):
        self.enqueue_listened()
        self.set_quiet(True)
        self.assertTrue(button_send.quiet_hours())
        write_pcm_wav(self.root / "queue" / "1000-new.wav", 1)
        with mock.patch.object(button_send, "ring_alert") as ring, \
             mock.patch.object(button_send.subprocess, "run") as play, \
             mock.patch.object(button_send.subprocess, "Popen") as spawn:
            button_send.maybe_ring()
            self.assertEqual(button_send.play_pending_listened(), 0)
            self.assertEqual(self.receipts.pending_count(), 1)
            self.set_quiet(False)
            button_send.maybe_ring()  # No delayed overnight arrival ring.
            ring.assert_not_called()
            play.assert_not_called()
            spawn.assert_not_called()
            play.return_value = SimpleNamespace(returncode=0)
            self.assertEqual(button_send.play_pending_listened(), 1)
