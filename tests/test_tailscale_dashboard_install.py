import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tailscale_dashboard_install",
    ROOT / "scripts" / "install" / "tailscale_dashboard.py",
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


HOSTNAME = "button-box-a7.example-tailnet.ts.net"


class FakeRun:
    def __init__(self, *, serve_ready=False, conflict=False, fail_restart=False):
        self.serve_ready = serve_ready
        self.conflict = conflict
        self.fail_restart = fail_restart
        self.calls = []

    def __call__(self, command, *, check=True):
        self.calls.append((tuple(command), check))
        if command == ["tailscale", "status", "--json"]:
            payload = {"Self": {"DNSName": HOSTNAME + "."}}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command == ["tailscale", "serve", "status", "--json"]:
            payload = {}
            if self.serve_ready or self.conflict:
                proxy = "http://127.0.0.1:9999" if self.conflict else installer.BACKEND
                payload = {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {f"{HOSTNAME}:443": {"Handlers": {"/": {"Proxy": proxy}}}},
                    "AllowFunnel": {f"{HOSTNAME}:443": False},
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command == [
            "tailscale",
            "serve",
            "--bg",
            "--https=443",
            installer.BACKEND,
        ]:
            self.serve_ready = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command == ["tailscale", "serve", "--https=443", "off"]:
            self.serve_ready = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "try-restart"]:
            if self.fail_restart and check:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)


class TailscaleDashboardInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = self.root / "env"
        self.env.write_text("MSGBOX_DASH_BIND=wlan0\n", encoding="utf-8")
        self.app = self.root / "app"
        marker = self.app / "messagebox" / "tailnet.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# installed\n", encoding="utf-8")

    def test_configures_private_serve_and_device_local_environment(self):
        run = FakeRun()

        url = installer.provision("button-box-a7", self.env, self.app, run=run)

        self.assertEqual(url, f"https://{HOSTNAME}/")
        self.assertIn(
            f"{installer.ENV_KEY}={HOSTNAME}",
            self.env.read_text(encoding="utf-8"),
        )
        commands = [call[0] for call in run.calls]
        self.assertIn(
            ("tailscale", "serve", "--bg", "--https=443", installer.BACKEND),
            commands,
        )
        self.assertTrue(any(command[:2] == ("systemctl", "try-restart") for command in commands))
        self.assertFalse(any("funnel" in part.lower() for command in commands for part in command))

    def test_rerun_is_idempotent(self):
        self.env.write_text(
            f"MSGBOX_DASH_BIND=wlan0\n{installer.ENV_KEY}={HOSTNAME}\n",
            encoding="utf-8",
        )
        run = FakeRun(serve_ready=True)

        installer.provision("button-box-a7", self.env, self.app, run=run)

        commands = [call[0] for call in run.calls]
        self.assertFalse(any(command[:2] == ("tailscale", "serve") and "--bg" in command for command in commands))
        self.assertFalse(any(command[:2] == ("systemctl", "try-restart") for command in commands))

    def test_conflicting_443_mapping_is_preserved_and_fails_closed(self):
        original = self.env.read_bytes()
        run = FakeRun(conflict=True)

        with self.assertRaisesRegex(installer.ProvisionError, "conflicting"):
            installer.provision("button-box-a7", self.env, self.app, run=run)

        self.assertEqual(self.env.read_bytes(), original)
        commands = [call[0] for call in run.calls]
        self.assertFalse(any("--bg" in command for command in commands))
        self.assertFalse(any(command[-1:] == ("off",) for command in commands))

    def test_unrelated_serve_mapping_does_not_block_https_443(self):
        payload = {
            "TCP": {"8443": {"HTTPS": True}},
            "Web": {
                f"{HOSTNAME}:8443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}
                }
            },
        }
        self.assertEqual(installer.serve_443_state(payload, HOSTNAME), "absent")

    def test_restart_failure_rolls_back_only_new_mapping_and_environment(self):
        original = self.env.read_bytes()
        run = FakeRun(fail_restart=True)

        with self.assertRaisesRegex(installer.ProvisionError, "could not enable"):
            installer.provision("button-box-a7", self.env, self.app, run=run)

        self.assertEqual(self.env.read_bytes(), original)
        commands = [call[0] for call in run.calls]
        self.assertIn(("tailscale", "serve", "--https=443", "off"), commands)
        self.assertFalse(any("reset" in command for command in commands))

    def test_dns_name_must_match_device(self):
        run = FakeRun()
        with self.assertRaisesRegex(installer.ProvisionError, "does not match"):
            installer.provision("button-box-other", self.env, self.app, run=run)
        self.assertNotIn(installer.ENV_KEY, self.env.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
