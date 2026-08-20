#!/bin/sh
# Install Message Box into fixed system paths under the messagebox service user.
# Usage on the Pi: ./scripts/setup.sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
SERVICE_USER=messagebox
SERVICE_GROUP=messagebox
ONBOARDING_USER=messagebox-onboarding
ONBOARDING_GROUP=messagebox-onboarding
APP_DIR=/opt/messagebox
CONFIG_DIR=/etc/messagebox
DATA_DIR=/var/lib/messagebox
ONBOARDING_CONFIG_DIR=/etc/messagebox-onboarding
ONBOARDING_DATA_DIR=/var/lib/messagebox-onboarding
RUNTIME_PYTHON="button_send.py contacts.py dashboard.py guided_reply.py listened_receipts.py
make_ringtones.py nfc.py nfc_state.py runtime_paths.py voicepoll.py"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run as a sudo-capable administrator, not root." >&2
  exit 1
fi

for name in $RUNTIME_PYTHON; do
  if [ ! -r "$REPO_DIR/src/$name" ]; then
    echo "Missing repository file: src/$name" >&2
    exit 1
  fi
done
for path in \
  config/onboarding/comitup.conf.template \
  config/onboarding/comitup-dbus.conf \
  config/onboarding/firewall.nft \
  scripts/install/comitup.sh \
  scripts/configure-wifi.sh \
  scripts/onboard.sh \
  scripts/messageboxctl \
  src/dashboard_static/app.js \
  src/dashboard_static/index.html \
  src/dashboard_static/styles.css \
  src/onboarding/app.py \
  src/onboarding/comitup_adapter.py \
  src/onboarding/connectivity.py \
  src/onboarding/__init__.py \
  src/onboarding/reset.py \
  src/onboarding/state.py \
  src/onboarding/whatsapp.py \
  src/onboarding/static/app.js \
  src/onboarding/static/index.html \
  src/onboarding/static/styles.css \
  systemd/messagebox-button.service \
  systemd/onboarding/comitup.service.d/messagebox.conf \
  systemd/onboarding/comitup-web.service.d/messagebox.conf \
  systemd/onboarding/messagebox-onboarding-home.service \
  systemd/onboarding/messagebox-whatsapp-pairing.service \
  systemd/onboarding/messagebox-wifi-reset.service \
  systemd/messagebox.target \
  systemd/messagebox.tmpfiles.conf; do
  if [ ! -r "$REPO_DIR/$path" ]; then
    echo "Missing repository file: $path" >&2
    exit 1
  fi
done

[ ! -e "$ONBOARDING_CONFIG_DIR/enabled" ] || {
  echo "Refusing to update while Wi-Fi onboarding is armed." >&2
  exit 1
}

for unit in \
  messagebox.target \
  messagebox-button.service \
  messagebox-sync.service \
  messagebox-poller.service \
  messagebox-dash.service \
  messagebox-nfc.service \
  comitup.service \
  comitup-web.service \
  messagebox-onboarding-home.service \
  messagebox-whatsapp-pairing.service; do
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    echo "Stop Message Box before updating: sudo messageboxctl stop" >&2
    exit 1
  fi
done

if account=$(getent passwd "$SERVICE_USER"); then
  account_home=$(printf '%s\n' "$account" | cut -d: -f6)
  account_shell=$(printf '%s\n' "$account" | cut -d: -f7)
  if [ "$account_home" != "$DATA_DIR" ] || [ "$account_shell" != "/usr/sbin/nologin" ]; then
    echo "Existing $SERVICE_USER account must use home $DATA_DIR and /usr/sbin/nologin." >&2
    exit 1
  fi
else
  sudo useradd \
    --system \
    --user-group \
    --home-dir "$DATA_DIR" \
    --create-home \
    --shell /usr/sbin/nologin \
    "$SERVICE_USER"
fi

if account=$(getent passwd "$ONBOARDING_USER"); then
  account_home=$(printf '%s\n' "$account" | cut -d: -f6)
  account_shell=$(printf '%s\n' "$account" | cut -d: -f7)
  if [ "$account_home" != "$ONBOARDING_DATA_DIR" ] || [ "$account_shell" != "/usr/sbin/nologin" ]; then
    echo "Existing $ONBOARDING_USER account must use home $ONBOARDING_DATA_DIR and /usr/sbin/nologin." >&2
    exit 1
  fi
else
  sudo useradd \
    --system \
    --user-group \
    --home-dir "$ONBOARDING_DATA_DIR" \
    --create-home \
    --shell /usr/sbin/nologin \
    "$ONBOARDING_USER"
fi

echo "Installing Message Box in $APP_DIR as $SERVICE_USER"

sudo apt-get update
sudo apt-get install -y \
  alsa-utils ca-certificates curl ffmpeg gunicorn=23.0.0-1 i2c-tools nftables \
  liblgpio-dev python3-dev python3-gpiozero python3-lgpio python3-venv swig

sudo usermod -a -G audio,gpio,i2c "$SERVICE_USER"

sudo install -d -o root -g root -m 0755 \
  "$APP_DIR" \
  "$APP_DIR/config" \
  "$APP_DIR/dashboard_static" \
  "$APP_DIR/onboarding" \
  "$APP_DIR/onboarding/static" \
  "$APP_DIR/ringtones" \
  "$APP_DIR/sounds/guided-reply" \
  "$APP_DIR/sounds/listen-receipts" \
  "$APP_DIR/sounds/nfc"
sudo install -d -o root -g "$SERVICE_GROUP" -m 0750 "$CONFIG_DIR"
sudo install -d -o root -g "$ONBOARDING_GROUP" -m 0750 "$ONBOARDING_CONFIG_DIR"
sudo install -d -o "$ONBOARDING_USER" -g "$ONBOARDING_GROUP" -m 0700 "$ONBOARDING_DATA_DIR"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 \
  "$DATA_DIR" \
  "$DATA_DIR/assets" \
  "$DATA_DIR/outbox" \
  "$DATA_DIR/queue" \
  "$DATA_DIR/state" \
  "$DATA_DIR/wacli"

for name in $RUNTIME_PYTHON; do
  sudo install -o root -g root -m 0755 "$REPO_DIR/src/$name" "$APP_DIR/$name"
done
sudo install -o root -g root -m 0644 \
  "$REPO_DIR/src/dashboard_static/app.js" \
  "$REPO_DIR/src/dashboard_static/index.html" \
  "$REPO_DIR/src/dashboard_static/styles.css" \
  "$APP_DIR/dashboard_static/"
sudo install -o root -g root -m 0755 "$REPO_DIR/src/syncloop.sh" "$APP_DIR/syncloop.sh"
sudo install -o root -g root -m 0755 "$REPO_DIR/scripts/test.sh" "$APP_DIR/test.sh"
for source in "$REPO_DIR"/src/onboarding/*.py; do
  sudo install -o root -g root -m 0644 "$source" "$APP_DIR/onboarding/$(basename "$source")"
done
sudo rm -f "$APP_DIR/onboarding/auth.py" "$ONBOARDING_DATA_DIR/session.key"
for source in "$REPO_DIR"/src/onboarding/static/*; do
  sudo install -o root -g root -m 0644 "$source" "$APP_DIR/onboarding/static/$(basename "$source")"
done

sudo install -o root -g root -m 0644 \
  "$REPO_DIR/config/requirements-nfc.txt" "$APP_DIR/config/requirements-nfc.txt"
for directory in guided-reply listen-receipts nfc; do
  for source in "$REPO_DIR/sounds/$directory"/*; do
    if [ -f "$source" ]; then
      sudo install -o root -g root -m 0644 "$source" "$APP_DIR/sounds/$directory/$(basename "$source")"
    fi
  done
done

sudo install -o root -g "$SERVICE_GROUP" -m 0640 \
  "$REPO_DIR/config/env.example" "$CONFIG_DIR/env.example"

sudo python3 "$APP_DIR/make_ringtones.py"
"$SCRIPT_DIR/install/wacli.sh"
"$SCRIPT_DIR/install/comitup.sh"

for name in messagebox-button messagebox-sync messagebox-poller messagebox-dash; do
  sudo install -o root -g root -m 0644 \
    "$REPO_DIR/systemd/$name.service" "/etc/systemd/system/$name.service"
done
sudo install -o root -g root -m 0644 \
  "$REPO_DIR/systemd/messagebox.target" /etc/systemd/system/messagebox.target
sudo install -o root -g root -m 0644 \
  "$REPO_DIR/systemd/messagebox.tmpfiles.conf" /etc/tmpfiles.d/messagebox.conf
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/messageboxctl" /usr/local/bin/messageboxctl
if [ -e /usr/local/bin/contact ] && [ ! -f /usr/local/bin/contact ] &&
   [ ! -L /usr/local/bin/contact ]; then
  echo "Cannot replace non-file path: /usr/local/bin/contact" >&2
  exit 1
fi
sudo ln -sfn "$APP_DIR/contacts.py" /usr/local/bin/contact
MSGBOX_SKIP_APT=1 "$SCRIPT_DIR/install/nfc.sh"
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/onboard.sh" /usr/local/bin/onboard.sh

sudo install -d -o root -g root -m 0755 \
  /usr/share/messagebox/onboarding \
  /etc/systemd/system/comitup-web.service.d
sudo install -o root -g root -m 0644 \
  "$REPO_DIR/config/onboarding/comitup.conf.template" \
  /usr/share/messagebox/onboarding/comitup.conf.template
sudo install -o root -g "$ONBOARDING_GROUP" -m 0640 \
  "$REPO_DIR/config/onboarding/firewall.nft" \
  "$ONBOARDING_CONFIG_DIR/firewall.nft"
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/configure-wifi.sh" \
  /usr/local/sbin/messagebox-configure-wifi
sudo rm -f /usr/local/sbin/messagebox-arm-wifi
sudo install -o root -g root -m 0644 \
  "$REPO_DIR/systemd/onboarding/comitup-web.service.d/messagebox.conf" \
  /etc/systemd/system/comitup-web.service.d/messagebox.conf
for name in messagebox-onboarding-home messagebox-whatsapp-pairing messagebox-wifi-reset; do
  sudo install -o root -g root -m 0644 \
    "$REPO_DIR/systemd/onboarding/$name.service" "/etc/systemd/system/$name.service"
done
sudo systemd-tmpfiles --create /etc/tmpfiles.d/messagebox.conf
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/messagebox.target \
  /etc/systemd/system/messagebox-button.service \
  /etc/systemd/system/messagebox-sync.service \
  /etc/systemd/system/messagebox-poller.service \
  /etc/systemd/system/messagebox-dash.service \
  /etc/systemd/system/messagebox-nfc.service \
  /etc/systemd/system/messagebox-onboarding-home.service \
  /etc/systemd/system/messagebox-whatsapp-pairing.service \
  /etc/systemd/system/messagebox-wifi-reset.service

PYTHONPYCACHEPREFIX=$(mktemp -d)
export PYTHONPYCACHEPREFIX
python3 -m py_compile "$APP_DIR"/*.py
python3 -m py_compile "$APP_DIR"/onboarding/*.py
rm -rf "$PYTHONPYCACHEPREFIX"

if sudo test -e "$ONBOARDING_CONFIG_DIR/configured"; then
  echo "Wi-Fi onboarding remains configured but was not started."
else
  echo "Wi-Fi onboarding was installed but not configured or armed."
  echo "Configure it with: sudo messagebox-configure-wifi"
fi
