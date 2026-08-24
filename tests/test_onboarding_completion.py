import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.onboarding.completion import complete, request_completion
from test_nfc_pairing_onboarding import CARD_A, PERSON, completed_recipients


class Clock:
    def __call__(self):
        return 1000.0


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.enabled = self.root / "enabled"
        self.enabled.write_text("enabled\n", encoding="ascii")
        self.enabled.chmod(0o600)
        self.request = self.root / "completion.json"
        request_completion(self.request)
        self.contacts, self.recipients, self.contacts_path = completed_recipients(
            self.root, Clock()
        )
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def command_runner(self, command, check=False):
        self.calls.append((command, check))
        return subprocess.CompletedProcess(command, 0)

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_zero_cards_enables_runtime_without_nfc_or_dashboard(self, _geteuid):
        result = complete(
            request_path=self.request,
            enabled_path=self.enabled,
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            run=self.command_runner,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result, {"has_cards": False})
        commands = [call[0] for call in self.calls]
        self.assertIn(["systemctl", "disable", "messagebox-nfc.service"], commands)
        self.assertIn(["systemctl", "disable", "messagebox-dash.service"], commands)
        self.assertIn(["systemctl", "start", "messagebox.target"], commands)
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.request.exists())

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_any_mapping_enables_fail_closed_nfc_runtime(self, _geteuid):
        self.contacts.assign_card(PERSON, CARD_A)
        complete(
            request_path=self.request,
            enabled_path=self.enabled,
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            run=self.command_runner,
            sleep=lambda _seconds: None,
        )
        self.assertIn(
            ["systemctl", "enable", "messagebox-nfc.service"],
            [call[0] for call in self.calls],
        )

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_failed_runtime_start_restores_onboarding_gate(self, _geteuid):
        def failing_run(command, check=False):
            self.calls.append((command, check))
            if command == ["systemctl", "start", "messagebox.target"]:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaises(subprocess.CalledProcessError):
            complete(
                request_path=self.request,
                enabled_path=self.enabled,
                contacts_path=self.contacts_path,
                recipients=self.recipients,
                run=failing_run,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(self.enabled.read_text(encoding="ascii"), "enabled\n")
        self.assertTrue(self.request.exists())
        self.assertIn(
            ["systemctl", "start", "comitup.service"],
            [call[0] for call in self.calls],
        )

    def test_request_is_private_and_content_free(self):
        self.assertEqual(self.request.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(self.request.read_text(encoding="utf-8")),
            {"version": 1, "complete": True},
        )


if __name__ == "__main__":
    unittest.main()
