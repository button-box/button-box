import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox.contacts import ContactStore
from messagebox.onboarding import voice_gate


PERSON = "15551234567@s.whatsapp.net"


class VoiceGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.enabled = self.root / "enabled"
        self.request = self.root / "request.json"
        self.contacts = self.root / "contacts.json"

    def tearDown(self):
        self.temporary.cleanup()

    def run_gate(self):
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(returncode=0)

        with (
            mock.patch.object(voice_gate, "ONBOARDING_ENABLED_PATH", self.enabled),
            mock.patch.object(voice_gate, "VOICE_REQUEST_FILE", self.request),
            mock.patch.object(voice_gate, "CONTACTS_FILE", self.contacts),
            mock.patch.object(voice_gate.os, "geteuid", return_value=0),
        ):
            result = voice_gate.main(run=run)
        return result, calls

    def test_valid_fixed_request_and_default_start_only_fixed_target(self):
        self.enabled.write_text("enabled\n", encoding="ascii")
        self.request.write_text(
            json.dumps({"version": 1, "enabled": True}), encoding="utf-8"
        )
        os.chmod(self.request, 0o600)
        ContactStore(self.contacts).add_contact(PERSON, "Grandma", make_default=True)

        result, calls = self.run_gate()

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [(["systemctl", "start", "messagebox-onboarding-voice.target"], {"check": True})],
        )

    def test_missing_onboarding_marker_stops_preview(self):
        result, calls = self.run_gate()
        self.assertEqual(result, 0)
        self.assertEqual(calls[0][0], ["systemctl", "stop", "messagebox-onboarding-voice.target"])

    def test_missing_voice_request_stops_preview(self):
        self.enabled.write_text("enabled\n", encoding="ascii")

        result, calls = self.run_gate()

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [
                (
                    ["systemctl", "stop", "messagebox-onboarding-voice.target"],
                    {"check": True},
                )
            ],
        )

    def test_invalid_request_or_missing_default_fails_closed(self):
        self.enabled.write_text("enabled\n", encoding="ascii")
        self.request.write_text("{}", encoding="ascii")
        os.chmod(self.request, 0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            self.run_gate()

        self.request.write_text(
            json.dumps({"version": 1, "enabled": True}), encoding="utf-8"
        )
        os.chmod(self.request, 0o600)
        with self.assertRaisesRegex(RuntimeError, "default"):
            self.run_gate()


if __name__ == "__main__":
    unittest.main()
