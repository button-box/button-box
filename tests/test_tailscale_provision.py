import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "scripts" / "provision-tailscale.sh"


class TailscaleProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.ssh_log = self.root / "ssh.log"
        self.install_input = self.root / "install-input.sh"
        self.ip_calls = self.root / "ip-calls"
        self._write_executable(
            "ssh",
            """#!/bin/sh
{
  printf 'CALL'
  for arg in "$@"; do printf '\\t%s' "$arg"; done
  printf '\\n'
} >>"$SSH_LOG"

case "$*" in
  *" hostname")
    printf '%s\\n' "${REMOTE_HOSTNAME:-message-box-001}"
    ;;
  *" sudo -n /bin/sh -s")
    cat >"$INSTALL_INPUT"
    [ "${INSTALL_FAIL:-0}" -eq 0 ] || exit 1
    ;;
  *"tailscale status --self=true --peers=false"*)
    count=0
    [ ! -f "$IP_CALLS" ] || count=$(cat "$IP_CALLS")
    count=$((count + 1))
    printf '%s\\n' "$count" >"$IP_CALLS"
    if [ "${ALREADY_CONNECTED:-0}" -eq 1 ] || [ "$count" -gt 1 ]; then
      printf '%s\\n' 100.100.100.100
    fi
    ;;
esac
""",
        )

    def _write_executable(self, name, content):
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, *arguments, confirmation="yes\n", **extra_env):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "SSH_LOG": str(self.ssh_log),
                "INSTALL_INPUT": str(self.install_input),
                "IP_CALLS": str(self.ip_calls),
                **extra_env,
            }
        )
        return subprocess.run(
            [str(PROVISION), *arguments],
            cwd=self.root,
            env=env,
            input=confirmation,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_installs_and_uses_interactive_browser_enrollment(self):
        result = self._run("admin@message-box-001.local")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            any(
                "\t-t\tadmin@message-box-001.local\t"
                "sudo -n tailscale up --hostname=message-box-001" in call
                for call in calls
            ),
            calls,
        )
        installer = self.install_input.read_text(encoding="utf-8")
        self.assertIn("https://pkgs.tailscale.com/stable/", installer)
        self.assertIn("debian|raspbian", installer)
        self.assertNotIn("auth-key", installer.lower())
        self.assertNotIn("--ssh", installer)
        self.assertIn("ssh admin@100.100.100.100", result.stdout)

    def test_existing_enrollment_does_not_run_tailscale_up(self):
        result = self._run(
            "--hostname",
            "message-box-beta",
            "admin@message-box-001.local",
            ALREADY_CONNECTED="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any("tailscale up" in call for call in calls), calls)
        self.assertTrue(
            any(
                "sudo -n tailscale set --hostname=message-box-beta "
                "--auto-update=false --ssh=false --accept-routes=false "
                "--advertise-routes= --advertise-exit-node=false "
                "--exit-node= --webclient=false" in call
                for call in calls
            ),
            calls,
        )
        self.assertIn("Device: message-box-beta", result.stdout)

    def test_rejects_unsafe_target_before_running_ssh(self):
        result = self._run("-oProxyCommand=bad")

        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid non-root SSH target", result.stderr)
        self.assertFalse(self.ssh_log.exists())

    def test_cancellation_makes_no_remote_changes(self):
        result = self._run(
            "admin@message-box-001.local", confirmation="no\n"
        )

        self.assertEqual(result.returncode, 1)
        calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2, calls)
        self.assertFalse(self.install_input.exists())

    def test_install_failure_does_not_attempt_enrollment(self):
        result = self._run(
            "admin@message-box-001.local", INSTALL_FAIL="1"
        )

        self.assertNotEqual(result.returncode, 0)
        calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any("tailscale up" in call for call in calls), calls)


if __name__ == "__main__":
    unittest.main()
