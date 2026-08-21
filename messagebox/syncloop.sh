#!/bin/bash
# Message Box wacli burst-sync loop (v0 production rig).
# Short sync bursts keep the store lock free between runs so the poller can download.
IDLE_EXIT="${MSGBOX_SYNC_IDLE_EXIT:-5s}"
GAP_S="${MSGBOX_SYNC_GAP_S:-3}"
WACLI_BIN=/usr/local/bin/wacli
WEBHOOK_URL="${MSGBOX_WACLI_WEBHOOK_URL:-}"
WEBHOOK_SECRET="${MSGBOX_WACLI_WEBHOOK_SECRET:-}"

SYNC_ARGS=(sync --once --idle-exit "$IDLE_EXIT")
if [[ -n "$WEBHOOK_URL" || -n "$WEBHOOK_SECRET" ]]; then
  if [[ -z "$WEBHOOK_URL" || -z "$WEBHOOK_SECRET" ]]; then
    echo "receipt webhook requires both MSGBOX_WACLI_WEBHOOK_URL and MSGBOX_WACLI_WEBHOOK_SECRET" >&2
    exit 2
  fi
  if ! "$WACLI_BIN" sync --help 2>&1 | grep -q -- '--webhook-events'; then
    echo "installed wacli does not support receipt webhooks; upgrade before enabling them" >&2
    exit 2
  fi
  SYNC_ARGS+=(
    --webhook "$WEBHOOK_URL"
    --webhook-secret "$WEBHOOK_SECRET"
    --webhook-events receipt
    --webhook-allow-private
  )
fi

while true; do
  "$WACLI_BIN" "${SYNC_ARGS[@]}"
  sleep "$GAP_S"
done
