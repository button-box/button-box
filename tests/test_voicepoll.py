import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from messagebox.contacts import ContactStore
from messagebox.voicepoll import (
    load_contact_authorizations,
    message_is_authorized,
    parse_wacli_timestamp,
)


PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363123456789@g.us"


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
