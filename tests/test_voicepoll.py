import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox import voicepoll

from messagebox.contacts import ContactStore
from messagebox.voicepoll import (
    load_contact_authorizations,
    message_is_authorized,
    parse_wacli_timestamp,
)


PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363123456789@g.us"


class PollingStoreTests(unittest.TestCase):
    def test_unlinked_poll_does_not_initialize_a_store_before_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "wacli.db"

            def run(arguments, **kwargs):
                # Models the real CLI boundary, reproduced on the device:
                # ordinary listing creates the database; read-only does not.
                if "--read-only" not in arguments:
                    database.touch()
                return SimpleNamespace(returncode=1, stdout="", stderr="")

            with (
                mock.patch.object(voicepoll, "load_seen", return_value=set()),
                mock.patch.object(voicepoll, "load_contact_authorizations", return_value={}),
                mock.patch.object(voicepoll.subprocess, "run", side_effect=run) as process,
                mock.patch.object(voicepoll.time, "sleep", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                voicepoll.main()
            process.assert_called_once()
            self.assertFalse(database.exists())

    def test_consecutive_senders_queue_oldest_first_without_store_write_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue"
            state = root / "seen.json"
            events = root / "events.jsonl"
            first = {
                "MsgID": "first-message",
                "ChatJID": GROUP,
                "SenderJID": PERSON,
                "SenderName": "First",
                "Timestamp": 1001,
                "MediaType": "audio",
                "FromMe": False,
            }
            second = {
                "MsgID": "second-message",
                "ChatJID": GROUP,
                "SenderJID": "15557654321@s.whatsapp.net",
                "SenderName": "Second",
                "Timestamp": 1002,
                "MediaType": "audio",
                "FromMe": False,
            }
            listing = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"data": {"messages": [second, first]}}),
                stderr="",
            )
            calls = []

            def run_wacli(*arguments):
                calls.append(arguments)
                if "messages" in arguments:
                    return listing
                output = Path(arguments[arguments.index("--output") + 1])
                output.write_bytes(b"downloaded")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "data": {"path": "ignored"}}),
                    stderr="",
                )

            def convert(arguments, **_kwargs):
                Path(arguments[-1]).write_bytes(b"wav")
                return SimpleNamespace(returncode=0)

            with (
                mock.patch.object(voicepoll, "QUEUE_DIR", str(queue)),
                mock.patch.object(voicepoll, "STATE_FILE", str(state)),
                mock.patch.object(voicepoll, "EVENTS_FILE", str(events)),
                mock.patch.object(voicepoll, "_last_queue_ms", 0),
                mock.patch.object(voicepoll, "wacli", side_effect=run_wacli),
                mock.patch.object(voicepoll.subprocess, "run", side_effect=convert),
            ):
                seen = set()
                self.assertEqual(voicepoll.poll_once(seen, {GROUP: 1000}), 2)

            wavs = sorted(queue.glob("*.wav"))
            self.assertEqual([path.name.split("-", 1)[1] for path in wavs], [
                "first-message.wav",
                "second-message.wav",
            ])
            self.assertEqual(seen, {"first-message", "second-message"})
            self.assertEqual(len(list(queue.glob("*.media.part"))), 0)
            downloads = [call for call in calls if "media" in call]
            self.assertEqual(len(downloads), 2)
            for call in downloads:
                self.assertEqual(call[0], "--read-only")
                self.assertIn("--output", call)
                self.assertNotIn("--lock-wait", call)


class TimestampTests(unittest.TestCase):
    def test_parses_iso_and_unix_wacli_timestamp_forms(self):
        expected = datetime(2026, 7, 31, 12, tzinfo=timezone.utc).timestamp()
        for value in (
            "2026-07-31T12:00:00Z",
            "2026-07-31T14:00:00+02:00",
            "2026-07-31 12:00:00 UTC",
            expected,
            str(int(expected)),
            int(expected * 1000),
            int(expected * 1_000_000),
            int(expected * 1_000_000_000),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_wacli_timestamp(value), expected)

    def test_invalid_timestamps_fail_closed(self):
        for value in (None, True, "", "not-a-time", float("inf"), {}):
            with self.subTest(value=value):
                self.assertIsNone(parse_wacli_timestamp(value))


class ContactAuthorizationTests(unittest.TestCase):
    def test_store_is_reloaded_and_only_contacts_authorize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            store = ContactStore(path)
            store.upsert_listener("15550001@s.whatsapp.net", "Mom")

            self.assertEqual(load_contact_authorizations(path), {})
            store.add_contact(GROUP, "Family", receive_after=100)
            store.assign_card(GROUP, "04:A1:00:FF")
            self.assertEqual(load_contact_authorizations(path), {GROUP: 100})

            store.add_contact(PERSON, "Grandma", receive_after=200.5)
            self.assertEqual(
                load_contact_authorizations(path),
                {GROUP: 100, PERSON: 200.5},
            )

    def test_missing_empty_and_corrupt_store_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            self.assertEqual(load_contact_authorizations(path), {})
            path.write_text("", encoding="utf-8")
            self.assertEqual(load_contact_authorizations(path), {})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_contact_authorizations(path), {})

    def test_receive_after_blocks_historical_and_unparseable_messages(self):
        authorizations = {GROUP: 1_000.5}

        def message(timestamp, chat=GROUP):
            return {"ChatJID": chat, "Timestamp": timestamp}

        self.assertFalse(message_is_authorized(message(1_000.499), authorizations))
        self.assertTrue(message_is_authorized(message(1_000.5), authorizations))
        self.assertTrue(message_is_authorized(message("1970-01-01T00:16:41Z"), authorizations))
        self.assertFalse(message_is_authorized(message(None), authorizations))
        self.assertFalse(message_is_authorized(message(2_000, PERSON), authorizations))


if __name__ == "__main__":
    unittest.main()
