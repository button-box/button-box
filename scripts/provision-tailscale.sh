#!/bin/sh
# Install and interactively enroll a Button Box in an existing tailnet.
# Usage: ./scripts/provision-tailscale.sh [--hostname NAME] user@host
set -eu

usage() {
  printf '%s\n' \
    "Usage: $0 [--hostname NAME] user@host" \
    "Example: $0 admin@message-box-001.local" >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

TAILSCALE_HOSTNAME=""
case "$#" in
  1)
    TARGET=$1
    ;;
  3)
    [ "$1" = --hostname ] || { usage; exit 2; }
    TAILSCALE_HOSTNAME=$2
    TARGET=$3
    ;;
  *)
    usage
    exit 2
    ;;
esac

case "$TARGET" in
  root@*|-*|@*|*@|*[!A-Za-z0-9._@-]*|*@*@*)
    die "invalid non-root SSH target: $TARGET"
    ;;
  *@*) ;;
  *) die "SSH target must be in user@host form" ;;
esac
command -v ssh >/dev/null 2>&1 || die "ssh is required"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALLER=$SCRIPT_DIR/install/tailscale.sh
[ -r "$INSTALLER" ] || die "missing installer: $INSTALLER"

REMOTE_HOSTNAME=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" hostname)
case "$REMOTE_HOSTNAME" in
  message-box-*) ;;
  *) die "remote hostname is not a valid message-box hostname" ;;
esac
case "$REMOTE_HOSTNAME" in
  *[!A-Za-z0-9-]*|*-)
    die "remote hostname is not a valid message-box hostname"
    ;;
esac
[ "${#REMOTE_HOSTNAME}" -le 63 ] ||
  die "remote hostname is not a valid message-box hostname"

if [ -z "$TAILSCALE_HOSTNAME" ]; then
  TAILSCALE_HOSTNAME=$REMOTE_HOSTNAME
fi
case "$TAILSCALE_HOSTNAME" in
  *[!A-Za-z0-9-]*|-*|*-|"") die "invalid Tailscale hostname" ;;
esac
[ "${#TAILSCALE_HOSTNAME}" -le 63 ] || die "invalid Tailscale hostname"

ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" sudo -n true ||
  die "passwordless sudo is required for remote provisioning"

cat <<EOF

TAILSCALE REMOTE SUPPORT

Target:             $TARGET
Remote hostname:    $REMOTE_HOSTNAME
Tailscale hostname: $TAILSCALE_HOSTNAME

This installs Tailscale from its signed official repository and may open an
interactive authorization URL for the existing tailnet. It does not enable
Tailscale SSH, expose the dashboard, advertise routes, or store an auth key.

Continue? [y/N]
EOF
IFS= read -r confirmation
case "$confirmation" in
  y|Y|yes|YES|Yes) ;;
  *) die "Tailscale provisioning cancelled; nothing was changed" ;;
esac

echo "Installing Tailscale on $REMOTE_HOSTNAME..."
ssh -o BatchMode=yes "$TARGET" sudo -n /bin/sh -s <"$INSTALLER"

TAILSCALE_IP_COMMAND="sudo -n tailscale status --self=true --peers=false 2>/dev/null | awk 'NR == 1 && \$1 ~ /^100\\./ { print \$1 }'"
read_tailscale_ip() {
  ssh -o BatchMode=yes "$TARGET" "$TAILSCALE_IP_COMMAND"
}

TAILSCALE_IP=$(read_tailscale_ip)
if [ -z "$TAILSCALE_IP" ]; then
  echo "Authorize $REMOTE_HOSTNAME in the browser when prompted."
  ssh -t "$TARGET" \
    "sudo -n tailscale up --hostname=$TAILSCALE_HOSTNAME"
  TAILSCALE_IP=$(read_tailscale_ip)
else
  # `tailscale set` changes only the requested preference and therefore does
  # not accidentally enable Tailscale SSH or alter existing route settings.
  ssh -o BatchMode=yes "$TARGET" \
    "sudo -n tailscale set --hostname=$TAILSCALE_HOSTNAME"
fi

case "$TAILSCALE_IP" in
  ""|*[!0-9.]*) die "Tailscale did not report a valid IPv4 address" ;;
esac

SSH_USER=${TARGET%%@*}
cat <<EOF

TAILSCALE REMOTE SUPPORT READY

Device: $TAILSCALE_HOSTNAME
Address: $TAILSCALE_IP

Verify from a different network:
  ssh $SSH_USER@$TAILSCALE_IP

Tailscale carries the connection; ordinary OpenSSH keys still control shell
access. Keep the LAN connection available until that independent test passes.
For a trusted shipped box, review and deliberately disable this device's key
expiry in the Tailscale admin console so unattended access does not expire.
EOF
