import json
import os
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
        self.uid = os.getuid()
        self.lock = self.root / "mode.lock"
        self.pending = self.root / "reconcile.pending"

    def tearDown(self):
        self.directory.cleanup()

    def command_runner(self, command, check=False):
        self.calls.append((command, check))
        return subprocess.CompletedProcess(command, 0)

    def complete(self, **kwargs):
        return complete(
            lock_path=self.lock,
            pending_path=self.pending,
            marker_uid=self.uid,
            lock_uid=self.uid,
            **kwargs,
        )

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_zero_cards_enables_runtime_dashboard_without_nfc(self, _geteuid):
        result = self.complete(
            request_path=self.request,
            enabled_path=self.enabled,
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            run=self.command_runner,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result, {"has_cards": False})
        commands = [call[0] for call in self.calls]
        self.assertEqual(
            commands[0],
            ["systemctl", "start", "--no-block", "messagebox-mode-reconcile.service"],
        )
        self.assertIn(["systemctl", "enable", "messagebox-nfc.service"], commands)
        self.assertIn(
            [
                "systemctl",
                "enable",
                "messagebox-button.service",
                "messagebox-sync.service",
                "messagebox-poller.service",
                "messagebox-dash.service",
            ],
            commands,
        )
        self.assertNotIn(
            ["systemctl", "enable", "messagebox.target"],
            commands,
        )
        self.assertIn(["systemctl", "start", "messagebox.target"], commands)
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.request.exists())

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_handoff_restores_hostname_after_setup_publisher_exits(self, _geteuid):
        hostname_available = True
        setup_running = True

        def discovery_runner(command, check=False):
            nonlocal hostname_available, setup_running
            self.calls.append((command, check))
            if command == [
                "systemctl",
                "stop",
                "messagebox-onboarding-voice.target",
                "messagebox-onboarding-nfc.service",
            ]:
                self.assertTrue(self.enabled.exists())
            elif command == ["systemctl", "daemon-reload"]:
                self.assertFalse(self.enabled.exists())
            if command == ["systemctl", "stop", "comitup.service"]:
                setup_running = False
                hostname_available = False
            elif command == ["systemctl", "restart", "avahi-daemon.service"]:
                self.assertFalse(setup_running)
                self.assertTrue(check)
                hostname_available = True
            elif command == ["systemctl", "start", "messagebox.target"]:
                self.assertTrue(hostname_available, "dashboard would lose its local URL")
            return subprocess.CompletedProcess(command, 0)

        self.complete(
            request_path=self.request,
            enabled_path=self.enabled,
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            run=discovery_runner,
            sleep=lambda _seconds: None,
        )
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.request.exists())

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_failed_hostname_restart_preserves_setup_for_retry(self, _geteuid):
        self.contacts.assign_card(PERSON, CARD_A)
        original_contacts = self.contacts_path.read_bytes()

        def failing_run(command, check=False):
            self.calls.append((command, check))
            if command == ["systemctl", "restart", "avahi-daemon.service"]:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaises(subprocess.CalledProcessError):
            self.complete(
                request_path=self.request,
                enabled_path=self.enabled,
                contacts_path=self.contacts_path,
                recipients=self.recipients,
                run=failing_run,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(self.enabled.read_text(encoding="ascii"), "enabled\n")
        self.assertEqual(self.enabled.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.request.exists())
        self.assertEqual(self.contacts_path.read_bytes(), original_contacts)
        commands = [call[0] for call in self.calls]
        self.assertNotIn(["systemctl", "start", "messagebox.target"], commands)
        self.assertEqual(commands[-2:], [
            ["systemctl", "stop", "messagebox.target"],
            ["systemctl", "start", "comitup.service"],
        ])

        result = self.complete(
            request_path=self.request,
            enabled_path=self.enabled,
            contacts_path=self.contacts_path,
            recipients=self.recipients,
            run=self.command_runner,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result, {"has_cards": True})
        self.assertEqual(self.contacts_path.read_bytes(), original_contacts)
        self.assertFalse(self.enabled.exists())
        self.assertFalse(self.request.exists())

    @mock.patch("messagebox.onboarding.completion.os.geteuid", return_value=0)
    def test_any_mapping_enables_fail_closed_nfc_runtime(self, _geteuid):
        self.contacts.assign_card(PERSON, CARD_A)
        self.complete(
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
            self.complete(
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
