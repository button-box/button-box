#!/bin/sh
# Button Box one-command installer. Run it ON the Raspberry Pi:
#
#   curl -fsSL https://raw.githubusercontent.com/button-box/button-box/<tag>/scripts/install/install.sh \
#     | sudo sh -s -- [--mode cloud|wacli] [--updates auto|ask]
#
# or from a checkout: sudo sh scripts/install/install.sh [options]. See
# INSTALL.md. It installs the latest signed stable release (or --version),
# verified with the pinned minisign key before any of its code runs. Running it
# again repairs or does nothing. Everything is logged, without secrets, to
# /var/log/button-box-install.log.
set -eu

# Pinned release signing key. It must equal
# config/release-keys/button-box-release-2026.pub; tests/test_installer.py checks it.
RELEASE_KEY_ID=221CAE6604AC0F93
RELEASE_KEY=RWSTD6wEZq4cIkJ8pU3BusN3m2qOnDjHA733gbpSqmbeKaKYIKA7Hr+6

# Defaults. The environment can override them (tests and mirrors use this).
REPOSITORY=${BUTTON_BOX_REPOSITORY:-button-box/button-box}
GITHUB_API=${BUTTON_BOX_GITHUB_API:-https://api.github.com}
GITHUB_URL=${BUTTON_BOX_GITHUB_URL:-https://github.com}
LOG_FILE=${BUTTON_BOX_INSTALL_LOG:-/var/log/button-box-install.log}
WORK_ROOT=${BUTTON_BOX_INSTALL_WORK:-/var/cache/button-box-install}
MODEL_FILE=${BUTTON_BOX_MODEL_FILE:-/proc/device-tree/model}
OS_RELEASE=${BUTTON_BOX_OS_RELEASE:-/etc/os-release}
RPI_ISSUE=${BUTTON_BOX_RPI_ISSUE:-/etc/rpi-issue}
ENV_FILE=${BUTTON_BOX_ENV_FILE:-/etc/messagebox/env}
MIN_FREE_MB=${BUTTON_BOX_MIN_FREE_MB:-2048}
NETWORK_WAIT=${BUTTON_BOX_NETWORK_WAIT:-120}
CLOCK_WAIT=${BUTTON_BOX_CLOCK_WAIT:-60}
APT_WAIT=${BUTTON_BOX_APT_WAIT:-900}
# On first boot apt-daily-upgrade.service can hold the dpkg lock for minutes.
APT_LOCK_TIMEOUT=600
MAX_ARCHIVE_BYTES=134217728
SYSTEM_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

MODE=
UPDATES=
VERSION=
ADMIN=
TIMEZONE=
RELEASE_DIR=
PUBLIC_KEY=
CHECK_ONLY=0
WORK=
TEE_PID=
CHANNEL=stable

usage() {
  cat <<'EOF'
Usage: install.sh [OPTIONS]

Turn a fresh Raspberry Pi OS Lite (trixie, 64-bit) Pi into a Button Box.
Run as root: curl ... | sudo sh -s -- OPTIONS, or sudo sh install.sh OPTIONS.

Options:
  --mode cloud|wacli     cloud (default): Button Box Cloud with signed updates.
                         wacli: self-hosted with your own WhatsApp account.
  --updates auto|ask     Cloud updates install by themselves (auto, default) or
                         wait for a button press (ask).
  --version vX.Y.Z       Install this signed release instead of the latest.
  --timezone ZONE        Set the timezone first, for example Europe/Lisbon.
  --admin USER           The sudo user that runs setup (default: the user who
                         ran sudo, else the first passwordless sudo user).
  --check                Run the preflight checks only, then stop.
  --release-dir DIR      Install the four release files from DIR, not GitHub.
  --public-key FILE      With --release-dir only: verify with this key. For
                         tests; a box installed this way trusts the real key.
  -h, --help             Show this help.

Exit codes: 0 done, 2 bad option, 3 preflight failed, 4 download or signature
failed (nothing installed), 5 install step failed, 6 Cloud setup link failed,
7 installed but doctor found a problem.
EOF
}

say() {
  printf '%s\n' "$*"
}

step() {
  printf '\n==> [%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

# die CODE MESSAGE [NEXT STEP]
die() {
  code=$1
  printf 'ERROR: %s\n' "$2" >&2
  if [ $# -gt 2 ]; then
    printf 'Next: %s\n' "$3" >&2
  fi
  exit "$code"
}

usage_error() {
  printf 'install.sh: %s\n' "$1" >&2
  printf 'Run with --help for the options.\n' >&2
  exit 2
}

parse_args() {
  while [ $# -gt 0 ]; do
    option=$1
    value=
    case "$option" in
      --*=*)
        value=${option#*=}
        option=${option%%=*}
        ;;
      --mode|--updates|--version|--timezone|--admin|--release-dir|--public-key)
        [ $# -ge 2 ] || usage_error "$option needs a value"
        value=$2
        shift
        ;;
    esac
    case "$option" in
      --mode) MODE=$value ;;
      --updates) UPDATES=$value ;;
      --version) VERSION=$value ;;
      --timezone) TIMEZONE=$value ;;
      --admin) ADMIN=$value ;;
      --release-dir) RELEASE_DIR=$value ;;
      --public-key) PUBLIC_KEY=$value ;;
      --check) CHECK_ONLY=1 ;;
      -h|--help) usage; exit 0 ;;
      *) usage_error "unknown option: $option" ;;
    esac
    shift
  done
  case "$MODE" in ''|cloud|wacli) ;; *) usage_error "--mode must be cloud or wacli" ;; esac
  case "$UPDATES" in ''|auto|ask) ;; *) usage_error "--updates must be auto or ask" ;; esac
  if [ -n "$VERSION" ] && ! printf '%s\n' "$VERSION" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+(-canary\.[0-9]+)?'; then
    usage_error "--version must look like v0.3.13"
  fi
  case "$VERSION" in *-canary.*) CHANNEL=canary ;; esac
  case "$TIMEZONE" in
    '') ;;
    *..*|/*|*[!A-Za-z0-9_+/-]*) usage_error "--timezone must look like Europe/Lisbon" ;;
  esac
  case "$ADMIN" in
    '') ;;
    root|-*|*[!a-z0-9_-]*) usage_error "--admin must be a non-root user name" ;;
  esac
  if [ -n "$PUBLIC_KEY" ] && [ -z "$RELEASE_DIR" ]; then
    usage_error "--public-key works only with --release-dir"
  fi
}

# ------------------------------------------------------------------ preflight

check_root() {
  [ "$(id -u)" -eq 0 ] || die 3 "Run the installer as root." \
    "curl -fsSL <installer URL> | sudo sh -s -- $*"
}

check_model() {
  [ -r "$MODEL_FILE" ] || die 3 "This is not a Raspberry Pi (no model information)." \
    "Use a Raspberry Pi 4 Model B or Pi 3 Model A+."
  model=$(tr -d '\000' <"$MODEL_FILE")
  case "$model" in
    "Raspberry Pi 4 Model B"*|"Raspberry Pi 3 Model A Plus"*)
      say "Pi model: $model" ;;
    "Raspberry Pi Zero 2 W"*)
      die 3 "Raspberry Pi Zero 2 W is not supported for Cloud updates." \
        "Use a Pi 4 Model B or Pi 3 Model A+. For a self-hosted Zero 2 W, follow the manual install in README.md." ;;
    *)
      die 3 "This Pi is not supported: $model" \
        "Use a Raspberry Pi 4 Model B or Pi 3 Model A+." ;;
  esac
}

check_os() {
  [ -r "$OS_RELEASE" ] || die 3 "Cannot identify the operating system."
  codename=$(sed -n 's/^VERSION_CODENAME=//p' "$OS_RELEASE" | tr -d '"')
  [ "$codename" = trixie ] || die 3 "Raspberry Pi OS trixie is required (found: ${codename:-unknown})." \
    "Flash Raspberry Pi OS Lite (64-bit) based on Debian 13 with Raspberry Pi Imager."
  [ -e "$RPI_ISSUE" ] || die 3 "This is not Raspberry Pi OS." \
    "Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager."
  if [ "$(uname -m)" != aarch64 ] || [ "$(dpkg --print-architecture)" != arm64 ]; then
    die 3 "The 64-bit OS is required (found: $(uname -m))." \
      "Flash Raspberry Pi OS Lite (64-bit), not the 32-bit image."
  fi
  say "OS: Raspberry Pi OS $codename, 64-bit"
}

check_internet() {
  waited=0
  until curl -fsS --max-time 15 -o /dev/null -I "$GITHUB_URL"; do
    [ "$waited" -lt "$NETWORK_WAIT" ] || die 3 "No internet connection ($GITHUB_URL is not reachable)." \
      "Connect the Pi to Wi-Fi or Ethernet (check: ping -c 3 github.com), then run the installer again."
    [ "$waited" -gt 0 ] || say "Waiting for the network..."
    sleep 5
    waited=$((waited + 5))
  done
  say "Internet: $GITHUB_URL is reachable"
}

check_disk() {
  free_mb=$(df -Pm / | awk 'NR == 2 { print $4 }')
  case "$free_mb" in ''|*[!0-9]*) die 3 "Cannot read the free disk space." ;; esac
  [ "$free_mb" -ge "$MIN_FREE_MB" ] || die 3 "Not enough free space: ${free_mb} MB (need ${MIN_FREE_MB} MB)." \
    "Use a microSD card of 16 GB or more, or free space, then run the installer again."
  say "Disk: ${free_mb} MB free"
}

clock_matches_network() {
  remote=$(curl -fsSI --max-time 15 "$GITHUB_URL" 2>/dev/null | tr -d '\r' | sed -n 's/^[Dd]ate: //p' | head -n 1)
  [ -n "$remote" ] || return 1
  remote_s=$(date -u -d "$remote" +%s 2>/dev/null) || return 1
  local_s=$(date -u +%s)
  difference=$((local_s - remote_s))
  [ "$difference" -lt 0 ] && difference=$((-difference))
  [ "$difference" -le 300 ]
}

check_time() {
  synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)
  if [ "$synced" != yes ] && systemctl is-active --quiet systemd-timesyncd.service 2>/dev/null; then
    say "Waiting for the clock to synchronise..."
    waited=0
    while [ "$waited" -lt "$CLOCK_WAIT" ]; do
      sleep 5
      waited=$((waited + 5))
      synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)
      [ "$synced" != yes ] || break
    done
  fi
  if [ "$synced" = yes ]; then
    say "Clock: synchronised"
  elif clock_matches_network; then
    say "Clock: matches network time (not NTP-synchronised yet)"
  else
    die 3 "The clock is not synchronised." \
      "Wait a minute after the Pi joins the network (check: timedatectl status), then run the installer again."
  fi
}

find_admin() {
  if [ -z "$ADMIN" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    ADMIN=$SUDO_USER
  fi
  if [ -z "$ADMIN" ]; then
    # cloud-init and other root shells: the first passwordless sudo user.
    for candidate in $(getent group sudo | cut -d: -f4 | tr ',' ' '); do
      if runuser -u "$candidate" -- sudo -n true 2>/dev/null; then
        ADMIN=$candidate
        break
      fi
    done
  fi
  [ -n "$ADMIN" ] || die 3 "No administrator account found to run setup." \
    "Create the Pi with a user in Raspberry Pi Imager, or pass --admin USER."
  getent passwd "$ADMIN" >/dev/null || die 3 "User $ADMIN does not exist."
  runuser -u "$ADMIN" -- sudo -n true 2>/dev/null || die 3 "User $ADMIN needs passwordless sudo." \
    "Use the user Raspberry Pi Imager created (it has passwordless sudo), or pass --admin USER."
  say "Administrator: $ADMIN"
}

installed_transport() {
  [ -f "$ENV_FILE" ] || return 0
  value=$(sed -n 's/^MSGBOX_TRANSPORT=//p' "$ENV_FILE" | tail -n 1 | tr -d "\"'")
  printf '%s\n' "${value:-wacli}"
}

resolve_mode() {
  existing=$(installed_transport)
  if [ -n "$existing" ]; then
    if [ -n "$MODE" ] && [ "$MODE" != "$existing" ]; then
      die 3 "This box is already installed in $existing mode." \
        "Run the installer again with --mode $existing. Changing the mode is not supported."
    fi
    MODE=$existing
  fi
  MODE=${MODE:-cloud}
  if [ "$MODE" = wacli ] && [ -n "$UPDATES" ]; then
    say "Note: --updates applies to Cloud boxes only; self-hosted boxes update with provision.sh."
  fi
  UPDATES=${UPDATES:-auto}
  say "Mode: $MODE"
}

preflight() {
  step "Checking this Pi"
  check_model
  check_os
  check_internet
  check_disk
  check_time
  find_admin
  resolve_mode
}

# -------------------------------------------------------------------- logging

start_log() {
  mkdir -p "$(dirname "$LOG_FILE")" "$WORK_ROOT"
  touch "$LOG_FILE"
  chmod 0600 "$LOG_FILE"
  chmod 0755 "$WORK_ROOT"
  WORK=$(mktemp -d "$WORK_ROOT/run.XXXXXX")
  chmod 0755 "$WORK"
  mkfifo -m 0600 "$WORK/log.fifo"
  tee -a "$LOG_FILE" <"$WORK/log.fifo" &
  TEE_PID=$!
  exec 3>&1 4>&2 >"$WORK/log.fifo" 2>&1
  printf '\n===== Button Box install %s =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

finish() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$WORK" ]; then
    rm -rf -- "$WORK/download" "$WORK/stage" "$WORK/verifier" "$WORK/keys"
  fi
  if [ -n "$TEE_PID" ]; then
    printf '===== exit %s =====\n' "$status"
    exec 1>&3 2>&4
    wait "$TEE_PID" 2>/dev/null || true
  fi
  [ -z "$WORK" ] || rm -rf -- "$WORK"
  exit "$status"
}

# ------------------------------------------------------------ release download

latest_tag() {
  curl -fsSL --proto '=https' --max-time 30 \
    -H 'Accept: application/vnd.github+json' \
    "$GITHUB_API/repos/$REPOSITORY/releases/latest" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])'
}

fetch_release() {
  download=$WORK/download
  mkdir -m 0700 "$download"
  if [ -n "$RELEASE_DIR" ]; then
    if [ -z "$VERSION" ]; then
      for archive in "$RELEASE_DIR"/button-box-v*.tar.gz; do
        [ -f "$archive" ] || continue
        VERSION=$(basename "$archive" .tar.gz)
        VERSION=${VERSION#button-box-}
      done
    fi
    [ -n "$VERSION" ] || die 4 "No button-box-vX.Y.Z.tar.gz in $RELEASE_DIR."
    for name in "button-box-$VERSION.tar.gz" release-manifest.json SHA256SUMS SHA256SUMS.minisig; do
      [ -f "$RELEASE_DIR/$name" ] || die 4 "Missing release file: $RELEASE_DIR/$name"
      cp "$RELEASE_DIR/$name" "$download/$name"
    done
    say "Release: $VERSION from $RELEASE_DIR"
  else
    if [ -z "$VERSION" ]; then
      VERSION=$(latest_tag) || die 4 "Cannot find the latest release of $REPOSITORY." \
        "Check the internet connection, or pass --version vX.Y.Z."
    fi
    printf '%s\n' "$VERSION" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+(-canary\.[0-9]+)?' ||
      die 4 "Unexpected release tag from GitHub."
    base="$GITHUB_URL/$REPOSITORY/releases/download/$VERSION"
    say "Release: $VERSION from $GITHUB_URL/$REPOSITORY"
    for name in "button-box-$VERSION.tar.gz" release-manifest.json SHA256SUMS SHA256SUMS.minisig; do
      curl -fsSL --proto '=https' --retry 3 --max-filesize "$MAX_ARCHIVE_BYTES" \
        -o "$download/$name" "$base/$name" ||
        die 4 "Cannot download $name for $VERSION." \
          "Check that release $VERSION exists and has signed assets, then run the installer again."
    done
  fi
}

verify_release() {
  keys=$WORK/keys/release-keys
  mkdir -p "$keys"
  if [ -n "$PUBLIC_KEY" ]; then
    say "WARNING: verifying with the test key $PUBLIC_KEY, not the pinned release key."
    cp "$PUBLIC_KEY" "$keys/test.pub"
  else
    printf 'untrusted comment: minisign public key %s\n%s\n' "$RELEASE_KEY_ID" "$RELEASE_KEY" \
      >"$keys/button-box-release.pub"
  fi
  download=$WORK/download
  # 1. The signature over SHA256SUMS, before any release byte is trusted.
  minisign -Vm "$download/SHA256SUMS" -x "$download/SHA256SUMS.minisig" -p "$keys"/*.pub -q ||
    die 4 "The release signature is not valid. Nothing was installed." \
      "Do not continue with these files. Report this at $GITHUB_URL/$REPOSITORY/issues."
  # 2. Exactly the archive and the manifest, with matching SHA-256 hashes.
  python3 - "$download" "$VERSION" <<'PY' || die 4 "The release files do not match their signed hashes. Nothing was installed." "Run the installer again; if it fails again, report it."
import hashlib, re, sys
from pathlib import Path
directory, version = Path(sys.argv[1]), sys.argv[2]
expected = {}
for line in (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
    if not match or match[2] in expected:
        raise SystemExit("invalid SHA256SUMS")
    expected[match[2]] = match[1]
if set(expected) != {f"button-box-{version}.tar.gz", "release-manifest.json"}:
    raise SystemExit("SHA256SUMS names the wrong files")
for name, digest in expected.items():
    if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
        raise SystemExit("hash mismatch: " + name)
PY
  # 3. The release's own verifier: manifest identity, channel and this Pi.
  mkdir -m 0700 "$WORK/verifier"
  tar -xzOf "$download/button-box-$VERSION.tar.gz" scripts/install/release_verify.py \
    >"$WORK/verifier/release_verify.py" ||
    die 4 "Release $VERSION has no verifier." "Use a newer release."
  stage=$WORK/stage
  if ! python3 -I - "$WORK/verifier" "$download" "$keys" "$VERSION" "$CHANNEL" "$stage" "$WORK/sequence" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from release_verify import ReleaseError, extract_bundle, local_platform, verify_bundle
directory, keys, version, channel, stage, sequence = sys.argv[2:8]
try:
    manifest = verify_bundle(directory, keys, version=version, channel=channel, floor=0,
                             platform=local_platform())
    extract_bundle(directory, stage, version)
    identity = json.loads(open(stage + "/release.json", "rb").read())
    if identity.get("version") != version or identity.get("commit") != manifest["commit"]:
        raise ReleaseError("identity_mismatch")
except ReleaseError as exc:
    print("release check failed: " + str(exc))
    raise SystemExit(1)
open(sequence, "w").write(str(manifest["release_sequence"]) + "\n")
print("Verified " + version + " (commit " + manifest["commit"][:12] + ")")
PY
  then
    die 4 "Release $VERSION failed verification. Nothing was installed." \
      "The reason is in the line above. 'incompatible' means this release does not support this Pi or OS."
  fi
  for path in scripts/setup.sh scripts/install/update-bootstrap.sh scripts/install/tailscale.sh \
    config/env.example config/release-keys/button-box-release-2026.pub; do
    [ -f "$stage/$path" ] || die 4 "Release $VERSION is older than this installer (no $path)." \
      "Use a newer release with --version, or install from a checkout as README.md describes."
  done
  chmod 0755 "$stage"
}

# --------------------------------------------------------------------- install

runtime_units="messagebox.target messagebox-button.service messagebox-sync.service messagebox-poller.service
messagebox-dash.service messagebox-nfc.service messagebox-wifi-reset.service comitup.service comitup-web.service
messagebox-onboarding-home.service messagebox-onboarding-nfc.service messagebox-onboarding-complete.service
messagebox-onboarding-button.service messagebox-onboarding-voice-gate.service messagebox-onboarding-voice.target
messagebox-whatsapp-pairing.service"

run_setup() {
  step "Installing Button Box $VERSION (scripts/setup.sh as $ADMIN)"
  was_running=
  if [ -d /opt/messagebox ]; then
    for unit in messagebox.target comitup.service; do
      if systemctl is-active --quiet "$unit" 2>/dev/null; then
        was_running="$was_running $unit"
      fi
    done
    # setup.sh refuses to replace a running box; stop it for the repair.
    # shellcheck disable=SC2086
    systemctl stop $runtime_units 2>/dev/null || true
  fi
  wait_for_apt
  admin_home=$(getent passwd "$ADMIN" | cut -d: -f6)
  (
    cd /
    runuser -u "$ADMIN" -- env -i HOME="$admin_home" USER="$ADMIN" LOGNAME="$ADMIN" \
      PATH="$SYSTEM_PATH" LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive \
      sh "$WORK/stage/scripts/setup.sh" </dev/null
  ) || die 5 "Button Box setup failed." "Read $LOG_FILE, fix the reported problem, then run the installer again."
  for unit in $was_running; do
    systemctl start "$unit" || say "Note: could not restart $unit; the doctor below shows its state."
  done
}

set_transport() {
  current=$(installed_transport)
  [ "$current" != "$MODE" ] || return 0
  step "Selecting $MODE mode"
  if grep -q '^MSGBOX_TRANSPORT=' "$ENV_FILE"; then
    sed -i "s/^MSGBOX_TRANSPORT=.*/MSGBOX_TRANSPORT=$MODE/" "$ENV_FILE"
  else
    printf 'MSGBOX_TRANSPORT=%s\n' "$MODE" >>"$ENV_FILE"
  fi
  [ "$(installed_transport)" = "$MODE" ] || die 5 "Could not set MSGBOX_TRANSPORT=$MODE."
}

write_identity() {
  # The signed updater and the Cloud heartbeat read the installed identity.
  for name in release.json VERSION release-manifest.json; do
    install -o root -g root -m 0644 "$WORK/stage/$name" "/opt/messagebox/$name"
  done
}

bootstrap_updater() {
  step "Turning on signed updates ($UPDATES, $CHANNEL)"
  sh "$WORK/stage/scripts/install/update-bootstrap.sh" "$UPDATES" "$CHANNEL" </dev/null ||
    die 5 "The signed updater could not be installed." \
      "Read $LOG_FILE, then run the installer again. It continues from where it stopped."
}

# The signed updater refuses releases at or below this floor. Bootstrap does
# not set it, so set it to the release just installed (never lower it).
record_sequence_floor() {
  python3 -I - "$(cat "$WORK/sequence")" <<'PY' || die 5 "Could not record the update sequence floor." "Run the installer again."
import sys
sys.path.insert(0, "/usr/lib/messagebox-update")
from pathlib import Path
from bounded_update import read_state, save_state, transaction_locks
sequence = int(sys.argv[1])
import time
from bounded_update import UpdateError
for attempt in range(30):
    try:
        with transaction_locks(Path("/")):
            if read_state(Path("/"), "sequence.json", 0) < sequence:
                save_state(Path("/"), "sequence.json", sequence)
        break
    except UpdateError as exc:
        if str(exc) != "not_idle" or attempt == 29:
            raise
        time.sleep(2)
print(f"Update floor: release sequence {max(sequence, read_state(Path('/'), 'sequence.json', 0))}")
PY
}

install_tailscale() {
  if dpkg-query -W -f='${Status}' tailscale 2>/dev/null | grep -q 'install ok'; then
    return 0
  fi
  if [ ! -f "$WORK/stage/scripts/install/tailscale.sh" ]; then
    say "Note: remote support (Tailscale) is not installed."
    return 0
  fi
  step "Installing remote support (Tailscale, not enrolled)"
  sh "$WORK/stage/scripts/install/tailscale.sh" </dev/null ||
    die 5 "Tailscale could not be installed." "Run the installer again."
}

repair_updater() {
  step "Signed updater is installed; checking its services"
  systemctl enable --now messagebox-update-recovery.service messagebox-update-controller.service \
    messagebox-update-supervisor.timer messagebox-update-discovery.timer ||
    die 5 "The updater services could not start." "Read: sudo journalctl -u messagebox-update-controller -n 50"
}

apt_busy() {
  systemctl is-active --quiet apt-daily.service 2>/dev/null ||
    systemctl is-active --quiet apt-daily-upgrade.service 2>/dev/null
}

# The automatic first-boot update holds the dpkg lock; setup.sh's apt calls
# would fail with "Could not get lock /var/lib/dpkg/lock-frontend".
wait_for_apt() {
  waited=0
  while apt_busy; do
    [ "$waited" -lt "$APT_WAIT" ] || die 5 "Automatic system updates are still running after $((APT_WAIT / 60)) minutes." \
      "Wait for them to finish (systemctl status apt-daily-upgrade.service), then run the installer again."
    [ "$waited" -gt 0 ] || say "Waiting for the automatic system update (apt-daily) to finish..."
    sleep 10
    waited=$((waited + 10))
  done
}

prerequisites() {
  wait_for_apt
  missing=
  for tool in curl minisign python3 qrencode; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if [ "$MODE" = cloud ]; then
    command -v espeak-ng >/dev/null 2>&1 || missing="$missing espeak-ng"
  fi
  [ -n "$missing" ] || return 0
  step "Installing tools:$missing"
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=$APT_LOCK_TIMEOUT update -q ||
    die 5 "apt-get update failed." "Check the internet connection, then run the installer again."
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=$APT_LOCK_TIMEOUT install -y -q \
    --no-install-recommends ca-certificates $missing ||
    die 5 "Could not install:$missing" "Run: sudo apt-get install$missing, then run the installer again."
}

main() {
  # Never inherit a strict administrator umask: services must read the files.
  umask 022
  # When piped from curl, the shell reads this script on stdin. Nothing below
  # may read it.
  exec </dev/null
  PATH=$PATH:$SYSTEM_PATH
  export PATH
  parse_args "$@"
  check_root "$@"
  start_log
  trap finish EXIT
  trap 'exit 130' HUP INT TERM
  preflight
  if [ "$CHECK_ONLY" -eq 1 ]; then
    say "Preflight passed. Nothing was installed."
    return 0
  fi

  if [ -n "$TIMEZONE" ]; then
    [ -f "/usr/share/zoneinfo/$TIMEZONE" ] || die 3 "Unknown timezone: $TIMEZONE" "List them: timedatectl list-timezones"
    timedatectl set-timezone "$TIMEZONE"
  fi
  prerequisites

  need_setup=1
  [ ! -L /opt/messagebox ] || need_setup=0
  need_bootstrap=0
  if [ "$MODE" = cloud ] && [ ! -f /etc/button-box-update/installed ]; then
    need_bootstrap=1
  fi
  if [ "$need_setup" -eq 1 ] || [ "$need_bootstrap" -eq 1 ]; then
    step "Downloading and verifying the signed release"
    fetch_release
    verify_release
  fi
  if [ "$need_setup" -eq 1 ]; then
    run_setup
    set_transport
    write_identity
  fi
  if [ "$need_bootstrap" -eq 1 ]; then
    [ -f /opt/messagebox/release.json ] || [ -L /opt/messagebox ] ||
      die 5 "/opt/messagebox/release.json is missing, so updates cannot be turned on." "Run the installer again."
    bootstrap_updater
    record_sequence_floor
  elif [ "$MODE" = cloud ]; then
    repair_updater
  fi
  install_tailscale

  step "Checking the box (sudo messageboxctl doctor)"
  doctor_status=0
  messageboxctl doctor || doctor_status=$?

  link_status=0
  if [ "$MODE" = cloud ]; then
    step "Connecting to Button Box Cloud"
    messageboxctl claim-link || link_status=$?
  else
    step "Next steps"
    say "Finish setup on this Pi: messagebox-dev-onboard (see INSTALL.md)."
  fi
  [ "$doctor_status" -eq 0 ] || die 7 "Installed, but the doctor found a problem (see FAIL lines above)." \
    "Follow each Fix line, then run: sudo messageboxctl doctor"
  [ "$link_status" -eq 0 ] || die 6 "Installed, but the Cloud setup link is not ready." \
    "Run: sudo messageboxctl claim-link"
  say ""
  say "Button Box is installed. Log: $LOG_FILE"
}

main "$@"
