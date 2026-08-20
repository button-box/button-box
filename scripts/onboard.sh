#!/bin/sh
# Interactively configure an installed Message Box prototype.
set -eu

ENV_FILE=/etc/messagebox/env
ENV_EXAMPLE=/etc/messagebox/env.example
WACLI_STORE=/var/lib/messagebox/wacli
WACLI_BIN=/usr/local/bin/wacli
MESSAGEBOXCTL=/usr/local/bin/messageboxctl
CONTACT=/usr/local/bin/contact
HARDWARE_TEST=/opt/messagebox/test.sh

usage() {
  cat <<'EOF'
Usage: onboard.sh

Pairs WhatsApp, configures the prototype, tests its hardware, and optionally
enables and starts its services.
EOF
}

die() {
  echo "error: $1" >&2
  exit 1
}

confirm() {
  prompt=$1
  default=${2:-yes}
  while true; do
    if [ "$default" = "yes" ]; then
      printf '%s [Y/n] ' "$prompt"
    else
      printf '%s [y/N] ' "$prompt"
    fi
    IFS= read -r answer || exit 1
    case "$answer" in
      "") [ "$default" = "yes" ] && return 0 || return 1 ;;
      y|Y|yes|YES|Yes) return 0 ;;
      n|N|no|NO|No) return 1 ;;
      *) echo "Please answer yes or no." ;;
    esac
  done
}

run_wacli() {
  sudo -u messagebox -H env \
    WACLI_STORE_DIR="$WACLI_STORE" \
    WACLI_SYNC_MAX_DB_SIZE=2GB \
    "$WACLI_BIN" "$@"
}

run_contact() {
  sudo -u messagebox -H "$CONTACT" "$@"
}

read_auth_state() {
  AUTH_STATUS=$(run_wacli auth status 2>&1 || true)
  case "$AUTH_STATUS" in
    *"Authenticated as "*)
      AUTHENTICATED=1
      echo "WhatsApp is authenticated."
      ;;
    *"Not authenticated"*|*"not authenticated"*)
      AUTHENTICATED=0
      echo "WhatsApp is not authenticated."
      ;;
    *)
      printf '%s\n' "$AUTH_STATUS" >&2
      die "could not determine wacli authentication state"
      ;;
  esac
}

verify_whatsapp_connection() {
  CONNECTION_STATUS=$(run_wacli --json --timeout 15s doctor --connect 2>&1 || true)
  if printf '%s\n' "$CONNECTION_STATUS" |
     grep -Eq '"connected"[[:space:]]*:[[:space:]]*true'; then
    echo "WhatsApp connectivity verified."
    return 0
  fi
  printf '%s\n' "$CONNECTION_STATUS" >&2
  die "WhatsApp could not be reached; check the network and rerun onboarding"
}

set_env_value() {
  key=$1
  value=$2
  temporary=$(mktemp)
  if ! sudo awk -v key="$key" -v value="$value" '
    BEGIN { updated = 0 }
    $0 ~ "^" key "=" { print key "=" value; updated = 1; next }
    { print }
    END { if (!updated) print key "=" value }
  ' "$ENV_FILE" >"$temporary"; then
    rm -f "$temporary"
    die "could not update $ENV_FILE"
  fi
  sudo install -o root -g messagebox -m 0640 "$temporary" "$ENV_FILE"
  rm -f "$temporary"
}

validate_bind_address() {
  case "$1" in
    ""|0.0.0.0|::|*[!A-Za-z0-9._:-]*)
      die "dashboard bind address must be one private address without whitespace or shell syntax"
      ;;
  esac
}

finish_instructions() {
  printf '%s\n' \
    '' \
    'Onboarding finished.' \
    'To add a second contact and enroll its card:' \
    '  contact add LABEL JID' \
    'To enroll a spare card for an existing contact:' \
    '  contact enroll JID'
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

[ -t 0 ] && [ -t 1 ] || die "onboarding requires an interactive terminal"
[ "$(id -u)" -ne 0 ] || die "run as a sudo-capable administrator, not root"
sudo test -f "$ENV_EXAMPLE" || die "installation is incomplete; missing $ENV_EXAMPLE"
for path in \
  "$WACLI_BIN" \
  "$MESSAGEBOXCTL" \
  "$CONTACT" \
  "$HARDWARE_TEST" \
  /opt/messagebox/venv-nfc/bin/python \
  /etc/systemd/system/messagebox-nfc.service; do
  [ -e "$path" ] || die "installation is incomplete; missing $path"
done
if ! sudo test -f "$ENV_FILE"; then
  sudo install -o root -g messagebox -m 0640 "$ENV_EXAMPLE" "$ENV_FILE"
fi

# Ghostty's terminfo is not present on a default Raspberry Pi OS image.
case "${TERM:-}" in
  ""|xterm-ghostty) TERM=xterm-256color; export TERM ;;
esac

# Stop every unit so authentication, configuration, and hardware tests have exclusive use.
sudo "$MESSAGEBOXCTL" stop

echo ""
echo "1. Pair WhatsApp"
read_auth_state
NEED_PAIRING=0
if [ "$AUTHENTICATED" -eq 1 ]; then
  verify_whatsapp_connection
else
  NEED_PAIRING=1
fi

if [ "$NEED_PAIRING" -eq 1 ]; then
  run_wacli auth logout || true
  printf 'Phone number in E.164 format (leave blank to pair with QR): '
  IFS= read -r PHONE
  case "$PHONE" in
    "") run_wacli auth --idle-exit 30s ;;
    +[0-9]*)
      case "${PHONE#+}" in
        *[!0-9]*) die "phone number must contain only + and digits" ;;
      esac
      run_wacli auth --idle-exit 30s --phone "$PHONE"
      ;;
    *) die "phone number must start with + or be blank" ;;
  esac
  read_auth_state
  [ "$AUTHENTICATED" -eq 1 ] || die "WhatsApp authentication did not complete"
fi

echo ""
echo "2. Configure Message Box"
echo "Synced chats are shown below with their exact JIDs."
run_wacli --read-only chats list --limit 200
CONTACT_COUNT=$(run_contact count)
case "$CONTACT_COUNT" in
  *[!0-9]*|"") die "contact count returned an invalid result" ;;
esac
if [ "$CONTACT_COUNT" -eq 0 ]; then
  printf 'Label for the first contact (for example, Family): '
  IFS= read -r CONTACT_LABEL || exit 1
  [ -n "$CONTACT_LABEL" ] || die "contact label cannot be blank"
  printf 'Exact CHAT_JID shown above (123456789@g.us or 15551234567@s.whatsapp.net): '
  IFS= read -r CONTACT_JID || exit 1
  run_contact add "$CONTACT_LABEL" "$CONTACT_JID" --no-card ||
    die "could not add the first contact"
else
  echo "Existing contacts:"
  run_contact list
fi

if confirm "Enable the private dashboard on this device?" yes; then
  DASHBOARD=1
  TAILSCALE_IP=""
  if command -v tailscale >/dev/null 2>&1; then
    CANDIDATE=$(tailscale ip -4 2>/dev/null || true)
    case "$CANDIDATE" in
      ""|0.0.0.0|*[!0-9.]*) ;;
      *) TAILSCALE_IP=$CANDIDATE ;;
    esac
  fi
  if [ -n "$TAILSCALE_IP" ]; then
    echo "Tailscale address candidate: $TAILSCALE_IP"
    printf 'Dashboard bind address [%s]: ' "$TAILSCALE_IP"
    IFS= read -r DASH_BIND || exit 1
    DASH_BIND=${DASH_BIND:-$TAILSCALE_IP}
  else
    printf 'Dashboard bind address: '
    IFS= read -r DASH_BIND || exit 1
  fi
  validate_bind_address "$DASH_BIND"
  set_env_value MSGBOX_DASH_BIND "$DASH_BIND"
else
  DASHBOARD=0
  set_env_value MSGBOX_DASH_BIND ""
fi

echo ""
echo "3. Test hardware"
HARDWARE_OVERRIDE=0
if confirm "Run the interactive hardware test now?" yes; then
  if ! sudo -u messagebox -H "$HARDWARE_TEST"; then
    printf '%s\n' \
      '' \
      'One or more hardware checks failed.' \
      'WhatsApp and contact configuration were saved, but failed hardware may prevent services from starting.'
    if confirm "Continue and allow service startup despite failed hardware checks?" no; then
      HARDWARE_OVERRIDE=1
    else
      echo "Services were left stopped. Connect the missing hardware and rerun onboard.sh."
      finish_instructions
      exit 0
    fi
  fi
fi

echo ""
echo "4. Start Message Box"
if [ "$HARDWARE_OVERRIDE" -ne 1 ] &&
   ! confirm "Enable the selected services and start Message Box?" yes; then
  echo "Services were not enabled or started."
  finish_instructions
  exit 0
fi

if [ "$DASHBOARD" -eq 1 ]; then
  sudo "$MESSAGEBOXCTL" enable button sync poller dashboard nfc
  SELECTED="button sync poller dashboard nfc"
else
  sudo "$MESSAGEBOXCTL" disable dashboard
  sudo "$MESSAGEBOXCTL" enable button sync poller nfc
  SELECTED="button sync poller nfc"
fi
sudo "$MESSAGEBOXCTL" start
sleep 2

FAILED_UNITS=""
for service in $SELECTED; do
  case "$service" in
    dashboard) unit=messagebox-dash.service ;;
    *) unit=messagebox-$service.service ;;
  esac
  if ! systemctl is-active --quiet "$unit"; then
    FAILED_UNITS="$FAILED_UNITS $unit"
  fi
done
if [ -n "$FAILED_UNITS" ]; then
  "$MESSAGEBOXCTL" status
  # shellcheck disable=SC2086
  sudo "$MESSAGEBOXCTL" disable $SELECTED
  die "services failed to start:$FAILED_UNITS"
fi
"$MESSAGEBOXCTL" services
finish_instructions
