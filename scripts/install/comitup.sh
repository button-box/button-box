#!/bin/sh
# Install the pinned Comitup package without activating Wi-Fi onboarding.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$(dirname "$SCRIPT_DIR")")
COMITUP_VERSION=1.47.1-1
COMITUP_URL=https://davesteele.github.io/comitup/repo/pool/main/c/comitup/comitup_1.47.1-1_all.deb
COMITUP_SHA256=2a2456e00ae7cf2f12dde4a47f767fd2bd3dc57924e11cb510e8f0b3eaa4e78a
NETWORKMANAGER_VERSION=2.2-3
NETWORKMANAGER_URL=https://deb.debian.org/debian/pool/main/p/python-networkmanager/python3-networkmanager_2.2-3_all.deb
NETWORKMANAGER_SHA256=730555a2c6d362dd25bc776df026ab0a4a26598846b178f46e557150aaa3f90c
INSTALL_LOCK=/run/lock/messagebox-comitup-install.lock
DBUS_POLICY=/usr/share/dbus-1/system.d/comitup-dbus.conf
DBUS_DIVERT=/usr/share/dbus-1/system.d/comitup-dbus.conf.distrib
TMP_DIR=

die() {
  echo "$*" >&2
  exit 1
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  sudo systemctl stop comitup-web.service messagebox-onboarding-home.service messagebox-whatsapp-pairing.service comitup.service 2>/dev/null || true
  sudo systemctl disable comitup.service 2>/dev/null || true
  [ -z "$TMP_DIR" ] || sudo rm -rf -- "$TMP_DIR"
  exit "$status"
}

[ "$(id -u)" -ne 0 ] || die "Run as a sudo-capable administrator, not root."
command -v sudo >/dev/null 2>&1 || die "sudo is required."
# Validate root access through the same command policy used by the installer.
sudo true
sudo touch "$INSTALL_LOCK"
sudo chmod 0666 "$INSTALL_LOCK"
exec 9>"$INSTALL_LOCK"
flock -n 9 || die "Another Comitup installation is running."

[ "$#" -eq 0 ] || die "Usage: $0"

[ ! -e /etc/messagebox-onboarding/enabled ] || die "Refusing to modify explicitly armed onboarding."
TMP_DIR=$(mktemp -d /tmp/messagebox-comitup.XXXXXX)
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

[ "$(uname -m)" = aarch64 ] || die "Comitup onboarding requires aarch64."
[ "$(dpkg --print-architecture)" = arm64 ] || die "Comitup onboarding requires Debian arm64."
[ -r /proc/device-tree/model ] || die "Raspberry Pi model information is unavailable."
case $(tr -d '\000' </proc/device-tree/model) in
  *"Raspberry Pi 4 Model"*) ;;
  *) die "Comitup onboarding is validated only on Raspberry Pi 4." ;;
esac
. /etc/os-release
[ "${VERSION_CODENAME:-}" = trixie ] || die "Raspberry Pi OS/Debian trixie is required."
systemctl is-active --quiet NetworkManager.service || die "NetworkManager must be active."
nmcli -g GENERAL.NM-MANAGED device show wlan0 2>/dev/null | grep -qx yes || die "NetworkManager must manage wlan0."
systemctl is-active --quiet avahi-daemon.service || die "Avahi must already be active."
systemctl is-enabled --quiet avahi-daemon.service || die "Avahi must already be enabled."
getent passwd messagebox-onboarding >/dev/null || die "The messagebox-onboarding account must exist first."

DROPIN_SOURCE="$REPO_DIR/systemd/onboarding/comitup.service.d/messagebox.conf"
DBUS_SOURCE="$REPO_DIR/config/onboarding/comitup-dbus.conf"
[ -r "$DROPIN_SOURCE" ] || die "Missing Comitup systemd drop-in."
[ -r "$DBUS_SOURCE" ] || die "Missing Comitup D-Bus policy."

for path in \
  /boot/comitup.conf \
  /boot/firmware/comitup.conf \
  /usr/local/bin/comitup-callback \
  /etc/NetworkManager/dnsmasq-shared.d/nm-dns-sabotage.conf; do
  [ ! -e "$path" ] && [ ! -L "$path" ] || die "Refusing conflicting path: $path"
done
if [ -L "$DBUS_POLICY" ] || [ -L "$DBUS_DIVERT" ]; then
  die "Refusing a symlinked Comitup D-Bus policy."
fi

# This condition is installed before apt invokes Comitup's enabling postinst.
sudo install -d -o root -g root -m 0755 /etc/systemd/system/comitup.service.d
sudo install -o root -g root -m 0644 "$DROPIN_SOURCE" \
  /etc/systemd/system/comitup.service.d/messagebox.conf
sudo systemctl daemon-reload
sudo systemctl stop comitup-web.service messagebox-onboarding-home.service messagebox-whatsapp-pairing.service comitup.service 2>/dev/null || true
sudo systemctl disable comitup.service 2>/dev/null || true

divert_owner=$(sudo dpkg-divert --listpackage "$DBUS_POLICY" 2>/dev/null || true)
case "$divert_owner" in
  '') sudo dpkg-divert --quiet --local --add --rename --divert "$DBUS_DIVERT" "$DBUS_POLICY" ;;
  LOCAL) ;;
  *) die "The Comitup D-Bus policy is diverted by $divert_owner." ;;
esac
sudo install -o root -g root -m 0644 "$DBUS_SOURCE" "$DBUS_POLICY"

if [ "$(dpkg-query -W -f='${Version}' comitup 2>/dev/null || true)" != "$COMITUP_VERSION" ] || \
   [ "$(dpkg-query -W -f='${Version}' python3-networkmanager 2>/dev/null || true)" != "$NETWORKMANAGER_VERSION" ]; then
  curl -fL --proto '=https' --tlsv1.2 "$COMITUP_URL" -o "$TMP_DIR/comitup.deb"
  curl -fL --proto '=https' --tlsv1.2 "$NETWORKMANAGER_URL" -o "$TMP_DIR/python3-networkmanager.deb"
  printf '%s  %s\n%s  %s\n' \
    "$COMITUP_SHA256" "$TMP_DIR/comitup.deb" \
    "$NETWORKMANAGER_SHA256" "$TMP_DIR/python3-networkmanager.deb" |
    sha256sum --check --strict

  sudo apt-get install -y --no-install-recommends \
    "$TMP_DIR/python3-networkmanager.deb" "$TMP_DIR/comitup.deb"
fi
sudo busctl call org.freedesktop.DBus /org/freedesktop/DBus \
  org.freedesktop.DBus ReloadConfig
sudo systemctl stop comitup-web.service messagebox-onboarding-home.service messagebox-whatsapp-pairing.service comitup.service 2>/dev/null || true
sudo systemctl disable comitup.service 2>/dev/null || true

[ "$(dpkg-query -W -f='${Version}' comitup)" = "$COMITUP_VERSION" ] || die "Unexpected Comitup version."
[ "$(dpkg-query -W -f='${Version}' python3-networkmanager)" = "$NETWORKMANAGER_VERSION" ] || die "Unexpected python3-networkmanager version."
systemctl is-active --quiet comitup.service && die "comitup.service unexpectedly started."
systemctl is-active --quiet comitup-web.service && die "comitup-web.service unexpectedly started."
[ ! -e /etc/messagebox-onboarding/enabled ] || die "Onboarding was unexpectedly armed."

trap - EXIT HUP INT TERM
sudo rm -rf -- "$TMP_DIR"
TMP_DIR=

echo "Installed Comitup $COMITUP_VERSION; onboarding remains disabled and inactive."
