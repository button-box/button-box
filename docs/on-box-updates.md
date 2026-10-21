# On-box software updates

This updater is for Cloud boxes on Raspberry Pi OS Trixie and aarch64. The
supported models are `Raspberry Pi 4 Model B` and `Raspberry Pi 3 Model A Plus`
(`SUPPORTED_MODELS` in `scripts/install/release_verify.py`). The updater and
bootstrap reject other models. It installs signed application releases directly
from `button-box/button-box` GitHub Releases. OS, package and dependency
upgrades are attended maintenance. Pi 3 A+ memory, timing and hardware
acceptance is still required before deployment.

This checkout pins two public keys. The release key is
`config/release-keys/button-box-release-2026.pub` (key ID 221CAE6604AC0F93).
The emergency key is `config/emergency-keys/button-box-emergency-2026.pub`
(key ID 73BD56BB8F9940FC). Bootstrap copies them to
`/etc/button-box-update/release-keys` and `/etc/button-box-update/emergency-keys`.
Without a pinned release key there, verification fails closed. Generate signing
keys offline; never transfer private keys to a box.

## Layout and bootstrap

- `/opt/messagebox-releases/<tag>-<full-commit>/`: root-owned application trees.
- `/opt/messagebox`: the only permitted application symlink, replaced atomically.
- `/opt/messagebox-deps/venv-nfc`: stable dependency environment, linked from
  every release at `venv-nfc`. Existing executable paths continue to work.
- `/usr/lib/messagebox-update`: fixed installer, verifier, broker and downloader.
- `/var/lib/button-box-update`: root-owned journal, snapshots, policy, sequence
  floor, quarantine, operation receipts and public status. Application rollback
  never restores this policy or lowers the sequence floor.
- `/var/cache/button-box-update`: unprivileged downloads, API ETags and cooldown.
- `/run/messagebox-update`: peer-checked Unix socket, maintenance reservation and
  transaction-bound readiness evidence.

An attended bootstrap first installs the fixed recovery unit and start-order
inhibit drop-ins, and enables recovery **before** moving the legacy application.
It then journals the conversion, stops activators before services, renames the
existing tree and switches the pointer. A power cut at any conversion boundary
restores the legacy directory on boot. NFC dependency bytes stay at the stable
path, with the compatibility symlink restored even after a cut during the move.
The conversion requires a verified full installed commit in `release.json`.
The old application need not have updater support: after conversion, root can
queue the first signed B72 application with `update_controller.py attended-install
--bundle <root-owned-assets> --version <exact-tag>`. The independent controller
owns health and rollback for this first application transaction too. No legacy
in-place application update is needed between recovery bootstrap and B72.

Run `scripts/install/update-bootstrap.sh auto stable` as root on an already
reviewed Cloud application installation. The first argument may be `ask`, and
the second may be `canary`. Provision the box timezone before bootstrap; bootstrap reads it through timedatectl.
Install `minisign` and `espeak-ng` in this attended step; routine updates never
run apt, pip, setup, provisioning or migration hooks. A completed bootstrap
cannot be rerun to override Ask policy or replace its executing controller.
Bootstrap copies the six fixed Python modules directly from the source tree
containing `update-bootstrap.sh`, under `scripts/install/`. Newly bootstrapped
boxes therefore get that tree's sound-validation controller from the start.

The controller and recovery launcher remain fixed for B72. Releases stage and
syntax-check controller copies in `updater/`; they do not overwrite the running
recovery implementation. Promoting changed fixed code, emergency pins, recovery
units or dependencies requires a separate attended maintenance release with
recovery testing. B72 routine updates reject changed fixed units or NFC
requirements. `setup.sh` and legacy `apply` refuse converted installations.

## Emergency release-key replacement

Release keys authenticate application releases only. A distinct emergency key
authenticates only a bounded `trust-update.json` and its
`trust-update.json.minisig`. Bootstrap installs reviewed `config/emergency-keys/*.pub`
as root-owned 0644 files at `/etc/button-box-update/emergency-keys/` and refuses
overlapping release/emergency key IDs. No emergency public pin means this feature
is off; ordinary release verification and discovery retain their existing path.
This checkout pins the emergency public key
`config/emergency-keys/button-box-emergency-2026.pub` (key ID 73BD56BB8F9940FC).
It contains no throwaway trusted key.

The schema is exactly `schema: 1`, `product: "button-box"`, positive integer
`trust_sequence`, `release_keys` (1–3 canonical minisign base64 public-key lines,
without comments), `revoked_key_ids` (unique uppercase 16-digit minisign key IDs),
and `created` (ISO timestamp with seconds and timezone). Unknown or duplicate
fields, invalid types/keys/dates and JSON over 8 KiB are rejected. The created
timestamp is descriptive; only the signed sequence establishes order.

Publish the two assets on a normal GitHub release or a dedicated `trust-v<N>`
prerelease. With an emergency pin, the same unprivileged downloader scans bounded
paginated releases on both channels, verifies the highest emergency-signed trust
sequence and hands bytes to root before considering application releases, even
for an explicit owner install request. Root copies through no-follow descriptors,
verifies again using emergency pins only, and requires a sequence above
`/var/lib/button-box-update/trust-sequence.json`. Only the downloader UID may
submit that local command; Cloud has only the existing discovery hint. No
physical confirmation, audio cue or microphone operation is involved. A bounded
log records the sequence, and heartbeat reports `software_update.trust_sequence`.

`release-keys` is a root-owned symlink to a complete 0755 generation under
`/etc/button-box-update/release-keysets/` with root-owned 0644 `.pub` files. The
controller fsyncs a root-owned pending intent, writes and fsyncs the new key
directory, atomically renames the symlink, and fsyncs the sequence/revocation
record. On restart it rolls a pending intent forward before accepting work.
There is always a complete old or new key set; application rollback cannot
restore either the trust record or old pins. Trust replacement and the worker's
signature check through installation share a nonblocking exclusive lock. If an
install is already running, discovery defers replacement through its existing
backoff. After replacement, even an already downloaded candidate must pass the
worker's check with current release keys. Emergency keys are excluded from
normal release verification, including an accidental duplicate release pin.

Revocation history is cumulative on the device: a later update cannot re-add a
revoked ID. Every published trust document must also carry **all** previously
revoked IDs so boxes that skipped earlier trust updates retain that protection.
For a stolen release key, drop/revoke it and add a new release key in an
emergency-signed trust update, then publish a normal release signed with the new
key. If both secret keys are lost, existing Cloud features keep working but box
software freezes; recovery is an SD-card swap. See [release procedures](releases.md#emergency-trust-updates).

## Signed asset contract

Each release has exactly these required assets:

```text
button-box-<exact-tag>.tar.gz
release-manifest.json
SHA256SUMS
SHA256SUMS.minisig
```

Sign the exact `SHA256SUMS` bytes with minisign. Its two canonical entries name
only the archive and external manifest. Verify the signature before trusting
any metadata or hashes. Both downloader and root worker check compatibility;
the root worker copies bytes through no-follow file descriptors and verifies
again before extraction and installation. The archive's manifest must match
the signed external manifest. No links, hardlinks, devices, traversal, duplicate
paths, oversized members or more than 10,000 members are accepted. Compressed
and expanded sizes are bounded at 128 MiB and 256 MiB.

The schema matches the release tooling: `schema: 1`, `product: "button-box"`,
`tag` and `version` equal the exact tag, full `commit`, positive integer
`release_sequence`, `channel: "stable" | "canary"`, `supported_os`,
`supported_arch`, `supported_models`, `min_updater_version` as `major.minor.patch`,
and the existing complete installed-file mapping/hashes. This controller is
`1.2.0`. The voice release requires `min_updater_version: "1.2.0"`. The updater
preparation release remains installable with `min_updater_version: "1.1.0"`.
Sequence ordering is global across channels; version strings are never
sorted to decide eligibility. Stable boxes reject canaries; canary boxes may
accept a stable release with a higher signed sequence. Recovery permits only
the journal's exact previous tree, regardless of the higher sequence floor.

The signed release defines its own bundled sound list. The original cue, voice,
voice-pack, ringtone and Cloud files remain required; additional sounds may be
added and later removed without changing the controller. Program code, units
and configuration retain their existing install allowlists. The trust model is
unchanged: the signature covers the manifest list and every file byte through
the signed hashes.

New files are allowed directly in `sounds/cues`, `sounds/cloud`, `sounds/voice`,
`sounds/ringtones`, or the existing `sounds/voices/{pirate,alien,dj,robot,french,charlie,tata,buttonbox}`
folders. WAV basenames must match `[a-z0-9][a-z0-9._-]{0,80}\.wav`.
`manifest.json` and `cues.json` are also permitted in those folders. Existing
README files and ringtone lamp files retain only their exact existing mappings.
Other folders, nested paths, traversal, links and nonregular files are rejected.
Each WAV must be nonempty RIFF/WAVE PCM, 48 kHz, 16-bit mono, with complete frame
data. Limits are 2 MiB per sound file, 160 MiB for all sound files (including
metadata), and 2,000 sound files. The shipped tree already exceeds 64 MiB.
New sounds map beneath `/opt/messagebox/sounds`; original ringtones retain
`/opt/messagebox/ringtones` for the shared runtime. Both paths live in each
release directory. Previous releases keep their own bytes, so rollback restores
their sounds without sharing or overwriting another release's pack.

## Transaction and recovery

Lock order is update, mode transition, then the exclusive interaction/audio
reservation. Onboarding and active interactions defer installation. New
recordings, playback, previews and NFC audio use shared reservations; mode
transitions recheck the inhibit under their own lock. No new microphone permit
is created for an update gesture.

The root worker stages a complete tree and a bounded snapshot of integration
files, selectors and unit states, then fsyncs `prepared` before any installed
mutation. It advances the signed sequence floor, sets persistent inhibit, stops
path/timer activators first, stops services, installs integration files and
atomically changes the pointer. It reloads systemd, removes the start inhibit
and restarts only the recorded services in the existing order. Activator paths and timers stay suspended through probation and resume after
health passes. Competing mode reconciliation stays locked throughout probation; interaction reservations
reopen for the candidate runtime.

Health has a five-minute deadline and requires 60 seconds of uninterrupted
success: recorded intended units active; stable required daemon PID/restart
counters; fresh successful Cloud heartbeat for the transaction's version,
commit and boot; button owner with GPIO acquired; expected ALSA capture and
playback cards enumerated as the runtime user; installed identity/file hashes.
Heartbeat and button evidence are required only from producers that were
selected (running or enabled) at transaction start; a crashed but enabled
producer still has to prove itself. A unit that was already `failed` before
the update was not running, so `inactive` or `failed` both satisfy it.

**Setup mode.** Only an attended install may run while the box is in setup
mode; owner and automatic installs still return `onboarding_active`. Leave the
marker in place: do not move `/etc/messagebox-onboarding/enabled` around an
install. The snapshot then records setup-mode units and generator output, and
rollback restores setup mode exactly. The transaction refuses with
`setup_not_ready` before any mutation if ComItUp is not active. The journal
records `mode: setup`; health then skips app evidence (no household app runs),
requires the recorded setup units (ComItUp, setup portal, guided button) in
their recorded states, the `setup:marker` check, the pointer and file hashes.
The only capture-tool health command is `arecord -l`, which lists devices and
never opens a recording stream. No audio is recorded. Presence checks do not
prove physical switch, microphone or speaker operation.

The independent supervisor enforces timeout, worker death, lost readiness and
three daemon restarts within 60 seconds. It stops the worker before acquiring
its locks, then restores the exact previous pointer, integration files and
recorded unit states. Before restarting units it runs `systemctl reset-failed`
only on `.path`/`.timer` activators whose result is `start-limit-hit`,
`unit-start-limit-hit` or `trigger-limit-hit`. A restored activator that trips
its own rate limit again is accepted as the old release's behavior; services,
and activators that failed for any other reason, still fail restoration. The
mode generator reruns on every reload, so the unit it selects from the restored
marker may verify as `enabled-runtime`. When restoration fails, the root-only
journal keeps `restore_error` (an updater-authored step name) next to the
health `detail`. Boot recovery runs before runtime and onboarding units;
it leaves their starts to normal boot ordering, avoiding a dependency deadlock.
Power loss during rollback resumes the same restoration. A failed restoration
keeps inhibit in place and reports `rollback_failed`; it never claims rollback
succeeded. Failed candidates are quarantined against automatic retry.

Retention keeps current plus one previous known-good tree. A pending candidate
may temporarily be a third tree. Successful commit removes snapshots and
staging; terminal recovery removes failed trees while retaining the quarantine
receipt. Pending journals protect every referenced tree/snapshot. Download
partials are never promoted after network loss; abandoned completed downloads
and partials are bounded and cleaned. Historical operator-selected legacy
backups are not deleted automatically.

## Local policy and confirmation

Auto installs from 02:00 to 04:00 in the locally provisioned timezone, with a
persistent 0–30 minute offset, and only while idle. Unsynchronized/invalid time, onboarding or
an active reservation defers installation. Busy automatic attempts remain
`waiting_for_window` and retry through the existing button idle loop after a
bounded delay. Downloading may happen earlier.

An automatic operation that is still `waiting_for_window` never blocks other
work. It closes as `installed`/`already_installed` once any install (attended,
owner or automatic) reaches its target version or a newer one, and as
`expired`/`superseded` when a different candidate replaces it. An owner
`install_update` for the same or a newer version replaces it
(`expired`/`superseded_by_owner`) and still needs its own one-use button hold.
A started (`installing`) automatic operation still returns `update_busy`. These
closures update only the operation receipt, never `result.json`. The broker and
`attended-install` serialize operation-pointer writes through
`controller.lock`.

`install_update {version}` always asks for physical confirmation, including on
an Auto box. Once the exact version is independently verified and the box is
idle, its sole GPIO owner voices:

> A new update is ready! Hold my button for three seconds to install it.

This uses bundled `sounds/cues/update-ready.wav`. Ask-to-Auto uses
`auto-updates.wav`: "Hold my button for three seconds to turn on automatic
updates." Missing or invalid recordings retain the fixed English espeak fallback.
A healthy owner-approved installation announces `update-done.wav`: "All done!
I'm updated and ready." If that recording is missing or invalid, it uses
`cue-all_set.wav`. The completion receipt, quiet-hours and idle rules remain the
same; automatic and attended bootstrap installs do not announce completion.

The box asks again once if nobody holds the button. Forty seconds after the
first ask, while at least 20 seconds of the window remain, it plays
`update-reminder.wav`: "I'm still waiting! Hold my button for three seconds to
install the update." (Ask-to-Auto repeats `auto-updates.wav`.) A short hold
keeps this one reminder. A missing or invalid reminder recording is skipped.

When a valid hold starts, the box plays the short `cue-press.wav` tap at
press-down, so the person knows the box is counting. It is the same tap as an
ordinary press, and it ends long before three seconds. A hold released before
three seconds plays only this tap: nothing is confirmed, the window stays open
and the one reminder is kept. Expired, already-held and not-yet-armed presses
get no tap. At three seconds the box plays the short `cue-card_saved.wav` "yes"
while the button is still held. After release, and before the button owner sends
`confirm`, it plays `update-installing.wav`: "Got it! I'm installing the update
now. I'll be back in a few minutes." (Ask-to-Auto: `auto-updates-on.wav`, "Got
it! Automatic updates are on.") The line waits at most 6 seconds and is skipped
when fewer than 7 seconds of the window remain, or when its recording is
missing or invalid. Like the "yes", it ends and releases its shared audio
reservation before `confirm`, so the install's exclusive reservation is never
blocked by playback.

Mute does not block this owner-approved flow: the ask, the reminder, the
press-down tap, the three-second beep and the "Got it!" line play with
`allow_muted`, because the
owner just tapped Install (or chose Auto) and a muted box could otherwise never
open the window. Quiet times never blocked the ask. Mute still silences all
other audio, including `update-done.wav`. Automatic installs have no ask and
never speak. A mute or Quiet now command sent during a line still stops it.
These lines live in the application
(`messagebox/cloud_update.py` and `messagebox/button_send.py`); the fixed
controller is unchanged.

The two-minute monotonic window starts after the cue finishes and release is
observed. A fresh continuous three-second hold must finish with release before
the deadline. Approval is one-use and bound to operation, manifest, version and
boot. Short, successful, already-held and expired confirmation presses are
consumed through release before B71's capture-session wrapper. Reboot cancels
a pending approval; duplicates cannot rearm it. An expired window needs a new
owner operation.

`set_update_mode {mode: "ask" | "auto"}` is the box-side mode command. Ask takes
effect immediately; enabling Auto from Ask requires the same physical hold.
The effective policy remains local and outside application rollback. Cloud
cannot choose a channel, URL, command, force option or downgrade. It cannot
send the button-owner IPC commands: the broker checks kernel UID **and** the
systemd button service's main PID. Heartbeat evidence similarly comes only
from the poller main PID. Root alone owns the install ticket and journal.

## Cloud reporting and discovery

The capability is advertised only after fixed installation, active recovery,
and a GPIO-owner confirmation-path handshake for the current process/boot:

```json
{"software_update":{"protocol":1,"modes":["auto","ask"],"physical_confirmation":true}}
```

Registration/heartbeat retain existing `version` semantics (`cloud-mvp-1`).
Installed identity uses `software_version` and `software_commit`. The local
page's existing footer reads the same installed record. Heartbeats include
`software_update` with `effective_mode`, `channel`, `available_version` and
`last_result` and `trust_sequence`. Update ACKs persist/replay through existing Cloud ledgers:
`downloading`, `waiting_for_button`, `waiting_for_window`, `installing`,
`installed`, `failed`, `expired`, `rolled_back`, `rollback_failed`, with exact
target/installed identity and bounded reason codes. Acceptance is not installation.
When the broker refuses a request, it returns one fixed allowlisted code (for
example `update_busy`, `operation_conflict` or `request_expired`; otherwise
`request_rejected`). The runtime ACKs the Cloud operation `rejected` with that
code in both `reason` and `error_code`; an unreachable broker reports
`updater_unavailable`.

Without an emergency pin, the unprivileged downloader uses the GitHub stable
latest endpoint or bounded paginated release lists for canary. With a pin, both
channels scan bounded pages for independent trust updates. It authenticates candidate metadata and
chooses the highest eligible signed sequence before downloading that archive.
API ETags, rate-limit reset, Retry-After, failure backoff and a ten-minute hint
cooldown persist across restarts. A twice-daily schedule has jitter; the short
systemd timer only wakes the process to honor that persisted schedule or a
coalesced request. Root has no network capability; network input never becomes
a shell string or general sudo request.

## Physical acceptance

Before merging to main, complete the canary device matrix: failed health;
power cuts before/after switch, integration writes and rollback; kill worker;
crash-loop; reboot during probation; network loss during download and health;
wrong key/tampered assets/downgrade/channel mismatch; short/held/expired/rebooted
and duplicate approvals; Ask-to-Auto request; concurrent playback/recording/
onboarding; repeated successful installs and low disk. Compare restored file
hashes, identity, enabled/active states and durable result after each failure.

Validate `/dev/watchdog0` and systemd hardware-watchdog support on the Pi before
configuring RuntimeWatchdogSec. Test a system hang under attended conditions;
unit tests do not prove hardware reset or SD recovery. The independent software
supervisor handles application crashes; a hardware watchdog covers system hangs.

Then verify voice send, receive/play, reply, family-card taps, quiet hours and
listened notices on the Cloud canary and on a physical wacli box. No deployment
or physical acceptance is established by local CI.

## Health diagnostics and attended fixed-controller maintenance

The supervisor permits a readiness dip after the candidate was ready. It records
`first_unready_mono` and rolls back only after 20 continuous seconds of failure;
a ready sample resets that clock. Initial startup still has the five-minute
deadline. The 60-second stable-success requirement and crash-loop threshold are
unchanged. `supervisor.json` retains at most 32 failing check names (160
characters each); rollback `detail` contains the first code. These codes contain
check names, never evidence values, device identifiers or command output. The
terminal operation/result retains `rolled_back` or `rollback_failed` and its
cause after the worker exits. Recovery persists the cause before restoration.

From an attended SSH session, run either:

```sh
sudo python3 /usr/lib/messagebox-update/health-probe.py \
  --journal /var/lib/button-box-update/journal.json
sudo python3 /usr/lib/messagebox-update/health-probe.py --current
```

The first command checks the transaction's pointer, hashes, intended unit states
(including suspended activators), and transaction-bound evidence. Use it during
probation. `--current` checks the running pointer, manifest application hashes,
intended units and current identity/boot evidence without transaction binding.
Every evaluated check prints `PASS` or `FAIL`; exit status is 0 only when all
pass. The probe writes no files, including Python import caches, and runs no
service mutations or audio capture. A converted legacy release without an
application manifest or readiness evidence will report missing proof; this is
not a physical audio test. After rollback the transaction probe appropriately
fails against the candidate; use `--current` for the restored release.

Bootstrap removes only unused top-level `.lgd-nfy*` FIFOs after inspecting all
`/proc/*/fd` links (including inode aliases). An open FIFO, an unreadable process
FD directory, or any other unsafe legacy entry still fails closed. Identical
canonical emergency pins are accepted on key-bootstrap retry; changed pins are
rejected before either key role is modified. This does not make the complete
bootstrap safe to rerun on an installed box.

For an **already bootstrapped** Cloud Pi, changing application `updater/` copies
will not activate these fixes. The maintainer must promote the reviewed fixed
controller separately. Do not remove `/etc/button-box-update/installed`, rerun
`update-bootstrap.sh`, call `bootstrap_keys`, run setup, or reconvert the layout.
Keep policy, pins, trust/sequence floors, quarantine, journals and snapshots.

The release-defined sound support changes the fixed `bounded_update.py` module
and bumps `release_verify.py` to controller version 1.2.0. The downloader also
gets a 1,500-second transfer budget, checked between 64 KiB reads, with the
existing 30-second socket timeout. Its fixed service needs the reviewed
1,800-second maintenance drop-in. An already bootstrapped
canary needs the attended procedure below before it can accept extra sounds.
The maintenance module list remains **bounded_update, release_verify, trust_update,
update_controller, update_discovery, health-probe**; stage and exchange the complete
set even though only three modules changed. All fixed systemd unit files are unchanged:
compare them with the source, do not reinstall them. New boxes get this through
bootstrap. Ordinary signed application updates cannot perform this promotion.

Perform this maintenance in an attended idle session, with no install or
onboarding in progress. Verify the SSH host is the intended box, Pi model,
Trixie/aarch64, Cloud mode, root-owned installed marker, controlled application
pointer and installed identity. Confirm the journal is absent or terminal
`committed`/`rolled_back`; resolve `rollback_failed` with the existing recovery
procedure first: read `restore_error` in the journal, fix the cause, then run
`sudo python3 /usr/lib/messagebox-update/bounded_update.py recover`. It repeats
the restoration and, on success, records `rolled_back`, quarantines the
candidate and clears the inhibit. Never delete the inhibit by hand. A box that
left setup mode during recovery returns with `sudo messageboxctl
enter-cloud-claim` (this keeps Wi-Fi and Cloud identity). Review the exact maintenance source revision and its hashes,
run both transport checks, and complete the release privacy review before
transferring any artifacts. Transfer only the reviewed installer modules and
fixed unit files into a root-owned, non-writable-by-others maintenance source,
for example `/root/button-box-maintenance`. Keep its installer files under
`scripts/install/`, unit files under `systemd/`, and the reviewed
`update-discovery-voice-ready.conf` under `config/`. No secret signing keys belong
on the box. The following commands run **on the verified box**, as root:

```sh
sudo -i
set -eu
SOURCE=/root/button-box-maintenance
FIXED=/usr/lib/messagebox-update
NEXT=/usr/lib/messagebox-update.next
# Record original enablement/active states privately for restoration on error.
systemctl is-active messagebox-update-install.service || true
# Continue only if the worker is inactive/failed, never activating or running.
# Quiesce triggers first, then their services and the broker. Leave boot recovery
# enabled; it must always have a complete fixed implementation.
systemctl stop messagebox-update-discovery.timer messagebox-update-supervisor.timer
systemctl stop messagebox-update-discovery.service messagebox-update-supervisor.service
# Hold trust first (as the worker does), then update/mode/interaction.
# All acquisitions fail if busy. Do not stop a broker required by a live worker.
exec 6>/var/lib/button-box-update/trust.lock
flock -n 6
exec 9>/run/lock/messagebox-bounded-update.lock
flock -n 9
exec 8>/run/lock/messagebox-mode-transition.lock
flock -n 8
exec 7<>/run/messagebox-update/maintenance.lock
flock -n 7
# Recheck under the locks before stopping the broker. A queued worker is busy
# even if it has not yet switched the application. Abort and restore triggers.
case "$(systemctl is-active messagebox-update-install.service || true)" in
  inactive|failed) ;; *) echo 'Install in progress; stop maintenance.' >&2; exit 1 ;;
esac
systemctl stop messagebox-update-controller.service
# Check terminal journal and controlled pointer again under the locks.
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
import stat
import sys
from pathlib import Path
sys.path.insert(0, '/usr/lib/messagebox-update')
import bounded_update as update
from release_verify import SUPPORTED_MODELS, local_platform
platform = local_platform()
if (platform['os'] != 'raspios-trixie' or platform['architecture'] != 'aarch64'
        or platform['model'] not in SUPPORTED_MODELS):
    raise SystemExit('This canary maintenance requires a supported Pi / Trixie / aarch64.')
marker = Path('/etc/button-box-update/installed').lstat()
if not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0 or marker.st_mode & 0o022:
    raise SystemExit('Installed marker is unsafe.')
lines = Path('/etc/messagebox/env').read_text().splitlines()
if not any(line.strip() in {'MSGBOX_TRANSPORT=cloud', 'MSGBOX_TRANSPORT="cloud"'} for line in lines):
    raise SystemExit('Cloud mode required.')
journal = update.read_state(Path('/'), 'journal.json')
if journal:
    update._validate_journal(Path('/'), journal)
    if journal['phase'] not in {'committed', 'rolled_back'}:
        raise SystemExit('Recovery required; stop maintenance.')
pointer = update.controlled_release(Path('/'))
identity = json.loads((Path(pointer) / 'release.json').read_bytes())
if pointer != update.RELEASES + '/' + identity['version'] + '-' + identity['commit']:
    raise SystemExit('Installed identity/pointer mismatch.')
if Path('/etc/messagebox-onboarding/enabled').exists():
    raise SystemExit('Onboarding active; stop maintenance.')
PY
# Fixed units remain unchanged for this repair. Any mismatch needs its own
# recovery-tested unit maintenance, rather than an ordinary app installation.
for unit in "$SOURCE"/systemd/messagebox-update-*; do
  cmp "$unit" "/etc/systemd/system/$(basename "$unit")"
done
[ -d "$FIXED" ] && [ ! -L "$FIXED" ]
[ ! -e "$NEXT" ] && [ ! -L "$NEXT" ]
install -d -o root -g root -m 0755 "$NEXT"
for module in bounded_update release_verify trust_update update_controller update_discovery health-probe; do
  install -o root -g root -m 0644 "$SOURCE/scripts/install/$module.py" "$NEXT/$module.py"
  cmp "$SOURCE/scripts/install/$module.py" "$NEXT/$module.py"
done
# Compile in memory, fsync the complete staged directory, and exchange both
# directories atomically on Linux. A power cut sees a complete old or new set.
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import ctypes
import os
from pathlib import Path
stage = Path('/usr/lib/messagebox-update.next')
for path in stage.iterdir():
    compile(path.read_bytes(), path.name, 'exec')
    with path.open('rb') as source:
        os.fsync(source.fileno())
for directory in (stage, stage.parent):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
libc = ctypes.CDLL(None, use_errno=True)
exchange = libc.renameat2
exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
exchange.restype = ctypes.c_int
if exchange(-100, b'/usr/lib/messagebox-update', -100,
            b'/usr/lib/messagebox-update.next', 2) != 0:
    raise OSError(ctypes.get_errno(), 'fixed controller exchange failed')
fd = os.open('/usr/lib', os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
# NEXT now holds the previous complete controller for attended fallback.
# Record any existing voice-ready.conf privately before this maintenance.
# Abort if an existing file differs; review its owner and purpose first.
DROPIN=/etc/systemd/system/messagebox-update-discovery.service.d
install -d -o root -g root -m 0755 "$DROPIN"
if [ -e "$DROPIN/voice-ready.conf" ]; then
  cmp "$SOURCE/config/update-discovery-voice-ready.conf" "$DROPIN/voice-ready.conf"
else
  install -o root -g root -m 0644 "$SOURCE/config/update-discovery-voice-ready.conf" "$DROPIN/voice-ready.conf"
fi
cmp "$SOURCE/config/update-discovery-voice-ready.conf" "$DROPIN/voice-ready.conf"
systemctl daemon-reload
[ "$(systemctl show messagebox-update-discovery.service --property=TimeoutStartUSec --value)" = 30min ]
flock -u 7
flock -u 8
flock -u 9
flock -u 6
systemctl start messagebox-update-controller.service
# Reconcile any old installing operation against its terminal rollback receipt.
python3 "$FIXED/bounded_update.py" supervise
systemctl start messagebox-update-supervisor.timer messagebox-update-discovery.timer
systemctl is-active messagebox-update-recovery.service messagebox-update-controller.service \
  messagebox-update-supervisor.timer messagebox-update-discovery.timer
python3 "$FIXED/health-probe.py" --current
```

Compare all installed module bytes/ownership/modes with the reviewed source and
check controller/supervisor journals. Read back the operation and result to
confirm terminal rollback reconciliation. A legacy `--current` failure due to
missing evidence/manifest needs an explicit explanation; do not call it a
healthy candidate. If any pre-exchange check fails, keep the fixed copy intact,
release locks and restore the updater services/timers to their recorded state.
If post-exchange controller startup fails, quiesce the same updater services,
acquire the same locks, exchange the directories back with the same atomic
command, and restore recorded states. Retain the old directory until attended
recovery acceptance; no household-data backup is a prerequisite.

The onboarding voice watcher previously watched all of `/var/lib/messagebox-cloud`.
Atomic Cloud state/ledger writes can change that directory repeatedly, even
outside onboarding, triggering a gate skipped by its marker condition. The
watcher now targets `claim.json`; the wacli voice-request watch is unchanged.
It remains suspended during application probation, so this trigger noise does
not itself establish the cause of a probation health dip. Confirm the live unit
contents, trigger rate and actual failing health codes during the next canary.

Finally queue a **new higher-sequence signed candidate** through the attended
installer, retaining all signature/platform/channel/sequence/idle checks. A
failed candidate remains quarantined; do not clear quarantine or lower the
sequence floor to retry it. Observe probation and its probe, preserved result,
rollback/reboot recovery, then physical send/play/reply, NFC, quiet hours and
listened notices. Fixed-controller promotion, atomic exchange/power-loss
behavior and physical canary acceptance require device verification; local
checks establish none of those outcomes.


## Voice release transition from v0.3.12

Use two releases. Test box 4 first at each step. Do not send the second release
until the first release and fixed-controller maintenance pass acceptance.

1. Build the updater preparation branch from `origin/release-v0.3.12`. Set
   `--min-updater-version 1.1.0`. It changes the controller sound allowlist and
   raises the total sound limit from 128 MiB to 160 MiB. It adds `buttonbox`
   paths and accepts the new setup WAVs through the existing sound-path rule.
   It keeps per-file, count, hash, PCM, routing, and archive checks. The fixed
   v0.3.12 controller can install this application payload. The payload keeps
   the old packs and runtime unchanged.
2. Promote the complete six-module controller set with the attended procedure
   above. Verify version 1.2.0 and all six file hashes. Apply and read back the
   discovery drop-in in the same maintenance window. An application update
   alone only stages the modules in its `updater/` directory. It does not
   activate them. Never rerun bootstrap on an installed box.
3. Build the voice release after it is based on the preparation commit. Set
   `--min-updater-version 1.2.0`, with a higher release sequence. The old verifier
   rejects this minimum before downloading the archive. The preparation
   controller can install it. Verify box 4, then use the normal batched rollout.

The larger time budget keeps WAV playback and all existing voice packs. It
allows a slow or failed download to occupy discovery for longer. The 1,800-second
service limit is the final bound. The 128 MiB compressed archive limit, 256 MiB
expanded limit, signature checks, and partial-file cleanup stay in force. No
resume support is added. A link slower than the budget can still fail safely.
Do not publish the second release until the drop-in is active on its target.
New controller bootstrap installs this same drop-in; wacli does not use bootstrap.

For attended controller fallback, quiesce and lock as above. Exchange the
complete old module directory back. Restore the previous discovery drop-in, or
remove only this maintenance-created file if it did not exist before. Reload
systemd, verify the effective timeout, and restore the recorded service states.
Application rollback alone does not restore the fixed controller or its drop-in.
Physical power-loss and controller-fallback tests remain required.
