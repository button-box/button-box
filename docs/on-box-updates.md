# On-box software updates

This updater is for Cloud boxes on Raspberry Pi 4 Model B, Raspberry Pi OS
Trixie and aarch64. It installs signed application releases directly from
`button-box/button-box` GitHub Releases. OS, package and dependency upgrades are
attended maintenance. Pi 3 support is deliberately rejected by the updater until
its hardware acceptance is complete.

The production signing key is not provisioned in this checkout. Without a pinned
minisign `.pub` key in `/etc/button-box-update/release-keys`, verification fails
closed. Generate signing keys offline; never transfer private keys to a box.

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

The controller and recovery launcher remain fixed for B72. Releases stage and
syntax-check controller copies in `updater/`; they do not overwrite the running
recovery implementation. Promoting changed fixed code, trust keys, recovery
units or dependencies requires a separate attended maintenance release with
recovery testing. B72 routine updates reject changed fixed units or NFC
requirements. `setup.sh` and legacy `apply` refuse converted installations.

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
`1.0.0`. Sequence ordering is global across channels; version strings are never
sorted to decide eligibility. Stable boxes reject canaries; canary boxes may
accept a stable release with a higher signed sequence. Recovery permits only
the journal's exact previous tree, regardless of the higher sequence floor.

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
The only capture-tool health command is `arecord -l`, which lists devices and
never opens a recording stream. No audio is recorded. Presence checks do not
prove physical switch, microphone or speaker operation.

The independent supervisor enforces timeout, worker death, lost readiness and
three daemon restarts within 60 seconds. It stops the worker before acquiring
its locks, then restores the exact previous pointer, integration files and
recorded unit states. Boot recovery runs before runtime and onboarding units;
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

`install_update {version}` always asks for physical confirmation, including on
an Auto box. Once the exact version is independently verified and the box is
idle, its sole GPIO owner voices:

> Software update ready. Hold the button for three seconds to install.

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
`last_result`. Update ACKs persist/replay through existing Cloud ledgers:
`downloading`, `waiting_for_button`, `waiting_for_window`, `installing`,
`installed`, `failed`, `expired`, `rolled_back`, `rollback_failed`, with exact
target/installed identity and bounded reason codes. Acceptance is not installation.

The unprivileged downloader uses the GitHub stable latest endpoint or bounded
paginated release lists for canary. It authenticates candidate metadata and
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
