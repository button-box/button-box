#!/usr/bin/env zsh
# Connect directly to the Pi through a macOS USB Ethernet adapter using dnsmasq.
# Run `sudo ./scripts/dev/macos-direct-ethernet.sh en8`, then `ssh admin@10.77.77.77`.
# If the Pi does not request a lease, unplug and reconnect the Ethernet cable.
# Ctrl-C stops DHCP and restores the host interface.

set -euo pipefail

SCRIPT_PATH=${0:A}
HOST_IP=10.77.77.1
PI_IP=10.77.77.77
NETMASK=255.255.255.0
LEASE_TIME=12h

usage() {
  printf '%s\n' \
    "Usage: sudo $SCRIPT_PATH INTERFACE" \
    "" \
    "Temporarily configures a macOS Ethernet interface as ${HOST_IP}/24" \
    "and runs a foreground DHCP server for direct Message Box development." \
    "" \
    "  Pi IP: ${PI_IP}" \
    "" \
    "Example:" \
    "  sudo $SCRIPT_PATH en8" \
    "" \
    "Press Ctrl-C to stop DHCP and remove the temporary host address."
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 2
fi

IFACE=$1

[[ "$(uname -s)" == "Darwin" ]] || die "this helper supports macOS only"
[[ "$EUID" -eq 0 ]] || die "run with sudo: sudo $SCRIPT_PATH $IFACE"
[[ "$IFACE" =~ '^en[0-9]+$' ]] || die "unexpected interface name: $IFACE"
/sbin/ifconfig "$IFACE" >/dev/null 2>&1 || die "interface does not exist: $IFACE"

DNSMASQ_BIN=
for candidate in /opt/homebrew/sbin/dnsmasq /usr/local/sbin/dnsmasq; do
  if [[ -x "$candidate" ]]; then
    DNSMASQ_BIN=$candidate
    break
  fi
done
if [[ -z "$DNSMASQ_BIN" ]]; then
  DNSMASQ_BIN=$(command -v dnsmasq || true)
fi
[[ -n "$DNSMASQ_BIN" ]] || die "dnsmasq is missing; install it with: brew install dnsmasq"

if /usr/bin/pgrep -f '^/usr/libexec/InternetSharing$' >/dev/null 2>&1; then
  die "macOS Internet Sharing is using DHCP; turn it off before running this helper"
fi
if /usr/sbin/lsof -nP -iUDP:67 >/dev/null 2>&1; then
  die "UDP port 67 is already in use; run: sudo lsof -nP -iUDP:67"
fi

IFACE_WAS_UP=0
if /sbin/ifconfig "$IFACE" | /usr/bin/grep -q 'flags=.*<[^>]*UP'; then
  IFACE_WAS_UP=1
fi

STATIC_IP_WAS_PRESENT=0
if /sbin/ifconfig "$IFACE" | /usr/bin/grep -q "^[[:space:]]*inet ${HOST_IP}[[:space:]]"; then
  STATIC_IP_WAS_PRESENT=1
fi

LEASE_FILE="/var/run/messagebox-${IFACE}.leases"
DNSMASQ_PID=

cleanup() {
  local code=$?
  trap - EXIT HUP INT TERM TSTP
  if [[ -n "$DNSMASQ_PID" ]] && /bin/kill -0 "$DNSMASQ_PID" >/dev/null 2>&1; then
    /bin/kill -CONT "$DNSMASQ_PID" >/dev/null 2>&1 || true
    /bin/kill -TERM "$DNSMASQ_PID" >/dev/null 2>&1 || true
    wait "$DNSMASQ_PID" 2>/dev/null || true
  fi
  /bin/rm -f "$LEASE_FILE"
  if [[ "$STATIC_IP_WAS_PRESENT" -eq 0 ]]; then
    /sbin/ifconfig "$IFACE" inet "$HOST_IP" delete >/dev/null 2>&1 || true
  fi
  if [[ "$IFACE_WAS_UP" -eq 1 ]]; then
    /sbin/ifconfig "$IFACE" up >/dev/null 2>&1 || true
  else
    /sbin/ifconfig "$IFACE" down >/dev/null 2>&1 || true
  fi
  printf '\nDirect Ethernet DHCP stopped; restored %s.\n' "$IFACE"
  exit "$code"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 148' TSTP

if [[ "$STATIC_IP_WAS_PRESENT" -eq 0 ]]; then
  /sbin/ifconfig "$IFACE" inet "$HOST_IP" netmask "$NETMASK" alias
fi
/sbin/ifconfig "$IFACE" up

dnsmasq_args=(
  --keep-in-foreground
  --conf-file=
  --port=0
  --interface="$IFACE"
  --bind-interfaces
  --dhcp-authoritative
  --dhcp-range="${PI_IP},${PI_IP},${NETMASK},${LEASE_TIME}"
  --dhcp-option=3
  --dhcp-option=6
  --dhcp-leasefile="$LEASE_FILE"
  --log-facility=-
)

printf '%s\n' \
  "Direct Message Box Ethernet is ready." \
  "  Interface:  $IFACE" \
  "  Mac IP:     $HOST_IP" \
  "  Pi IP:      $PI_IP" \
  "  SSH:        ssh admin@$PI_IP"
printf '%s\n' \
  "" \
  "Reconnect the cable, reboot the Pi, or reactivate eth0 to request a lease." \
  "Press Ctrl-C to stop and restore the Mac interface." \
  ""

"$DNSMASQ_BIN" "${dnsmasq_args[@]}" &
DNSMASQ_PID=$!
wait "$DNSMASQ_PID"
