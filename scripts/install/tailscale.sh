#!/bin/sh
# Install Tailscale from its official Debian repository without enrolling the
# device. Enrollment remains an explicit, interactive operator action.
set -eu

die() {
  echo "error: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "run this installer as root"
[ -r /etc/os-release ] || die "cannot identify the operating system"
. /etc/os-release

[ "${VERSION_CODENAME:-}" = trixie ] ||
  die "Tailscale provisioning is validated only on Debian 13 (trixie)"
[ "$(dpkg --print-architecture)" = arm64 ] ||
  die "Tailscale provisioning is validated only on 64-bit Raspberry Pi OS"
case "${ID:-}" in
  debian|raspbian) DISTRIBUTION=$ID ;;
  *) die "Tailscale provisioning requires Debian or Raspberry Pi OS" ;;
esac

KEYRING=/usr/share/keyrings/tailscale-archive-keyring.gpg
REPOSITORY=/etc/apt/sources.list.d/tailscale.list
for path in "$KEYRING" "$REPOSITORY"; do
  [ ! -L "$path" ] || die "refusing to replace symlinked $path"
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl

STAGING=$(mktemp -d /tmp/messagebox-tailscale.XXXXXX)
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -rf -- "$STAGING"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

curl --proto '=https' --tlsv1.2 -fsSL \
  "https://pkgs.tailscale.com/stable/$DISTRIBUTION/trixie.noarmor.gpg" \
  -o "$STAGING/tailscale-archive-keyring.gpg"
printf '%s%s%s\n' \
  'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/' \
  "$DISTRIBUTION" \
  ' trixie main' \
  >"$STAGING/tailscale.list"
install -o root -g root -m 0644 \
  "$STAGING/tailscale-archive-keyring.gpg" "$KEYRING"
install -o root -g root -m 0644 "$STAGING/tailscale.list" "$REPOSITORY"

apt-get update
if ! dpkg-query -W -f='${Status}\n' tailscale 2>/dev/null |
  grep -qx 'install ok installed'; then
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled

# Shipped boxes are updated deliberately after a support check, not by an
# unattended package run that could remove the active recovery path.
apt-mark hold tailscale >/dev/null

echo "Tailscale is installed and held for deliberate updates."
