#!/bin/bash
# Keep one WhatsApp sync connection alive. Media downloads use wacli's explicit
# read-only output mode and therefore do not need sync to release the store lock.
GAP_S="${MSGBOX_SYNC_GAP_S:-3}"
PAUSE_POLL_S="${MSGBOX_SYNC_PAUSE_POLL_S:-0.2}"
WACLI_BIN=/usr/local/bin/wacli
SYNC_PAUSE_FILE=/var/lib/messagebox/whatsapp-pairing/sync-paused
WEBHOOK_URL="${MSGBOX_WACLI_WEBHOOK_URL:-}"
WEBHOOK_SECRET="${MSGBOX_WACLI_WEBHOOK_SECRET:-}"

SYNC_ARGS=(sync --follow --max-reconnect 0)
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

sync_pid=""

stop_sync() {
  if [[ -n "$sync_pid" ]] && kill -0 "$sync_pid" 2>/dev/null; then
    kill "$sync_pid"
    wait "$sync_pid" 2>/dev/null || true
  fi
  sync_pid=""
}

trap 'stop_sync; exit 0' INT TERM

while true; do
  while [[ -e "$SYNC_PAUSE_FILE" ]]; do
    sleep "$PAUSE_POLL_S"
  done
  "$WACLI_BIN" "${SYNC_ARGS[@]}" &
  sync_pid=$!
  while kill -0 "$sync_pid" 2>/dev/null; do
    if [[ -e "$SYNC_PAUSE_FILE" ]]; then
      stop_sync
      break
    fi
    sleep "$PAUSE_POLL_S"
  done
  if [[ -n "$sync_pid" ]]; then
    wait "$sync_pid" || true
    sync_pid=""
    sleep "$GAP_S"
  fi
done
