import os
import shutil
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
        self._write_executable(
            "ssh",
            """#!/bin/sh
{
  printf 'CALL'
  for arg in "$@"; do printf '\\t%s' "$arg"; done
  printf '\\n'
} >>"$SSH_LOG"
for last; do :; done
case "$last" in
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

    def _run(self, target, *, prompt_dir=None, options=(), extra_env=None):
        env = os.environ.copy()
        env.pop("SSH_IDENTITY", None)
        env.update(extra_env or {})
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "SSH_LOG": str(self.ssh_log),
                "RSYNC_LOG": str(self.rsync_log),
            }
        )
        arguments = [str(prompt_dir or PROVISION), *options, target]
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
        self.assertEqual(len(rsync_calls), 1)
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

        ssh_calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ssh_calls), 3)
        self.assertEqual(
            ssh_calls[0],
            "CALL\tadmin@message-box.local\tmktemp -d /tmp/messagebox-provision.XXXXXX",
        )
        self.assertEqual(
            ssh_calls[1],
            "CALL\t-t\tadmin@message-box.local\t"
            "MESSAGEBOX_SSH_TARGET='admin@message-box.local' "
            "'/tmp/messagebox-provision.test/scripts/setup.sh'",
        )
        self.assertEqual(
            ssh_calls[2],
            "CALL\tadmin@message-box.local\trm -rf -- '/tmp/messagebox-provision.test'",
        )

    def assert_identity_used(self, key):
        ssh_calls = [line.split("\t")[1:] for line in self.ssh_log.read_text().splitlines()]
        self.assertEqual(len(ssh_calls), 3)
        for call in ssh_calls:
            self.assertEqual(call[:4], ["-i", str(key), "-o", "IdentitiesOnly=yes"])
        self.assertEqual(ssh_calls[1][4], "-t")
        rsync_args = self.rsync_log.read_text().splitlines()[0].split("\t")[1:]
        self.assertEqual(rsync_args[:2], ["-azR", f"--rsh=ssh -i '{key}' -o IdentitiesOnly=yes"])

    def test_identity_option_is_used_for_ssh_and_rsync(self):
        key = self.root / "keys dir" / "box_ed25519"
        key.parent.mkdir()
        key.write_text("not a real key\n")
        result = self._run("admin@message-box.local", options=("-i", str(key)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_identity_used(key)

    def test_identity_environment_variable_and_relative_path(self):
        (self.root / "box_ed25519").write_text("not a real key\n")
        result = self._run("admin@message-box.local", extra_env={"SSH_IDENTITY": "box_ed25519"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_identity_used(self.root.resolve() / "box_ed25519")

    def test_missing_or_unsafe_identity_fails_before_connecting(self):
        for identity, message in ((str(self.root / "missing"), "not a readable file"),
                                  ("-oProxyCommand=bad", "Invalid SSH identity file")):
            with self.subTest(identity=identity):
                result = self._run("admin@message-box.local", options=("-i", identity))
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse(self.rsync_log.exists())

    def test_missing_sound_pack_fails_before_connecting(self):
        repo = self.root / "repo"
        for area in ("scripts", "messagebox", "sounds"):
            shutil.copytree(ROOT / area, repo / area)
        (repo / "sounds/cues/cue-press.wav").unlink()
        result = self._run("admin@message-box.local", prompt_dir=repo / "scripts/provision.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or invalid cues sound pack", result.stderr)
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
