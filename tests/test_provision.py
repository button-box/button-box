import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "scripts" / "provision.sh"


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.ssh_log = self.root / "ssh.log"
        self.rsync_log = self.root / "rsync.log"
        self.prompt_dir = self.root / "guided-prompts"
        self.prompt_dir.mkdir()
        for name in (
            "reply-countdown.wav",
            "standalone-countdown.wav",
            "press-to-send.wav",
            "delete-warning.wav",
            "not-sent.wav",
        ):
            (self.prompt_dir / name).write_bytes(b"test prompt")

        self._write_executable(
            "ssh",
            """#!/bin/sh
{
  printf 'CALL'
  for arg in "$@"; do printf '\\t%s' "$arg"; done
  printf '\\n'
} >>"$SSH_LOG"
case "${2:-}" in
  'mktemp -d /tmp/messagebox-provision.XXXXXX')
    printf '%s\\n' /tmp/messagebox-provision.test
    ;;
esac
""",
        )
        self._write_executable(
            "rsync",
            """#!/bin/sh
{
  printf 'CALL'
  for arg in "$@"; do printf '\\t%s' "$arg"; done
  printf '\\n'
} >>"$RSYNC_LOG"
""",
        )

    def _write_executable(self, name, content):
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, target, *, prompt_dir=None, transport=None, provision=PROVISION, extra=()):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "SSH_LOG": str(self.ssh_log),
                "RSYNC_LOG": str(self.rsync_log),
            }
        )
        arguments = [
            str(provision),
            "--guided-prompts",
            str(prompt_dir or self.prompt_dir),
            target,
            *extra,
        ]
        if transport is not None:
            arguments += ["--transport", transport]
        return subprocess.run(
            arguments,
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_stages_only_pi_installation_inputs_and_cleans_up(self):
        result = self._run("admin@message-box.local")

        self.assertEqual(result.returncode, 0, result.stderr)
        rsync_calls = [line.split("\t") for line in self.rsync_log.read_text().splitlines()]
        self.assertEqual(len(rsync_calls), 2)
        rsync_args = rsync_calls[0][1:]
        self.assertEqual(rsync_args[0], "-azR")
        self.assertEqual(
            rsync_args[-1],
            "admin@message-box.local:/tmp/messagebox-provision.test/",
        )
        self.assertTrue(
            all(not os.path.isabs(path) for path in rsync_args[1:-1]),
            rsync_args,
        )

        staged_paths = {
            arg.split("/./", 1)[1]
            for arg in rsync_args[1:-1]
            if "/./" in arg
        }
        self.assertIn("scripts/dev/onboard.sh", staged_paths)
        self.assertIn("scripts/dev/hardware-test.sh", staged_paths)
        self.assertNotIn("scripts/dev/", staged_paths)
        self.assertIn("messagebox/onboarding/initialize.py", staged_paths)
        self.assertIn("messagebox/onboarding/nfc.py", staged_paths)
        self.assertIn("messagebox/onboarding/completion.py", staged_paths)
        self.assertIn("messagebox/settings.py", staged_paths)
        self.assertIn("messagebox/identity.py", staged_paths)
        self.assertNotIn("scripts/dev/dashboard-preview.py", staged_paths)
        self.assertIn("messagebox/wifi_change.py", staged_paths)
        self.assertNotIn("messagebox/dashboard/static/app.js", staged_paths)
        self.assertIn("messagebox/syncloop.sh", staged_paths)

        prompt_args = rsync_calls[1][1:]
        self.assertEqual(prompt_args[0], "-az")
        self.assertEqual(
            {Path(path).name for path in prompt_args[1:-1]},
            {
                "reply-countdown.wav",
                "standalone-countdown.wav",
                "press-to-send.wav",
                "delete-warning.wav",
                "not-sent.wav",
            },
        )
        self.assertEqual(
            prompt_args[-1],
            "admin@message-box.local:/tmp/messagebox-provision.test/sounds/guided-reply/",
        )

        ssh_calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ssh_calls), 4)
        self.assertEqual(
            ssh_calls[0],
            "CALL\tadmin@message-box.local\tmktemp -d /tmp/messagebox-provision.XXXXXX",
        )
        self.assertEqual(
            ssh_calls[1],
            "CALL\tadmin@message-box.local\t"
            "mkdir -p '/tmp/messagebox-provision.test/sounds/guided-reply'",
        )
        self.assertEqual(
            ssh_calls[2],
            "CALL\t-t\tadmin@message-box.local\t"
            "MESSAGEBOX_SSH_TARGET='admin@message-box.local' "
            "'/tmp/messagebox-provision.test/scripts/setup.sh'",
        )
        self.assertEqual(
            ssh_calls[3],
            "CALL\tadmin@message-box.local\trm -rf -- '/tmp/messagebox-provision.test'",
        )

    def test_forwards_cloud_and_stages_exact_release_identity(self):
        source = self.root / "release"
        source.mkdir()
        for name in ("messagebox", "systemd", "scripts", "sounds", "config"):
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "VERSION", source / "VERSION")
        manifest = subprocess.check_output([sys.executable, str(ROOT / "scripts/dev/release-manifest.py")], text=True)
        (source / "release-manifest.json").write_text(manifest)
        # Guided overrides must match the pinned release, checked again on the Pi.
        result = self._run("admin@message-box.local", transport="cloud", provision=source / "scripts/provision.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.ssh_log.read_text().splitlines()
        self.assertTrue(calls[2].endswith("setup.sh' --transport 'cloud'"))
        staged = self.rsync_log.read_text().splitlines()[0].split("\t")
        self.assertIn("./VERSION", staged)
        self.assertIn("./release-manifest.json", staged)
        self.assertIn("./scripts/dev/release-manifest.py", staged)

    def test_cloud_without_release_or_invalid_flags_never_connects(self):
        source = self.root / "developer"
        (source / "scripts").mkdir(parents=True)
        provision = source / "scripts/provision.sh"
        shutil.copyfile(PROVISION, provision)
        provision.chmod(0o755)
        result = self._run("admin@message-box.local", transport="cloud", provision=provision)
        self.assertEqual(result.returncode, 2)
        self.assertIn("pinned release", result.stderr)
        for extra in (("--transport", "invalid"), ("--transport",), ("--guided-prompts", "duplicate")):
            result = self._run("admin@message-box.local", extra=extra)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse(self.rsync_log.exists())

    def test_missing_prompt_pack_fails_before_connecting(self):
        result = self._run(
            "admin@message-box.local", prompt_dir=self.root / "missing-prompts"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Missing guided-reply prompt", result.stderr)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse(self.rsync_log.exists())

    def test_rejects_ssh_option_before_running_external_commands(self):
        result = self._run("-oProxyCommand=bad")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid SSH target", result.stderr)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse(self.rsync_log.exists())


if __name__ == "__main__":
    unittest.main()
