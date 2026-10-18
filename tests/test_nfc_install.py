import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NFC_INSTALL = ROOT / "scripts" / "install" / "nfc.sh"
UPDATE_BOOTSTRAP = ROOT / "scripts" / "install" / "update-bootstrap.sh"

# Stand-ins for the host commands. sudo runs the command as the test user, so
# the caller's umask reaches the venv exactly as it does under real sudo.
STUBS = {
    "sudo": '#!/bin/sh\nexec "$@"\n',
    "getent": "#!/bin/sh\nexit 0\n",
    "systemctl": '#!/bin/sh\n[ "$1" != "is-active" ]\n',
    "raspi-config": "#!/bin/sh\nexit 0\n",
    "usermod": "#!/bin/sh\nexit 0\n",
    # Only directory creation matters here; root ownership is not testable.
    "install": """#!/bin/sh
if [ "$1" = "-d" ]; then
  for arg in "$@"; do last=$arg; done
  mkdir -p "$last"
fi
exit 0
""",
    # A fake venv: real files and directories, created with the inherited umask.
    "python3": """#!/bin/sh
[ "$1 $2" = "-m venv" ] || exit 2
venv=$3
mkdir -p "$venv/bin"
cat >"$venv/bin/python" <<'EOF'
#!/bin/sh
[ "$1 $2 $3" = "-m pip install" ] || exit 2
site=$(dirname "$(dirname "$0")")/lib/python3/site-packages/board
mkdir -p "$site"
printf 'x = 1\\n' >"$site/__init__.py"
EOF
chmod 0777 "$venv/bin/python"
printf 'home = /usr/bin\\n' >"$venv/pyvenv.cfg"
ln -sf /bin/sh "$venv/bin/python3"
""",
}


class NfcInstallPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        for name, content in STUBS.items():
            path = self.bin_dir / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        self.app_dir = self.root / "opt" / "messagebox"
        for name in ("nfc.py", "nfc_state.py", "contacts.py"):
            path = self.app_dir / "messagebox" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        (self.app_dir / "config").mkdir()
        (self.app_dir / "config" / "requirements-nfc.txt").write_text("", encoding="utf-8")
        self.venv = self.app_dir / "venv-nfc"

    def _run(self):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "MSGBOX_APP_DIR": str(self.app_dir),
                "MSGBOX_SKIP_APT": "1",
            }
        )
        return subprocess.run(
            [str(NFC_INSTALL)],
            env=env,
            preexec_fn=lambda: os.umask(0o077),
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_readable_by_others(self):
        entries = [self.venv, *self.venv.rglob("*")]
        self.assertGreater(len(entries), 4)
        for path in entries:
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                self.assertEqual(mode, 0o755, path)
            else:
                self.assertTrue(mode & 0o004, f"{path} is {oct(mode)}")
                self.assertFalse(mode & 0o022, f"{path} is {oct(mode)}")
        python = stat.S_IMODE((self.venv / "bin" / "python").stat().st_mode)
        self.assertEqual(python, 0o755)

    def test_strict_umask_still_gives_service_user_a_readable_venv(self):
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_readable_by_others()

    def test_repairs_a_venv_left_root_only_by_an_earlier_install(self):
        site = self.venv / "lib" / "python3" / "site-packages" / "old"
        site.mkdir(parents=True)
        (site / "module.py").write_text("", encoding="utf-8")
        (site / "module.py").chmod(0o600)
        for path in (site, site.parent, site.parent.parent, self.venv / "lib", self.venv):
            path.chmod(0o700)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_readable_by_others()
        self.assertEqual(stat.S_IMODE((site / "module.py").stat().st_mode), 0o644)


class UpdateBootstrapUmaskTests(unittest.TestCase):
    def test_sets_umask_before_creating_any_path(self):
        lines = UPDATE_BOOTSTRAP.read_text(encoding="utf-8").splitlines()
        umask = lines.index("umask 022")
        first_write = next(
            index
            for index, line in enumerate(lines)
            if line.startswith(("install ", "useradd ", "python3 ", "getent "))
        )
        self.assertLess(umask, first_write)


if __name__ == "__main__":
    unittest.main()
