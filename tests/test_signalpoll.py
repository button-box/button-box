import tempfile
import unittest
from pathlib import Path

from messagebox.contacts import ContactStore
from messagebox.signalpoll import (
    envelope_source,
    envelope_timestamp,
    load_contact_authorizations,
    message_is_authorized,
    voice_attachment,
)


SIGNAL_PERSON = "+15551234567"
SIGNAL_GROUP_ID = "dGVzdC1ncm91cC1pZA=="
WHATSAPP_PERSON = "15559876543@s.whatsapp.net"


def envelope(*, source=SIGNAL_PERSON, timestamp=1_700_000_000_000, group_id=None, attachments=None):
    data_message = {"attachments": attachments or []}
    if group_id:
        data_message["groupInfo"] = {"groupId": group_id}
    return {
        "envelope": {
            "source": source,
            "sourceNumber": source,
            "timestamp": timestamp,
            "dataMessage": data_message,
        }
    }


class EnvelopeParsingTests(unittest.TestCase):
    def test_source_prefers_group_id_over_sender(self):
        self.assertEqual(envelope_source(envelope(group_id=SIGNAL_GROUP_ID)), f"group.{SIGNAL_GROUP_ID}")
        self.assertEqual(envelope_source(envelope()), SIGNAL_PERSON)

    def test_timestamp_is_normalized_to_seconds(self):
        self.assertEqual(envelope_timestamp(envelope(timestamp=1_700_000_000_000)), 1_700_000_000.0)
        self.assertIsNone(envelope_timestamp(envelope(timestamp=None)))
        self.assertIsNone(envelope_timestamp(envelope(timestamp=True)))

    def test_voice_attachment_requires_audio_content_type(self):
        audio = {"id": "a1", "contentType": "audio/ogg"}
        image = {"id": "a2", "contentType": "image/png"}
        self.assertEqual(voice_attachment(envelope(attachments=[image, audio])), audio)
        self.assertIsNone(voice_attachment(envelope(attachments=[image])))
        self.assertIsNone(voice_attachment(envelope(attachments=[])))


class ContactAuthorizationTests(unittest.TestCase):
    def test_only_signal_channel_contacts_authorize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            store = ContactStore(path)
            store.add_contact(SIGNAL_PERSON, "Aunt", channel="signal", receive_after=100)
            store.add_contact(WHATSAPP_PERSON, "Grandma", receive_after=200)

            self.assertEqual(load_contact_authorizations(path), {SIGNAL_PERSON: 100})

    def test_missing_and_corrupt_store_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            self.assertEqual(load_contact_authorizations(path), {})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_contact_authorizations(path), {})


class AuthorizationGateTests(unittest.TestCase):
    def test_receive_after_blocks_historical_and_unrecognized_senders(self):
        authorizations = {SIGNAL_PERSON: 1_000.5}
        self.assertFalse(message_is_authorized(envelope(timestamp=1000), authorizations))
        self.assertTrue(message_is_authorized(envelope(timestamp=1001), authorizations))
        self.assertFalse(
            message_is_authorized(envelope(source="+19998887777", timestamp=1001), authorizations)
        )


if __name__ == "__main__":
    unittest.main()
