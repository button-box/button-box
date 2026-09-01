import subprocess
import tempfile
import unittest
from pathlib import Path

from messagebox.wifi_change import PROOFS, execute_change, load_status, request_change


class Checker:
    def __init__(self, *results):
        self.results = iter(results)

    def check(self):
        return next(self.results)


class Runner:
    def __init__(self, uuids):
        self.uuids = iter(uuids)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout = ""
        if "GENERAL.CON-UUID" in command:
            stdout = next(self.uuids) + "\n"
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
        outcome = execute_change(
            request_path=self.request,
            status_path=self.status,
            runner=runner,
            checker=Checker({"ok": True, "proof": sorted(PROOFS)}),
            clock=lambda: 1000,
        )
        self.assertEqual(outcome, "connected")
        commands = [command for command, _kwargs in runner.calls]
        self.assertNotIn("private-password", " ".join(" ".join(command) for command in commands))
        connect = next(call for call in runner.calls if "connect" in call[0])
        self.assertEqual(connect[1]["input"], "private-password\n")
        self.assertEqual(commands[-1], ["nmcli", "connection", "delete", "uuid", "1111-aaaa"])
        self.assertEqual(load_status(self.status)["status"], "connected")
        self.assertFalse(self.request.exists())

    def test_failed_candidate_rolls_back_old_profile_without_hotspot(self):
        self.submit()
        runner = Runner(["1111-aaaa", "2222-bbbb"])
        fallback_calls = []
        outcome = execute_change(
            request_path=self.request,
            status_path=self.status,
            runner=runner,
            checker=Checker(
                {"ok": False, "proof": []},
                {"ok": True, "proof": sorted(PROOFS)},
            ),
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

    def test_failed_candidate_and_rollback_reopen_hotspot_without_enabling_units(self):
        self.submit()
        runner = Runner(["1111-aaaa", "2222-bbbb"])
        fallback_calls = []
        outcome = execute_change(
            request_path=self.request,
            status_path=self.status,
            runner=runner,
            checker=Checker(
                {"ok": False, "proof": []},
                {"ok": False, "proof": []},
            ),
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
