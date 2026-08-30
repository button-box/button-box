import json
import tempfile
import unittest
from pathlib import Path

from messagebox.contacts import ContactStore
from messagebox.onboarding.recipients import RecipientError, RecipientSetup


PERSON = "15551234567@s.whatsapp.net"
SECOND_PERSON = "14155550199@s.whatsapp.net"
GROUP = "120363123456789@g.us"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        self.value += 1
        return self.value


class Tokens:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"recipient-token-{self.value:04d}"


class RecipientSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.clock = Clock()
        self.contacts_path = root / "contacts.json"
        self.events_path = root / "events.jsonl"
        self.request_path = root / "voice-request.json"
        self.setup = RecipientSetup(
            state_path=root / "recipient-state.json",
            contacts_path=self.contacts_path,
            events_path=self.events_path,
            voice_request_path=self.request_path,
            clock=self.clock,
            token_factory=Tokens(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def candidates(self):
        return self.setup.reconcile(
            [
                {"jid": PERSON, "label": "Grandma"},
                {"jid": GROUP, "label": "Family"},
            ]
        )

    def test_selection_uses_opaque_token_and_atomically_sets_fixed_default(self):
        listed = self.candidates()
        rendered = json.dumps(listed)
        self.assertNotIn(PERSON, rendered)
        self.assertNotIn(GROUP, rendered)
        token = next(
            item["token"]
            for item in listed["recipients"]
            if item["label"] == "+15551234567"
        )

        selected = self.setup.select_default(token)

        self.assertEqual(selected["status"], "testing")
        self.assertEqual(selected["default"]["label"], "+15551234567")
        contacts = ContactStore(self.contacts_path).load()
        self.assertEqual(contacts["default_recipient"], PERSON)
        self.assertEqual(set(contacts["contacts"]), {PERSON})
        self.assertEqual(
            json.loads(self.request_path.read_text(encoding="utf-8")),
            {"version": 1, "enabled": True},
        )
        with self.assertRaisesRegex(RecipientError, "fixed"):
            self.setup.select_default(token)

    def test_manual_phone_can_be_selected_without_discovery(self):
        selected = self.setup.select_phone("+14155550199")

        self.assertEqual(selected["status"], "testing")
        self.assertEqual(selected["default"]["label"], "+14155550199")
        self.assertNotIn(SECOND_PERSON, json.dumps(selected))
        contacts = ContactStore(self.contacts_path).load()
        self.assertEqual(contacts["default_recipient"], SECOND_PERSON)
        with self.assertRaisesRegex(RecipientError, "invalid"):
            RecipientSetup(
                state_path=self.setup.state_path.parent / "other-state.json",
                contacts_path=self.setup.state_path.parent / "other-contacts.json",
            ).select_phone("+0123")

    def test_receive_play_reply_proof_requires_one_exact_correlated_flow(self):
        listed = self.candidates()
        token = next(
            item["token"]
            for item in listed["recipients"]
            if item["label"] == "+15551234567"
        )
        self.setup.select_default(token)
        started = self.clock.value
        events = [
            {"type": "received", "ts": started - 1, "chat": PERSON, "file": "old.wav"},
            {"type": "received", "ts": started + 1, "chat": GROUP, "file": "wrong.wav"},
            {"type": "received", "ts": started + 2, "chat": PERSON, "file": "new.wav"},
            {
                "type": "guided_session_started",
                "ts": started + 3,
                "flow": "reply",
                "source_file": "new.wav",
                "session_id": "session-1",
            },
            {"type": "guided_inbound_played", "ts": started + 4, "session_id": "session-1"},
            {
                "type": "guided_approved",
                "ts": started + 5,
                "flow": "reply",
                "session_id": "session-1",
                "message_id": "message-1",
            },
            {
                "type": "sent",
                "ts": started + 6,
                "flow": "reply",
                "message_id": "message-1",
                "target": PERSON,
            },
        ]
        self.events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        complete = self.setup.public_state()

        self.assertEqual(complete["status"], "complete")
        self.assertEqual(
            complete["proof"], {"received": True, "played": True, "replied": True}
        )
        self.assertNotIn(PERSON, json.dumps(complete))

    def test_later_complete_reply_replaces_incomplete_proof_anchor(self):
        listed = self.candidates()
        token = next(
            item["token"]
            for item in listed["recipients"]
            if item["label"] == "+15551234567"
        )
        self.setup.select_default(token)
        started = self.clock.value
        events = [
            {"type": "received", "ts": started + 1, "chat": PERSON, "file": "first.wav"},
            {
                "type": "guided_session_started",
                "ts": started + 2,
                "flow": "reply",
                "source_file": "first.wav",
                "session_id": "session-1",
            },
            {"type": "guided_inbound_played", "ts": started + 3, "session_id": "session-1"},
            {"type": "guided_playback_only", "ts": started + 4, "session_id": "session-1"},
            {"type": "received", "ts": started + 5, "chat": PERSON, "file": "retry.wav"},
            {
                "type": "guided_session_started",
                "ts": started + 6,
                "flow": "reply",
                "source_file": "retry.wav",
                "session_id": "session-2",
            },
            {"type": "guided_inbound_played", "ts": started + 7, "session_id": "session-2"},
            {
                "type": "guided_approved",
                "ts": started + 8,
                "flow": "reply",
                "session_id": "session-2",
                "message_id": "message-2",
            },
            {
                "type": "sent",
                "ts": started + 9,
                "flow": "reply",
                "message_id": "message-2",
                "target": PERSON,
            },
        ]
        self.events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        complete = self.setup.public_state()

        self.assertEqual(complete["status"], "complete")
        self.assertEqual(
            complete["proof"], {"received": True, "played": True, "replied": True}
        )
        persisted = json.loads(self.setup.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["proof"]["received_file"], "retry.wav")
        self.assertEqual(persisted["proof"]["session_id"], "session-2")
        self.assertEqual(persisted["proof"]["message_id"], "message-2")

    def test_standalone_send_after_playback_does_not_complete_reply_proof(self):
        listed = self.candidates()
        token = next(
            item["token"]
            for item in listed["recipients"]
            if item["label"] == "+15551234567"
        )
        self.setup.select_default(token)
        started = self.clock.value
        events = [
            {"type": "received", "ts": started + 1, "chat": PERSON, "file": "voice.wav"},
            {
                "type": "guided_session_started",
                "ts": started + 2,
                "flow": "reply",
                "source_file": "voice.wav",
                "session_id": "reply-session",
            },
            {
                "type": "guided_inbound_played",
                "ts": started + 3,
                "session_id": "reply-session",
            },
            {
                "type": "guided_approved",
                "ts": started + 4,
                "flow": "standalone",
                "session_id": "standalone-session",
                "message_id": "standalone-message",
            },
            {
                "type": "sent",
                "ts": started + 5,
                "flow": "standalone",
                "message_id": "standalone-message",
                "target": PERSON,
            },
        ]
        self.events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        incomplete = self.setup.public_state()

        self.assertEqual(incomplete["status"], "testing")
        self.assertEqual(
            incomplete["proof"], {"received": True, "played": True, "replied": False}
        )

    def test_manager_changes_default_and_removes_only_non_default_recipients(self):
        listed = self.candidates()
        tokens = {item["label"]: item["token"] for item in listed["recipients"]}
        self.setup.select_default(tokens["+15551234567"])
        state = json.loads(self.setup.state_path.read_text(encoding="utf-8"))
        state["status"] = "complete"
        state["proof"].update(received=True, played=True, replied=True)
        self.setup._write(state)

        added = self.setup.add(tokens["Family"])
        self.assertEqual(len([item for item in added["recipients"] if item["configured"]]), 2)
        with self.assertRaisesRegex(RecipientError, "default"):
            self.setup.remove(tokens["+15551234567"])

        changed = self.setup.choose_default(tokens["Family"])

        self.assertEqual(changed["default"]["label"], "Family")
        self.assertEqual(ContactStore(self.contacts_path).load()["default_recipient"], GROUP)
        with self.assertRaisesRegex(RecipientError, "default"):
            self.setup.remove(tokens["Family"])
        self.setup.remove(tokens["+15551234567"])
        self.assertEqual(set(ContactStore(self.contacts_path).load()["contacts"]), {GROUP})

    def test_manager_rejects_unconfigured_default_and_recovers_store_first_change(self):
        listed = self.candidates()
        tokens = {item["label"]: item["token"] for item in listed["recipients"]}
        self.setup.select_default(tokens["+15551234567"])
        state = json.loads(self.setup.state_path.read_text(encoding="utf-8"))
        state["status"] = "complete"
        state["proof"].update(received=True, played=True, replied=True)
        self.setup._write(state)
        with self.assertRaisesRegex(RecipientError, "not configured"):
            self.setup.choose_default(tokens["Family"])

        self.setup.add(tokens["Family"])
        ContactStore(self.contacts_path).choose_default_recipient(GROUP)

        recovered = self.setup.public_state()
        self.assertEqual(recovered["default"]["label"], "Family")
        persisted = json.loads(self.setup.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["default_token"], tokens["Family"])

    def test_manager_can_allow_manual_phone_without_changing_default(self):
        listed = self.candidates()
        token = next(
            item["token"]
            for item in listed["recipients"]
            if item["label"] == "+15551234567"
        )
        self.setup.select_default(token)
        state = json.loads(self.setup.state_path.read_text(encoding="utf-8"))
        state["status"] = "complete"
        state["proof"].update(received=True, played=True, replied=True)
        self.setup._write(state)

        added = self.setup.add_phone("+14155550199")

        contacts = ContactStore(self.contacts_path).load()
        self.assertEqual(contacts["default_recipient"], PERSON)
        self.assertIn(SECOND_PERSON, contacts["contacts"])
        manual = next(
            recipient
            for recipient in added["recipients"]
            if recipient["label"] == "+14155550199"
        )
        self.assertTrue(manual["configured"])
        self.assertFalse(manual["is_default"])
        with self.assertRaisesRegex(RecipientError, "already exists"):
            self.setup.add_phone("+14155550199")

    def test_defer_is_resumable_and_does_not_activate_messaging(self):
        self.candidates()
        deferred = self.setup.defer()
        self.assertEqual(deferred["status"], "deferred")
        self.assertFalse(self.request_path.exists())
        self.assertEqual(ContactStore(self.contacts_path).load()["contacts"], {})


class ContactMigrationTests(unittest.TestCase):
    def test_version_one_default_migration_preserves_existing_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            base = {"version": 1, "revision": 4, "contacts": {}, "listeners": {}}
            path.write_text(json.dumps(base), encoding="utf-8")
            self.assertIsNone(ContactStore(path).load()["default_recipient"])

            contact = {
                "label": "Grandma",
                "kind": "person",
                "receive_after": 0,
                "card_uids": [],
                "card_clip": "",
            }
            base["contacts"] = {PERSON: contact}
            path.write_text(json.dumps(base), encoding="utf-8")
            self.assertEqual(ContactStore(path).load()["default_recipient"], PERSON)

            base["contacts"][GROUP] = {**contact, "label": "Family", "kind": "group"}
            path.write_text(json.dumps(base), encoding="utf-8")
            self.assertIsNone(ContactStore(path).load()["default_recipient"])

    def test_multiple_legacy_contacts_can_choose_an_existing_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contact = {
                "label": "Grandma",
                "kind": "person",
                "receive_after": 0,
                "card_uids": [],
                "card_clip": "",
            }
            (root / "contacts.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "revision": 4,
                        "contacts": {
                            PERSON: contact,
                            GROUP: {**contact, "label": "Family", "kind": "group"},
                        },
                        "listeners": {},
                    }
                ),
                encoding="utf-8",
            )
            setup = RecipientSetup(
                state_path=root / "recipient-state.json",
                contacts_path=root / "contacts.json",
                events_path=root / "events.jsonl",
                voice_request_path=root / "voice-request.json",
                clock=Clock(),
                token_factory=Tokens(),
            )
            listed = setup.reconcile(
                [
                    {"jid": PERSON, "label": "Grandma"},
                    {"jid": GROUP, "label": "Family"},
                ]
            )
            token = next(
                item["token"] for item in listed["recipients"] if item["label"] == "Family"
            )

            setup.select_default(token)

            migrated = ContactStore(root / "contacts.json").load()
            self.assertEqual(migrated["default_recipient"], GROUP)
            self.assertEqual(set(migrated["contacts"]), {PERSON, GROUP})


if __name__ == "__main__":
    unittest.main()
