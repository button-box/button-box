#!/bin/sh
# Run this on your computer to install or update a Pi over SSH.
# It sends only installation inputs to a temporary directory on the Pi, then
# runs setup.sh there. The installed runtime uses fixed system paths.
# Usage: ./scripts/provision.sh user@host
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 user@host" >&2
  exit 2
fi

TARGET=$1
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")

case "$TARGET" in
  -*|*[!A-Za-z0-9._@-]*)
    echo "Invalid SSH target: $TARGET" >&2
    exit 2
    ;;
esac

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

echo "Copying installation files to $TARGET:$REMOTE_SOURCE"
rsync -azR \
  "$REPO_DIR/./config/env.example" \
  "$REPO_DIR/./config/onboarding/" \
  "$REPO_DIR/./config/requirements-nfc.txt" \
  "$REPO_DIR/./scripts/install/" \
  "$REPO_DIR/./scripts/configure-wifi.sh" \
  "$REPO_DIR/./scripts/onboard.sh" \
  "$REPO_DIR/./scripts/messageboxctl" \
  "$REPO_DIR/./scripts/setup.sh" \
  "$REPO_DIR/./scripts/test.sh" \
  "$REPO_DIR/./sounds/" \
  "$REPO_DIR/./src/button_send.py" \
  "$REPO_DIR/./src/contacts.py" \
  "$REPO_DIR/./src/dashboard.py" \
  "$REPO_DIR/./src/guided_reply.py" \
  "$REPO_DIR/./src/listened_receipts.py" \
  "$REPO_DIR/./src/make_ringtones.py" \
  "$REPO_DIR/./src/nfc.py" \
  "$REPO_DIR/./src/nfc_state.py" \
  "$REPO_DIR/./src/onboarding/" \
  "$REPO_DIR/./src/runtime_paths.py" \
  "$REPO_DIR/./src/syncloop.sh" \
  "$REPO_DIR/./src/voicepoll.py" \
  "$REPO_DIR/./systemd/" \
  "$TARGET:$REMOTE_SOURCE/"

echo "Running setup on $TARGET"
ssh -t "$TARGET" "'$REMOTE_SOURCE/scripts/setup.sh'"

printf '\n%s\n' \
  '============================================================' \
  '     ✅ MESSAGE BOX PROVISIONING SUCCESSFUL ✅' \
  '============================================================' \
  'No Message Box or Comitup services were enabled or started.' \
  '' \
  'NEXT STEP' \
  '  Configure the protected Wi-Fi setup card over direct Ethernet:' \
  "    ssh -t $TARGET sudo messagebox-configure-wifi" \
  '  Then start onboarding only after recording that card:' \
  "    ssh -t $TARGET sudo messageboxctl reset-wifi" \
  '  WhatsApp and contact onboarding follows Wi-Fi acceptance:' \
  "    ssh $TARGET" \
  '    onboard.sh' \
  '============================================================'
