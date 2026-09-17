#!/bin/sh
# Reset consumer onboarding on a disposable test box, then deploy this tree.
# Supports routed Ethernet and macOS Internet Sharing.
#
# This helper is intentionally narrower than a clean-card installation. It
# preserves Raspberry Pi OS, packages, users, NetworkManager profiles, contacts,
# the incoming queue, and the installed Comitup package. It deletes generated
# Wi-Fi onboarding credentials/state, WhatsApp authentication state, and pending
# outbound recordings, then calls the canonical provision.sh. Use a freshly
# imaged card to validate the true first-install path.
set -eu

usage() {
  printf '%s\n' \
    "Usage: $0 user@host" \
    "Examples:" \
    "  $0 admin@button-box-001.local" >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

[ "$#" -eq 1 ] || { usage; exit 2; }
TARGET=$1

case "$TARGET" in
  root@*|-*|@*|*@|*[!A-Za-z0-9._@-]*|*@*@*)
    die "invalid non-root SSH target: $TARGET"
    ;;
  *@*) ;;
  *) die "SSH target must be in user@host form" ;;
esac
[ -t 0 ] && [ -t 1 ] || die "reprovisioning requires an interactive terminal"
command -v ssh >/dev/null 2>&1 || die "ssh is required"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$(dirname "$SCRIPT_DIR")")

BOX_HOSTNAME=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" hostname)
MACHINE_ID=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" cat /etc/machine-id)
case "$BOX_HOSTNAME" in
  button-box-*) box_id=${BOX_HOSTNAME#button-box-} ;;
  message-box-*) box_id=${BOX_HOSTNAME#message-box-} ;;
  *) die "remote hostname is not a valid Button Box hostname" ;;
esac
case "$box_id" in
  ''|*[!a-z0-9-]*) die "remote hostname is not a valid Button Box hostname" ;;
esac
[ "${#box_id}" -le 32 ] || die "remote hostname is not a valid Button Box hostname"
case "$MACHINE_ID" in
  *[!a-f0-9]*) die "remote machine identity is invalid" ;;
esac
[ "${#MACHINE_ID}" -eq 32 ] || die "remote machine identity is invalid"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" sudo -n true ||
  die "passwordless sudo is required for this dev helper"

cat <<EOF

BUTTON BOX TEST REPROVISION

Target:   $TARGET
Hostname: $BOX_HOSTNAME

This deletes the generated Wi-Fi onboarding credentials and state, WhatsApp
pairing state, the Button Box WhatsApp store, and pending outbound recordings.
It preserves the operating system, packages, service users, current network
profile, contacts, incoming queue, and hardware configuration. It is not a
clean-card test.

Continue? [y/N]
EOF
IFS= read -r confirmation
case "$confirmation" in
  y|Y|yes|YES|Yes) ;;
  *) die "reprovisioning cancelled; nothing was changed" ;;
esac

echo "Stopping onboarding and clearing test credentials..."
ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" \
  "sudo -n env EXPECTED_HOST=$BOX_HOSTNAME EXPECTED_MACHINE=$MACHINE_ID /bin/sh -s" <<'REMOTE'
set -eu

[ "$(hostname)" = "$EXPECTED_HOST" ] || {
  echo "Remote hostname changed; refusing cleanup." >&2
  exit 1
}
[ "$(cat /etc/machine-id)" = "$EXPECTED_MACHINE" ] || {
  echo "Remote machine identity changed; refusing cleanup." >&2
  exit 1
}
[ ! -L /etc/comitup.conf ] || {
  echo "Refusing to remove symlinked /etc/comitup.conf." >&2
  exit 1
}

umask 077
exec 8>/run/lock/messagebox-init-wifi-onboarding.lock
flock -n 8 || { echo "Wi-Fi initialization is running." >&2; exit 1; }
exec 9>/run/lock/messagebox-comitup-install.lock
flock -n 9 || { echo "Comitup installation is running." >&2; exit 1; }
python3 - <<'PY'
import fcntl
import os
import shutil
import stat
import subprocess
import sys

service_unit = "messagebox-mode-reconcile.service"
path_unit = "messagebox-mode-reconcile.path"
load_state = subprocess.run(
    ["systemctl", "show", "--property=LoadState", "--value", service_unit],
    check=False,
    stdout=subprocess.PIPE,
    text=True,
).stdout.strip()
selector_artifacts = (
    "/usr/local/lib/systemd/system-generators/messagebox-mode-generator",
    "/etc/systemd/system/messagebox-mode-reconcile.service",
    "/etc/systemd/system/messagebox-mode-reconcile.path",
)
artifacts_present = tuple(os.path.lexists(path) for path in selector_artifacts)
if load_state == "loaded" and all(artifacts_present):
    selector_installed = True
elif load_state in {"", "not-found"} and not any(
    artifacts_present
):
    selector_installed = False
else:
    raise SystemExit("The boot-mode selector is partially installed or unavailable.")

def stop_worker():
    stopped = subprocess.run(["systemctl", "stop", service_unit], check=False)
    state = subprocess.run(
        ["systemctl", "is-active", service_unit],
        check=False,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    if stopped.returncode != 0 or state in {"active", "activating", "deactivating"}:
        raise RuntimeError(f"Could not quiesce {service_unit} ({state or 'unknown'}).")

lock_fd = None
try:
    if selector_installed:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open("/run/lock/messagebox-mode-transition.lock", flags, 0o600)
        metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeError("Mode transition lock is unavailable or unsafe.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A mode transition is running.") from exc

        path_active = subprocess.run(
            ["systemctl", "is-active", "--quiet", path_unit], check=False
        ).returncode == 0
        if path_active:
            stopped = subprocess.run(["systemctl", "stop", path_unit], check=False)
            still_active = subprocess.run(
                ["systemctl", "is-active", "--quiet", path_unit], check=False
            ).returncode == 0
            if stopped.returncode != 0 or still_active:
                raise RuntimeError("Could not quiesce the mode reconciliation path.")

        # An active worker either held the lock and made the acquisition fail,
        # or is still waiting. Stop it only after this cleanup owns the lock.
        stop_worker()
        try:
            os.unlink("/run/messagebox-mode-reconcile.pending")
        except FileNotFoundError:
            pass
        run_directory = os.open(
            "/run", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(run_directory)
        finally:
            os.close(run_directory)

    units = (
        "messagebox.target",
        "messagebox-button.service",
        "messagebox-sync.service",
        "messagebox-poller.service",
        "messagebox-dash.service",
        "messagebox-nfc.service",
        "messagebox-wifi-reset.service",
        "messagebox-wifi-change.path",
        "messagebox-wifi-change.service",
        "messagebox-onboarding-home.service",
        "messagebox-onboarding-button.service",
        "messagebox-onboarding-voice-gate.service",
        "messagebox-onboarding-voice.path",
        "messagebox-onboarding-voice.target",
        "messagebox-onboarding-nfc.service",
        "messagebox-onboarding-complete.service",
        "messagebox-onboarding-complete.path",
        "messagebox-whatsapp-pairing.service",
        "comitup-web.service",
        "comitup.service",
    )
    subprocess.run(["systemctl", "stop", *units], check=False)
    subprocess.run(
        ["systemctl", "disable", "messagebox-wifi-reset.service", "comitup.service"],
        check=False,
    )
    for unit in units:
        state = subprocess.run(
            ["systemctl", "is-active", unit],
            check=False,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        if state in {"active", "activating", "deactivating"}:
            raise RuntimeError(f"Could not stop {unit} ({state}).")

    subprocess.run(
        ["nft", "delete", "table", "inet", "messagebox_onboarding"], check=False
    )
    for path in (
        "/etc/messagebox-onboarding/enabled",
        "/etc/messagebox-onboarding/configured",
        "/etc/messagebox-onboarding/config.json",
        "/etc/comitup.conf",
        "/etc/comitup.conf.tmp",
        "/var/lib/comitup/dhcpleaseinfo",
    ):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    for path in (
        "/var/lib/messagebox-onboarding",
        "/var/lib/messagebox/outbox",
        "/var/lib/messagebox/whatsapp-pairing",
        "/var/lib/messagebox/wacli",
        "/run/messagebox-whatsapp-pairing",
        "/run/messagebox-onboarding-nfc",
    ):
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    subprocess.run(["systemctl", "reset-failed", *units], check=False)
except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
finally:
    if lock_fd is not None:
        os.close(lock_fd)
PY
REMOTE

echo "Deploying the current working tree..."
"$REPO_DIR/scripts/provision.sh" "$TARGET"
