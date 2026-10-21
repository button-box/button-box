# Independent controller recovery and promotion

The permanent root launcher is outside the active controller directory. It runs
before application recovery and from an independent monitor. Its bytes become
fixed at first bootstrap. This revision must pass acceptance before that first
installation. It adds an internal promotion API, with no OTA trigger or Cloud
permit. No manual action on a shipped box is part of this design.

## Paths and trust

Support directories are root-owned, mode 0755. Source and retained proof use
0644. Persistent state and locks use 0600. File operations use no-follow parent
and file descriptors. Unsafe owners, writable parents, hardlinks and special
files are rejected.

| Path | Purpose |
|---|---|
| `/usr/lib/messagebox-recovery/launcher.py` | Permanent root entry point. |
| `/usr/lib/messagebox-recovery/{release_verify,trust_update}.py` | Permanent verifier anchors. |
| `/usr/lib/messagebox-recovery/anchors.json` | Anchor hashes, mode 0600. |
| `/usr/lib/messagebox-recovery/recovery-keys/` | Retained public recovery proof keys. No new admission authority. |
| `/usr/lib/messagebox-update/` | One complete active six-module controller. |
| `/usr/lib/messagebox-update-stage-<transaction>/` | Candidate before exchange; retained old set after exchange. |
| `/var/lib/button-box-controller/state.json` | Atomic helper selection, accepted descriptor and signature digest pins, sequence and version floors. |
| `/var/lib/button-box-controller/journal.json` and `journal.json.copy` | Two integrity-checked 32 KiB records, mode 0600. |
| `/var/lib/button-box-controller/owner.lock` | Ownership from dispatch through activation and completion. |
| `/var/lib/button-box-controller/owner.json` | PID, process start token, boot ID and monotonic deadline. |
| `/var/lib/button-box-controller/promotion.lock` | Inner promotion guard. |
| `/var/lib/button-box-controller/recovery-reserve` | Two MiB of flushed reserve data before promotion. |
| `/var/lib/button-box-controller/controllers/legacy/` | Frozen original controller snapshot. |
| `/var/lib/button-box-controller/helpers/<digest>/{current,fallback}/` | Accepted helper bundles and retained proof. |
| `/run/messagebox-update/promotion-inhibit.json` | Public admission flag, root-owned 0644. |
| `/etc/systemd/system/messagebox-update-monitor.service` | Independent permanent recovery monitor. |

The first bootstrap accepts only the exact v0.3.9–v0.3.12 fixed controller hashes.
The frozen worker stages recovery source and installs the new monitor unit through
its existing `messagebox-*.service` path. Bootstrap checks the monitor's exact
bytes against the signed launcher. It flushes support and both helper slots
before publishing hooks. It enables the monitor before arming bootstrap.

Descriptor schema 2 signs all six controller modules, the three recovery sources
and both generated drop-ins. Literal data is parsed, never imported. Admission
verifies signatures under current release trust before publishing root-owned
descriptor and signature digest pins. Recovery checks those accepted pins and
every helper hash without temporary files or current release-key dependence.
Release-key revocation still rejects new admission. These separate recovery pins
authorize only the selected, previously accepted bytes. They cannot admit another
old descriptor. Retained public keys allow later offline proof inspection.

Each helper runs in a fresh Python interpreter with its own 45-second deadline. Missing
or corrupt code, exceptions, invalid returns, hangs and process exits select the
compatible fallback. The launcher stops and reaps the failed process group, then
reloads durable state. It executes captured bytes. Linux's parent-death signal
kills a helper if its launcher dies. The child does not inherit lock descriptors or Python thread state.
A fallback does not lower the admission floor. With no usable helper, an intact
legacy bootstrap can continue. A separately verified terminal active controller
can also continue. New promotion still requires valid proof and redundancy.

## Ownership and independent supervision

An outer owner lock stays held through activation. Inner lock order is trust,
application update, promotion, mode transition, then interaction. The broker can
take the inner locks while it starts. Another promotion cannot recover a live
owner's transaction. Locks do not wait except for the bounded boot gate.

The monitor has no controller or application dependency. It starts after boot
recovery, remains enabled, and promotion never stops it. It checks interrupted
ownership and runs recovery without a reboot. An expired owner can be killed only
while its lock is held and its boot ID and process start token still match.
The trusted bootstrap sets the lease before executing candidate helper code.
Successful callers disarm it.

The boot hook waits boundedly for busy recovery ownership; it does not admit app
startup while another process can still exchange controllers. The supervisor
hook uses `--supervisor`. It may defer so the existing supervisor can inspect and
stop an expired application worker before taking trust and updater locks.
Recovery carries the launcher parent PID into its helper so it cannot stop its
own supervisor pre-start service.

## Journal and restore

The journal value is schema 1, kind `controller`, at most 16 KiB. It records the
transaction, phase, stage path, old/new identities and six hashes, application
identity, descriptor sequence and signer, integration bytes, prior service states,
attempt, monotonic deadline and a durable `services_restored` flag. Rollout policy
remains null until permit support.

Each record wraps the value with a generation and SHA-256 over the generation
and value. Recovery selects the highest intact generation. Updates write and
flush the older record first, then the other copy. Each update flushes the file
and directory. Slots have allocated storage before promotion. Missing, truncated
or corrupt copies do not block recovery with one valid copy. Safe regular-file
slots with the wrong size are repaired in place. If both records are lost,
independent accepted descriptor and frozen-controller identities can preserve a
verified active set; promotion remains disabled without a valid journal.
With one valid record, recovery also repairs the damaged primary copy. This
keeps the existing OTA admission guard usable after recovery.

| Phase or physical state | Result |
|---|---|
| Bootstrap or prior terminal record | Keep that verified active set. Ignore incomplete new staging. |
| `intent`, `staged`, `exchange_intent` | Compare whole identities. Keep old if active; otherwise restore verified old. |
| `exchanged`, `activated`, `restore_intent` | Stop candidate consumers and restore verified old. Record `restored`. |
| `committed`, active intact | Keep new. A damaged spare blocks promotion, not startup. |
| `committed`, active damaged, spare intact | Restore the verified old set. |
| `restored`, `unsupported`, `aborted` | Keep verified old and restore service selection. |

Ordinary files use a flushed temporary file, atomic replacement and parent flush.
The complete stage is flushed before Linux `renameat2(RENAME_EXCHANGE)` exchanges
two real directories under one parent FD. The parent is then flushed. Unsupported
exchange (`ENOSYS`, `EINVAL`, `EOPNOTSUPP`, `EXDEV`) refuses promotion safely.
**There is no two-rename fallback.**

Recovery compares identities because exchange can finish before its next record.
It exchanges existing directory entries to restore; it needs no new inode or
file allocation. It releases the reserve before restore. Integration is unchanged
and is not rewritten. Record failure after restore cannot block a verified old
controller. The next attempt derives the result from identities and repairs the
record. Service restoration finishes before its durable flag is set and before
the runtime inhibit is removed. A kill between those operations is recoverable.
Terminal recovery is read-only once finalization is durable and no inhibit needs
removal. Bootstrap also allocates the reserve before arming hooks. Under disk or
inode pressure, the boot gate frees it for the frozen application's existing
small terminal metadata writes.
A persistent device error that prevents every exchange cannot be solved by
software alone.

## Activation

Activation has a 90-second deadline. It requires imports, normal manifest/path/hash
rejection tests, exact controller version and hashes, and a broker handshake.
Readiness retries within the deadline. Linux checks root peer credentials, live
systemd MainPID and a stable process identity around the handshake.

Offline discovery exercises normal poll, download, signature/hash checks and
extraction, including corrupt-archive rejection. Both self-tests run through `systemd-run`
with discovery's user, groups, private temporary directory, device restrictions,
strict filesystem protection, address-family limit, empty capability set and
umask. The fixed public self-test key is confined to that temporary fixture; it
has no release admission authority.

Activation checks effective unit executables, ordering and enablement, 900 or
1,800 seconds for discovery, 330 seconds for install, and current application
health. Health uses `--current` and local evidence in wacli mode. No Cloud call
is part of this transaction. Completion rechecks the application, current trust,
persistent floor, proof digest and active hashes. Failure restores old. A restored
descriptor is quarantined. A new signed sequence is needed to retry.

## Verification limits

Tests use real minisign, all four frozen workers and actual release-builder
packages. The promotion matrix models retained and lost unflushed writes.
A second matrix terminates recovery itself at each executed syscall boundary
with a real built payload. Tests inject ENOSPC, EIO and fsync errors, corrupt each
record, run signed helpers that raise/hang/exit, overlap two processes, and kill
promotion with SIGKILL at stop, exchange and activation boundaries while a separate
monitor runs. A Linux socket test checks delayed/stale sockets and peer PID.

Host tests substitute systemd and application health. Native Linux validation
executes the controller and discovery sandboxes. Passing these tests does not prove the
native service boot graph, process-group death under systemd, actual application
health, Pi ext4/SD flush behavior or physical power cuts. These need native Linux
and Pi acceptance before shipment. Live OTA, GPIO, NFC, audio, Wi-Fi, WhatsApp and
physical Cloud/wacli acceptance also remain separate checks.

Before any later publication, inspect all outgoing surfaces and audiences under
the standing privacy rules. Keep private review receipts out of public artifacts.
