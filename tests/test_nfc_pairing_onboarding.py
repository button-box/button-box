import json
import tempfile
import unittest
from pathlib import Path

from messagebox.contacts import ContactStore
from messagebox.onboarding.nfc import NfcOnboardingEngine, NfcOnboardingError
from messagebox.onboarding.recipients import RecipientSetup


TOKEN_A = "recipient-token-0001"
TOKEN_B = "recipient-token-0002"
PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
CARD_A = b"\x04\x01\x02\x03"
CARD_B = b"\x04\x05\x06\x07"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class Reader:
    def read(self):
        return None


def completed_recipients(root, clock):
    contacts_path = root / "contacts.json"
    store = ContactStore(contacts_path, clock=clock)
    store.add_contact(PERSON, "Grandma", make_default=True)
    store.add_contact(GROUP, "Family")
    state_path = root / "recipient.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "complete",
                "default_token": TOKEN_A,
                "started_at": clock(),
                "candidates": {
                    TOKEN_A: {
                        "jid": PERSON,
                        "label": "Grandma",
                        "kind": "person",
                        "available": True,
                    },
                    TOKEN_B: {
                        "jid": GROUP,
                        "label": "Family",
                        "kind": "group",
                        "available": True,
                    },
                },
                "proof": {
                    "received": True,
                    "played": True,
                    "replied": True,
                    "received_file": "voice.ogg",
                    "session_id": "session",
                    "message_id": "message",
                },
            }
        ),
        encoding="utf-8",
    )
    recipients = RecipientSetup(
        state_path=state_path,
        contacts_path=contacts_path,
        events_path=root / "events.jsonl",
        voice_request_path=root / "voice-request.json",
        clock=clock,
    )
    return store, recipients, contacts_path


class NfcPairingOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.clock = Clock()
        self.contacts, self.recipients, self.contacts_path = completed_recipients(
            self.root, self.clock
        )
        self.tones = []
        self.engine = NfcOnboardingEngine(
            state_path=self.root / "nfc.json",
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            reader_factory=Reader,
            tone_player=self.tones.append,
            clock=self.clock,
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_tag_first_pairing_is_redacted_atomic_and_allows_multiple_tags(self):
        self.assertEqual(self.engine.start()["status"], "waiting")
        detected = self.engine.observe(CARD_A)
        self.assertEqual(detected["status"], "choose")
        self.assertEqual(self.tones, ["read"])
        self.assertNotIn("04:01:02:03", json.dumps(detected))
        self.assertNotIn("@s.whatsapp.net", json.dumps(detected))

        paired = self.engine.assign(TOKEN_A)
        self.assertEqual(paired["status"], "success")
        self.assertEqual(paired["recipient"], {"label": "+15551234567", "kind": "person"})
        self.assertEqual(paired["mapped_count"], 1)
        self.assertEqual(self.tones, ["read", "success"])

        waiting = self.engine.next()
        self.assertTrue(waiting["remove_tag"])
        self.clock.value += 1
        self.engine.observe(None)
        self.engine.observe(CARD_B)
        second = self.engine.assign(TOKEN_A)
        self.assertEqual(second["mapped_count"], 2)
        self.assertEqual(self.contacts.public_view()["contacts"][PERSON]["card_count"], 2)

    def test_existing_mapping_requires_explicit_reassignment(self):
        self.contacts.assign_card(PERSON, "04:01:02:03")
        self.engine.start()
        mapped = self.engine.observe(CARD_A)
        self.assertEqual(mapped["status"], "already_paired")
        self.assertEqual(mapped["recipient"]["label"], "+15551234567")
        with self.assertRaises(NfcOnboardingError):
            self.engine.assign(TOKEN_B)
        self.assertEqual(self.engine.allow_reassign()["status"], "choose")
        reassigned = self.engine.assign(TOKEN_B)
        self.assertEqual(reassigned["recipient"]["label"], "Family")
        self.assertEqual(self.contacts.resolve_card("04:01:02:03")["jid"], GROUP)

    def test_pending_tag_resumes_then_expires_without_exposing_uid(self):
        self.engine.start()
        self.engine.observe(CARD_A)
        self.clock.value += 60
        resumed = NfcOnboardingEngine(
            state_path=self.root / "nfc.json",
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            reader_factory=Reader,
            tone_player=self.tones.append,
            clock=self.clock,
        )
        self.assertEqual(resumed.public_state()["status"], "choose")
        self.clock.value += 61
        expired = resumed.public_state()
        self.assertEqual(expired["status"], "waiting")
        self.assertNotIn("04:01:02:03", json.dumps(expired))

    def test_reader_failure_can_retry_or_finish_without_cards(self):
        attempts = []

        def reader_factory():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("private hardware detail")
            return Reader()

        engine = NfcOnboardingEngine(
            state_path=self.root / "retry.json",
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            reader_factory=reader_factory,
            tone_player=self.tones.append,
            clock=self.clock,
        )
        self.assertEqual(engine.start()["status"], "unavailable")
        self.assertEqual(engine.retry()["status"], "waiting")
        self.assertEqual(engine.finish()["status"], "idle")

    def test_recipient_removal_drops_its_mappings(self):
        self.contacts.assign_card(GROUP, "04:01:02:03")
        self.recipients.remove(TOKEN_B)
        self.assertIsNone(self.contacts.resolve_card("04:01:02:03"))


if __name__ == "__main__":
    unittest.main()
