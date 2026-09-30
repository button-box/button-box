import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from messagebox.onboarding import mode


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "systemd/messagebox-mode-generator"


def load_generator():
    loader = importlib.machinery.SourceFileLoader("test_mode_generator", str(GENERATOR_PATH))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class Runner:
    def __init__(self, *, active=(), fail=()):
        self.active = set(active)
        self.fail = {tuple(command) for command in fail}
        self.calls = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        if tuple(command) in self.fail:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(
            command,
            0 if command[-1] in self.active else 3,
        )


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.marker = self.root / "etc/messagebox-onboarding/enabled"
        self.marker.parent.mkdir(parents=True)
        self.marker.parent.chmod(0o750)
        self.lock = self.root / "run/lock/mode.lock"
        self.lock.parent.mkdir(parents=True)
        self.pending = self.root / "run/reconcile.pending"
        self.uid = os.getuid()
        self.generator = load_generator()

    def arm(self):
        self.marker.write_bytes(b"enabled\n")
        self.marker.chmod(0o600)

    def cloud_transition_fixture(self):
        configured = self.marker.parent / "configured"
        configured.write_bytes(b"configured\n")
        configured.chmod(0o640)
        env = self.root / "etc/messagebox/env"
        env.parent.mkdir(parents=True)
        original = b"# preserve other settings\nMSGBOX_TRANSPORT=business\nMSGBOX_CLOUD_API_URL=https://example.invalid/cloud-api/v1\n"
        env.write_bytes(original)
        env.chmod(0o640)
        cloud_dir = self.root / "var/lib/messagebox-cloud"
        cloud_dir.mkdir(parents=True)
        cloud_dir.chmod(0o2770)
        household = self.root / "var/lib/messagebox/outbox/retained.job/audio.wav"
        household.parent.mkdir(parents=True)
        household.write_bytes(b"retained family recording")
        options = {"env_path": env, "configured_path": configured,
                   "enabled_path": self.marker, "cloud_dir": cloud_dir,
                   "lock_path": self.lock, "pending_path": self.pending,
                   "trusted_uid": self.uid, "env_gid": os.getgid(),
                   "service_uid": self.uid, "settings_gid": os.getgid(),
                   "required_cloud_mode": stat.S_IMODE(cloud_dir.stat().st_mode)}
        return env, original, household, options

    @mock.patch("messagebox.onboarding.mode.os.geteuid", return_value=0)
    def test_enter_cloud_claim_preserves_wifi_household_and_other_env(self, _geteuid):
        env, original, household, options = self.cloud_transition_fixture()
        runner = Runner()
        result = mode.enter_cloud_claim(**options, run=runner)
        self.assertEqual(result, mode.Mode.SETUP)
        self.assertEqual(env.read_bytes(), original.replace(b"MSGBOX_TRANSPORT=business",
                                                            b"MSGBOX_TRANSPORT=cloud"))
        self.assertEqual(self.marker.read_bytes(), b"enabled\n")
        self.assertEqual(household.read_bytes(), b"retained family recording")
        commands = [call[0] for call in runner.calls]
        self.assertIn(["systemctl", "stop", "messagebox.target"], commands)
        self.assertIn(["systemctl", "start", "comitup.service"], commands)
        self.assertFalse(any("nmcli" in command or "reset" in command or "unlink" in command
                             for command in commands))

    @mock.patch("messagebox.onboarding.mode.os.geteuid", return_value=0)
    def test_enter_cloud_claim_failure_restores_runtime_without_data_loss(self, _geteuid):
        env, original, household, options = self.cloud_transition_fixture()
        runner = Runner(fail=(["systemctl", "start", "comitup.service"],))
        with self.assertRaises(subprocess.CalledProcessError):
            mode.enter_cloud_claim(**options, run=runner)
        self.assertEqual(env.read_bytes(), original)
        self.assertFalse(self.marker.exists())
        self.assertEqual(household.read_bytes(), b"retained family recording")
        self.assertIn(["systemctl", "start", "messagebox.target"],
                      [call[0] for call in runner.calls])

    @mock.patch("messagebox.onboarding.mode.os.geteuid", return_value=0)
    def test_enter_cloud_claim_rolls_back_marker_committed_before_fsync_failure(self, _geteuid):
        env, original, household, options = self.cloud_transition_fixture()
        actual_write = mode.write_setup_marker
        def committed_then_failed(path):
            actual_write(path)
            raise OSError("directory fsync failed")
        runner = Runner()
        with mock.patch("messagebox.onboarding.mode.write_setup_marker", side_effect=committed_then_failed):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                mode.enter_cloud_claim(**options, run=runner)
        self.assertEqual(env.read_bytes(), original)
        self.assertFalse(self.marker.exists())
        self.assertEqual(household.read_bytes(), b"retained family recording")
        self.assertIn(["systemctl", "start", "messagebox.target"],
                      [call[0] for call in runner.calls])

    @mock.patch("messagebox.onboarding.mode.os.geteuid", return_value=0)
    def test_enter_cloud_claim_repeat_resumes_existing_setup(self, _geteuid):
        env, _original, _household, options = self.cloud_transition_fixture()
        mode.enter_cloud_claim(**options, run=Runner())
        before = env.read_bytes()
        runner = Runner()
        self.assertEqual(mode.enter_cloud_claim(**options, run=runner), mode.Mode.SETUP)
        self.assertEqual(env.read_bytes(), before)
        self.assertNotIn(["systemctl", "stop", "messagebox.target"],
                         [call[0] for call in runner.calls])

    @mock.patch("messagebox.onboarding.mode.os.geteuid", return_value=0)
    def test_enter_cloud_claim_rejects_symlink_env_before_mutation(self, _geteuid):
        env, _original, household, options = self.cloud_transition_fixture()
        moved = env.with_name("env-real")
        env.replace(moved)
        env.symlink_to(moved)
        runner = Runner()
        with self.assertRaises(mode.ModeError):
            mode.enter_cloud_claim(**options, run=runner)
        self.assertFalse(self.marker.exists())
        self.assertEqual(household.read_bytes(), b"retained family recording")
        self.assertEqual(runner.calls, [])

    def generated(self):
        output = self.root / "generator"
        selected = self.generator.generate(output, self.marker, trusted_uid=self.uid)
        links = {
            item.name: os.readlink(item)
            for item in (output / "multi-user.target.wants").iterdir()
        }
        return selected, links

    def test_generator_selects_exactly_one_mode(self):
        selected, links = self.generated()
        self.assertEqual(selected, "runtime")
        self.assertEqual(links, {"messagebox.target": "/etc/systemd/system/messagebox.target"})

        self.arm()
        selected, links = self.generated()
        self.assertEqual(selected, "setup")
        self.assertEqual(links, {"comitup.service": "/usr/lib/systemd/system/comitup.service"})

    def test_marker_trust_failures_select_neither_mode_without_blocking_on_fifo(self):
        cases = []
        self.marker.symlink_to("missing")
        cases.append("symlink")
        for kind in cases:
            with self.subTest(kind=kind):
                with self.assertRaises(self.generator.UnsafeMarker):
                    self.generator.generate(self.root / f"out-{kind}", self.marker, trusted_uid=self.uid)
        self.marker.unlink()
        os.mkfifo(self.marker, 0o600)
        scripts = (
            """from messagebox.onboarding.mode import read_mode, ModeError
import sys
try: read_mode(sys.argv[1], trusted_uid=int(sys.argv[2]))
except ModeError: raise SystemExit(0)
raise SystemExit(1)
""",
            f"""import importlib.machinery, importlib.util, sys
loader=importlib.machinery.SourceFileLoader('fifo_generator', {str(GENERATOR_PATH)!r})
spec=importlib.util.spec_from_loader(loader.name, loader)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
try: module.read_mode(sys.argv[1], trusted_uid=int(sys.argv[2]))
except module.UnsafeMarker: raise SystemExit(0)
raise SystemExit(1)
""",
        )
        for script in scripts:
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.marker), str(self.uid)],
                cwd=ROOT,
                timeout=1,
                check=False,
            )
            self.assertEqual(result.returncode, 0)

        self.marker.unlink()
        self.marker.write_bytes(b"wrong\n")
        self.marker.chmod(0o600)
        with self.assertRaises(self.generator.UnsafeMarker):
            self.generator.generate(self.root / "out-content", self.marker, trusted_uid=self.uid)
        self.marker.write_bytes(b"enabled\n")
        self.marker.chmod(0o640)
        with self.assertRaises(self.generator.UnsafeMarker):
            self.generator.generate(self.root / "out-mode", self.marker, trusted_uid=self.uid)

    def test_runtime_reader_matches_generator_and_rejects_nonregular(self):
        self.assertIs(mode.read_mode(self.marker, trusted_uid=self.uid), mode.Mode.RUNTIME)
        self.arm()
        self.assertIs(mode.read_mode(self.marker, trusted_uid=self.uid), mode.Mode.SETUP)
        self.marker.unlink()
        self.marker.mkdir()
        with self.assertRaises(mode.ModeError):
            mode.read_mode(self.marker, trusted_uid=self.uid)

    def test_queue_failure_removes_pending_request(self):
        command = ["systemctl", "start", "--no-block", "messagebox-mode-reconcile.service"]
        runner = Runner(fail=[command])
        with self.assertRaises(subprocess.CalledProcessError):
            mode.queue_reconcile(run=runner, pending_path=self.pending)
        self.assertFalse(self.pending.exists())

    def test_reconcile_consumes_request_and_is_noop_when_already_selected(self):
        self.pending.write_text("transition\n", encoding="ascii")
        runner = Runner(active={"messagebox.target"})
        selected = mode.reconcile(
            enabled_path=self.marker,
            lock_path=self.lock,
            pending_path=self.pending,
            run=runner,
            trusted_uid=self.uid,
        )
        self.assertIs(selected, mode.Mode.RUNTIME)
        self.assertFalse(self.pending.exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ["systemctl", "is-active", "--quiet", "comitup.service"],
                ["systemctl", "is-active", "--quiet", "messagebox.target"],
                ["systemctl", "stop", "comitup.service"],
                ["systemctl", "start", "messagebox.target"],
            ],
        )

    def test_failed_reconcile_is_bounded_and_later_request_can_retry(self):
        self.pending.write_text("transition\n", encoding="ascii")
        failing = Runner(fail=[["systemctl", "start", "messagebox.target"]])
        with self.assertRaises(subprocess.CalledProcessError):
            mode.reconcile(
                enabled_path=self.marker,
                lock_path=self.lock,
                pending_path=self.pending,
                run=failing,
                trusted_uid=self.uid,
            )
        self.assertFalse(self.pending.exists())

        mode.queue_reconcile(run=Runner(), pending_path=self.pending)
        self.assertTrue(self.pending.exists())
        selected = mode.reconcile(
            enabled_path=self.marker,
            lock_path=self.lock,
            pending_path=self.pending,
            run=Runner(),
            trusted_uid=self.uid,
        )
        self.assertIs(selected, mode.Mode.RUNTIME)
        self.assertFalse(self.pending.exists())

    def test_reconcile_does_not_block_on_unsafe_pending_fifo(self):
        os.mkfifo(self.pending, 0o600)
        script = """from messagebox.onboarding.mode import reconcile
import sys
class Runner:
    def __call__(self, command, **kwargs):
        import subprocess
        return subprocess.CompletedProcess(command, 3 if command[1] == 'is-active' else 0)
reconcile(enabled_path=sys.argv[1], lock_path=sys.argv[2], pending_path=sys.argv[3], run=Runner(), trusted_uid=int(sys.argv[4]))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.marker),
                str(self.lock),
                str(self.pending),
                str(self.uid),
            ],
            cwd=ROOT,
            timeout=1,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.pending.exists())

    def test_setup_reconcile_requeues_an_existing_voice_proof_only(self):
        self.arm()
        for active, expected_start in (({"comitup.service"}, False), ({"messagebox-onboarding-voice.target"}, True)):
            with self.subTest(active=active):
                runner = Runner(active=active)
                mode.reconcile(
                    enabled_path=self.marker,
                    lock_path=self.lock,
                    pending_path=self.pending,
                    run=runner,
                    trusted_uid=self.uid,
                )
                commands = [call[0] for call in runner.calls]
                voice_restart = [
                    "systemctl",
                    "restart",
                    "messagebox-onboarding-voice.target",
                ]
                self.assertEqual(voice_restart in commands, expected_start)

    def test_unit_contract_watches_exact_marker_and_transient_request(self):
        path_unit = (ROOT / "systemd/messagebox-mode-reconcile.path").read_text()
        service = (ROOT / "systemd/messagebox-mode-reconcile.service").read_text()
        self.assertIn("PathChanged=/etc/messagebox-onboarding/enabled", path_unit)
        self.assertIn("PathExists=/run/messagebox-mode-reconcile.pending", path_unit)
        self.assertIn("TimeoutStartSec=infinity", service)
        self.assertTrue(stat.S_IMODE(GENERATOR_PATH.stat().st_mode) & 0o111)

    def test_transition_lock_serializes_concurrent_actors(self):
        entered = threading.Event()
        finished = threading.Event()

        def contender():
            with mode.transition_lock(self.lock, trusted_uid=self.uid):
                entered.set()
            finished.set()

        with mode.transition_lock(self.lock, trusted_uid=self.uid):
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertFalse(entered.wait(0.1))
        self.assertTrue(entered.wait(1))
        self.assertTrue(finished.wait(1))
        thread.join()


if __name__ == "__main__":
    unittest.main()
