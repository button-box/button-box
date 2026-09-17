import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts/install/development-sudo-bootstrap.sh"
ROLLBACK = ROOT / "scripts/install/development-sudo-rollback.sh"
SETUP = ROOT / "scripts/setup.sh"


class DevelopmentAdminTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bin = Path(self.directory.name)
        getent = self.bin / "getent"
        getent.write_text(
            """#!/bin/sh
test "$1" = passwd || exit 2
case "$2" in
  devoperator) echo 'devoperator:x:1000:1000:Dev:/home/devoperator:/bin/sh' ;;
  root) echo 'root:x:0:0:root:/root:/bin/sh' ;;
  daemon) echo 'daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin' ;;
  *) exit 2 ;;
esac
""",
            encoding="utf-8",
        )
        getent.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{self.bin}:{self.environment['PATH']}"

    def run_bootstrap(self, *arguments):
        return subprocess.run(
            ["bash", str(BOOTSTRAP), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_policy_is_broad_explicit_and_limited_to_interactive_operator(self):
        rendered = self.run_bootstrap("--render-policy", "devoperator")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("devoperator ALL=(ALL:ALL) NOPASSWD: ALL", rendered.stdout)
        for rejected in ("root", "daemon", "bad/name"):
            with self.subTest(operator=rejected):
                result = self.run_bootstrap("--validate-operator", rejected)
                self.assertNotEqual(result.returncode, 0)

    def test_setup_keeps_policy_opt_in_and_runs_after_service_user_creation(self):
        setup = SETUP.read_text(encoding="utf-8")
        gate = 'if [ -n "$DEVELOPMENT_ADMIN" ]; then'
        self.assertIn("DEVELOPMENT_ADMIN=${MESSAGEBOX_DEVELOPMENT_ADMIN:-}", setup)
        self.assertIn(gate, setup)
        self.assertGreater(
            setup.index(gate),
            setup.index('if account=$(getent passwd "$ONBOARDING_USER")'),
        )
        self.assertIn("messagebox-development-sudo-bootstrap", setup)
        self.assertIn("messagebox-development-sudo-rollback", setup)

    def test_helpers_do_not_mutate_accounts_credentials_or_ssh(self):
        combined = BOOTSTRAP.read_text() + ROLLBACK.read_text()
        for forbidden in (
            "authorized_keys",
            "chpasswd",
            "sshd_config",
            "useradd",
            "usermod",
        ):
            self.assertNotIn(forbidden, combined)

    def test_rollback_rejects_dot_backup_identifiers_before_privileged_work(self):
        for backup_id in (".", ".."):
            result = subprocess.run(
                [
                    "bash",
                    str(ROLLBACK),
                    "--development-only",
                    "--operator",
                    "devoperator",
                    "--backup-id",
                    backup_id,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
