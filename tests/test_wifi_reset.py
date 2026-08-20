import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.onboarding import reset
from src.onboarding.state import StateStore


class Result:
    def __init__(self, stdout=""):
        self.stdout = stdout


class Runner:
    def __init__(self, profiles="", modes=None):
        self.profiles = profiles
        self.modes = modes or {}
        self.calls = []

    def __call__(self, arguments, **kwargs):
        arguments = list(arguments)
        self.calls.append((arguments, kwargs))
        if arguments[:5] == [
            "nmcli",
            "--terse",
            "--fields",
            "UUID,TYPE",
            "connection",
        ]:
            return Result(self.profiles)
        if arguments[:3] == ["nmcli", "--get-values", "802-11-wireless.mode"]:
            return Result(self.modes[arguments[-1]])
        return Result()


class Button:
    def __init__(self, values):
        self.values = iter(values)
        self.last = False

    @property
    def is_pressed(self):
        self.last = next(self.values, self.last)
        return self.last


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class WifiResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.enabled = self.root / "etc/messagebox-onboarding/enabled"
        self.configured = self.root / "etc/messagebox-onboarding/configured"
        self.state = self.root / "var/lib/messagebox-onboarding/state.json"

    def tearDown(self):
        self.directory.cleanup()

    def initialize_state(self):
        return StateStore(self.state, clock=lambda: 100.0).initialize()

    def run_held(self, runner):
        self.configured.parent.mkdir(parents=True, exist_ok=True)
        self.configured.write_text("configured\n", encoding="ascii")
        clock = Clock()
        return reset.reset_if_held(
            Button([True]),
            clock=clock,
            sleeper=clock.sleep,
            runner=runner,
            enabled_path=self.enabled,
            configured_path=self.configured,
            state_path=self.state,
            state_clock=lambda: 200.0,
        )

    def test_not_initially_pressed_exits_without_waiting_or_side_effects(self):
        clock = Clock()
        runner = Runner()
        status = reset.reset_if_held(
            Button([False]),
            clock=clock,
            sleeper=clock.sleep,
            runner=runner,
            enabled_path=self.enabled,
            state_path=self.state,
        )
        self.assertIs(status, reset.ResetStatus.NOT_PRESSED)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.enabled.exists())

    def test_release_before_ten_seconds_cancels_safely(self):
        clock = Clock()
        runner = Runner()
        status = reset.reset_if_held(
            Button([True, True, True, False]),
            clock=clock,
            sleeper=clock.sleep,
            runner=runner,
            enabled_path=self.enabled,
            state_path=self.state,
        )
        self.assertIs(status, reset.ResetStatus.RELEASED_EARLY)
        self.assertLess(clock.now, reset.HOLD_SECONDS)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.enabled.exists())

    def test_profile_filter_deletes_only_wifi_infrastructure(self):
        runner = Runner(
            profiles=(
                "home-a:802-11-wireless\n"
                "home-b:wifi\n"
                "hotspot:wifi\n"
                "adhoc:wifi\n"
                "wired:802-3-ethernet\n"
                "vpn:vpn\n"
            ),
            modes={
                "home-a": "infrastructure\n",
                "home-b": "\n",
                "hotspot": "ap\n",
                "adhoc": "adhoc\n",
            },
        )
        profiles = reset.list_infrastructure_wifi_profiles(runner)
        self.assertEqual(profiles, ["home-a", "home-b"])
        queried = [call[0][-1] for call in runner.calls[1:]]
        self.assertEqual(queried, ["home-a", "home-b", "hotspot", "adhoc"])

    def test_reset_updates_private_state_and_restarts_hotspot(self):
        store = StateStore(self.state, clock=lambda: 100.0)
        store.initialize()
        store.begin_connect("Private home SSID")
        runner = Runner(
            profiles="home:wifi\nhotspot:wifi\n",
            modes={"home": "infrastructure\n", "hotspot": "ap\n"},
        )
        self.run_held(runner)

        state = StateStore(self.state).load()
        self.assertEqual(state["phase"], "WIFI_SELECT")
        self.assertEqual(state["proofs"], [])
        self.assertEqual(self.enabled.read_bytes(), b"enabled\n")
        commands = [call[0] for call in runner.calls]
        self.assertIn(["systemctl", "enable", *reset.SETUP_UNITS], commands)
        self.assertIn(["systemctl", "stop", *reset.RUNTIME_UNITS], commands)
        self.assertIn(["systemctl", "stop", *reset.ONBOARDING_UNITS], commands)
        self.assertIn(["nmcli", "connection", "delete", "uuid", "home"], commands)
        self.assertNotIn(["nmcli", "connection", "delete", "uuid", "hotspot"], commands)
        self.assertEqual(
            commands[-1], ["systemctl", "--no-block", "start", *reset.HOTSPOT_UNITS]
        )

    def test_confirmed_reset_is_idempotent(self):
        self.initialize_state()
        runner = Runner()
        self.run_held(runner)
        self.run_held(runner)

        self.assertEqual(self.enabled.read_bytes(), b"enabled\n")
        self.assertEqual(StateStore(self.state).load()["phase"], "WIFI_SELECT")
        self.assertEqual(
            sum(
                call[0] == ["systemctl", "--no-block", "start", *reset.HOTSPOT_UNITS]
                for call in runner.calls
            ),
            2,
        )

    def test_reset_recovers_a_corrupt_state_file(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("not-json", encoding="utf-8")
        self.run_held(Runner())
        self.assertEqual(StateStore(self.state).load()["phase"], "WIFI_SELECT")
        self.assertEqual(self.state.stat().st_uid, self.state.parent.stat().st_uid)

    def test_reset_never_touches_contact_or_runtime_data_paths(self):
        contact_dir = self.root / "etc/messagebox"
        runtime_dir = self.root / "var/lib/messagebox"
        contact_dir.mkdir(parents=True)
        runtime_dir.mkdir(parents=True)
        contact = contact_dir / "env"
        runtime = runtime_dir / "state.json"
        contact.write_bytes(b"contact configuration")
        runtime.write_bytes(b"runtime data")
        self.initialize_state()

        self.run_held(Runner())

        self.assertEqual(contact.read_bytes(), b"contact configuration")
        self.assertEqual(runtime.read_bytes(), b"runtime data")

    def test_main_requires_root_before_loading_gpio(self):
        error = io.StringIO()
        with mock.patch("src.onboarding.reset._gpio_button") as gpio:
            code = reset.main(geteuid=lambda: 1000, stderr=error)
        self.assertEqual(code, reset.EXIT_NOT_ROOT)
        gpio.assert_not_called()
        self.assertIn("root", error.getvalue())

    def test_main_reports_failures_without_command_or_profile_details(self):
        self.configured.parent.mkdir(parents=True, exist_ok=True)
        self.configured.write_text("configured\n", encoding="ascii")
        self.state.parent.mkdir(parents=True, exist_ok=True)

        def failed_runner(arguments, **kwargs):
            raise subprocess.CalledProcessError(1, arguments, stderr="secret profile")

        clock = Clock()
        error = io.StringIO()
        code = reset.main(
            button=Button([True]),
            clock=clock,
            sleeper=clock.sleep,
            runner=failed_runner,
            enabled_path=self.enabled,
            configured_path=self.configured,
            state_path=self.state,
            geteuid=lambda: 0,
            stderr=error,
        )
        self.assertEqual(code, reset.EXIT_FAILED)
        self.assertIn("failed", error.getvalue())
        self.assertNotIn("secret", error.getvalue())


if __name__ == "__main__":
    unittest.main()
