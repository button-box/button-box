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
PACKAGE_DIR=$APP_DIR/messagebox
CONFIG_DIR=/etc/messagebox
DATA_DIR=/var/lib/messagebox
ONBOARDING_CONFIG_DIR=/etc/messagebox-onboarding
ONBOARDING_DATA_DIR=/var/lib/messagebox-onboarding
SSH_TARGET=${MESSAGEBOX_SSH_TARGET:-}
PACKAGE_PYTHON="__init__.py button_send.py contacts.py guided_reply.py listened_receipts.py
make_ringtones.py nfc.py nfc_state.py runtime_paths.py voicepoll.py"
DASHBOARD_PYTHON="dashboard/__init__.py dashboard/app.py"
ONBOARDING_PYTHON="onboarding/__init__.py onboarding/app.py
onboarding/comitup_adapter.py onboarding/connectivity.py onboarding/initialize.py
onboarding/paths.py onboarding/recipients.py onboarding/reset.py onboarding/state.py
onboarding/voice_gate.py onboarding/whatsapp.py"
STATIC_ASSETS="dashboard/static/app.js dashboard/static/index.html dashboard/static/styles.css
onboarding/static/app.js onboarding/static/index.html onboarding/static/styles.css"
GUIDED_PROMPT_DIR=$REPO_DIR/sounds/guided-reply

case "$SSH_TARGET" in
  '') ;;
  -*|*[!A-Za-z0-9._@-]*)
    echo "Invalid SSH target supplied for completion instructions." >&2
    exit 2
    ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  echo "Run as a sudo-capable administrator, not root." >&2
  exit 1
fi

for name in $PACKAGE_PYTHON $DASHBOARD_PYTHON $ONBOARDING_PYTHON $STATIC_ASSETS; do
  if [ ! -r "$REPO_DIR/messagebox/$name" ]; then
    echo "Missing repository file: messagebox/$name" >&2
    exit 1
  fi
done
for path in \
  config/env.example \
  config/requirements-nfc.txt \
  config/onboarding/comitup.conf.template \
  config/onboarding/comitup-dbus.conf \
  config/onboarding/firewall.nft \
  scripts/install/comitup.sh \
  scripts/install/nfc.sh \
  scripts/install/wacli.sh \
  scripts/commands/messagebox-comitup-state \
  scripts/commands/messagebox-contact \
  scripts/commands/messagebox-init-wifi-onboarding \
  scripts/dev/onboard.sh \
  scripts/dev/hardware-test.sh \
  scripts/messageboxctl \
  messagebox/syncloop.sh \
  systemd/messagebox-button.service \
  systemd/messagebox-sync.service \
  systemd/messagebox-poller.service \
  systemd/messagebox-dash.service \
  systemd/messagebox-nfc.service \
  systemd/onboarding/comitup.service.d/messagebox.conf \
  systemd/onboarding/comitup-web.service.d/messagebox.conf \
  systemd/onboarding/messagebox-onboarding-home.service \
  systemd/onboarding/messagebox-onboarding-button.service \
  systemd/onboarding/messagebox-onboarding-voice-gate.service \
  systemd/onboarding/messagebox-onboarding-voice.path \
  systemd/onboarding/messagebox-onboarding-voice.target \
  systemd/onboarding/messagebox-whatsapp-pairing.service \
  systemd/onboarding/messagebox-wifi-reset.service \
  systemd/messagebox.target \
  systemd/messagebox.tmpfiles.conf; do
  if [ ! -r "$REPO_DIR/$path" ]; then
    echo "Missing repository file: $path" >&2
    exit 1
  fi
done

PYTHONDONTWRITEBYTECODE=1 python3 - "$GUIDED_PROMPT_DIR" <<'PY'
import sys
import wave
from pathlib import Path

root = Path(sys.argv[1])
invalid = []
for name in (
    "reply-countdown.wav",
    "standalone-countdown.wav",
    "press-to-send.wav",
    "delete-warning.wav",
    "not-sent.wav",
):
    path = root / name
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        with wave.open(str(path), "rb") as prompt:
            if prompt.getnframes() <= 0 or prompt.getnchannels() != 1:
                raise wave.Error
    except (OSError, EOFError, wave.Error):
        invalid.append(name)
if invalid:
    print(
        "Missing or invalid guided-reply prompts: " + ", ".join(invalid),
        file=sys.stderr,
    )
    print(
        "Supply a complete licensed prompt set before running setup.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY

for destination in \
  /usr/local/bin/messagebox-contact \
  /usr/local/bin/messagebox-dev-onboard \
  /usr/local/bin/messageboxctl \
  /usr/local/sbin/messagebox-comitup-state \
  /usr/local/sbin/messagebox-init-wifi-onboarding; do
  if [ -L "$destination" ] || { [ -e "$destination" ] && [ ! -f "$destination" ]; }; then
    echo "Cannot replace non-regular command path: $destination" >&2
    exit 1
  fi
done

if [ -L "$APP_DIR" ] || { [ -e "$APP_DIR" ] && [ ! -d "$APP_DIR" ]; }; then
  echo "Cannot install into non-directory application path: $APP_DIR" >&2
  exit 1
fi
if [ -L "$CONFIG_DIR/env" ] || { [ -e "$CONFIG_DIR/env" ] && [ ! -f "$CONFIG_DIR/env" ]; }; then
  echo "Cannot use non-regular runtime configuration: $CONFIG_DIR/env" >&2
  exit 1
fi

if sudo test -e "$ONBOARDING_CONFIG_DIR/enabled"; then
  echo "Refusing to update while Wi-Fi onboarding is armed." >&2
  exit 1
fi

for unit in \
  messagebox.target \
  messagebox-button.service \
  messagebox-sync.service \
  messagebox-poller.service \
  messagebox-dash.service \
  messagebox-nfc.service \
  messagebox-wifi-reset.service \
  comitup.service \
  comitup-web.service \
  messagebox-onboarding-home.service \
  messagebox-onboarding-button.service \
  messagebox-onboarding-voice-gate.service \
  messagebox-onboarding-voice.target \
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
sudo apt-get install -y --no-install-recommends \
  alsa-utils ca-certificates curl ffmpeg gunicorn=23.0.0-1 i2c-tools nftables \
  liblgpio-dev python3-dev python3-gpiozero python3-lgpio python3-venv swig

sudo usermod -a -G audio,gpio,i2c "$SERVICE_USER"

sudo rm -rf \
  "$PACKAGE_DIR" \
  "$APP_DIR/dev" \
  "$APP_DIR/ringtones"
sudo install -d -o root -g root -m 0755 \
  "$APP_DIR" \
  "$APP_DIR/config" \
  "$APP_DIR/dev" \
  "$PACKAGE_DIR" \
  "$PACKAGE_DIR/dashboard" \
  "$PACKAGE_DIR/dashboard/static" \
  "$PACKAGE_DIR/onboarding" \
  "$PACKAGE_DIR/onboarding/static" \
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

for name in $PACKAGE_PYTHON $DASHBOARD_PYTHON $ONBOARDING_PYTHON; do
  sudo install -o root -g root -m 0644 \
    "$REPO_DIR/messagebox/$name" "$PACKAGE_DIR/$name"
done
for name in $STATIC_ASSETS; do
  sudo install -o root -g root -m 0644 \
    "$REPO_DIR/messagebox/$name" "$PACKAGE_DIR/$name"
done
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/messagebox/syncloop.sh" "$PACKAGE_DIR/syncloop.sh"
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/dev/hardware-test.sh" "$APP_DIR/dev/hardware-test.sh"
sudo rm -rf \
  "$APP_DIR/__pycache__" \
  "$APP_DIR/dashboard_static" \
  "$APP_DIR/onboarding"
sudo rm -f \
  "$APP_DIR/button_send.py" \
  "$APP_DIR/contacts.py" \
  "$APP_DIR/dashboard.py" \
  "$APP_DIR/guided_reply.py" \
  "$APP_DIR/listened_receipts.py" \
  "$APP_DIR/make_ringtones.py" \
  "$APP_DIR/nfc.py" \
  "$APP_DIR/nfc_state.py" \
  "$APP_DIR/runtime_paths.py" \
  "$APP_DIR/syncloop.sh" \
  "$APP_DIR/test.sh" \
  "$APP_DIR/voicepoll.py" \
  /usr/local/sbin/messagebox-arm-wifi \
  /usr/local/sbin/messagebox-configure-wifi
sudo rm -f "$ONBOARDING_DATA_DIR/session.key"

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
if ! sudo test -e "$CONFIG_DIR/env"; then
  sudo install -o root -g "$SERVICE_GROUP" -m 0640 \
    "$REPO_DIR/config/env.example" "$CONFIG_DIR/env"
fi

(cd "$APP_DIR" && sudo /usr/bin/python3 -m messagebox.make_ringtones)
sudo chmod 0644 "$APP_DIR"/ringtones/*.wav
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
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/commands/messagebox-contact" \
  /usr/local/bin/messagebox-contact
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/commands/messagebox-comitup-state" \
  /usr/local/sbin/messagebox-comitup-state
MSGBOX_SKIP_APT=1 "$SCRIPT_DIR/install/nfc.sh"
sudo install -o root -g root -m 0755 \
  "$REPO_DIR/scripts/dev/onboard.sh" /usr/local/bin/messagebox-dev-onboard

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
  "$REPO_DIR/scripts/commands/messagebox-init-wifi-onboarding" \
  /usr/local/sbin/messagebox-init-wifi-onboarding
sudo install -o root -g root -m 0644 \
  "$REPO_DIR/systemd/onboarding/comitup-web.service.d/messagebox.conf" \
  /etc/systemd/system/comitup-web.service.d/messagebox.conf
for name in messagebox-onboarding-home messagebox-onboarding-button \
  messagebox-onboarding-voice-gate messagebox-whatsapp-pairing messagebox-wifi-reset; do
  sudo install -o root -g root -m 0644 \
    "$REPO_DIR/systemd/onboarding/$name.service" "/etc/systemd/system/$name.service"
done
for name in messagebox-onboarding-voice.path messagebox-onboarding-voice.target; do
  sudo install -o root -g root -m 0644 \
    "$REPO_DIR/systemd/onboarding/$name" "/etc/systemd/system/$name"
done
sudo systemd-tmpfiles --create /etc/tmpfiles.d/messagebox.conf
sudo systemctl daemon-reload
sudo systemctl enable messagebox-onboarding-voice.path
sudo systemd-analyze verify \
  /etc/systemd/system/messagebox.target \
  /etc/systemd/system/messagebox-button.service \
  /etc/systemd/system/messagebox-sync.service \
  /etc/systemd/system/messagebox-poller.service \
  /etc/systemd/system/messagebox-dash.service \
  /etc/systemd/system/messagebox-nfc.service \
  /usr/lib/systemd/system/comitup-web.service \
  /etc/systemd/system/messagebox-onboarding-home.service \
  /etc/systemd/system/messagebox-onboarding-button.service \
  /etc/systemd/system/messagebox-onboarding-voice-gate.service \
  /etc/systemd/system/messagebox-onboarding-voice.path \
  /etc/systemd/system/messagebox-onboarding-voice.target \
  /etc/systemd/system/messagebox-whatsapp-pairing.service \
  /etc/systemd/system/messagebox-wifi-reset.service

PYTHONPYCACHEPREFIX=$(mktemp -d)
export PYTHONPYCACHEPREFIX
(cd "$APP_DIR" && /usr/bin/python3 -m compileall -q messagebox)
rm -rf "$PYTHONPYCACHEPREFIX"

if [ -n "$SSH_TARGET" ]; then
  dev_command="ssh -t $SSH_TARGET messagebox-dev-onboard"
  initialize_command="ssh -t $SSH_TARGET sudo messagebox-init-wifi-onboarding"
  reset_command="ssh -t $SSH_TARGET sudo messageboxctl reset-wifi"
  shutdown_command="ssh -t $SSH_TARGET sudo shutdown now"
else
  dev_command=messagebox-dev-onboard
  initialize_command="sudo messagebox-init-wifi-onboarding"
  reset_command="sudo messageboxctl reset-wifi"
  shutdown_command="sudo shutdown now"
fi

printf '%s\n' \
  '' \
  '============================================================' \
  '             ✅ MESSAGE BOX SETUP COMPLETE ✅' \
  '============================================================' \
  'No Message Box runtime or Comitup services were started.' \
  '' \
  'DEV FLOW' \
  '  Run the standalone shell setup and hardware checks:' \
  "    $dev_command" \
  '' \
  '============================= OR =============================' \
  '' \
  'CONSUMER HANDOFF (MANUFACTURER)'
if sudo test -e "$ONBOARDING_CONFIG_DIR/configured"; then
  printf '%s\n' \
    '  Wi-Fi credentials already exist. Confirm they are recorded.'
else
  printf '%s\n' \
    '  Generate the Wi-Fi hotspot password and setup URL:' \
    "    $initialize_command" \
    '  Print the box number, hotspot, password, and setup URL' \
    '     as an insert packaged with the box.'
fi
printf '%s\n' \
  '  Arm onboarding for the recipient:' \
  "    $reset_command" \
  '  Optionally verify the hotspot, then shut down:' \
  "    $shutdown_command" \
  '' \
  '  Do not complete the browser flow. The recipient does that' \
  '  after powering on the box.' \
  '============================================================'
