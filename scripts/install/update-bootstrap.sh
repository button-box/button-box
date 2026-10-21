#!/bin/sh
# Attended installation of fixed updater/recovery, separately from app updates.
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Run the attended bootstrap as root.' >&2; exit 1; }
REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
MODE=${1:-auto}
CHANNEL=${2:-stable}
[ ! -e /etc/button-box-update/installed ] || { echo "Already bootstrapped; use an attended controller maintenance release." >&2; exit 1; }
case "$MODE" in auto|ask) ;; *) exit 2 ;; esac
case "$CHANNEL" in stable|canary) ;; *) exit 2 ;; esac
# Only Cloud boxes support this path. No modifications to a wacli installation.
python3 - "$REPO_DIR" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts/install'))
from release_verify import SUPPORTED_MODELS, local_platform, public_key_line, public_key_id
source = Path(sys.argv[1]) / 'config'
release = {public_key_id(public_key_line(key)) for key in (source / 'release-keys').glob('*.pub')}
emergency = {public_key_id(public_key_line(key)) for key in (source / 'emergency-keys').glob('*.pub')}
if release & emergency:
    raise SystemExit('Emergency and release keys must be distinct.')
platform = local_platform()
if platform['os'] != 'raspios-trixie' or platform['architecture'] != 'aarch64' or platform['model'] not in SUPPORTED_MODELS:
    raise SystemExit('Updater bootstrap requires a supported Pi 4 or Pi 3 A+ box on Trixie / aarch64.')
lines = Path('/etc/messagebox/env').read_text().splitlines()
if not any(line.strip() in {'MSGBOX_TRANSPORT=cloud', 'MSGBOX_TRANSPORT="cloud"'} for line in lines):
    raise SystemExit('Updater bootstrap requires a Cloud box.')
PY
BOX_TIMEZONE=$(timedatectl show --property=Timezone --value)
[ -n "$BOX_TIMEZONE" ] || { echo "Configure the box timezone before bootstrap." >&2; exit 1; }
command -v minisign >/dev/null
command -v espeak-ng >/dev/null
getent passwd messagebox-update >/dev/null || useradd --system --user-group --home-dir /var/cache/button-box-update --shell /usr/sbin/nologin messagebox-update
install -d -o root -g root -m 0755 /usr/lib/messagebox-update /etc/button-box-update /var/lib/button-box-update /run/messagebox-update
install -d -o root -g root -m 0700 /var/lib/button-box-update/snapshots /var/lib/button-box-update/staging /var/lib/button-box-update/downloads
install -d -o messagebox-update -g messagebox-update -m 0750 /var/cache/button-box-update
for module in bounded_update release_verify trust_update update_controller update_discovery health-probe; do
  if [ -e "/usr/lib/messagebox-update/$module.py" ] && ! cmp -s "$REPO_DIR/scripts/install/$module.py" "/usr/lib/messagebox-update/$module.py"; then
    echo "Existing fixed controller differs; attended recovery-tested maintenance required." >&2
    exit 1
  fi
  install -o root -g root -m 0644 "$REPO_DIR/scripts/install/$module.py" "/usr/lib/messagebox-update/$module.py"
done
python3 - "$REPO_DIR" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/usr/lib/messagebox-update')
from trust_update import bootstrap_keys
bootstrap_keys(Path(sys.argv[1]) / 'config', Path('/etc/button-box-update'))
PY
# The files/units installed during this attended step are frozen for B72.
for unit in "$REPO_DIR"/systemd/messagebox-update-*; do
  install -o root -g root -m 0644 "$unit" /etc/systemd/system/
done
install -d -o root -g root -m 0755 /etc/systemd/system/messagebox-update-discovery.service.d
install -o root -g root -m 0644 "$REPO_DIR/config/update-discovery-voice-ready.conf" /etc/systemd/system/messagebox-update-discovery.service.d/voice-ready.conf
install -o root -g root -m 0644 /dev/null /run/messagebox-update/maintenance.lock
# Add ordering to existing units, including activators and ComItUp. Dependencies
# live in bootstrap drop-ins so ordinary wacli setup does not need recovery.
for unit in messagebox.target messagebox-audio-detect.service messagebox-button.service messagebox-sync.service messagebox-poller.service messagebox-dash.service messagebox-nfc.service messagebox-wifi-change.service messagebox-wifi-change.path messagebox-wifi-watchdog.service messagebox-wifi-watchdog.timer messagebox-mode-reconcile.service messagebox-mode-reconcile.path comitup.service comitup-web.service messagebox-onboarding-home.service messagebox-onboarding-nfc.service messagebox-onboarding-complete.service messagebox-onboarding-complete.path messagebox-onboarding-button.service messagebox-onboarding-voice-gate.service messagebox-onboarding-voice.path messagebox-onboarding-voice.target messagebox-whatsapp-pairing.service messagebox-wifi-reset.service; do
  install -d -o root -g root -m 0755 "/etc/systemd/system/$unit.d"
  cat > "/etc/systemd/system/$unit.d/software-update.conf" <<'EOF'
[Unit]
Requires=messagebox-update-recovery.service
After=messagebox-update-recovery.service
ConditionPathExists=!/var/lib/button-box-update/install-inhibit
EOF
done
# Arm boot recovery before the first layout mutation. A power cut during
# conversion must be recoverable without SSH or the candidate application.
systemctl daemon-reload
systemctl enable --now messagebox-update-recovery.service
# Bootstrap has its own journal and uses the old installed identity verbatim.
python3 /usr/lib/messagebox-update/bounded_update.py bootstrap
python3 - "$MODE" "$CHANNEL" "$BOX_TIMEZONE" <<'PY'
import secrets
import sys
from pathlib import Path
sys.path.insert(0, '/usr/lib/messagebox-update')
from bounded_update import durable_json
policy = Path('/var/lib/button-box-update/policy.json')
# An existing Ask policy cannot be overwritten by rerunning bootstrap.
if not policy.exists():
    from zoneinfo import ZoneInfo
    timezone = sys.argv[3]
    ZoneInfo(timezone)
    durable_json(policy, {'mode': sys.argv[1], 'channel': sys.argv[2], 'timezone': timezone, 'jitter_minutes': secrets.randbelow(31)})
PY
systemctl daemon-reload
systemctl enable --now messagebox-update-recovery.service messagebox-update-controller.service messagebox-update-supervisor.timer messagebox-update-discovery.timer
install -o root -g root -m 0644 /dev/null /etc/button-box-update/installed
printf '%s\n' 'Fixed updater installed. Run the canary recovery and physical checks before rollout.'
