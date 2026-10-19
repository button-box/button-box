#!/bin/sh
# Run this on your computer to install or update a Pi over SSH.
# It sends only installation inputs to a temporary directory on the Pi, then
# runs setup.sh there. The installed runtime uses fixed system paths.
# Usage: ./scripts/provision.sh [-i IDENTITY_FILE] user@host
# The SSH identity can also come from SSH_IDENTITY=/path/to/key. It is used for
# ssh and for rsync's ssh.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
SSH_IDENTITY=${SSH_IDENTITY:-}
if [ "${1:-}" = -i ] && [ "$#" -ge 2 ]; then
  SSH_IDENTITY=$2
  shift 2
fi
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [-i IDENTITY_FILE] user@host" >&2
  exit 2
fi
TARGET=$1
if [ -n "$SSH_IDENTITY" ]; then
  case "$SSH_IDENTITY" in
    -*|*[\'\"]*)
      echo "Invalid SSH identity file: $SSH_IDENTITY" >&2
      exit 2
      ;;
  esac
  if [ ! -f "$SSH_IDENTITY" ] || [ ! -r "$SSH_IDENTITY" ]; then
    echo "SSH identity file is not a readable file: $SSH_IDENTITY" >&2
    exit 2
  fi
  # rsync runs from the repository root; keep a relative path valid there.
  SSH_IDENTITY=$(CDPATH= cd -- "$(dirname -- "$SSH_IDENTITY")" && pwd)/$(basename -- "$SSH_IDENTITY")
fi

# ssh with the selected identity only, when one is given.
remote() {
  if [ -n "$SSH_IDENTITY" ]; then
    ssh -i "$SSH_IDENTITY" -o IdentitiesOnly=yes "$@"
  else
    ssh "$@"
  fi
}
RSYNC_RSH=
if [ -n "$SSH_IDENTITY" ]; then
  # The single quotes are for rsync, which splits --rsh itself.
  # shellcheck disable=SC2089
  RSYNC_RSH="ssh -i '$SSH_IDENTITY' -o IdentitiesOnly=yes"
fi

case "$TARGET" in
  -*|*[!A-Za-z0-9._@-]*)
    echo "Invalid SSH target: $TARGET" >&2
    exit 2
    ;;
esac

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR" python3 -m messagebox.sound_pack "$REPO_DIR/sounds"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR" python3 \
  "$SCRIPT_DIR/install/ringtones.py" "$REPO_DIR/sounds/ringtones"

REMOTE_SOURCE=$(remote "$TARGET" 'mktemp -d /tmp/messagebox-provision.XXXXXX')
case "$REMOTE_SOURCE" in
  /tmp/messagebox-provision.*) ;;
  *) echo "Unexpected remote staging path: $REMOTE_SOURCE" >&2; exit 1 ;;
esac

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  remote "$TARGET" "rm -rf -- '$REMOTE_SOURCE'" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

echo "Copying installation files to $TARGET:$REMOTE_SOURCE"
(
cd "$REPO_DIR"
REPO_DIR=.
# shellcheck disable=SC2090
rsync -azR ${RSYNC_RSH:+"--rsh=$RSYNC_RSH"} \
  "$REPO_DIR/./config/env.example" \
  "$REPO_DIR/./config/onboarding/" \
  "$REPO_DIR/./config/journald.conf.d/" \
  "$REPO_DIR/./config/requirements-nfc.txt" \
  "$REPO_DIR/./scripts/install/" \
  "$REPO_DIR/./scripts/commands/messagebox-comitup-state" \
  "$REPO_DIR/./scripts/commands/messagebox-contact" \
  "$REPO_DIR/./scripts/commands/messagebox-init-wifi-onboarding" \
  "$REPO_DIR/./scripts/dev/onboard.sh" \
  "$REPO_DIR/./scripts/dev/hardware-test.sh" \
  "$REPO_DIR/./scripts/messageboxctl" \
  "$REPO_DIR/./scripts/setup.sh" \
  "$REPO_DIR/./sounds/" \
  "$REPO_DIR/./messagebox/__init__.py" \
  "$REPO_DIR/./messagebox/button_send.py" \
  "$REPO_DIR/./messagebox/mic_capture.py" \
  "$REPO_DIR/./messagebox/device_http.py" \
  "$REPO_DIR/./messagebox/software.py" \
  "$REPO_DIR/./messagebox/cloud_update.py" \
  "$REPO_DIR/./messagebox/maintenance.py" \
  "$REPO_DIR/./messagebox/cloud_device.py" \
  "$REPO_DIR/./messagebox/cloud_claim.py" \
  "$REPO_DIR/./messagebox/qrcodegen.py" \
  "$REPO_DIR/./messagebox/cloud_runtime.py" \
  "$REPO_DIR/./messagebox/cloud_quiet.py" \
  "$REPO_DIR/./messagebox/sound_pack.py" \
  "$REPO_DIR/./messagebox/audio_requests.py" \
  "$REPO_DIR/./messagebox/cloud_events.py" \
  "$REPO_DIR/./messagebox/cloud_voice.py" \
  "$REPO_DIR/./messagebox/contacts.py" \
  "$REPO_DIR/./messagebox/guided_reply.py" \
  "$REPO_DIR/./messagebox/identity.py" \
  "$REPO_DIR/./messagebox/listened_receipts.py" \
  "$REPO_DIR/./messagebox/message_age.py" \
  "$REPO_DIR/./messagebox/played_history.py" \
  "$REPO_DIR/./messagebox/nfc.py" \
  "$REPO_DIR/./messagebox/nfc_state.py" \
  "$REPO_DIR/./messagebox/remote_access.py" \
  "$REPO_DIR/./messagebox/runtime_paths.py" \
  "$REPO_DIR/./messagebox/self_install.py" \
  "$REPO_DIR/./messagebox/settings.py" \
  "$REPO_DIR/./messagebox/tailnet.py" \
  "$REPO_DIR/./messagebox/syncloop.sh" \
  "$REPO_DIR/./messagebox/voicepoll.py" \
  "$REPO_DIR/./messagebox/wifi_change.py" \
  "$REPO_DIR/./messagebox/wifi_watchdog.py" \
  "$REPO_DIR/./messagebox/event_log.py" \
  "$REPO_DIR/./messagebox/dashboard/__init__.py" \
  "$REPO_DIR/./messagebox/dashboard/app.py" \
  "$REPO_DIR/./messagebox/onboarding/__init__.py" \
  "$REPO_DIR/./messagebox/onboarding/app.py" \
  "$REPO_DIR/./messagebox/onboarding/activity.py" \
  "$REPO_DIR/./messagebox/onboarding/comitup_adapter.py" \
  "$REPO_DIR/./messagebox/onboarding/connectivity.py" \
  "$REPO_DIR/./messagebox/onboarding/completion.py" \
  "$REPO_DIR/./messagebox/onboarding/initialize.py" \
  "$REPO_DIR/./messagebox/onboarding/mode.py" \
  "$REPO_DIR/./messagebox/onboarding/nfc.py" \
  "$REPO_DIR/./messagebox/onboarding/paths.py" \
  "$REPO_DIR/./messagebox/onboarding/recipients.py" \
  "$REPO_DIR/./messagebox/onboarding/reset.py" \
  "$REPO_DIR/./messagebox/onboarding/state.py" \
  "$REPO_DIR/./messagebox/onboarding/voice_gate.py" \
  "$REPO_DIR/./messagebox/onboarding/whatsapp.py" \
  "$REPO_DIR/./messagebox/onboarding/static/app.js" \
  "$REPO_DIR/./messagebox/onboarding/static/clipboard.js" \
  "$REPO_DIR/./messagebox/onboarding/static/cloud-connect.html" \
  "$REPO_DIR/./messagebox/onboarding/static/cloud-connect.js" \
  "$REPO_DIR/./messagebox/onboarding/static/cloud-local.html" \
  "$REPO_DIR/./messagebox/onboarding/static/cloud-local.js" \
  "$REPO_DIR/./messagebox/onboarding/static/wifi-handoff.js" \
  "$REPO_DIR/./messagebox/onboarding/static/index.html" \
  "$REPO_DIR/./messagebox/onboarding/static/styles.css" \
  "$REPO_DIR/./systemd/" \
  "$TARGET:$REMOTE_SOURCE/"
)

echo "Running setup on $TARGET"
remote -t "$TARGET" \
  "MESSAGEBOX_SSH_TARGET='$TARGET' '$REMOTE_SOURCE/scripts/setup.sh'"
