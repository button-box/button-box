# Cloud boot recovery ordering repair

The bootstrap gates application services, targets and activators with
`Requires=` / `After=messagebox-update-recovery.service`. Normal service defaults
put recovery after `basic.target`. Path/timer defaults put activators before
`paths.target` / `timers.target`, which precede basic initialization. This is an
ordering cycle. Conditions, including install-inhibit and setup-mode conditions,
do not remove ordering edges while systemd constructs the transaction.

Setup enables the onboarding-complete and onboarding-voice watchers on every
installation, independently of the selected mode. Thus an otherwise ordinary
runtime **or setup** boot can contain the cycle. Verified affected field boots
consistently deleted recovery's job and left its dependents inactive. Replugging
is not a rescue procedure for those boxes. Prior successful runtime activation
by setup completion or an operator does not establish reboot safety.

## One persistent switch

`messagebox/cloud_boot_repair.py` writes exactly one drop-in:
`/etc/systemd/system/messagebox-update-recovery.service.d/80-boot-ordering.conf`.
It retains the original frozen service file and its command/selector routes:

```ini
[Unit]
DefaultDependencies=no
Requires=sysinit.target
After=local-fs.target sysinit.target
Before=basic.target shutdown.target
Conflicts=shutdown.target
RequiresMountsFor=/usr /var /opt /run
```

Recovery runs after local filesystems and sysinit, before basic initialization
and the application activators. Python, `/usr`, `/var`, `/opt`, `/run`, the
journal/snapshots, and sysinit's tmpfiles setup are available at that stage.
Existing `PrivateTmp` and other sandbox settings remain. Root systemctl calls
use PID 1's local control socket; boot recovery does not need network service
or the application broker. The `--boot` path restores files/enablement and reloads
systemd without starting application services. The selector's boot route also
runs the selected recovery worker in boot mode before admitting consumers.
Native verification of implicit mount/tmpfiles dependencies is still required.

The five application path/timer files remain byte-identical to the pre-hotfix
source. Their recovery gates, trigger semantics and shutdown defaults stay in
place. wacli performs no repair writes or systemd operations.

The helper validates root-owned paths, uses the existing update -> mode lock
order, and refuses install-inhibit, a nonterminal journal or a pointer that does
not match the committed application. Re-entry after completed rollback requires
the restored pointer to match the old release and known-good record. Initial attended bootstrap has a separate
explicit entry point before layout conversion. The helper writes and fsyncs a
complete temporary file, atomically renames it, then flushes the inode and parent
directories before daemon-reload. A completion marker is written only after a
successful reload. Retrying after rename or a failed directory flush settles
existing bytes again. Different existing contents require maintenance rather
than silent overwrite.

The drop-in is deliberately absent from the application manifest. Therefore it
is not snapshotted or removed by application rollback, and the frozen selector's
manifest admission does not reject it as a new controller-route payload. It does
not replace a controller or alter its command routes. Only the signed root
helper has this narrow, fixed write effect.

There is no partially repaired five-unit graph: the switch is absent or complete.
Once its directory flush has completed, future application interruption/rollback
retains the repair. **Before the first durable repair write, the original boot
bug still exists.** No new release can promise recovery from a power cut before
its code has reached and repaired an old box. Host durability tests distinguish
that original state from the repaired state; they do not pretend systemd can
boot the original cyclic graph. Keep running boxes powered until the repair and
live daemon-reload have been verified.

## Delivery to running boxes

The ordinary current release adds the helper to the runtime/setup/provisioning
and payload allowlists. `messagebox-update-bootstrap.service` executes it as an
`ExecStartPre` before controller initialization/completion. The existing
supervisor hook queues that service after the transaction worker exits. The
helper takes the real locks and checks commit again. Contention/failure prevents
bootstrap completion, so the existing hook retries. No timer, poller, privilege
grant or unprivileged systemd writer is added.

A normal application process runs as `messagebox` and cannot safely write root
systemd configuration. Calling repair from that process would need a new
privilege boundary. The existing signed post-commit root hook avoids that change.
It also avoids depending on legacy allowlists for the generated drop-in itself.

The full current payload still requires controller 1.3.0 because of the existing
buttonbox voice assets. For original-controller boxes running application
v0.3.12, the normal release builder now supports `--boot-hotfix-base v0.3.12`
with `--min-updater-version 1.1.0`. The profile exports the exact local base tag's
complete payload, preserves its program and sounds, and adds the repair module,
bootstrap service and existing supervisor queue hook. It retains signed candidate
controller 1.3.0 sources/proof but does not promote or replace the selected
controller. Clean-tag checks, both build/check passes, descriptor signing,
archive inspection, signature-first outer verification and release sequencing
remain the normal release process. No tag or production signature is created by
preparing this change.

The legacy profile's staged mode-migration preflight rejects any installed
application other than v0.3.12 **before mutation**. It must never replace a newer
voice application's program with the legacy application. Use distinct release
tags/sequences for the legacy capsule and the full release. While original
clients still depend on `/latest`, keep an eligible capsule discoverable or use
the existing exact-version request; do not assume a higher incompatible latest
release will automatically expose an older capsule to every discovery policy.

Delivery coverage is based on the selected worker and installed application,
not application version alone:

- v0.3.12 + original 1.1.0 worker, direct or behind the selector: the complete
  legacy-profile artifact is exercised through real minisign verification,
  extraction, transaction and the delivered root helper for every recorded
  v0.3.9-v0.3.12 frozen worker.
- Compatible selected 1.3.0 worker: the complete current payload is exercised
  through the same real outer-signature and transaction boundaries.
- A newer voice application selected onto an original 1.1.0 fallback worker is
  not covered by the legacy capsule: its base guard rejects that combination.
  It needs compatible controller restoration/delivery before the full release;
  see the specific worker limitation below.
- Unbootstrapped, unsupported-platform, offline/untrusted or storage-failing
  boxes cannot acquire a normal signed update until their specific prerequisite
  is restored. Setup mode still defers normal application installation.

### Newer application with frozen 1.1.0 fallback: not safely covered

This combination remains outside the two application artifacts' delivery
coverage. The frozen worker's `load_candidate()` requires the complete sound
floor, canonical manifest equality and checked mode migration inputs. A tiny
repair-only application cannot pass those contracts. Its `_expected_target()`
also rejects `sounds/voices/buttonbox/*`, present in every v0.3.13–v0.3.15
payload. The worker builds a fresh candidate release from listed files; it does
not overlay the installed application's program or sounds. Omitting that voice
pack would discard it. Merely widening the v0.3.12 capsule's version guard would
replace the newer application's program/sounds with v0.3.12, so it remains
forbidden. Lowering the full artifact's minimum version cannot change the
frozen path allowlist.

Safely supporting this case needs a separately proven controller-restoration
path before the full application release. Copying installed files or writing
root configuration from the staged migration **check** would introduce mutation
before the transaction's snapshot/journal and is not a safe shortcut. B76 keeps
that check read-only and does not bypass worker validation. Signed rejection
tests exercise both exact artifacts against application identities v0.3.13,
v0.3.14 and v0.3.15 with frozen 1.1.0 selected; program/voice bytes remain
unchanged. Those are admission tests with host systemd/health boundaries, not
execution tests of each newer application.

**Field detection:** no existing heartbeat field reports the selected worker.
`software_version` / `software_commit` describe the application.
`software_update` contains only `effective_mode`, `channel`, `available_version`,
`last_result` and `trust_sequence`; its capability `protocol=1` is also not a
worker version. These fields cannot distinguish a 1.1.0 fallback from 1.3.0.
The coordinator must treat that state as unknown from Cloud heartbeat alone.

With operator access on a selector-equipped box, validate the current-boot
selection and its module hashes using the installed selector, rather than
inferring selection from whichever slot has the newest files:

```sh
sudo python3 -I - <<'PYCODE'
import json
import runpy
from pathlib import Path
api = runpy.run_path('/usr/lib/messagebox-selector/selector.py')
root = Path('/')
with api['lock'](root, 'gate', shared=True):
    selected = api['selected'](root)
    print(json.dumps({'slot': selected['slot'],
                      'version': selected['identity']['version'],
                      'legacy': selected['identity']['legacy'],
                      'trial': selected['trial']}))
PYCODE
```

`version=1.1.0` with an application v0.3.13–v0.3.15 identifies the unsupported
combination. A missing/invalid selection is unknown, not evidence of 1.3.0.
On a pre-selector installation, inspect `systemctl show
messagebox-update-controller.service --property=ExecStart,MainPID` and the live
process command line to establish the worker directory; compare its six module
hashes with the frozen provenance in `tests/fixtures/field-updater/provenance.json`.
Inspect the selected directory's `release_verify.py` `UPDATER_VERSION` only after
establishing that route. No new telemetry is deployed by this hotfix; adding
selected-worker identity to Cloud reporting is a companion task.

Host tests substitute systemd and application health. Production signing,
publication, live policy/window/approval and physical acceptance remain separate.

## Known limit: already-dead boxes require operator recovery

The coordinator accepts this existing limitation for B76: a box already dead
after the failed boot cannot acquire the repair through normal OTA. Recover it
through operator access (for example, Tailscale where working access exists),
or retrieve it. This hotfix does not add an autonomous or family-button rescue.

`messagebox-wifi-reset.service` itself has the bootstrap's recovery
`Requires`/`After` gate. When that dependency job is deleted, its
`ExecStart=/usr/bin/python3 -m messagebox.onboarding.reset` never runs. Therefore
`wait_for_hold()` cannot observe the ten-second power-on hold, and
`perform_reset()` cannot write the setup marker or queue Comitup.

Even if a marker were changed externally, both always-enabled watchers still
participate in setup boot, and Comitup/onboarding services have recovery gates.
Setup completion would normally remove the marker, reload systemd and start
`messagebox.target`, but that root completion service must first be running.
This is not a reachable family-only rescue for the reported failed boot.

Discovery may independently retry recovery/controller after the boot jobs have
settled. That does not start the failed reset/runtime units, and the broker's
`installed()` gate still requires an active button PID and current-boot proof.
Thus a fully inactive runtime cannot normally install either repair artifact.
Do not bypass this gate or ask a family to repeat a gesture whose reader is dead.
An already-dead box needs an existing operator access path to restore runtime,
or a prepared repaired microSD/retrieval workflow. This change does not establish
that such access exists on any particular box.

## Test and bench gates

The portable graph contract preserves every Unit section, includes bootstrap and
setup enablement dependencies and checks separate runtime/setup transactions.
Its small OS model is explicitly limited. It is not a certificate of the complete
field graph. The native test requires `MSGBOX_BOOT_GRAPH_ROOT`, an exact local
export containing `/etc/systemd/system`, `/usr/lib/systemd/system`, the `/lib`
alias when used by unit links, and generated
unit links, including packaged Comitup and tmpfiles units. It preserves sandbox
properties and replaces only executable commands. It verifies the original cycle
and repaired graph in both modes, without substituting vendor units. Any
`Found ordering cycle` or `deleted to break ordering cycle` diagnostic rejects
the repaired graph, even if systemd exits zero after deleting a job. A separate
deliberately cyclic native fixture checks this boundary without a field export;
captured box-4 diagnostics test the parser on every host. Missing
export/native systemd is an explicit skip. Export no environment files, secrets,
controller state or device data.

On the Pi bench, verify effective early recovery dependencies, helper completion
and active runtime/heartbeat before removing power. Run repeated power cycles in
both modes, then interrupted application installs and rollback with the durable
repair already present. Separately exercise a cut during repair publication and
check old-or-complete bytes; an unpatched result retains the original known bug.
Native PID 1 boot scheduling, GPIO/audio/NFC/Wi-Fi/WhatsApp, and physical wacli
send/play/reply/card/quiet-hours/listened-notice acceptance remain unverified by
host tests. No merge or field deployment is implied.

Cloud assistant, Cloud service/dashboard and setup screens need no feature
change. A claimed-box missing-heartbeat alert remains a companion detection
feature, not an implemented rescue.
