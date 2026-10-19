"""messageboxctl doctor and claim-link (messagebox/self_install.py)."""

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox import self_install
from messagebox.cloud_device import CloudDeviceError, CloudRateLimited
from messagebox.onboarding.state import WIFI_CONNECTING, StateStore


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class FakeRun:
    """Answer commands by their first words; record every command."""

    def __init__(self, answers=None, default=None):
        self.answers = answers or {}
        self.default = default or completed()
        self.commands = []

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        text = " ".join(command)
        for prefix, answer in self.answers.items():
            if text.startswith(prefix):
                return answer(command) if callable(answer) else answer
        return self.default


class SetupUrlTests(unittest.TestCase):
    API = "https://button.box/cloud-api/v1"

    def test_cloud_link_is_used_when_it_matches_the_code(self):
        result = {"setup_code": "AB34CD", "setup_url": "https://button.box/s/AB34CD"}
        self.assertEqual(self_install.setup_url(result, self.API), "https://button.box/s/AB34CD")

    def test_link_is_built_from_the_api_host_when_missing(self):
        self.assertEqual(self_install.setup_url({"setup_code": "AB34CD"}, self.API), "https://button.box/s/AB34CD")

    def test_unsafe_or_mismatched_links_are_refused(self):
        for result in (
            {},
            {"setup_code": "a b"},
            {"setup_code": "AB34CD", "setup_url": "http://button.box/s/AB34CD"},
            {"setup_code": "AB34CD", "setup_url": "https://button.box/s/OTHER1"},
            {"setup_code": "AB34CD", "setup_url": "https://button.box/s/AB34CD?next=x"},
            {"setup_code": "AB34CD", "setup_url": "https://user@button.box/s/AB34CD"},
            {"setup_code": "AB34CD", "setup_url": 7},
        ):
            with self.subTest(result=result), self.assertRaises(ValueError):
                self_install.setup_url(result, self.API)


class FakeClient:
    def __init__(self, register=None, checkin=None, error=None):
        self.register_reply = register or {"claimed": False, "claim_id": "x" * 16}
        self.checkin_reply = checkin or {"claimed": False, "setup_code": "AB34CD",
                                         "setup_url": "https://button.box/s/AB34CD", "pending_claim": None}
        self.error = error
        self.registered = None

    def register(self, capabilities):
        if self.error:
            raise self.error
        self.registered = capabilities
        return self.register_reply

    def setup_checkin(self):
        return self.checkin_reply


class CloudRegisterTests(unittest.TestCase):
    def register(self, client):
        out = io.StringIO()
        status = self_install.cloud_register({}, client=client, out=out)
        return status, json.loads(out.getvalue())

    def test_unclaimed_box_gets_its_setup_link(self):
        client = FakeClient()
        self.assertEqual(self.register(client), (0, {"claimed": False, "setup_url": "https://button.box/s/AB34CD"}))
        self.assertIs(client.registered["natural_registration_text"], True)
        self.assertIn("color", client.registered)

    def test_claimed_box_reports_only_that(self):
        self.assertEqual(self.register(FakeClient(register={"claimed": True})), (0, {"claimed": True}))
        self.assertEqual(self.register(FakeClient(checkin={"claimed": True})), (0, {"claimed": True}))

    def test_errors_never_include_server_data(self):
        self.assertEqual(self.register(FakeClient(error=CloudDeviceError("x"))), (1, {"error": "cloud_unavailable"}))
        self.assertEqual(self.register(FakeClient(error=CloudRateLimited("x", 5))), (1, {"error": "rate_limited"}))
        bad = FakeClient(checkin={"claimed": False, "setup_code": "AB34CD", "setup_url": "https://evil.example/x"})
        self.assertEqual(self.register(bad), (1, {"error": "cloud_unavailable"}))


class ClaimLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / "env"
        self.env.write_text("MSGBOX_TRANSPORT=cloud\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def claim(self, reply, **kwargs):
        run = FakeRun({"systemd-run": completed(json.dumps(reply) + "\n")}, default=completed("", 1))
        out = io.StringIO()
        status = self_install.claim_link(out, run=run, env_path=self.env, **kwargs)
        return status, out.getvalue(), run

    def test_link_and_qr_are_printed_and_registration_runs_as_the_service_user(self):
        status, output, run = self.claim({"claimed": False, "setup_url": "https://button.box/s/AB34CD"}, arm=False)
        self.assertEqual(status, 0)
        self.assertIn("Or open: https://button.box/s/AB34CD", output)
        self.assertIn("█", output)  # the bundled QR encoder (qrencode answered nothing)
        systemd_run = next(command for command in run.commands if command[0] == "systemd-run")
        self.assertIn("User=messagebox", systemd_run)
        self.assertIn("SupplementaryGroups=messagebox-settings", systemd_run)
        self.assertIn("EnvironmentFile=-/etc/messagebox/env", systemd_run)
        self.assertEqual(systemd_run[-3:], ["-m", "messagebox.self_install", "cloud-register"])

    def test_unattended_install_prints_the_link_and_the_next_command(self):
        with mock.patch.object(self_install, "ONBOARDING_CONFIGURED", self.tmp / "configured"):
            status, output, run = self.claim({"claimed": False, "setup_url": "https://button.box/s/AB34CD"},
                                             tty_opener=lambda: None)
        self.assertEqual(status, 0)
        self.assertIn("https://button.box/s/AB34CD", output)
        self.assertIn("sudo messageboxctl claim-link", output)
        self.assertFalse(any("enter-cloud-claim" in " ".join(command) for command in run.commands))

    def test_claimed_and_failed_registrations(self):
        status, output, _ = self.claim({"claimed": True})
        self.assertEqual(status, 0)
        self.assertIn("already set up", output)
        status, output, _ = self.claim({"error": "cloud_unavailable"})
        self.assertEqual(status, 1)
        self.assertIn("Fix:", output)

    def test_self_hosted_box_has_no_link(self):
        self.env.write_text("MSGBOX_TRANSPORT=wacli\n")
        status, output, run = self.claim({})
        self.assertEqual(status, 1)
        self.assertIn("self-hosted", output)
        self.assertEqual(run.commands, [])

    def test_configured_box_enters_claim_setup(self):
        configured = self.tmp / "configured"
        configured.write_text("configured\n")
        run = FakeRun({"systemd-run": completed(json.dumps({"claimed": False,
                                                            "setup_url": "https://button.box/s/AB34CD"}))})
        with mock.patch.object(self_install, "ONBOARDING_CONFIGURED", configured), \
                mock.patch.object(self_install, "adopt_home_wifi", return_value=True):
            status = self_install.claim_link(io.StringIO(), run=run, env_path=self.env)
        self.assertEqual(status, 0)
        self.assertIn(["/usr/bin/python3", "-m", "messagebox.onboarding.mode", "--enter-cloud-claim"], run.commands)

    def test_failed_wifi_adoption_does_not_enter_setup(self):
        configured = self.tmp / "configured"
        configured.write_text("configured\n")
        run = FakeRun()
        with mock.patch.object(self_install, "ONBOARDING_CONFIGURED", configured), \
                mock.patch.object(self_install, "adopt_home_wifi", return_value=False):
            self.assertEqual(self_install.arm_claim_setup(io.StringIO(), run=run), (False, "setup_state"))
        self.assertEqual(run.commands, [])


class AdoptWifiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state.json"
        StateStore(self.state).initialize()
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        patcher = mock.patch.object(self_install.pwd, "getpwnam", return_value=account)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_connected_wifi_becomes_the_chosen_network(self):
        run = FakeRun({
            "nmcli -g GENERAL.STATE": completed("100 (connected)\nhome-profile\n"),
            "nmcli -g 802-11-wireless.ssid": completed("Home Net\n"),
        })
        self.assertIs(self_install.adopt_home_wifi(run, self.state), True)
        state = StateStore(self.state).load()
        self.assertEqual(state["phase"], WIFI_CONNECTING)
        self.assertEqual(state["dispatched_generation"], state["generation"])
        self.assertNotIn("Home Net", self.state.read_text())  # the SSID is never stored
        # Running again keeps the state.
        self.assertIs(self_install.adopt_home_wifi(run, self.state), True)

    def test_no_wifi_keeps_the_setup_hotspot_path(self):
        run = FakeRun({"nmcli": completed("30 (disconnected)\n\n")})
        self.assertIsNone(self_install.adopt_home_wifi(run, self.state))
        self.assertEqual(StateStore(self.state).load()["phase"], "WIFI_SELECT")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def report(self):
        out = io.StringIO()
        return self_install.Report(out), out

    def test_services(self):
        states = {"messagebox-button.service": ("active", "enabled"), "messagebox-poller.service": ("failed", "enabled")}

        def systemctl(command):
            if command[1] == "list-units":
                return completed("messagebox-poller.service loaded failed failed Poller\n")
            active, enabled = states.get(command[2], ("inactive", "disabled"))
            return completed(active if command[1] == "is-active" else enabled)

        report, out = self.report()
        self_install.check_services(report, "cloud", run=FakeRun({"systemctl": systemctl}),
                                    enabled_marker=self.tmp / "enabled")
        self.assertIn("FAIL  services: failed: messagebox-poller.service", out.getvalue())
        self.assertIn("FAIL  services: selected but not running: poller", out.getvalue())
        self.assertIn("Fix:", out.getvalue())

        report, out = self.report()
        self_install.check_services(report, "cloud", run=FakeRun({"systemctl": completed("")}),
                                    enabled_marker=self.tmp / "enabled")
        self.assertIn("WARN  services: Button Box is installed but not started yet", out.getvalue())
        self.assertIn("claim-link", out.getvalue())

    def test_setup_mode_services(self):
        marker = self.tmp / "enabled"
        marker.write_text("")
        report, out = self.report()
        self_install.check_services(report, "cloud", run=FakeRun({"systemctl is-active": completed("active")}),
                                    enabled_marker=marker)
        self.assertIn("PASS  services: setup mode is running", out.getvalue())

    def test_permissions(self):
        report, out = self.report()
        self_install.check_permissions(report, run=FakeRun({"runuser": completed("[]")}), app_dir=self.tmp)
        self.assertIn("PASS  permissions", out.getvalue())
        report, out = self.report()
        self_install.check_permissions(report, run=FakeRun({"runuser": completed('["/opt/messagebox/x.py"]')}),
                                       app_dir=self.tmp)
        self.assertIn("FAIL  permissions: 1 path(s) not readable by messagebox, e.g. /opt/messagebox/x.py", out.getvalue())

    def test_audio(self):
        module = SimpleNamespace(detect_speaker=lambda: ("Device", "plughw:CARD=Device"),
                                 detect_microphone=mock.Mock(side_effect=ValueError("none")))
        report, out = self.report()
        self_install.check_audio(report, loader=lambda: module)
        self.assertIn("PASS  audio: speaker found (card Device)", out.getvalue())
        self.assertIn("FAIL  audio: no microphone found", out.getvalue())

    def test_nfc_is_a_warning_only(self):
        device = self.tmp / "i2c-1"
        report, out = self.report()
        self_install.check_nfc(report, device=device)
        self.assertIn("WARN  nfc", out.getvalue())
        device.write_text("")
        table = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\n20: -- -- -- -- 24 -- -- -- \n"
        report, out = self.report()
        self_install.check_nfc(report, run=FakeRun({"i2cdetect": completed(table)}), device=device)
        self.assertIn("PASS  nfc", out.getvalue())
        report, out = self.report()
        self_install.check_nfc(report, run=FakeRun({"i2cdetect": completed(table.replace("24", "--"))}), device=device)
        self.assertIn("WARN  nfc: no card reader", out.getvalue())
        self.assertEqual(report.counts[self_install.FAIL], 0)

    def test_cloud_reachability(self):
        def http_error(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://button.box/", 404, "Not Found", {}, io.BytesIO(b""))

        def offline(*_args, **_kwargs):
            raise urllib.error.URLError("no route")

        report, out = self.report()
        self_install.check_cloud(report, {}, opener=http_error)
        self.assertIn("PASS  cloud: button.box is reachable", out.getvalue())
        report, out = self.report()
        self_install.check_cloud(report, {}, opener=offline)
        self.assertIn("FAIL  cloud: button.box is not reachable", out.getvalue())

    def test_updater(self):
        app = self.tmp / "messagebox"
        app.symlink_to("/opt/messagebox-releases/v0.3.13-" + "a" * 40)
        marker = self.tmp / "installed"
        marker.write_text("")
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"mode": "ask"}))
        run = FakeRun({"systemctl is-active": completed("active"), "systemctl is-enabled": completed("enabled")})
        report, out = self.report()
        self_install.check_updater(report, run=run, app_dir=app, marker=marker, policy=policy)
        self.assertIn("PASS  updates: signed updater is on (ask), running v0.3.13", out.getvalue())
        marker.unlink()
        report, out = self.report()
        self_install.check_updater(report, run=run, app_dir=app, marker=marker, policy=policy)
        self.assertIn("FAIL  updates: signed updater: not bootstrapped", out.getvalue())

    def test_doctor_exit_status_and_self_hosted_lines(self):
        env = self.tmp / "env"
        env.write_text("MSGBOX_TRANSPORT=wacli\n")
        module = SimpleNamespace(detect_speaker=lambda: ("Device", "x"), detect_microphone=lambda: ("mic", "y"))
        run = FakeRun({"runuser": completed("[]"), "/usr/local/bin/wacli": completed("wacli 0.17.1\n")})
        out = io.StringIO()
        with mock.patch.object(self_install, "I2C_DEVICE", self.tmp / "missing"):
            status = self_install.doctor(out, env_path=env, run=run, audio_loader=lambda: module)
        self.assertEqual(status, 0, out.getvalue())
        self.assertIn("self-hosted box", out.getvalue())
        self.assertIn("PASS  wacli: wacli 0.17.1 is installed", out.getvalue())
        self.assertIn("Result: 0 failed", out.getvalue())


class QrTests(unittest.TestCase):
    def test_bundled_encoder_draws_a_square(self):
        with mock.patch.object(self_install.shutil, "which", return_value=None):
            text = self_install.qr_text("https://button.box/s/AB34CD")
        rows = text.splitlines()
        self.assertGreater(len(rows), 10)
        self.assertEqual(len({len(row) for row in rows}), 1)


if __name__ == "__main__":
    unittest.main()
