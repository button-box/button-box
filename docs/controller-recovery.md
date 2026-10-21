# Independent controller recovery and promotion

The permanent root launcher stays outside the active controller directory. It
selects a signed helper and recovers the controller journal before the normal
application recovery or supervisor command runs. The launcher bytes from the
first bootstrap remain unchanged.

This step adds an internal promotion API. It adds no OTA trigger, Cloud permit,
or new main unit. The later bridge must call this API after it commits the
application. No manual action on a shipped box is part of this design.

## Trust and fixed paths

All support directories are root-owned, mode 0755. Python source, controller
metadata and retained proof use mode 0644. Persistent state and locks use mode
0600. Reads and writes use no-follow directory descriptors. Unsafe owners,
writable parents, hardlinks and special files are rejected.

| Path | Purpose |
|---|---|
| `/usr/lib/messagebox-recovery/launcher.py` | Permanent boot entry point. |
| `/usr/lib/messagebox-recovery/{release_verify,trust_update}.py` | Permanent trust anchors. |
| `/usr/lib/messagebox-recovery/anchors.json` | Hashes of the anchors, mode 0600. |
| `/usr/lib/messagebox-update/` | One complete active six-module controller set. |
| `/usr/lib/messagebox-update-stage-<transaction>/` | Staging before exchange; the previous complete set after exchange. |
| `/var/lib/button-box-controller/state.json` | Atomic helper selection and descriptor sequence, digest, controller-version and helper-version floors. |
| `/var/lib/button-box-controller/journal.json` | Separate bounded controller journal, mode 0600. |
| `/var/lib/button-box-controller/promotion.lock` | Exclusive promotion lock. |
| `/var/lib/button-box-controller/controllers/legacy/` | Frozen original controller snapshot. |
| `/var/lib/button-box-controller/helpers/<digest>/{current,fallback}/` | Signed helper bundles and offline proof. |
| `/run/messagebox-update/promotion-inhibit.json` | Root-owned public admission flag, mode 0644; discovery can read it. |

The first bootstrap still accepts only the exact v0.3.9–v0.3.12 fixed module
hash set. It installs and flushes support before it publishes the existing
recovery and supervisor drop-ins. These hooks invoke:

```text
/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py
```

Descriptor schema 2 signs the six controller modules, all three recovery source
files, and the two generated drop-ins. Literal descriptor data is parsed, never
imported. Each helper selection verifies retained proof with current release
pins. Emergency keys cannot sign executable helper code. The selected helper
executes captured verified bytes. It imports no application or controller code
to recover. It checks the permanent launcher's pinned hash before it uses its
file and lock functions.

Helper 1.1.0 implements this controller journal. Before the first exchange it
replaces both PR3-only helper slots, since helper 1.0.0 cannot recover this
journal. Later upgrades retain the previous compatible helper as fallback.
Missing or corrupt current helper bytes select that verified fallback. A
fallback does not lower the admission floor. Both missing helpers permit a safe
no-op only for an unchanged legacy controller and a bootstrap journal.

## Transaction and journal

The internal entry point is `messagebox.recovery_bootstrap.promote()`. It first
calls the immutable recovery launcher to finish any prior transaction. It then
checks the committed application, current trust and captured signed candidate helper
before it calls the helper. The helper checks proof again under the locks.

Lock order is trust, application update, promotion, mode transition, then
interaction. Missing volatile mode and interaction locks are created with their
fixed safe permissions. Locks do not wait. Promotion rejects a running install or
supervisor. It stops discovery and supervisor timers first, then controller
consumers, and checks pending jobs and main PIDs. During supervisor recovery,
the launcher's exact pre-start PID can remain; no supervisor main process may
remain. Recovery must not stop the service that owns this pre-start process.

The controller journal is JSON, schema 1, kind `controller`, at most 16 KiB.
It records:

- A transaction ID, phase, staging path and required helper protocol.
- Old and new versions, descriptor identities, all six file hashes and sizes,
  and the hash of each set's `controller.json` metadata where present.
- The committed application path, version, commit, transaction and journal hash.
- Descriptor hash, signing-key ID and sequence.
- Prior and new integration bytes, modes, service active and enablement states.
- Attempt count, monotonic activation deadline and rollout-policy revision
  (`null` until permit support is added).

| Durable phase | Recovery outcome |
|---|---|
| Prior bootstrap or prior terminal journal | Keep that complete active set; ignore incomplete new staging. |
| `intent`, `staged`, `exchange_intent` | Compare complete identities. Keep old if active; otherwise exchange back from known-good. |
| `exchanged`, `activated` | Stop candidate consumers, restore the old set and integration, record `restored`. |
| `restore_intent` | Inspect both complete identities; finish restore without a second swap if old is already active. |
| `committed` | Verify the new active set and retained old set; restore service selection. |
| `restored`, `unsupported`, `aborted` | Verify and keep the old active set; restore service selection. |

Each file write flushes file data, replaces its name, and flushes the directory.
New directories and their parent entries are flushed. Every journal transition
uses the same ordering. The staged controller is complete and flushed before
exchange. The helper calls Linux `renameat2(RENAME_EXCHANGE)` through `ctypes`
on two real directories under one parent FD, then flushes that parent.

If exchange is unsupported (`ENOSYS`, `EINVAL`, `EOPNOTSUPP` or `EXDEV`), the
helper refuses promotion, keeps the old active set, restores services, and
records `unsupported`. It never uses two ordinary renames as a fallback.

The helper compares full identities because an exchange can finish before its
next journal write. An uncommitted transaction always restores old. A durable
commit keeps new. The old complete set remains in the transaction's staging
path as known-good. This step does not remove retained sets.

## Activation and admission

Activation releases the file locks that the new broker needs. The durable
journal and public inhibit flag reserve the transaction. New broker commands,
workers and discovery respect that reservation. Root-only version/hash
handshake and runtime heartbeat/status requests remain available. Completion
reacquires all locks in order and rechecks the transaction, application, current
trust, persistent floor, exact proof digest and active hashes before commit.

Activation has a 90-second deadline. It requires controller imports and a
self-test, exact version and six-hash results, a live broker handshake,
restricted-user offline discovery, effective unit executables, recovery order
and enablement, frozen service time limits, and current application health.
The health probe uses `--current` so a committed application's old boot ID does
not invalidate fresh runtime evidence. Existing local health evidence applies
in wacli mode. No Cloud request is part of this helper.

Any failed check restores the old controller and integration. Previous service
enablement and active selection are restored, including an initially inactive
broker. Boot recovery queues start jobs without waiting while launcher locks
are held. Repeated boot recovery is idempotent. A restored descriptor digest
is quarantined; it cannot retry through the promotion API. A new signed
sequence is needed. The persistent floor survives controller rollback.

Controller updates do not change the application journal format or application
state. The helper binds both controller sets to the same committed application
identity. Actual application/controller compatibility on hardware is still an
acceptance requirement.

## Verification limits

The contract tests use real minisign proof, frozen field sources and actual
release-builder packages. All four frozen workers install the real package
before promotion. The crash model tests every instrumented write, file flush,
replace, directory creation and flush, exchange, activation and commit boundary.
It reconstructs separate boot roots with unflushed changes lost and with writes
retained. Boot roots omit `/opt`; recovery cannot depend on the application.
The same matrix includes failure and interrupted automatic restore.

Native Linux tests exercise real `renameat2`; macOS tests use a test-only atomic
swap adapter. Systemd, application health and the broker handshake are controlled
boundaries in the transaction suite. Controller imports and offline discovery
execute real candidate code. A Linux restricted-UID test checks public admission
with the private journal unreadable. These tests do not prove native systemd
boot, actual broker startup, Pi ext4/SD-card flush behavior, physical power-cut
recovery, live OTA, GPIO, NFC, audio, Wi-Fi or WhatsApp acceptance. Shipment
acceptance is not complete.

Before any later push or publication, inspect outgoing surfaces and audiences
under the standing privacy rules. Keep private review receipts out of public
artifacts.
