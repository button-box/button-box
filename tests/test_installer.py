"""scripts/install/install.sh: options, preflight failures and signature checks.

The installer runs with stub system commands on PATH and its paths moved into a
temporary directory, so these tests never touch the machine running them.
"""

import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install/install.sh"
TAG = "v0.3.99"

STUB = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >>"$STUB_CALLS"
case "$(basename "$0")" in
  id) [ "$1" = -u ] && printf '%s\\n' "${STUB_UID:-0}" || /usr/bin/id "$@" ;;
  uname) printf '%s\\n' "${STUB_ARCH:-aarch64}" ;;
  dpkg) printf 'arm64\\n' ;;
  dpkg-query) exit 1 ;;
  curl) [ "${STUB_CURL:-ok}" = ok ] || exit 7 ;;
  df) printf 'Filesystem 1M-blocks Used Available Capacity Mounted\\n/dev/root 30000 1000 %s 4%% /\\n' "${STUB_FREE_MB:-20000}" ;;
  timedatectl) printf '%s\\n' "${STUB_NTP:-yes}" ;;
  systemctl)
    # STUB_APT_BUSY names a file with how many more is-active checks succeed.
    if [ "$1" = is-active ] && [ -n "${STUB_APT_BUSY:-}" ]; then
      left=$(cat "$STUB_APT_BUSY")
      if [ "$left" -gt 0 ]; then
        printf '%s\n' $((left - 1)) >"$STUB_APT_BUSY"
        exit 0
      fi
    fi
    exit 3 ;;
  sleep) exit 0 ;;
  getent)
    case "$1" in
      passwd) printf 'admin:x:1000:1000::/home/admin:/bin/bash\\n' ;;
      group) printf 'sudo:x:27:admin\\n' ;;
    esac ;;
  runuser|sudo|apt-get|qrencode|espeak-ng|messageboxctl) exit 0 ;;
  minisign) exit "${STUB_MINISIGN:-1}" ;;
esac
"""
STUBBED = ("id", "uname", "dpkg", "dpkg-query", "curl", "df", "timedatectl", "systemctl", "getent",
           "runuser", "sudo", "apt-get", "qrencode", "espeak-ng", "messageboxctl", "minisign", "sleep")


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in STUBBED:
            path = self.bin / name
            path.write_text(STUB)
            path.chmod(0o755)
        self.calls = self.tmp / "calls.log"
        self.calls.touch()
        self.model = self.tmp / "model"
        self.model.write_bytes(b"Raspberry Pi 4 Model B Rev 1.5\x00")
        self.os_release = self.tmp / "os-release"
        self.os_release.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_CODENAME=trixie\n')
        self.rpi_issue = self.tmp / "rpi-issue"
        self.rpi_issue.write_text("Raspberry Pi reference\n")
        self.log = self.tmp / "log/button-box-install.log"
        self.work = self.tmp / "work"

    def env(self, **overrides):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB_CALLS": str(self.calls),
            "SUDO_USER": "admin",
            "BUTTON_BOX_MODEL_FILE": str(self.model),
            "BUTTON_BOX_OS_RELEASE": str(self.os_release),
            "BUTTON_BOX_RPI_ISSUE": str(self.rpi_issue),
            "BUTTON_BOX_ENV_FILE": str(self.tmp / "env"),
            "BUTTON_BOX_INSTALL_LOG": str(self.log),
            "BUTTON_BOX_INSTALL_WORK": str(self.work),
            "BUTTON_BOX_NETWORK_WAIT": "0",
            "BUTTON_BOX_CLOCK_WAIT": "0",
        }
        env.update(overrides)
        return env

    def install(self, *args, **overrides):
        return subprocess.run(["/bin/sh", str(INSTALLER), *args], env=self.env(**overrides),
                              capture_output=True, text=True, timeout=60)

    def output(self, result):
        log = self.log.read_text() if self.log.exists() else ""
        return result.stdout + result.stderr + log

    # ------------------------------------------------------------ options

    def test_help_lists_every_option(self):
        result = self.install("--help", PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 0)
        for option in ("--mode cloud|wacli", "--updates auto|ask", "--version", "--check",
                       "--release-dir", "--public-key", "--timezone", "--admin"):
            self.assertIn(option, result.stdout)

    def test_bad_options_exit_2_before_any_command(self):
        cases = (
            (["--mode", "local"], "--mode must be cloud or wacli"),
            (["--mode=local"], "--mode must be cloud or wacli"),
            (["--updates", "never"], "--updates must be auto or ask"),
            (["--version", "latest"], "--version must look like"),
            (["--version", "v1.2"], "--version must look like"),
            (["--timezone", "../../etc/passwd"], "--timezone must look like"),
            (["--admin", "root"], "--admin must be a non-root user name"),
            (["--public-key", "key.pub"], "--public-key works only with --release-dir"),
            (["--mode"], "--mode needs a value"),
            (["--frobnicate"], "unknown option: --frobnicate"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                result = self.install(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.calls.read_text(), "")
        self.assertFalse(self.log.exists())

    def test_good_options_are_accepted(self):
        result = self.install("--check", "--mode=wacli", "--updates", "ask", "--version", "v0.3.13-canary.2",
                              "--timezone", "Europe/Lisbon", "--admin", "admin")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("Preflight passed. Nothing was installed.", self.output(result))
        self.assertIn("Mode: wacli", self.output(result))

    # ---------------------------------------------------------- preflight

    def assert_preflight_fails(self, result, message):
        self.assertEqual(result.returncode, 3, self.output(result))
        self.assertIn(message, self.output(result))
        calls = self.calls.read_text()
        for name in ("apt-get", "minisign", "messageboxctl", "systemctl stop"):
            self.assertNotIn(name, calls)

    def test_not_root(self):
        result = self.install("--check", STUB_UID="1000")
        self.assert_preflight_fails(result, "Run the installer as root.")
        self.assertFalse(self.log.exists())

    def test_unsupported_models(self):
        for model, message in (
            (b"Raspberry Pi Zero 2 W Rev 1.0\x00", "Raspberry Pi Zero 2 W is not supported for Cloud updates."),
            (b"Raspberry Pi 5 Model B Rev 1.0\x00", "This Pi is not supported"),
        ):
            with self.subTest(model=model):
                self.model.write_bytes(model)
                self.assert_preflight_fails(self.install("--check"), message)
        self.model.unlink()
        self.assert_preflight_fails(self.install("--check"), "This is not a Raspberry Pi")

    def test_pi_3_a_plus_is_supported(self):
        self.model.write_bytes(b"Raspberry Pi 3 Model A Plus Rev 1.1\x00")
        result = self.install("--check")
        self.assertEqual(result.returncode, 0, self.output(result))

    def test_wrong_os(self):
        self.os_release.write_text("VERSION_CODENAME=bookworm\n")
        self.assert_preflight_fails(self.install("--check"), "Raspberry Pi OS trixie is required (found: bookworm)")
        self.os_release.write_text("VERSION_CODENAME=trixie\n")
        self.rpi_issue.unlink()
        self.assert_preflight_fails(self.install("--check"), "This is not Raspberry Pi OS.")

    def test_32_bit_os(self):
        self.assert_preflight_fails(self.install("--check", STUB_ARCH="armv7l"), "The 64-bit OS is required")

    def test_no_internet(self):
        self.assert_preflight_fails(self.install("--check", STUB_CURL="fail"), "No internet connection")

    def test_low_disk(self):
        self.assert_preflight_fails(self.install("--check", STUB_FREE_MB="900"), "Not enough free space: 900 MB")

    def test_clock_not_synced(self):
        # curl -I prints no Date header, so network time cannot vouch for the clock.
        self.assert_preflight_fails(self.install("--check", STUB_NTP="no"), "The clock is not synchronised.")

    def test_no_passwordless_admin(self):
        (self.bin / "runuser").write_text("#!/bin/sh\nexit 1\n")
        self.assert_preflight_fails(self.install("--check"), "User admin needs passwordless sudo.")

    def test_mode_cannot_change_on_an_installed_box(self):
        (self.tmp / "env").write_text("MSGBOX_TRANSPORT=wacli\n")
        self.assert_preflight_fails(self.install("--check", "--mode", "cloud"),
                                    "This box is already installed in wacli mode.")
        result = self.install("--check")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("Mode: wacli", self.output(result))

    def test_log_is_private(self):
        result = self.install("--check")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertIn("Pi model: Raspberry Pi 4 Model B", self.log.read_text())
        self.assertEqual(list(self.work.iterdir()), [])

    # --------------------------------------------------------- signatures

    def release_dir(self, *, archive=b"not really a tarball", manifest=b"{}\n", sums=None):
        directory = self.tmp / "release"
        directory.mkdir()
        (directory / f"button-box-{TAG}.tar.gz").write_bytes(archive)
        (directory / "release-manifest.json").write_bytes(manifest)
        if sums is None:
            sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(
                {f"button-box-{TAG}.tar.gz": archive, "release-manifest.json": manifest}.items()))
        (directory / "SHA256SUMS").write_text(sums)
        (directory / "SHA256SUMS.minisig").write_text("untrusted comment: signature\nAAAA\n")
        (directory / "test.pub").write_text("untrusted comment: minisign public key 0\nAAAA\n")
        return directory

    def assert_nothing_installed(self, result):
        self.assertEqual(result.returncode, 4, self.output(result))
        self.assertIn("Nothing was installed.", self.output(result))
        calls = self.calls.read_text()
        self.assertNotIn("runuser -u admin -- env", calls)  # setup.sh never ran
        self.assertNotIn("messageboxctl", calls)
        self.assertNotIn("systemctl stop", calls)
        self.assertFalse(any(self.work.iterdir()), "staging is cleaned up")

    def test_bad_signature_aborts_before_installing_anything(self):
        directory = self.release_dir()
        result = self.install("--release-dir", str(directory), "--public-key", str(directory / "test.pub"))
        self.assert_nothing_installed(result)
        self.assertIn("The release signature is not valid.", self.output(result))
        self.assertIn("WARNING: verifying with the test key", self.output(result))

    def test_pinned_key_is_used_without_a_test_key(self):
        directory = self.release_dir()
        result = self.install("--release-dir", str(directory))
        self.assert_nothing_installed(result)
        verify = [line for line in self.calls.read_text().splitlines() if line.startswith("minisign ")]
        self.assertEqual(len(verify), 1)
        self.assertIn("-Vm", verify[0])

    def test_hash_mismatch_after_a_good_signature_aborts(self):
        directory = self.release_dir(sums=f"{'0' * 64}  button-box-{TAG}.tar.gz\n{'0' * 64}  release-manifest.json\n")
        result = self.install("--release-dir", str(directory), "--public-key", str(directory / "test.pub"),
                              STUB_MINISIGN="0")
        self.assert_nothing_installed(result)
        self.assertIn("do not match their signed hashes", self.output(result))

    @unittest.skipUnless(shutil.which("minisign"), "minisign is not installed")
    def test_real_minisign_rejects_a_release_signed_by_another_key(self):
        directory = self.release_dir()
        keys = self.tmp / "keys"
        keys.mkdir()
        for name in ("signer", "other"):
            subprocess.run(["minisign", "-G", "-W", "-p", str(keys / f"{name}.pub"), "-s", str(keys / f"{name}.key")],
                           check=True, capture_output=True)
        subprocess.run(["minisign", "-S", "-s", str(keys / "signer.key"), "-m", str(directory / "SHA256SUMS"),
                        "-x", str(directory / "SHA256SUMS.minisig")], check=True, capture_output=True)
        (self.bin / "minisign").unlink()
        result = self.install("--release-dir", str(directory), "--public-key", str(keys / "other.pub"))
        self.assert_nothing_installed(result)
        self.assertIn("The release signature is not valid.", self.output(result))

    # ------------------------------------------------------------ apt lock

    def test_waits_for_the_first_boot_apt_update_before_installing(self):
        busy = self.tmp / "apt-busy"
        busy.write_text("3\n")
        directory = self.release_dir()
        result = self.install("--release-dir", str(directory), "--public-key", str(directory / "test.pub"),
                              STUB_APT_BUSY=str(busy))
        # It waited, then went on to verification (which the stub signature fails).
        self.assertEqual(result.returncode, 4, self.output(result))
        self.assertIn("Waiting for the automatic system update (apt-daily) to finish", self.output(result))
        self.assertIn("sleep 10", self.calls.read_text())
        self.assertEqual(busy.read_text().strip(), "0")

    def test_apt_update_that_never_ends_stops_before_installing(self):
        busy = self.tmp / "apt-busy"
        busy.write_text("1000\n")
        directory = self.release_dir()
        result = self.install("--release-dir", str(directory), STUB_APT_BUSY=str(busy), BUTTON_BOX_APT_WAIT="30")
        self.assertEqual(result.returncode, 5, self.output(result))
        self.assertIn("Automatic system updates are still running", self.output(result))
        self.assertNotIn("minisign", self.calls.read_text())

    def test_every_apt_call_waits_for_the_dpkg_lock(self):
        for script in ("scripts/setup.sh", "scripts/install/comitup.sh", "scripts/install/nfc.sh",
                       "scripts/install/tailscale.sh", "scripts/install/install.sh"):
            text = (ROOT / script).read_text()
            # Lines that run apt-get (not messages that mention it).
            calls = re.findall(r"^\s*(?:sudo\s+|DEBIAN_FRONTEND=\S+\s+)*apt-get\s+(.*)$", text, re.M)
            with self.subTest(script=script):
                self.assertTrue(calls)
                for call in calls:
                    self.assertTrue(call.startswith("-o DPkg::Lock::Timeout="), call)

    # ------------------------------------------------------- consistency

    def test_pinned_key_matches_the_checked_in_release_key(self):
        text = INSTALLER.read_text()
        key_id = re.search(r"^RELEASE_KEY_ID=(\S+)$", text, re.M)[1]
        key = re.search(r"^RELEASE_KEY=(\S+)$", text, re.M)[1]
        pub = (ROOT / "config/release-keys/button-box-release-2026.pub").read_text().splitlines()
        self.assertEqual(pub[1], key)
        self.assertEqual(pub[0], f"untrusted comment: minisign public key {key_id}")

    def test_release_payload_has_everything_the_installer_runs(self):
        payload = set((ROOT / "scripts/release/payload.txt").read_text().splitlines())
        setup = (ROOT / "scripts/setup.sh").read_text()
        # Files setup.sh requires from its source tree.
        required = set(re.findall(r"^  ((?:config|scripts|systemd|messagebox)/[^\s\\]+) \\$", setup, re.M))
        required |= {f"messagebox/{name}" for name in re.search(
            r'^PACKAGE_PYTHON="([^"]*)"', setup, re.M)[1].split()}
        required |= {"scripts/setup.sh", "scripts/install/update-bootstrap.sh", "scripts/install/tailscale.sh",
                     "scripts/install/release_verify.py", "config/release-keys/button-box-release-2026.pub",
                     "config/emergency-keys/button-box-emergency-2026.pub"}
        self.assertGreater(len(required), 60)
        self.assertEqual(sorted(required - payload), [])

    def test_bootstrap_checks_the_installed_identity_before_changing_anything(self):
        text = (ROOT / "scripts/install/update-bootstrap.sh").read_text()
        self.assertLess(text.index("/opt/messagebox/release.json is missing or invalid"), text.index("install -d"))

    def test_installer_writes_identity_before_bootstrap_and_sets_the_floor_after(self):
        main = INSTALLER.read_text().split("\nmain() {", 1)[1]
        order = [main.index(name) for name in ("write_identity", "release.json is missing", "bootstrap_updater",
                                               "record_sequence_floor")]
        self.assertEqual(order, sorted(order))

    def test_installer_is_posix_sh_and_reads_nothing_from_stdin(self):
        text = INSTALLER.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('exec </dev/null', text)
        self.assertEqual(text.rstrip().splitlines()[-1], 'main "$@"')
        self.assertEqual(text.count("proc/device-tree/model"), 1)  # scripts/dev/pi-os-ci/shim.sh


if __name__ == "__main__":
    unittest.main()
