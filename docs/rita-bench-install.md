# Integrated candidate: fresh bench install

Use this procedure for a new Cloud box. Do all checks before the handout.
Do not use it to update an installed box. Do not rerun `scripts/setup.sh` or
`scripts/install/update-bootstrap.sh` after the application path becomes a
symlink. The later A/B slot bootstrap is a separate, idempotent operation.
Do not use the old attended controller
promotion procedure. This candidate includes the A/B updater and the voice pack.
It excludes `feat/announce-recipient`.

The commands below are instructions. This source test run did not install a Pi.
The handout target is 23 October 2026 at 16:00 Europe/Lisbon.

## 1. Prepare the inputs

Use Raspberry Pi OS Lite 64-bit, Debian 13 (Trixie), on a Raspberry Pi 4 Model B.
Use a non-root administrator with sudo. Connect the USB microphone, USB speaker,
button, LED and NFC reader before setup. Use one capture device; disconnect
other microphones. Setup selects the lowest-numbered capture device. Provide
bench internet access. Enable
SSH in Imager and verify its host key by an independent method. Use wired
Ethernet for bench SSH. Keep it connected through ship mode. Wi-Fi reset removes
the saved bench Wi-Fi network, and the hotspot firewall blocks SSH.

Use the exact final commit on `release/rita-candidate`. Record its full hash.
Use a clean local checkout; keep the untracked Astra review note outside that
checkout. Do not push or publish as part of this procedure.

Prepare a signed candidate on the offline signing computer with the existing
release tools. Use an unused tag such as `v0.3.13-canary.1`, application and
controller sequences reserved from the private signing record, and these options:

```sh
python3 scripts/release/build.py "$TAG" \
  --release-sequence "$APP_SEQUENCE" --min-updater-version 1.3.0 \
  --controller-sequence "$CONTROLLER_SEQUENCE" --min-application-version "$TAG" \
  --controller-secret-key /media/offline-signing/release.key \
  --controller-public-key config/release-keys/button-box-release-2026.pub \
  --output "$ASSETS"
```

`TAG`, `APP_SEQUENCE`, `CONTROLLER_SEQUENCE` and `ASSETS` must contain the reviewed
values. Do not copy the example tag if it is already used. Complete the existing
[offline signing procedure](releases.md#offline-human-signing) for `SHA256SUMS`.
Keep both private keys off the Pi. Do not lower sequence floors or replace pins
with test keys. The source `VERSION` is a development value; the build generates
the installed version from the tag.

The required asset directory contains these four files:

```text
button-box-<TAG>.tar.gz
release-manifest.json
SHA256SUMS
SHA256SUMS.minisig
```

Also make a source archive from the exact candidate commit. Setup and bootstrap
are source tools; the signed application archive does not contain them.

```sh
# On the bench computer, in the clean candidate checkout:
CANDIDATE_COMMIT=$(git rev-parse release/rita-candidate)
git archive --format=tar "$CANDIDATE_COMMIT" -o /tmp/button-box-candidate-source.tar
PI=admin@button-box-NNN.local
ssh "$PI" 'hostname; cat /proc/device-tree/model; uname -m; cat /etc/os-release'
scp /tmp/button-box-candidate-source.tar "$PI":/tmp/
# Set ASSETS to the reviewed signed asset directory, and TAG to its exact tag.
ssh "$PI" 'mkdir -p /tmp/button-box-candidate-assets'
scp "$ASSETS/button-box-$TAG.tar.gz" "$ASSETS/release-manifest.json" \
  "$ASSETS/SHA256SUMS" "$ASSETS/SHA256SUMS.minisig" \
  "$PI":/tmp/button-box-candidate-assets/
ssh -t "$PI"
```

Replace `NNN` with the verified box number. Compare the reported hardware,
hostname and physical label. Stop if they differ. Do not create a tailnet route
for this install.

## 2. Install the application source and Cloud configuration

Run on the Pi as the administrator. Set the two values from the bench record.

```sh
set -eu
umask 022
CANDIDATE_COMMIT=REPLACE_WITH_THE_FULL_40_CHARACTER_CANDIDATE_COMMIT
TAG=v0.3.13-canary.1
mkdir -p "$HOME/button-box-candidate"
tar -xf /tmp/button-box-candidate-source.tar -C "$HOME/button-box-candidate"
cd "$HOME/button-box-candidate"
./scripts/setup.sh
sudo timedatectl set-timezone Europe/Lisbon
sudo apt-get install -y --no-install-recommends minisign espeak-ng
sudo sed -i 's/^MSGBOX_TRANSPORT=.*/MSGBOX_TRANSPORT=cloud/' /etc/messagebox/env
printf '%s\n' 'MSGBOX_CLOUD_API_URL=https://button.box/cloud-api/v1' | \
  sudo tee -a /etc/messagebox/env >/dev/null
```

Do not run `messagebox-dev-onboard`. That command pairs the standalone wacli
transport. Setup installs wacli dependencies in both modes; Cloud mode does not
use the linked-account transport.

Record this source installation with a distinct local identity. It is not yet a
committed signed application. This step is required: setup does not write
`release.json`, and layout bootstrap requires it.

```sh
sudo python3 - "$CANDIDATE_COMMIT" <<'PY'
import json, re, sys
from pathlib import Path
commit = sys.argv[1]
assert re.fullmatch('[0-9a-f]{40}', commit), 'Use the exact full source commit'
path = Path('/opt/messagebox/release.json')
assert not path.exists(), 'Fresh install only'
path.write_text(json.dumps({'version': 'bench-install', 'commit': commit}) + '\n')
path.chmod(0o644)
PY
sudo systemctl start messagebox-audio-detect.service
sudo ./scripts/install/update-bootstrap.sh auto canary
```

For a stable tag, use `auto stable`. Do not change channels to bypass signature
or sequence checks. Bootstrap must finish without an error.

## 3. Commit the signed application, then activate A/B

```sh
sudo install -d -o root -g root -m 0700 /root/button-box-candidate-assets
sudo install -o root -g root -m 0600 \
  /tmp/button-box-candidate-assets/* /root/button-box-candidate-assets/
sudo python3 /usr/lib/messagebox-update/update_controller.py attended-install \
  --bundle /root/button-box-candidate-assets --version "$TAG"
# Observe the worker. Wait until it has completed; a queued request is not success.
sudo journalctl -fu messagebox-update-install.service
```

Exit the log view with Ctrl-C. Check the transaction before continuing:

```sh
sudo python3 - "$TAG" <<'PY'
import json, sys
from pathlib import Path
state = Path('/var/lib/button-box-update')
journal = json.loads((state / 'journal.json').read_text())
result = json.loads((state / 'result.json').read_text())
assert journal['kind'] == 'application' and journal['phase'] == 'committed'
assert result['state'] == 'installed' and result['software_version'] == sys.argv[1]
assert Path('/opt/messagebox').resolve() == Path(journal['new'])
PY
sudo systemctl start messagebox-update-bootstrap.service
sudo systemctl is-enabled messagebox-update-selector.timer
sudo systemctl is-active messagebox-update-selector.timer
sudo systemctl show messagebox-update-discovery.service -p TimeoutStartUSec
```

Require `enabled`, `active` and `TimeoutStartUSec=30min`. The supervisor also
requests A/B bootstrap after the first application commit. The explicit service
start above completes or verifies that same idempotent operation after worker exit.
After success, a durable receipt and completion link stop repeat bootstrap jobs.
The generated `zz-bootstrap-trigger.conf` checks the application link before it
queues another job. The protected five-second safety timers stay unchanged.
Check the effective order on the Pi:

```sh
sudo systemctl show messagebox-update-supervisor.service -p ExecStartPost
sudo readlink /var/lib/button-box-controller/bootstrap-complete
sudo journalctl --since '2 minutes ago' -u messagebox-update-bootstrap.service
```

Require one `/bin/sh -c` post command with the `-ef` link check, completion at the
current committed application, and no repeated bootstrap jobs in steady use.
Bootstrap reloads systemd only after a unit changes or its loaded route differs.

Check the GOOD record and the active slot with the frozen selector:

```sh
sudo python3 -B - <<'PY'
import runpy
from pathlib import Path
api = runpy.run_path('/usr/lib/messagebox-selector/selector.py')
root = Path('/')
good = api['valid'](root)
assert good, 'No valid GOOD record'
for slot, record in good:
    print(slot, record['version'], record['generation'], record['descriptor'])
selected = api['selected'](root)
assert selected['slot'] in {slot for slot, _ in good}
assert not selected['trial']
print('active slot:', selected['slot'])
PY
sudo systemctl cat messagebox-update-install.service
sudo systemctl cat messagebox-update-recovery.service
sudo systemctl cat messagebox-update-controller.service
sudo systemctl cat messagebox-update-discovery.service
sudo systemctl cat messagebox-update-supervisor.service
```

Require controller version `1.3.0` and slot A on the first install. One GOOD record
is correct at this stage. Slot B gets GOOD after its first successful signed
controller publication. All five services must have the generated
`90-controller-slot.conf` route through the frozen selector. Do not hand-edit GOOD
or selection files. The active record is `/run/messagebox-selector/selected.json`.

## 4. Complete the bench checks

Reboot once. Repeat the GOOD, active-slot and timer checks. Require the broker,
recovery and supervisor services to be healthy. Check for failed units:

```sh
sudo reboot
# Reconnect after boot, then run:
systemctl --failed
systemctl is-active messagebox-update-controller.service \
  messagebox-update-recovery.service messagebox-update-selector.timer \
  messagebox-update-supervisor.timer messagebox-update-discovery.timer
sudo systemctl start messagebox-audio-detect.service
sudo grep -E '^MSGBOX_(MIC|SPK)_DEV=' /run/messagebox-audio/audio.env
arecord -l
aplay -l
sudo -u messagebox -H env MSGBOX_TRANSPORT=cloud PYTHONPATH=/opt/messagebox python3 - <<'PY'
from messagebox.settings import SettingsStore
settings, warning = SettingsStore().load()
assert not warning
assert settings['voice_pack'] == 'buttonbox'
print('voice_pack=buttonbox')
PY
```

Use the existing hardware test to check microphone, speaker, button, LED and NFC.
Listen to the USB microphone recording. Listen to the buttonbox setup prompts.
Use `sudo -u messagebox -H /opt/messagebox/dev/hardware-test.sh`.
Check `voice_pack` is `buttonbox` in the fresh local settings. Keep settings and
credentials out of shared logs. On a spare box with a disposable test identity, complete a Cloud setup test
with the real service: first greeting, first note, separate reply press, review, send,
listened notice, card routing, mispress, quiet hours and Wi-Fi return. Check the
rule-2b greeting offer and ACK contract against the live Cloud service. Require
both a fresh latest heartbeat and the current poll feature. Verify an atomic
single offer and no redelivery after a lost ACK or rollback, including a failed
rollback heartbeat. Verify the four wire ACK mappings: received → received,
applied → played, rejected → failed, expired → expired. Stall a greeting transfer
and send a real note; the note must still queue. Change voice while a greeting
waits; it must not play in the old voice. Source tests do not establish that
this server contract or the physical behavior is live.

On a spare box, install a higher-sequence signed test application through the
selected controller. Test rollback and reboot recovery. Do not induce power loss
on the handout box. Test unattended controller replacement with a signed future
application that remains installable by controller 1.3.0. It must carry changed
controller bytes and signed proof, add the trigger to the supervisor → bootstrap
path, and raise the updateable bootstrap timeout from 60 to 300 seconds. Call
bounded publication only after bootstrap returns and releases its locks. Keep
the publication service's 180-second bound. Do not invoke `promote` manually for
this acceptance test. Let the existing supervisor start it. Require the new
GOOD record, selection after reboot, independent timer recovery after a failed
trial, and another application OTA through the accepted new controller.

The host acceptance test covers this sequence. The candidate itself keeps the
existing activation behavior; the trigger and timeout change are in the signed
future application used by the test. Native Pi execution still needs acceptance.

Before packaging, arm the recipient setup with the existing manufacturer flow:

```sh
sudo messagebox-init-wifi-onboarding
# Print the insert privately. Keep its hotspot password out of Git and logs.
sudo messageboxctl reset-wifi
```

Verify the setup hotspot and voice. Do not complete the buyer's setup or send
messages on the buyer's behalf. Keep wired SSH and any existing Tailscale access until
all bench work, reboot tests and insert checks are complete.

## 5. Ship mode: the last remote operation

Run this only after all checks pass. It stops and disables both remote access
paths. The transient timer runs on the Pi, so the operation can finish after
the SSH connection closes. Do not use it until local physical access is available.

```sh
sudo systemd-run --unit=messagebox-ship-mode --on-active=10s \
  /bin/sh -c 'set -eu; for unit in tailscaled.service ssh.socket ssh.service; do
    if [ "$(systemctl show "$unit" -p LoadState --value)" != not-found ]; then
      systemctl disable --now "$unit"
    fi
  done'
```

Confirm loss of SSH and Tailscale access from the bench computer. Reboot locally
and confirm they stay off. Confirm the setup hotspot and prompt still work. If
the box is already claimed for a disposable test, its Cloud health report must
show `remote_access.ssh=off` and `remote_access.tailscale=off`. No remote report is
expected from a fresh unclaimed box. Power down locally and package the box.
Any further bench repair requires local access and another full final check.

## Automated evidence and limits

`tests/test_update_bootstrap.py` runs the setup shell, actual install allowlists,
bootstrap shell, signed first application, A/B bootstrap and selection, installed
voice runtime, and a later signed application through the permanent A/B route.
It interrupts probation and checks selector rollback, routes and voice restart.
The slot harness separately checks controller publication, trial, both slots,
write cuts and fallback. Tests use disposable minisign keys.

These tests replace OS packages, accounts, systemd and hardware. They do not
prove native service ordering, root permissions, Pi power-cut recovery, audio
quality, NFC reliability, live Cloud behavior or physical wacli acceptance.
