# Independent controller recovery: first bootstrap

This step installs recovery support. It does not exchange, promote, or restore a
controller directory. The six fixed field modules remain unchanged. The active
updater still reports version 1.1.0. The signed candidate describes controller
protocol 1.3.0 and keeps `self_upgrade=False`.

## Files

| Area | Files |
|---|---|
| New runtime support | `messagebox/recovery_launcher.py`, `messagebox/recovery_helper.py`, `messagebox/recovery_bootstrap.py` |
| Verification and release | `scripts/install/release_verify.py`, `scripts/release/build.py`, `scripts/release/release.py`, `scripts/release/payload.txt` |
| Installation allowlists | `scripts/setup.sh`, `scripts/provision.sh` |
| Tests | `tests/test_recovery_support.py` (new), `tests/test_field_release_payload.py`, `tests/test_release_tooling.py`, `tests/test_release_manifest.py` |
| Documentation | `docs/controller-recovery.md` (new), `docs/controller-descriptor.md`, `docs/on-box-updates.md`, `docs/releases.md` |

## Payload and trust

The release contains these ordinary application paths:

- `messagebox/recovery_bootstrap.py`: first bootstrap from a committed release.
- `messagebox/recovery_launcher.py`: source of the permanent root entry point.
- `messagebox/recovery_helper.py`: versioned, self-contained helper, version 1.0.0.

The frozen workers accept these `.py` paths. The release builder includes them
in its explicit payload. Descriptor schema 2 signs their exact paths, sizes,
modes and SHA-256 values in `recovery.files`. It also signs the six controller
modules. `unit_changes` explicitly signs the two generated service drop-ins.
Bootstrap compares their paths, sizes, modes and hashes before it installs
support. `recovery.protocol` is 1. Schema 1 remains readable for old retained
proof; it cannot initialize recovery support. The builder refuses a missing
recovery file. Release checks reject missing or changed recovery bytes before
publication.

The old worker's authenticated application installation is the initial root
trust boundary. Bootstrap requires its application journal to be committed,
the journal identity to match the application pointer and release metadata,
and the known-good pointer to name that release. It recovers pending trust
replacement under the trust lock before it verifies the controller proof.

The permanent verifier and trust code are copies from that authenticated
release. Bootstrap checks their descriptor hashes before it installs them.
Their hashes are retained in a root-owned anchor record. Later applications
cannot replace these permanent files through this bootstrap API.

Every helper selection checks the retained descriptor signature against current
release pins. Emergency pins cannot sign helper code. The launcher reads and
hashes all signed recovery files through no-follow descriptors. It executes the
captured helper bytes that passed verification. It never imports helper code
before verification or imports the active controller to perform verification.
The helper has no application or neighboring-module imports.

Separate controller key roles, emergency-pin rotation, approval thresholds and
persistent emergency stop remain later work. This step does not complete
promotion authorization.

## Fixed paths and permissions

All paths below are owned by root. Directories use mode 0755. Executable Python
source and retained proof use mode 0644. State, journal and lock files use mode
0600. No executable bit is needed: systemd invokes Python directly.

| Path | Purpose |
|---|---|
| `/usr/lib/messagebox-recovery/launcher.py` | Permanent recovery entry point; independent of the application pointer and controller. |
| `/usr/lib/messagebox-recovery/release_verify.py` | Permanent descriptor-verification anchor. |
| `/usr/lib/messagebox-recovery/trust_update.py` | Permanent trust-recovery anchor. |
| `/usr/lib/messagebox-recovery/anchors.json` | Anchor hashes, mode 0600. |
| `/var/lib/button-box-controller/state.json` | Atomic helper selection and persistent descriptor acceptance record. |
| `/var/lib/button-box-controller/journal.json` | Separate recovery journal; schema 1, kind `bootstrap`, phase `prepared` or `armed`. |
| `/var/lib/button-box-controller/promotion.lock` | Separate promotion guard. |
| `/var/lib/button-box-controller/controllers/legacy/` | Complete known-good copy of the original six field modules. |
| `/var/lib/button-box-controller/helpers/<descriptor-sha256>/current/` | Complete signed bundle and proof for the current helper. |
| `/var/lib/button-box-controller/helpers/<descriptor-sha256>/fallback/` | Independent complete copy for the fallback helper. |

Bootstrap accepts only the exact six-module hash set shared by frozen
v0.3.9–v0.3.12. It rejects an unknown fixed controller. It does not infer the
controller identity from the application version.

The lock order is trust, application update, then promotion:

1. `/var/lib/button-box-update/trust.lock`.
2. `/run/lock/messagebox-bounded-update.lock`.
3. `/var/lib/button-box-controller/promotion.lock`.

The existing mode and interaction locks are not needed for this step because it
does not mutate the active controller. Later promotion must take them after
these locks. Lock acquisition does not wait. Boot recovery fails on contention;
the existing recovery unit's restart policy retries it. A supervisor failure
waits for the next timer tick. Neither path bypasses independent recovery.
Root-owned sticky `/run/lock` is supported. Other writable parents, links,
hardlinks, special files and unsafe ownership are rejected.

## Bootstrap and boot ordering

The bootstrap API is intended for the later OTA bridge trigger. This PR does
not install that trigger or require a manual field action. The old attended
bootstrap must not be run again on converted boxes.

Bootstrap performs these steps while it holds the locks:

1. Check the committed application and recover current trust.
2. Verify and capture the complete signed candidate.
3. Copy and flush the unchanged, recognized old controller into `controllers/legacy`.
4. Write and flush independent current and fallback helper bundles and proof.
5. Install the permanent launcher, verifier, trust code and anchor record.
6. Atomically publish `state.json`, including both helper identities and the
   accepted descriptor sequence, digest, controller-version floor and helper-version floor.
7. Write the `prepared` bootstrap journal and verify both helper copies again.
8. Install the recovery and supervisor `messagebox.conf` drop-ins atomically.
9. Reload systemd. Verify effective commands, failure handling and recovery order.
10. Write the `armed` journal durably.

Each file write flushes file data, replaces the destination atomically, then
flushes its directory. New directory entries and directories are flushed too.
No service hook is published before the support files and selection are durable.
An incomplete bootstrap leaves the old controller unchanged. The same accepted
descriptor can resume it. Existing conflicting drop-ins are rejected.

The generated drop-ins add this command as `ExecStartPre` to the existing
recovery and supervisor services:

```text
/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py
```

The original service commands and enabled recovery service remain in place.
Effective-unit checks require the exact Python executable and argument list,
`ignore_errors=no`, the original recovery/supervisor commands, and recovery
before the application target, ComItUp and controller. A mismatch leaves the
journal `prepared`. This is a host-command contract; native systemd acceptance
remains unverified.

The permanent launcher selects a valid current helper, then a valid fallback.
A missing or corrupt current helper cannot run. When neither helper is valid,
a safe no-op is allowed only for a bootstrap journal and only if both the active
initial controller and its known-good copy still match every recorded hash.
Unknown transaction phases, missing selection with installed hooks, unsafe
state, and changed initial controller files fail closed.

The launcher passes bounded state to the verified helper through protocol 1.
A later signed helper can interpret promotion journals without replacement of
the permanent launcher. The helper in this PR only accepts bootstrap journals.
It does not implement PR 4's exchange or automatic restore.

## Persistent acceptance rules

The sequence floor and helper identities use one atomic `state.json` record.
A crash cannot publish a newer floor without its corresponding helper slots.
An older descriptor sequence is rejected. An equal sequence is accepted only
for the exact accepted descriptor digest. A higher sequence cannot lower the
controller or helper version floor. These records are outside application
rollback state and are never restored from an application snapshot.

The first-bootstrap API only resumes the same accepted descriptor. A higher,
otherwise valid descriptor needs the later helper-upgrade transaction. A pinned
fallback is authorized only for recovery; it does not bypass new admission.

## Verification and limits

`tests/test_recovery_support.py` covers real minisign proof, corrupt/missing
helpers, safe legacy no-op, captured-byte execution, signed replay attempts,
controller and helper version floors, pending trust recovery, unsafe paths,
conflicting units, lock contention, and Cloud/wacli execution with socket access
blocked. The existing runtime mode checks still apply.

The crash model records every bootstrap write, file flush, rename, directory
creation and directory flush, plus reload and effective-unit boundaries. It
reconstructs independent boot roots with unflushed changes discarded. It also
checks each prefix with writes retained. Boot roots omit `/opt`, so recovery
cannot use the application pointer. An actual interrupted invocation is retried.
This models flush ordering; it is not a Pi power-cut or filesystem test.

`tests/test_field_release_payload.py` packages the actual checkout with release
builder primitives. Every frozen worker installs the unchanged archive,
retains proof, and then accepts recovery bootstrap. No commit or tag is created
for that test; a temporary index describes new, uncommitted payload files.
The top-level clean-tag, production-signing and reproducible-build gates remain
separate release checks.

Checks on 21 October 2026:

| Check | Result |
|---|---|
| `MSGBOX_TEST_TRANSPORT=cloud make check` | PASS: syntax, lint, 1,166 Python tests and 54 JavaScript tests. |
| `MSGBOX_TEST_TRANSPORT=wacli make check` | PASS: syntax, lint, 1,166 Python tests and 54 JavaScript tests. |
| Recovery suite from a shell with umask `002` | PASS: 17 tests, including real signatures. |
| Bootstrap crash model | PASS: 401 boundaries; 53 distinct durable boot images. |
| Release-tool suite | PASS: 19 tests, including missing/changed recovery payload checks. |
| Manifest suite | PASS: 5 tests; 619 installed mappings and 633 payload sources. |
| Independent adversarial source review | No remaining blocker within PR 3 scope. |

Native systemd boot, storage flush behavior, real power cuts, live OTA delivery,
controller exchange, two successive upgrades, automatic controller restore,
audio, GPIO, NFC, Wi-Fi and physical WhatsApp acceptance remain unverified.
New-box shipment acceptance is not complete. No device is contacted by these
tests. No Cloud call is added to the wacli path.

Before any later push or publication, inspect every outgoing surface and its
actual audience under the standing public-output privacy rules. Keep private
coordination and review receipts out of public artifacts.
