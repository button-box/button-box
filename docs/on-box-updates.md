# On-box software updates

Controller selection and local publication run in Cloud and wacli modes on
Raspberry Pi OS Trixie and aarch64. Cloud application orchestration is described below. The
supported models are `Raspberry Pi 4 Model B` and `Raspberry Pi 3 Model A Plus`
(`SUPPORTED_MODELS` in `scripts/install/release_verify.py`). The updater and
bootstrap reject other models. It installs signed application releases directly
from `button-box/button-box` GitHub Releases. OS, package and dependency
upgrades are outside this application/controller OTA mechanism. No shipped
box may depend on a manual upgrade or repair step. Pi 3 A+ memory, timing and hardware
acceptance is still required before deployment.

This checkout pins two public keys. The release key is
`config/release-keys/button-box-release-2026.pub` (key ID 221CAE6604AC0F93).
The emergency key is `config/emergency-keys/button-box-emergency-2026.pub`
(key ID 73BD56BB8F9940FC). Bootstrap copies them to
`/etc/button-box-update/release-keys` and `/etc/button-box-update/emergency-keys`.
Without a pinned release key there, verification fails closed. Generate signing
keys offline; never transfer private keys to a box.

## Layout and bootstrap

Release tooling retains [signed controller proof](controller-descriptor.md)
with the application. Each descriptor binds the exact six staged modules. Its
signature is checked against current release trust, excluding emergency pins.
The literal `.py` proof files pass the frozen field path rules. Installing the
proof alone does not promote a controller. The local A/B publication API is
implemented; an OTA promotion trigger and Cloud permit are not added here.

- `/opt/messagebox-releases/<tag>-<full-commit>/`: root-owned application trees.
- `/opt/messagebox`: the application symlink, replaced atomically.
- `/opt/messagebox-deps/venv-nfc`: stable dependency environment.
- `/usr/lib/messagebox-update`: controller slot A, initially the frozen worker.
- `/usr/lib/messagebox-update-b`: controller slot B.
- `/usr/lib/messagebox-selector/selector.py`: the frozen independent selector.
- `/var/lib/button-box-update`: application journal, snapshots, policy, floors,
  quarantine, receipts and public status. Application rollback does not lower floors.
- `/var/cache/button-box-update`: unprivileged downloads, ETags and cooldown.
- `/run/messagebox-update`: peer-checked broker socket and readiness evidence.

Initial provisioning installs updater dependencies and the application recovery
gate before shipment. It converts the legacy application layout with the existing
journaled application worker. This application transaction is separate from
controller A/B selection.

An existing field box receives the signed bridge through its own old worker.
The supervisor queues bounded bootstrap after application commit and worker exit.
Bootstrap verifies the original six hashes, installs the selector and A's `GOOD`,
then installs five fixed command routes and arms the selector timer. It never
turns the original controller directory into a symlink. Ask mode still requires
its existing button approval; unattended delivery requires a working updater and
Auto mode. Do not rerun initial provisioning to update a shipped box.

The original seven unit files and NFC requirements remain frozen. The selector
and generated routing survive application pruning and later releases. New
controller code is written only to the inactive slot. See
[the A/B contract](controller-recovery.md) for trial, publication and boot rules.

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
`1.1.0`. Releases that add sounds require `min_updater_version: "1.1.0"`.
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
`sounds/ringtones`, or the existing `sounds/voices/{pirate,alien,dj,robot,french,charlie,tata}`
folders. WAV basenames must match `[a-z0-9][a-z0-9._-]{0,80}\.wav`.
`manifest.json` and `cues.json` are also permitted in those folders. Existing
README files and ringtone lamp files retain only their exact existing mappings.
Other folders, nested paths, traversal, links and nonregular files are rejected.
Each WAV must be nonempty RIFF/WAVE PCM, 48 kHz, 16-bit mono, with complete frame
data. Limits are 2 MiB per sound file, 128 MiB for all sound files (including
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

## Controller self-upgrade

The fixed field updater remains initial slot A. A signed bridge installs one
frozen selector and five permanent command routes. New controllers are tried in
slot B; later publications reuse only the inactive slot. Each slot keeps its own
accepted `GOOD` identity. Boot makes no persistent controller write and does not
use current release keys. See [the A/B contract](controller-recovery.md).

The journaled directory exchange and attended fixed-controller procedure are
removed. No shipped box may depend on an SSH or physical repair step. The frozen
application recovery path still writes metadata and needs separate full-disk
acceptance. Real Pi power-cut and physical Cloud/wacli acceptance remain open.
