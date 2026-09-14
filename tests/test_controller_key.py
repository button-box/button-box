import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "dev" / "authorize-controller-key.sh"


class ControllerKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.log = self.root / "calls.log"
        self.key = self.root / "controller.pub"
        self.key.write_text("ssh-ed25519 synthetic-test-key controller\n")
        self._write_executable(
            "ssh-keygen",
            "#!/bin/sh\nprintf '%s\\n' '256 SHA256:test-controller controller (ED25519)'\n",
        )
        self._write_executable(
            "ssh-copy-id",
            "#!/bin/sh\nprintf 'COPY' >>\"$CALL_LOG\"\nfor arg in \"$@\"; do printf '\\t%s' \"$arg\" >>\"$CALL_LOG\"; done\nprintf '\\n' >>\"$CALL_LOG\"\n",
        )
        self._write_executable(
            "ssh",
            "#!/bin/sh\nprintf 'SSH' >>\"$CALL_LOG\"\nfor arg in \"$@\"; do printf '\\t%s' \"$arg\" >>\"$CALL_LOG\"; done\nprintf '\\n' >>\"$CALL_LOG\"\ncase \"${FAIL_SUDO:-}:$*\" in '1:'*'sudo -n true') exit 1 ;; esac\n",
        )

    def _write_executable(self, name, content):
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, *args, fail_sudo=False):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "CALL_LOG": str(self.log),
                "FAIL_SUDO": "1" if fail_sudo else "0",
            }
        )
        return subprocess.run(
            [str(HELPER), *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_installs_public_key_and_verifies_ssh_and_sudo_separately(self):
        result = self._run("admin@button-box-001.local", str(self.key))

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(
            calls[0], f"COPY\t-i\t{self.key}\t--\tadmin@button-box-001.local"
        )
        self.assertIn("PasswordAuthentication=no", calls[1])
        self.assertTrue(calls[1].endswith("\tadmin@button-box-001.local\ttrue"))
        self.assertTrue(
            calls[2].endswith("\tadmin@button-box-001.local\tsudo\t-n\ttrue")
        )
        self.assertIn("SHA256:test-controller", result.stdout)
        self.assertIn("sudo -n succeeds", result.stdout)

    def test_reports_sudo_as_distinct_gate_after_key_login(self):
        result = self._run(
            "admin@button-box-001.local", str(self.key), fail_sudo=True
        )

        self.assertEqual(result.returncode, 3)
        self.assertIn("SSH works, but sudo still requires interaction", result.stdout)

    def test_rejects_root_target_before_external_commands(self):
        result = self._run("root@button-box-001.local", str(self.key))

        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid non-root SSH target", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
