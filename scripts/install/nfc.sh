#!/bin/sh
# Install or repair NFC dependencies, its fixed virtualenv, and systemd unit.
# Direct usage after runtime deployment: ./scripts/install/nfc.sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$(dirname "$SCRIPT_DIR")")
SERVICE_USER=messagebox
APP_DIR=/opt/messagebox
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
if [ ! -f "$APP_DIR/nfc.py" ] || [ ! -f "$APP_DIR/nfc_state.py" ] ||
   [ ! -f "$APP_DIR/contacts.py" ]; then
  echo "Expected deployed NFC sources in $APP_DIR" >&2
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
  sudo apt-get update
  # Blinka may build lgpio locally on Debian 13 / Python 3.13.
  sudo apt-get install -y python3-dev python3-venv swig liblgpio-dev i2c-tools
fi
sudo raspi-config nonint do_i2c 0
sudo usermod -a -G i2c,gpio "$SERVICE_USER"

sudo install -d -o root -g root -m 0755 "$APP_DIR"
sudo python3 -m venv "$VENV_DIR"
sudo "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/config/requirements-nfc.txt"

sudo install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_DEST"
sudo systemctl daemon-reload

echo "NFC software and unit installed without changing its enablement."
