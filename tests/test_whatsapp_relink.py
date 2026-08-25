import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from messagebox.contacts import ContactStore
from messagebox.onboarding.recipients import RecipientSetup
from messagebox.onboarding.whatsapp import PairingEngine


class WhatsAppRelinkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.live = self.root / "wacli"
        self.live.mkdir()
        (self.live / "store.db").write_text("private", encoding="ascii")
        self.candidates = self.live / "onboarding-candidates.json"
        self.candidates.write_text('{"version":1,"conversations":[]}', encoding="ascii")
        self.contacts_path = self.root / "state" / "contacts.json"
        self.recipient_path = self.root / "state" / "recipient.json"
        self.voice_path = self.root / "state" / "voice.json"
        self.other_paths = [self.root / "state" / f"private-{index}.json" for index in range(4)]
        self.recipients = RecipientSetup(
            contacts_path=self.contacts_path,
            state_path=self.recipient_path,
            voice_request_path=self.voice_path,
            account_reset_paths=self.other_paths,
            clock=lambda: 1000,
        )
        contacts = ContactStore(self.contacts_path, clock=lambda: 1000)
        jid = "15551234567@s.whatsapp.net"
        contacts.add_contact(jid, "+15551234567", make_default=True)
        contacts.assign_card(jid, "04:A1:00:FF")
        contacts.upsert_listener(jid, "Grandma")
        self.recipients.public_state()
        state = json.loads(self.recipient_path.read_text(encoding="utf-8"))
        state["status"] = "complete"
        state["proof"].update(received=True, played=True, replied=True)
        self.recipient_path.write_text(json.dumps(state), encoding="utf-8")
        self.voice_path.write_text("proof", encoding="ascii")
        for path in self.other_paths:
            path.write_text("private", encoding="ascii")

    def tearDown(self):
        self.directory.cleanup()

    def engine(self, returncode=0):
        def run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], returncode, "{}", "")

        engine = PairingEngine(
            pairing_root=self.root / "pairing",
            live_store=self.live,
            candidates_path=self.candidates,
            recipient_setup=self.recipients,
            run=run,
            recover=False,
        )
        engine._set_state(
            "ready", phone_hint="WhatsApp number ending in 4567", eligible_count=1
        )
        return engine

    def test_success_erases_account_store_routing_cards_and_proof(self):
        result = self.engine().relink()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(list(self.live.iterdir()), [])
        contacts = ContactStore(self.contacts_path).load()
        self.assertEqual(contacts["contacts"], {})
        self.assertEqual(contacts["listeners"], {})
        self.assertIsNone(contacts["default_recipient"])
        self.assertFalse(self.recipient_path.exists())
        self.assertFalse(self.voice_path.exists())
        self.assertTrue(all(not path.exists() for path in self.other_paths))

    def test_failed_logout_preserves_store_and_routing(self):
        result = self.engine(returncode=1).relink()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["safe_error"], "UNLINK_FAILED")
        self.assertTrue((self.live / "store.db").exists())
        self.assertTrue(ContactStore(self.contacts_path).load()["contacts"])
        self.assertTrue(self.recipient_path.exists())


if __name__ == "__main__":
    unittest.main()
