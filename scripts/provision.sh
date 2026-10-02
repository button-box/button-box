#!/bin/sh
# Run this on your computer to install or update a Pi over SSH.
# It sends only installation inputs to a temporary directory on the Pi, then
# runs setup.sh there. The installed runtime uses fixed system paths.
# Usage: ./scripts/provision.sh [--transport wacli|cloud] [--guided-prompts DIR] user@host
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
GUIDED_PROMPT_NAMES="reply-countdown.wav standalone-countdown.wav press-to-send.wav
delete-warning.wav not-sent.wav"

usage() {
  echo "Usage: $0 [--transport wacli|cloud] [--guided-prompts DIR] user@host" >&2
  exit 2
}
TRANSPORT=wacli
TRANSPORT_SET=0
GUIDED_PROMPT_DIR=$REPO_DIR/sounds/guided-reply
GUIDED_PROMPTS_SET=0
TARGET=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --transport)
      [ "$#" -ge 2 ] && [ "$TRANSPORT_SET" -eq 0 ] || usage
      case "$2" in wacli|cloud) TRANSPORT=$2 ;; *) usage ;; esac
      TRANSPORT_SET=1
      shift 2
      ;;
    --guided-prompts)
      [ "$#" -ge 2 ] && [ "$GUIDED_PROMPTS_SET" -eq 0 ] || usage
      GUIDED_PROMPT_DIR=$2
      GUIDED_PROMPTS_SET=1
      shift 2
      ;;
    *)
      [ -z "$TARGET" ] || usage
      TARGET=$1
      shift
      ;;
  esac
done
[ -n "$TARGET" ] || usage

case "$TARGET" in
  -*|*[!A-Za-z0-9._@-]*)
    echo "Invalid SSH target: $TARGET" >&2
    exit 2
    ;;
esac

for name in $GUIDED_PROMPT_NAMES; do
  if [ ! -f "$GUIDED_PROMPT_DIR/$name" ] || [ ! -r "$GUIDED_PROMPT_DIR/$name" ]; then
    echo "Missing guided-reply prompt: $GUIDED_PROMPT_DIR/$name" >&2
    echo "Supply a complete licensed prompt set with --guided-prompts DIR." >&2
    exit 2
  fi
done

HAS_RELEASE=0
if [ -e "$REPO_DIR/release-manifest.json" ] || [ -L "$REPO_DIR/release-manifest.json" ]; then
  python3 "$SCRIPT_DIR/install/setup_release.py" check-release "$REPO_DIR"
  HAS_RELEASE=1
elif [ "$TRANSPORT" = cloud ]; then
  echo "Fresh Cloud provisioning requires a pinned release with release-manifest.json." >&2
  exit 2
fi

REMOTE_SOURCE=$(ssh "$TARGET" 'mktemp -d /tmp/messagebox-provision.XXXXXX')
case "$REMOTE_SOURCE" in
  /tmp/messagebox-provision.*) ;;
  *) echo "Unexpected remote staging path: $REMOTE_SOURCE" >&2; exit 1 ;;
esac

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  ssh "$TARGET" "rm -rf -- '$REMOTE_SOURCE'" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

ssh "$TARGET" "mkdir -p '$REMOTE_SOURCE/sounds/guided-reply'"

echo "Copying installation files to $TARGET:$REMOTE_SOURCE"
(
cd "$REPO_DIR"
REPO_DIR=.
set --
if [ "$HAS_RELEASE" -eq 1 ]; then
  set -- ./VERSION ./release-manifest.json ./scripts/dev/release-manifest.py
fi
rsync -azR \
  "$@" \
  "$REPO_DIR/./config/env.example" \
  "$REPO_DIR/./config/onboarding/" \
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
  "$REPO_DIR/./messagebox/business_send.py" \
  "$REPO_DIR/./messagebox/device_http.py" \
  "$REPO_DIR/./messagebox/cloud_device.py" \
  "$REPO_DIR/./messagebox/cloud_claim.py" \
  "$REPO_DIR/./messagebox/qrcodegen.py" \
  "$REPO_DIR/./messagebox/cloud_runtime.py" \
  "$REPO_DIR/./messagebox/contacts.py" \
  "$REPO_DIR/./messagebox/guided_reply.py" \
  "$REPO_DIR/./messagebox/identity.py" \
  "$REPO_DIR/./messagebox/listened_receipts.py" \
  "$REPO_DIR/./messagebox/played_history.py" \
  "$REPO_DIR/./messagebox/make_ringtones.py" \
  "$REPO_DIR/./messagebox/nfc.py" \
  "$REPO_DIR/./messagebox/nfc_state.py" \
  "$REPO_DIR/./messagebox/runtime_paths.py" \
  "$REPO_DIR/./messagebox/settings.py" \
  "$REPO_DIR/./messagebox/tailnet.py" \
  "$REPO_DIR/./messagebox/syncloop.sh" \
  "$REPO_DIR/./messagebox/voicepoll.py" \
  "$REPO_DIR/./messagebox/business_receive.py" \
  "$REPO_DIR/./messagebox/wifi_change.py" \
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
  "$REPO_DIR/./messagebox/onboarding/static/index.html" \
  "$REPO_DIR/./messagebox/onboarding/static/styles.css" \
  "$REPO_DIR/./systemd/" \
  "$TARGET:$REMOTE_SOURCE/"
)

rsync -az \
  "$GUIDED_PROMPT_DIR/reply-countdown.wav" \
  "$GUIDED_PROMPT_DIR/standalone-countdown.wav" \
  "$GUIDED_PROMPT_DIR/press-to-send.wav" \
  "$GUIDED_PROMPT_DIR/delete-warning.wav" \
  "$GUIDED_PROMPT_DIR/not-sent.wav" \
  "$TARGET:$REMOTE_SOURCE/sounds/guided-reply/"

echo "Running setup on $TARGET"
if [ "$TRANSPORT_SET" -eq 1 ]; then
  ssh -t "$TARGET" \
    "MESSAGEBOX_SSH_TARGET='$TARGET' '$REMOTE_SOURCE/scripts/setup.sh' --transport '$TRANSPORT'"
else
  ssh -t "$TARGET" \
    "MESSAGEBOX_SSH_TARGET='$TARGET' '$REMOTE_SOURCE/scripts/setup.sh'"
fi
