import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.onboarding.connectivity import ConnectivityChecker
from messagebox.wifi_change import execute_change, load_status, request_change


class Runner:
    def __init__(self, uuids, https_codes=("204",)):
        self.uuids = iter(uuids)
        self.https_codes = iter(https_codes)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout = ""
        if "GENERAL.CON-UUID" in command:
            stdout = next(self.uuids) + "\n"
        elif "GENERAL.STATE,GENERAL.TYPE" in command:
            stdout = "GENERAL.STATE:100 (connected)\nGENERAL.TYPE:wifi\n"
        elif command[0] == "iw":
            stdout = "Interface wlan0\n\ttype managed\n"
        elif command[:3] == ["ip", "-4", "-o"]:
            stdout = "2: wlan0 inet 192.0.2.8/24 scope global wlan0\n"
        elif command[0] == "ip":
            stdout = "default via 192.0.2.1 dev wlan0\n"
        elif command[0] == "getent":
            stdout = "192.0.2.10 STREAM connectivitycheck.gstatic.com\n"
        elif command[0] == "curl":
            stdout = next(self.https_codes)
        return subprocess.CompletedProcess(command, 0, stdout, "")


class WifiChangeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.request = self.root / "request.json"
        self.status = self.root / "status.json"

    def tearDown(self):
        self.directory.cleanup()

    def submit(self):
        request_change(
            {"ssid": "New home", "password": "private-password", "security": "protected"},
            self.request,
        )

    def test_success_keeps_password_out_of_arguments_and_deletes_old_only_after_proof(self):
        self.submit()
        runner = Runner(["1111-aaaa", "2222-bbbb"])
        fallback = mock.Mock()
        outcome = execute_change(
            request_path=self.request,
            status_path=self.status,
            runner=runner,
            checker=ConnectivityChecker(command_runner=runner, attempts=1),
            fallback=fallback,
            clock=lambda: 1000,
        )
        self.assertEqual(outcome, "connected")
        fallback.assert_not_called()
        commands = [command for command, _kwargs in runner.calls]
        self.assertNotIn("private-password", " ".join(" ".join(command) for command in commands))
        connect = next(call for call in runner.calls if "connect" in call[0])
        self.assertEqual(connect[1]["input"], "private-password\n")
        self.assertEqual(commands[-1], ["nmcli", "connection", "delete", "uuid", "1111-aaaa"])
        self.assertEqual(commands[-2][0], "curl")
        self.assertEqual(load_status(self.status)["status"], "connected")
        self.assertFalse(self.request.exists())

    def test_failed_candidate_rolls_back_old_profile_without_hotspot(self):
        self.submit()
        runner = Runner(["1111-aaaa", "2222-bbbb"], https_codes=("200", "204"))
        fallback_calls = []
        outcome = execute_change(
            request_path=self.request,
            status_path=self.status,
            runner=runner,
            checker=ConnectivityChecker(command_runner=runner, attempts=1),
            fallback=lambda **kwargs: fallback_calls.append(kwargs),
            clock=lambda: 1000,
        )
        self.assertEqual(outcome, "rolled_back")
        self.assertEqual(fallback_calls, [])
        commands = [command for command, _kwargs in runner.calls]
        self.assertIn(
            ["nmcli", "connection", "up", "uuid", "1111-aaaa", "ifname", "wlan0"],
            commands,
        )
        self.assertEqual(load_status(self.status)["status"], "rolled_back")
        self.assertNotIn(["nmcli", "connection", "delete", "uuid", "1111-aaaa"], commands)
        self.assertEqual(commands[-1], ["nmcli", "connection", "delete", "uuid", "2222-bbbb"])
        self.assertEqual(commands[-2][0], "curl")

    def test_failed_candidate_and_rollback_reopen_hotspot_without_enabling_units(self):
        self.submit()
        runner = Runner(["1111-aaaa", "2222-bbbb"], https_codes=("200", "200"))
        fallback_calls = []
        outcome = execute_change(
            request_path=self.request,
            status_path=self.status,
            runner=runner,
            checker=ConnectivityChecker(command_runner=runner, attempts=1),
            fallback=lambda **kwargs: fallback_calls.append(kwargs),
            clock=lambda: 1000,
        )
        self.assertEqual(outcome, "hotspot")
        self.assertEqual(fallback_calls, [{"runner": runner, "enable_units": False}])
        self.assertEqual(load_status(self.status)["status"], "hotspot")

    def test_service_restarts_only_an_active_dashboard_after_wifi_processing(self):
        unit = (
            Path(__file__).parents[1] / "systemd/messagebox-wifi-change.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPost=/usr/bin/systemctl try-restart messagebox-dash.service\n",
            unit,
        )
        self.assertNotIn("ExecStartPost=/usr/bin/systemctl restart ", unit)


if __name__ == "__main__":
    unittest.main()
