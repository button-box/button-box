#!/bin/sh
# Generate one device's private Wi-Fi onboarding identity and setup card.
set -eu

CONFIG_DIR=/etc/messagebox-onboarding
CONFIG=$CONFIG_DIR/config.json
STATE_DIR=/var/lib/messagebox-onboarding
STATE=$STATE_DIR/state.json
OBSOLETE_SESSION_KEY=$STATE_DIR/session.key
CONFIGURED=$CONFIG_DIR/configured
ENABLED=$CONFIG_DIR/enabled
TEMPLATE=/usr/share/messagebox/onboarding/comitup.conf.template
TMP_DIR=
COMITUP_REPLACED=0
COMMITTED=0

die() {
  echo "$*" >&2
  exit 1
}

cleanup() {
  if [ "$COMITUP_REPLACED" -eq 1 ] && [ "$COMMITTED" -eq 0 ]; then
    if [ -e "$TMP_DIR/comitup.conf.previous" ]; then
      cp -p "$TMP_DIR/comitup.conf.previous" /etc/comitup.conf
    else
      rm -f /etc/comitup.conf
    fi
  fi
  if [ -n "$TMP_DIR" ]; then
    rm -rf -- "$TMP_DIR"
  fi
}
trap cleanup EXIT HUP INT TERM

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ -t 0 ] && [ -t 1 ] || die "Configuration requires an interactive terminal."
[ ! -e "$ENABLED" ] && [ ! -L "$ENABLED" ] || die "Wi-Fi onboarding is already armed."
[ ! -e "$CONFIGURED" ] && [ ! -L "$CONFIGURED" ] || die "Wi-Fi onboarding is already configured."
for unit in comitup.service comitup-web.service messagebox-onboarding-home.service messagebox-whatsapp-pairing.service; do
  systemctl is-active --quiet "$unit" 2>/dev/null && die "Stop $unit before configuring."
done
for path in "$CONFIG" "$STATE" "$OBSOLETE_SESSION_KEY"; do
  [ ! -L "$path" ] || die "Refusing symlinked onboarding credential: $path"
done
# Files without the final marker are residue from an interrupted configuration.
rm -f "$CONFIG" "$STATE" "$OBSOLETE_SESSION_KEY"
[ ! -L /etc/comitup.conf ] || die "Refusing symlink at /etc/comitup.conf."
[ -r "$TEMPLATE" ] || die "Missing Comitup configuration template."
getent passwd messagebox-onboarding >/dev/null || die "Missing messagebox-onboarding account."

TMP_DIR=$(mktemp -d /run/messagebox-onboarding-configure.XXXXXX)
chmod 0700 "$TMP_DIR"
DEVICE_HOSTNAME=$(hostname)
PYTHONPATH=/opt/messagebox TEMPLATE=$TEMPLATE OUTPUT=$TMP_DIR DEVICE_HOSTNAME=$DEVICE_HOSTNAME python3 <<'PY'
import json
import os
import re
import secrets
from pathlib import Path

from onboarding.state import StateStore

output = Path(os.environ["OUTPUT"])
hostname = os.environ["DEVICE_HOSTNAME"].lower()
match = re.fullmatch(r"message-box-([a-z0-9-]{1,32})", hostname)
if match is None:
    raise SystemExit("hostname must match message-box-ID")
device_id = match.group(1)
alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
hotspot_raw = "".join(secrets.choice(alphabet) for _ in range(12))
hotspot_password = f"{hotspot_raw[:6]}-{hotspot_raw[6:]}"
canonical_host = f"{hostname}.local"

metadata = {
    "version": 1,
    "device_id": device_id,
    "canonical_host": canonical_host,
}
(output / "config.json").write_text(
    json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="ascii",
)
StateStore(output / "state.json").initialize()

template = Path(os.environ["TEMPLATE"]).read_text(encoding="ascii")
rendered = template.replace("@HOTSPOT_PASSWORD@", hotspot_password)
if "@HOTSPOT_PASSWORD@" in rendered:
    raise SystemExit("unresolved Comitup template placeholder")
(output / "comitup.conf").write_text(rendered, encoding="ascii")
(output / "card").write_text(
    f"{device_id}\n{hostname}\n{hotspot_password}\n", encoding="ascii"
)
PY

device_id=$(sed -n '1p' "$TMP_DIR/card")
device_hostname=$(sed -n '2p' "$TMP_DIR/card")
hotspot_password=$(sed -n '3p' "$TMP_DIR/card")
printf '\nDevice ID:        %s\nHotspot:          %s\nHotspot password: %s\nSetup URL:        http://%s.local/\n\n' \
  "$device_id" "$device_hostname" "$hotspot_password" "$device_hostname"
printf 'Type the device ID to confirm this setup card was recorded: '
IFS= read -r confirmation
[ "$confirmation" = "$device_id" ] || die "Confirmation did not match; no credentials were installed."

if [ -e /etc/comitup.conf ]; then
  cp -p /etc/comitup.conf "$TMP_DIR/comitup.conf.previous"
fi
install -o root -g root -m 0600 "$TMP_DIR/comitup.conf" /etc/comitup.conf.tmp
mv -f /etc/comitup.conf.tmp /etc/comitup.conf
COMITUP_REPLACED=1
/usr/sbin/comitup --check

install -d -o root -g messagebox-onboarding -m 0750 "$CONFIG_DIR"
install -d -o messagebox-onboarding -g messagebox-onboarding -m 0700 "$STATE_DIR"
install -o root -g messagebox-onboarding -m 0640 "$TMP_DIR/config.json" "$CONFIG.tmp"
install -o messagebox-onboarding -g messagebox-onboarding -m 0600 "$TMP_DIR/state.json" "$STATE.tmp"
mv -f "$CONFIG.tmp" "$CONFIG"
mv -f "$STATE.tmp" "$STATE"
temporary=$(mktemp "$CONFIG_DIR/.configured.XXXXXX")
chmod 0640 "$temporary"
printf '%s\n' configured >"$temporary"
chown root:messagebox-onboarding "$temporary"
mv -f "$temporary" "$CONFIGURED"
COMMITTED=1

printf '\nWi-Fi onboarding is configured but not started. The hotspot password is stored root-only in /etc/comitup.conf.\nRun: sudo messageboxctl reset-wifi\n'
