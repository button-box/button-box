#!/bin/sh
# Reset consumer onboarding on a disposable test box, then deploy this tree.
# Supports direct Ethernet, routed Ethernet, and macOS Internet Sharing.
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
    "  $0 admin@message-box-001.local" \
    "  $0 admin@10.77.77.77" >&2
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
box_id=${BOX_HOSTNAME#message-box-}
case "$BOX_HOSTNAME" in
  message-box-*) ;;
  *) die "remote hostname is not a valid message-box hostname" ;;
esac
case "$box_id" in
  ''|*[!a-z0-9-]*) die "remote hostname is not a valid message-box hostname" ;;
esac
[ "${#box_id}" -le 32 ] || die "remote hostname is not a valid message-box hostname"
case "$MACHINE_ID" in
  *[!a-f0-9]*) die "remote machine identity is invalid" ;;
esac
[ "${#MACHINE_ID}" -eq 32 ] || die "remote machine identity is invalid"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" sudo -n true ||
  die "passwordless sudo is required for this dev helper"

cat <<EOF

MESSAGE BOX TEST REPROVISION

Target:   $TARGET
Hostname: $BOX_HOSTNAME

This deletes the generated Wi-Fi onboarding credentials and state, WhatsApp
pairing state, the Message Box WhatsApp store, and pending outbound recordings.
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

units="messagebox.target messagebox-button.service messagebox-sync.service messagebox-poller.service messagebox-dash.service messagebox-nfc.service messagebox-wifi-reset.service messagebox-onboarding-home.service messagebox-whatsapp-pairing.service comitup-web.service comitup.service"
systemctl stop $units 2>/dev/null || true
systemctl disable messagebox-wifi-reset.service comitup.service 2>/dev/null || true
for unit in $units; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  case "$state" in
    active|activating|deactivating)
      echo "Could not stop $unit ($state)." >&2
      exit 1
      ;;
  esac
done

nft delete table inet messagebox_onboarding 2>/dev/null || true
rm -f \
  /etc/messagebox-onboarding/enabled \
  /etc/messagebox-onboarding/configured \
  /etc/messagebox-onboarding/config.json \
  /etc/comitup.conf \
  /etc/comitup.conf.tmp \
  /var/lib/comitup/dhcpleaseinfo
rm -rf \
  /var/lib/messagebox-onboarding \
  /var/lib/messagebox/outbox \
  /var/lib/messagebox/whatsapp-pairing \
  /var/lib/messagebox/wacli \
  /run/messagebox-whatsapp-pairing
systemctl reset-failed $units 2>/dev/null || true
REMOTE

echo "Deploying the current working tree..."
"$REPO_DIR/scripts/provision.sh" "$TARGET"
