"""Self-install helpers: `messageboxctl doctor` and `messageboxctl claim-link`.

`doctor` prints one pass/fail line per area with a fix hint. `claim-link`
registers a Cloud box the way setup does, prints its setup link
(https://button.box/s/<setup_code>) with a QR code, and, from a terminal,
opens Cloud claim setup without changing Wi-Fi. Neither command prints or logs
the device credential.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ENV_PATH = Path("/etc/messagebox/env")
APP_DIR = Path("/opt/messagebox")
RELEASES_DIR = "/opt/messagebox-releases/"
AUDIO_CONFIG = Path("/usr/lib/messagebox/audio_config.py")
UPDATE_MARKER = Path("/etc/button-box-update/installed")
UPDATE_POLICY = Path("/var/lib/button-box-update/policy.json")
ONBOARDING_ENABLED = Path("/etc/messagebox-onboarding/enabled")
ONBOARDING_CONFIGURED = Path("/etc/messagebox-onboarding/configured")
ONBOARDING_STATE = Path("/var/lib/messagebox-onboarding/state.json")
WACLI = Path("/usr/local/bin/wacli")
I2C_DEVICE = Path("/dev/i2c-1")
PN532_ADDRESS = 0x24
DEFAULT_CLOUD_API = "https://button.box/cloud-api/v1"
SERVICE_USER = "messagebox"
SETTINGS_GROUP = "messagebox-settings"
ONBOARDING_USER = "messagebox-onboarding"
UPDATE_UNITS = (
    "messagebox-update-recovery.service",
    "messagebox-update-controller.service",
    "messagebox-update-supervisor.timer",
    "messagebox-update-discovery.timer",
)
RUNTIME_SERVICES = {
    "button": "messagebox-button.service",
    "sync": "messagebox-sync.service",
    "poller": "messagebox-poller.service",
    "dashboard": "messagebox-dash.service",
    "nfc": "messagebox-nfc.service",
}
SETUP_UNITS = ("comitup.service", "messagebox-onboarding-home.service")
SETUP_WATCHERS = ("messagebox-onboarding-complete.path", "messagebox-onboarding-voice.path")
# Paths the messagebox service user must be able to read (and enter).
READABLE_ROOTS = ("/opt/messagebox/", "/usr/lib/messagebox", "/etc/messagebox",
                  "/var/lib/messagebox", "/var/lib/messagebox-cloud")
_CODE = re.compile(r"[A-Za-z0-9]{4,32}\Z")

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "----"


def read_env(path=ENV_PATH):
    """Return KEY=VALUE pairs; values are never printed by callers."""
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def transport(env):
    # An absent setting means a self-hosted box, as in the runtime.
    return env.get("MSGBOX_TRANSPORT") or "wacli"


def _run(command, run=subprocess.run, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    kwargs.setdefault("timeout", 60)
    try:
        return run(command, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 127, "", type(exc).__name__)


def as_service_user(argv, *, run=subprocess.run, user=SERVICE_USER, timeout=120):
    """Run argv like a Button Box service: same user, groups and env file."""
    return _run(["systemd-run", "--quiet", "--pipe", "--wait", "--collect",
                 "--service-type=exec",
                 "-p", f"User={user}", "-p", f"Group={user}",
                 "-p", f"SupplementaryGroups={SETTINGS_GROUP}",
                 "-p", f"EnvironmentFile=-{ENV_PATH}",
                 "-p", f"WorkingDirectory={APP_DIR}",
                 "-p", "UMask=0077",
                 "-E", "PYTHONDONTWRITEBYTECODE=1",
                 *argv], run=run, timeout=timeout)


# ---------------------------------------------------------------- doctor


class Report:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}

    def line(self, status, area, message, fix=None):
        self.counts[status] += 1
        print(f"  {status}  {area}: {message}", file=self.out)
        if fix and status in {WARN, FAIL}:
            print(f"        Fix: {fix}", file=self.out)


def _unit_state(unit, run):
    active = _run(["systemctl", "is-active", unit], run=run).stdout.strip() or "unknown"
    enabled = _run(["systemctl", "is-enabled", unit], run=run).stdout.strip() or "unknown"
    return active, enabled


def check_services(report, mode, run=subprocess.run, enabled_marker=ONBOARDING_ENABLED):
    failed = _run(["systemctl", "list-units", "--failed", "--plain", "--no-legend",
                   "messagebox*", "comitup*"], run=run).stdout.split()
    failed = sorted({name for name in failed if name.startswith(("messagebox", "comitup"))})
    if failed:
        report.line(FAIL, "services", "failed: " + ", ".join(failed),
                    f"sudo journalctl -u {failed[0]} -n 50, then sudo systemctl restart {failed[0]}")
    if enabled_marker.exists():
        down = [unit for unit in SETUP_UNITS + SETUP_WATCHERS if _unit_state(unit, run)[0] != "active"]
        if down:
            report.line(FAIL, "services", "setup mode, but not running: " + ", ".join(down),
                        "sudo messageboxctl logs onboarding")
        else:
            report.line(PASS, "services", "setup mode is running (" + ", ".join(SETUP_UNITS) + ")")
        return
    selected = {name: unit for name, unit in RUNTIME_SERVICES.items()
                if _unit_state(unit, run)[1] == "enabled"}
    if not selected:
        hint = ("sudo messageboxctl claim-link, then scan the QR code" if mode == "cloud"
                else "messagebox-dev-onboard (see INSTALL.md)")
        report.line(WARN, "services", "Button Box is installed but not started yet", hint)
        return
    down = [name for name, unit in selected.items() if _unit_state(unit, run)[0] != "active"]
    if down:
        report.line(FAIL, "services", "selected but not running: " + ", ".join(down),
                    "sudo messageboxctl start, then sudo messageboxctl services")
    else:
        report.line(PASS, "services", "running: " + ", ".join(sorted(selected)))


WALK = r"""
import json, os, sys
bad = []
def visit(path):
    if os.path.islink(path) and not os.path.exists(path):
        bad.append(path); return
    if os.path.isdir(path):
        need = os.R_OK | os.X_OK
    else:
        need = os.R_OK | (os.X_OK if os.stat(path).st_mode & 0o111 else 0)
    if not os.access(path, need):
        bad.append(path)
for root in sys.argv[1:]:
    if not os.path.lexists(root):
        continue
    visit(root)
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: bad.append(e.filename)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in dirnames + filenames:
                visit(os.path.join(dirpath, name))
print(json.dumps(bad[:50]))
"""


def check_permissions(report, run=subprocess.run, app_dir=APP_DIR):
    roots = list(READABLE_ROOTS)
    venv = app_dir / "venv-nfc"
    if venv.exists():
        roots.append(os.path.realpath(venv) + "/")
    result = _run(["runuser", "-u", SERVICE_USER, "-g", SERVICE_USER, "-G", SETTINGS_GROUP, "--",
                   "/usr/bin/python3", "-c", WALK, *roots], run=run, timeout=300)
    try:
        bad = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        bad = None
    if bad is None:
        report.line(FAIL, "permissions", "could not check files as the messagebox user",
                    "check that the messagebox account exists: getent passwd messagebox")
    elif bad:
        report.line(FAIL, "permissions", f"{len(bad)} path(s) not readable by messagebox, e.g. {bad[0]}",
                    "run the installer again; it repairs program files. For one path: sudo chmod a+rX <path>")
    else:
        report.line(PASS, "permissions", "messagebox can read its program, venv and data files")


def _load_audio_config(path=AUDIO_CONFIG):
    import importlib.util
    spec = importlib.util.spec_from_file_location("messagebox_audio_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_audio(report, loader=_load_audio_config):
    try:
        module = loader()
    except (OSError, ImportError, SyntaxError, AttributeError):
        report.line(FAIL, "audio", "audio detection is not installed", "run the installer again")
        return
    for label, detect, hint in (
        ("speaker", "detect_speaker", "plug the USB speaker in directly (not through a hub), then run the doctor again"),
        ("microphone", "detect_microphone", "plug the USB microphone in directly (not through a hub), then run the doctor again"),
    ):
        try:
            card, _device = getattr(module, detect)()
        except Exception:  # Detection errors are safe, bounded messages only.
            report.line(FAIL, "audio", f"no {label} found", hint)
        else:
            report.line(PASS, "audio", f"{label} found (card {card})")


def check_nfc(report, run=subprocess.run, device=I2C_DEVICE):
    if not device.exists():
        report.line(WARN, "nfc", f"{device} is missing (I2C is off)",
                    "sudo raspi-config nonint do_i2c 0, then reboot. NFC cards are optional.")
        return
    result = _run(["i2cdetect", "-y", device.name.rsplit("-", 1)[-1],
                   hex(PN532_ADDRESS), hex(PN532_ADDRESS)], run=run, timeout=20)
    if result.returncode == 0 and re.search(rf"\b{PN532_ADDRESS:02x}\b", result.stdout.split("\n", 1)[-1]):
        report.line(PASS, "nfc", f"card reader answers on I2C address {hex(PN532_ADDRESS)}")
    else:
        report.line(WARN, "nfc", "no card reader on I2C (fine if it is not wired yet)",
                    "power off, check the PN532 wiring and that its switches select I2C. NFC cards are optional.")


def check_cloud(report, env, opener=urllib.request.urlopen):
    api = env.get("MSGBOX_CLOUD_API_URL") or DEFAULT_CLOUD_API
    parsed = urlsplit(api)
    if parsed.scheme != "https" or not parsed.netloc:
        report.line(FAIL, "cloud", "MSGBOX_CLOUD_API_URL in /etc/messagebox/env is invalid",
                    "remove the line to use the default Cloud address")
        return
    try:
        with opener(urllib.request.Request(api.rstrip("/") + "/", method="GET"), timeout=15):
            pass
    except urllib.error.HTTPError as exc:
        exc.close()  # Any HTTP answer proves DNS, TLS and the route work.
    except (OSError, ValueError):
        report.line(FAIL, "cloud", f"{parsed.netloc} is not reachable",
                    "check Wi-Fi and DNS: ping -c 3 " + parsed.hostname)
        return
    report.line(PASS, "cloud", f"{parsed.netloc} is reachable over HTTPS")


def check_updater(report, run=subprocess.run, app_dir=APP_DIR, marker=UPDATE_MARKER, policy=UPDATE_POLICY):
    target = os.readlink(app_dir) if app_dir.is_symlink() else ""
    problems = []
    if not marker.is_file():
        problems.append("not bootstrapped")
    if not target.startswith(RELEASES_DIR):
        problems.append("not running from a signed release")
    for unit in UPDATE_UNITS:
        active, enabled = _unit_state(unit, run)
        if enabled != "enabled" or active == "failed":
            problems.append(f"{unit} {enabled}/{active}")
    if problems:
        report.line(FAIL, "updates", "signed updater: " + "; ".join(problems),
                    "run the installer again (see INSTALL.md)")
        return
    try:
        mode = json.loads(policy.read_text())["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        mode = "unknown"
    version = Path(target).name.split("-", 1)[0]
    report.line(PASS, "updates", f"signed updater is on ({mode}), running {version}")


def check_wacli(report, run=subprocess.run):
    result = _run([str(WACLI), "--version"], run=run, timeout=20)
    if result.returncode == 0 and result.stdout.startswith("wacli "):
        report.line(PASS, "wacli", result.stdout.strip() + " is installed")
    else:
        report.line(FAIL, "wacli", "wacli is missing or broken", "run the installer again with --mode wacli")


def doctor(out=sys.stdout, *, env_path=ENV_PATH, run=subprocess.run, opener=urllib.request.urlopen,
           audio_loader=_load_audio_config):
    env = read_env(env_path)
    mode = transport(env)
    report = Report(out)
    print(f"Button Box doctor ({'Cloud' if mode == 'cloud' else 'self-hosted'} box)", file=out)
    check_services(report, mode, run=run)
    check_permissions(report, run=run)
    check_audio(report, loader=audio_loader)
    check_nfc(report, run=run)
    if mode == "cloud":
        check_cloud(report, env, opener=opener)
        check_updater(report, run=run)
    else:
        check_wacli(report, run=run)
        report.line(INFO, "updates", "signed updates are for Cloud boxes; self-hosted boxes update with provision.sh")
    failed, warned = report.counts[FAIL], report.counts[WARN]
    print(f"Result: {failed} failed, {warned} warning(s).", file=out)
    return 1 if failed else 0


# ------------------------------------------------------------ claim link


def setup_url(result, api_url):
    """Return the validated public setup link from a setup check-in reply."""
    code = result.get("setup_code")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError("setup code is missing or invalid")
    url = result.get("setup_url")
    if url is None:
        api = urlsplit(api_url)
        url = f"https://{api.netloc}/s/{code}"
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path != f"/s/{code}"):
        raise ValueError("setup link is invalid")
    return url


def cloud_register(environ=None, client=None, out=sys.stdout):
    """Run as the messagebox user. Prints JSON without tokens or identity."""
    from messagebox.cloud_device import CloudDeviceClient, CloudDeviceError, CloudRateLimited, capabilities
    from messagebox.identity import read_box_color
    from messagebox.runtime_paths import NFC_HEALTH_FILE

    environ = os.environ if environ is None else environ
    try:
        client = client or CloudDeviceClient.from_environment(environ)
        try:
            nfc = NFC_HEALTH_FILE.exists()
        except OSError:
            nfc = False
        # The same registration request the setup page sends.
        registered = client.register({**capabilities(nfc=nfc), "natural_registration_text": True,
                                      "color": read_box_color()})
        if registered.get("claimed") is True:
            print(json.dumps({"claimed": True}), file=out)
            return 0
        checkin = client.setup_checkin()
        if checkin.get("claimed") is True:
            print(json.dumps({"claimed": True}), file=out)
            return 0
        url = setup_url(checkin, environ.get("MSGBOX_CLOUD_API_URL", DEFAULT_CLOUD_API))
    except CloudRateLimited:
        print(json.dumps({"error": "rate_limited"}), file=out)
        return 1
    except (CloudDeviceError, ValueError, TypeError, AttributeError):
        print(json.dumps({"error": "cloud_unavailable"}), file=out)
        return 1
    print(json.dumps({"claimed": False, "setup_url": url}), file=out)
    return 0


def qr_text(url, run=subprocess.run):
    """Terminal QR code: qrencode when installed, else the bundled encoder."""
    if shutil.which("qrencode"):
        result = _run(["qrencode", "-t", "ansiutf8", "-m", "2", url], run=run, timeout=20)
        if result.returncode == 0 and result.stdout:
            return result.stdout
    from messagebox.qrcodegen import QrCode
    qr = QrCode.encode_text(url, QrCode.Ecc.MEDIUM)
    size, border = qr.get_size(), 2

    def dark(x, y):
        return 0 <= x < size and 0 <= y < size and qr.get_module(x, y)

    rows = []
    for y in range(-border, size + border, 2):
        row = ""
        for x in range(-border, size + border):
            top, bottom = dark(x, y), dark(x, y + 1)
            # Light-on-dark terminals: print light modules as blocks.
            row += {(False, False): "█", (True, False): "▄", (False, True): "▀", (True, True): " "}[(top, bottom)]
        rows.append(row)
    return "\n".join(rows) + "\n"


def _open_tty():
    try:
        return open("/dev/tty", "r+", encoding="utf-8", buffering=1)
    except OSError:
        return None


def _wifi_ssid(run=subprocess.run):
    """The SSID wlan0 is connected to now, or None. Never printed."""
    state = _run(["nmcli", "-g", "GENERAL.STATE,GENERAL.CONNECTION", "device", "show", "wlan0"], run=run)
    lines = state.stdout.splitlines()
    if state.returncode != 0 or len(lines) < 2 or not lines[0].startswith("100"):
        return None
    ssid = _run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", lines[1]], run=run)
    value = ssid.stdout.strip()
    return value if ssid.returncode == 0 and value else None


def adopt_home_wifi(run=subprocess.run, state_path=ONBOARDING_STATE):
    """Let setup continue from the Wi-Fi this Pi already uses.

    Returns None when wlan0 is not connected, True when setup can continue on
    this Wi-Fi, and False when the setup state could not be updated.

    The setup page normally records the network the parent picks. Here the Pi is
    already online, so record that connection as chosen and dispatched. The
    setup service then proves connectivity itself before it checks in with Cloud.
    """
    from messagebox.onboarding.state import WIFI_FAILED, WIFI_SELECT, StateError, StateStore

    ssid = _wifi_ssid(run)
    if ssid is None:
        return None
    account = pwd.getpwnam(ONBOARDING_USER)
    store = StateStore(state_path, owner=(account.pw_uid, account.pw_gid))
    try:
        state = store.load()
        if state["phase"] not in {WIFI_SELECT, WIFI_FAILED}:
            return True
        state = store.begin_connect(ssid)
        store.mark_dispatched(state["generation"])
    except StateError:
        return False
    return True


def arm_claim_setup(out, run=subprocess.run, tty_opener=_open_tty):
    """Open Cloud claim setup without deleting Wi-Fi. Returns (armed, reason)."""
    if not ONBOARDING_CONFIGURED.exists():
        tty = tty_opener()
        if tty is None:
            return False, "unattended"
        with tty:
            print("\nThe box makes its own setup Wi-Fi only when it cannot reach your home Wi-Fi.\n"
                  "Write down its setup Wi-Fi password now. It is shown only here, never logged.",
                  file=tty)
            result = _run(["/usr/local/sbin/messagebox-init-wifi-onboarding"], run=run,
                          stdin=tty, stdout=tty, stderr=tty, capture_output=False, timeout=None)
        if result.returncode != 0:
            return False, "declined"
    adopted = adopt_home_wifi(run)
    if adopted is False:
        return False, "setup_state"
    if adopted is None:
        print("This Pi is not on Wi-Fi, so it opens its setup Wi-Fi. Join it from your phone.", file=out)
    result = _run(["/usr/bin/python3", "-m", "messagebox.onboarding.mode", "--enter-cloud-claim"],
                  run=run, cwd=str(APP_DIR), timeout=120)
    if result.returncode != 0:
        return False, "setup_failed"
    # setup.sh only enables these watchers; without them a claim made before
    # the next reboot never completes setup.
    result = _run(["systemctl", "start", *SETUP_WATCHERS], run=run, timeout=60)
    if result.returncode != 0:
        return False, "setup_failed"
    return True, "armed"


def claim_link(out=sys.stdout, *, run=subprocess.run, env_path=ENV_PATH, arm=True, tty_opener=_open_tty):
    env = read_env(env_path)
    if transport(env) != "cloud":
        print("This is a self-hosted box. It has no Cloud setup link.", file=out)
        return 1
    result = as_service_user(["/usr/bin/python3", "-m", "messagebox.self_install", "cloud-register"], run=run)
    try:
        reply = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        reply = {"error": "cloud_unavailable"}
    if reply.get("claimed") is True:
        print("This box is already set up in Button Box Cloud. Manage it from your Cloud dashboard.", file=out)
        return 0
    if "setup_url" not in reply:
        wait = " Cloud asked the box to wait; try again in a minute." if reply.get("error") == "rate_limited" else ""
        print("Could not register this box with Button Box Cloud." + wait, file=out)
        print("  Fix: check the internet connection (sudo messageboxctl doctor), then: sudo messageboxctl claim-link",
              file=out)
        return 1
    url = reply["setup_url"]
    armed, reason = arm_claim_setup(out, run=run, tty_opener=tty_opener) if arm else (False, "skipped")
    print("\n" + "=" * 60, file=out)
    print("Set up your Button Box: scan this code with your phone", file=out)
    print("=" * 60, file=out)
    print(qr_text(url, run=run), file=out, end="")
    print(f"Or open: {url}", file=out)
    if armed:
        print("The box is waiting for you. Follow the steps in WhatsApp, then press the\n"
              "box's button when it asks you to.", file=out)
        return 0
    if reason == "skipped":
        return 0
    if reason == "unattended":
        print("Next: open a terminal on the box (ssh) and run: sudo messageboxctl claim-link\n"
              "It shows the setup Wi-Fi password once, then the box waits for your scan.", file=out)
        return 0
    print("Claim setup did not start (" + reason + "). Run: sudo messageboxctl claim-link", file=out)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="messageboxctl", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check this box and print a fix for each problem")
    link = commands.add_parser("claim-link", help="print this Cloud box's setup link and QR code")
    link.add_argument("--no-setup", action="store_true", help="only print the link; do not open claim setup")
    commands.add_parser("cloud-register", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "cloud-register":
        return cloud_register()
    if os.geteuid() != 0:
        print("Run this command with sudo.", file=sys.stderr)
        return 1
    if args.command == "doctor":
        return doctor()
    return claim_link(arm=not args.no_setup)


if __name__ == "__main__":
    raise SystemExit(main())
