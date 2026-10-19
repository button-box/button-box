#!/bin/sh
# Install or repair NFC dependencies, its fixed virtualenv, and systemd unit.
# Direct usage after runtime deployment: ./scripts/install/nfc.sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$(dirname "$SCRIPT_DIR")")
SERVICE_USER=messagebox
APP_DIR=${MSGBOX_APP_DIR:-/opt/messagebox}
VENV_DIR=$APP_DIR/venv-nfc
UNIT_SOURCE="$REPO_DIR/systemd/messagebox-nfc.service"
UNIT_DEST=/etc/systemd/system/messagebox-nfc.service

if [ "$(id -u)" -eq 0 ]; then
  echo "Run as a sudo-capable administrator, not root." >&2
  exit 1
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  echo "Run scripts/setup.sh first to create the $SERVICE_USER account." >&2
  exit 1
fi
if systemctl is-active --quiet messagebox-nfc.service 2>/dev/null; then
  echo "Stop NFC before repairing it: sudo systemctl stop messagebox-nfc.service" >&2
  exit 1
fi
if [ ! -f "$APP_DIR/messagebox/nfc.py" ] ||
   [ ! -f "$APP_DIR/messagebox/nfc_state.py" ] ||
   [ ! -f "$APP_DIR/messagebox/contacts.py" ]; then
  echo "Expected deployed NFC package sources in $APP_DIR/messagebox" >&2
  exit 1
fi
if [ ! -f "$APP_DIR/config/requirements-nfc.txt" ]; then
  echo "Missing pinned NFC requirements" >&2
  exit 1
fi
if [ ! -r "$UNIT_SOURCE" ]; then
  echo "Missing NFC systemd unit" >&2
  exit 1
fi

if [ "${MSGBOX_SKIP_APT:-0}" != "1" ]; then
  sudo apt-get -o DPkg::Lock::Timeout=600 update
  # Blinka may build lgpio locally on Debian 13 / Python 3.13.
  sudo apt-get -o DPkg::Lock::Timeout=600 install -y python3-dev python3-venv swig liblgpio-dev i2c-tools
fi
sudo raspi-config nonint do_i2c 0
sudo usermod -a -G i2c,gpio "$SERVICE_USER"

sudo install -d -o root -g root -m 0755 "$APP_DIR"
# The venv is root-owned but read by the messagebox service user. Do not
# inherit a strict administrator umask (for example 077) from the caller.
(
  umask 022
  sudo python3 -m venv "$VENV_DIR"
  sudo "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/config/requirements-nfc.txt"
)
# Repair a venv left unreadable by an earlier install. The trailing slash
# follows a venv-nfc symlink to the stable dependency directory; find does
# not follow the Python symlinks inside the venv.
sudo find "$VENV_DIR/" -type d -exec chmod 0755 {} +
sudo find "$VENV_DIR/" ! -type d ! -type l -exec chmod a+rX,go-w {} +

sudo install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_DEST"
sudo systemctl daemon-reload

echo "NFC software and unit installed without changing its enablement."
