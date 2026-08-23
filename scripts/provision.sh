#!/bin/sh
# Run this on your computer to install or update a Pi over SSH.
# It sends only installation inputs to a temporary directory on the Pi, then
# runs setup.sh there. The installed runtime uses fixed system paths.
# Usage: ./scripts/provision.sh [--guided-prompts DIR] user@host
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
GUIDED_PROMPT_NAMES="reply-countdown.wav standalone-countdown.wav press-to-send.wav
delete-warning.wav not-sent.wav"

case "$#" in
  1)
    TARGET=$1
    GUIDED_PROMPT_DIR=$REPO_DIR/sounds/guided-reply
    ;;
  3)
    if [ "$1" != "--guided-prompts" ]; then
      echo "Usage: $0 [--guided-prompts DIR] user@host" >&2
      exit 2
    fi
    GUIDED_PROMPT_DIR=$2
    TARGET=$3
    ;;
  *)
    echo "Usage: $0 [--guided-prompts DIR] user@host" >&2
    exit 2
    ;;
esac

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
rsync -azR \
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
  "$REPO_DIR/./messagebox/contacts.py" \
  "$REPO_DIR/./messagebox/guided_reply.py" \
  "$REPO_DIR/./messagebox/listened_receipts.py" \
  "$REPO_DIR/./messagebox/make_ringtones.py" \
  "$REPO_DIR/./messagebox/nfc.py" \
  "$REPO_DIR/./messagebox/nfc_state.py" \
  "$REPO_DIR/./messagebox/runtime_paths.py" \
  "$REPO_DIR/./messagebox/syncloop.sh" \
  "$REPO_DIR/./messagebox/voicepoll.py" \
  "$REPO_DIR/./messagebox/dashboard/__init__.py" \
  "$REPO_DIR/./messagebox/dashboard/app.py" \
  "$REPO_DIR/./messagebox/dashboard/static/app.js" \
  "$REPO_DIR/./messagebox/dashboard/static/index.html" \
  "$REPO_DIR/./messagebox/dashboard/static/styles.css" \
  "$REPO_DIR/./messagebox/onboarding/__init__.py" \
  "$REPO_DIR/./messagebox/onboarding/app.py" \
  "$REPO_DIR/./messagebox/onboarding/comitup_adapter.py" \
  "$REPO_DIR/./messagebox/onboarding/connectivity.py" \
  "$REPO_DIR/./messagebox/onboarding/initialize.py" \
  "$REPO_DIR/./messagebox/onboarding/paths.py" \
  "$REPO_DIR/./messagebox/onboarding/recipients.py" \
  "$REPO_DIR/./messagebox/onboarding/reset.py" \
  "$REPO_DIR/./messagebox/onboarding/state.py" \
  "$REPO_DIR/./messagebox/onboarding/voice_gate.py" \
  "$REPO_DIR/./messagebox/onboarding/whatsapp.py" \
  "$REPO_DIR/./messagebox/onboarding/static/app.js" \
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
ssh -t "$TARGET" \
  "MESSAGEBOX_SSH_TARGET='$TARGET' '$REMOTE_SOURCE/scripts/setup.sh'"
