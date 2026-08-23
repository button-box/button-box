import os
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
for arg in "$@"; do printf '%s\\0' "$arg"; done >"$RSYNC_LOG"
""",
        )

    def _write_executable(self, name, content):
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, target):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "SSH_LOG": str(self.ssh_log),
                "RSYNC_LOG": str(self.rsync_log),
            }
        )
        return subprocess.run(
            [str(PROVISION), target],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_stages_only_pi_installation_inputs_and_cleans_up(self):
        result = self._run("admin@button-box.local")

        self.assertEqual(result.returncode, 0, result.stderr)
        rsync_args = self.rsync_log.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(rsync_args[0], "-azR")
        self.assertEqual(
            rsync_args[-1],
            "admin@button-box.local:/tmp/messagebox-provision.test/",
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
        self.assertIn("messagebox/syncloop.sh", staged_paths)

        ssh_calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ssh_calls), 3)
        self.assertEqual(
            ssh_calls[0],
            "CALL\tadmin@button-box.local\tmktemp -d /tmp/messagebox-provision.XXXXXX",
        )
        self.assertEqual(
            ssh_calls[1],
            "CALL\t-t\tadmin@button-box.local\t"
            "MESSAGEBOX_SSH_TARGET='admin@button-box.local' "
            "'/tmp/messagebox-provision.test/scripts/setup.sh'",
        )
        self.assertEqual(
            ssh_calls[2],
            "CALL\tadmin@button-box.local\trm -rf -- '/tmp/messagebox-provision.test'",
        )

    def test_rejects_ssh_option_before_running_external_commands(self):
        result = self._run("-oProxyCommand=bad")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid SSH target", result.stderr)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse(self.rsync_log.exists())


if __name__ == "__main__":
    unittest.main()
