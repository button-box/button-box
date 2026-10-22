# Frozen selector and two controller slots

This replaces the unshipped journaled-exchange design. No device migration from
that design is supported. The OS, Python, systemd, and storage remain shared
requirements. Successful filesystem flushes must survive a power cut.

The guarantee is conditional: publication cannot remove the last complete,
accepted controller. It does not cover SD failure or incompatible shared state.
There is no manual repair step for a shipped box.

## Components and paths

| Path | Use |
|---|---|
| `/usr/lib/messagebox-selector/selector.py` | One frozen Python file. Standard library only. Never upgraded. |
| `/usr/lib/messagebox-update/` | Slot A: initial frozen field controller. |
| `/usr/lib/messagebox-update-b/` | Slot B. |
| Each slot's `GOOD` | Exact six files, sizes, SHA-256 hashes, commit generation, version, format 1 support, descriptor identity, record checksum. |
| Each slot's `controller_{descriptor,signature}.py` | Retained admission proof for offline audit; not a boot dependency. |
| Each slot's `TRIED` | Consumed descriptor digest, flushed before trial. |
| `/var/lib/button-box-controller/floor.json` | Integrity-checked atomic admission sequence, digest and version floor. Not a boot dependency. |
| `/run/messagebox-selector/selected.json` | Boot-bound exact slot and generation. Trial selection is volatile. |
| `/run/messagebox-selector/{operation,gate}` | Selection/publication ownership and consumer-start exclusion. |
| `/run/messagebox-selector/trial.json` | Public trial exclusion. All application installation paths reject it. |
| `/etc/systemd/system/messagebox-update-*.service.d/90-controller-slot.conf` | Five permanent routes. Future release payloads do not replace them. |

Persistent files belong to root. Code and slot records are 0644; the admission
floor is 0600. Directories are 0755. Reads reject writable parents, symlinks,
hardlinks, wrong owners, unsafe modes, oversized files and changed hashes.
New commit generations use the signed monotonic controller sequence.
`GOOD` wraps canonical JSON with its SHA-256. A bad record disables only its slot.
This checksum detects damage; it is not a substitute for release admission.

## Boot algorithm

1. Hold the boot gate. Acquire the volatile operation and consumer-start locks.
2. Wait for systemd to stop updater consumers. The gate blocks new consumers
   before they can resolve a slot.
3. Read each `GOOD` independently. Check integrity, ownership, modes and format 1.
4. Check every module hash. Ignore incomplete slots and consumed trials without `GOOD`.
5. Order valid slots by committed generation; use the slot name only to break ties.
6. Try the newest slot's application recovery command, with a 180-second limit.
7. Kill its process group after failure or timeout, then try the other valid slot.
8. Independently check the terminal application journal, exact application and
   known-good pointers, absence of install inhibit, and restored integration
   contents, modes, ownership, links, required removals, and saved service states.
   Capture those requirements before running either slot. At boot, check restored
   enablement and stopped services; application startup follows the recovery gate.
   At runtime, also require saved active services and auxiliary units to run.
   A zero exit alone fails
   this gate. Only then write the exact selection into `/run` and clear the trial flag.
9. Release the boot gate. All controller consumers use that selection.
10. Each consumer resolves once under the start lock, then executes that slot's command.
11. The independent timer checks broker readiness and its live PID and hash handshake.
    Each complete handshake has one two-second monotonic deadline, capped by the
    readiness deadline. Every receive uses only the remaining time.
12. On failure, stop consumers, prefer the other valid slot, then retry the current
    slot if needed. Recovery has one 380-second deadline. Start the broker only
    after recovery passes. Every timer run checks recovery before the handshake;
    a healthy broker cannot discharge pending restoration. The application
    supervisor retries `rollback_failed` after transient faults.
    If only one valid slot exists, recover and restart that slot. Runtime fallback
    uses normal application recovery, which restarts saved application services;
    only the boot gate uses `--boot`.

Service acceptance keeps the worker's defined exceptions. A saved-active managed
path or timer may remain failed only when its systemd `Result` is a known rate
limit. Required services and auxiliary units do not get this exception. The web
portal is optional when it was not active before the update or ComItUp was active.
Auxiliary names use all worker-supported unit types, including sockets and escaped
names; unsafe names remain rejected. The stdlib selector contains frozen copies
of `UNIT_NAME` and `LIMIT_RESULTS`; tests pin them to the current and field workers.

Selection makes no persistent write, exchange, repair, or reserve allocation.
No current release key or global controller identity file gates boot. If neither
slot is valid, the selector executes neither. Frozen application recovery can
still allocate and rewrite its own metadata. This design does **not** make full
application startup independent of disk or inode space.

## Publication and trial

Publication runs in a bounded root systemd oneshot, with exclusive operation
ownership. It takes the existing trust and application-update locks in that
order. It requires a committed application, complete effective routing, an armed
selector timer, current signatures, compatible state, and a monotonic admission
floor. Admission also requires candidate bytes plus 32 MiB free and at least
128 free inodes before invalidating the inactive slot. These checks are not a
controller-selection reserve. Equal sequence is allowed only for the accepted digest. Version downgrade
is rejected. Damage to the floor stops admission, not boot.

Before reuse, publication verifies and flushes the working slot's module files,
`GOOD`, and parent directories. This also completes a prior interrupted directory
flush; visible matching bytes alone do not prove durability.
The working slot and its `GOOD` stay unchanged. Publication removes the inactive
slot's `GOOD`, flushes the directory, writes and flushes candidate files and
retained proof, and verifies them. Both controllers' real parsers must accept the
current application journal and shared records. Format migration is not supported.
It persists the floor and writes and flushes `TRIED` before starting the trial.
A consumed digest is never retried automatically.

The trial selects the candidate only in `/run`. It stops consumers first. It
creates fresh boot-, application-, and controller-bound evidence context, removes
old readiness evidence, runs normal controller validation and offline discovery,
and runs the candidate's actual recovery CLI against a staged interrupted
application journal and nonempty snapshot with active services, a file to restore and an extra
drop-in to remove. The selector captures required restoration before the command
and checks it after the command, as well as the pointer, journal, and inhibit.
Recovery, install, and supervisor routes retain the existing rollback snapshot
until the selector checks restoration. A failed attempt cannot erase the proof
needed by another slot or a later boot. After the checks pass, the selector
flushes every restored regular file and its parent directories, including the
directories of required removals and restored links. This barrier also covers
retained field workers. A failed restoration flush keeps the state record.
Only then does the selector remove the snapshot's state record and flush its
directory before selection. If that flush fails, it restores the visible proof
and leaves recovery pending. Retry repeats the barrier and final directory
flush, even if the record is already absent. A
completed rollback therefore cannot undo a later setup-mode request. Remaining
backup data is pruned at the next application commit. A terminal but incomplete rollback is retried.
The staged test runs non-boot runtime recovery and checks both files and observed
service activity and enablement. It substitutes only systemctl; it cannot stop
live services. It then starts the broker. Readiness retries within the trial deadline. Root Unix
peer credentials, stable systemd PID, exact version and hashes must match.
Fresh button and local poller evidence from the selected application processes
and the local health probe must pass. `local.json` is separate from successful
Cloud communication in `heartbeat.json`. Only controller trials use the local
probe option; ordinary Cloud application probation still requires a successful
Cloud heartbeat. A candidate success string alone
cannot authorize `GOOD`.

Recovery and probe output is discarded. Required trial and systemctl results
are read in chunks, with a 64 KiB limit and an independent deadline. Exceeding
either limit kills the command's process group. Controller and discovery trials
use fixed transient unit names in `system.slice`, with a 20-second runtime limit,
five-second stop limit, and control-group kill. Cleanup stops the unit on success
and failure. Before fallback, verify that no start job remains and the cgroup is
empty or gone. Recovery repeats this barrier after a killed publisher. A missing
cgroup alone cannot discharge a pending start job.

Only the frozen selector publishes `GOOD`, after rechecking module bytes. It
flushes the record and directory before returning success. A cut before this
leaves the old slot usable. A cut during publication selects either the old slot
or a complete checked candidate. A failed or interrupted trial has no `GOOD`.
Python launches ignore stale slot bytecode through an unwritten volatile cache
prefix. The timer selects the unchanged accepted slot; it never starts a new trial.

The bounded check service has no `RemainAfterExit`, has explicit start/stop limits,
and disables permanent start-rate limiting. Boot has a 400-second unit limit;
the check has a 500-second limit, including readiness, two bounded recovery
attempts, process stops, and broker restart. Its timer remains enabled through
publication and trial. A busy operation causes a check to defer, not to compete.
Systemd terminates a hung publication's entire cgroup. A later timer check can
then select the accepted controller without reboot. The original application supervisor timer remains enabled. Its selector route
takes only the consumer-start lock, never admission or publication ownership.
It retains the existing bounded worker-deadline and rollback behavior.

## First installation and fixed integration

The signed bridge keeps minimum updater 1.1.0. The frozen workers accept both
new Python files, the new services and timer, and the supervisor `messagebox.conf`
queue hook. That hook queues bounded bootstrap after the supervisor exits.
Bootstrap waits for a committed application and the real worker locks.

Bootstrap treats the existing real directory as slot A. It verifies the six
frozen hashes, installs and flushes the selector, and publishes A's record before
installing any route. Converted entry points still select A. A trial is permitted
only after all five routes exist and the independent timer is active. The
separate generated route filename survives later application manifest updates.
The original seven fixed unit files are not changed by the payload.
On retries, bootstrap flushes verified existing selector bytes, slot records and
modules, routes, and their parent directories before enabling the timer. The
permanent install route wraps even a retained legacy worker's transaction call:
it rejects selector-unit changes and controller route overrides before mutation.
It also excludes these fixed files from application writes and rollback snapshots.
This prevents legacy rollback from unlinking the permanent service contract.

There are no helpers, verifier anchors, recovery-key copies, replicated controller
journals, owner leases, reserve files, directory exchanges or monitor loops.
There is no two-rename fallback because there is no exchange transaction.

## Acceptance limits

Tests use real minisign, frozen v0.3.9–v0.3.12 workers and real builder output.
They inject cuts at publication and bootstrap writes, missing/corrupt records,
I/O failures, bounded child failure, and concurrent ownership. Host tests replace
systemd and hardware explicitly. A durability model discards unflushed file data
and directory entries. It includes kill, retry, then power loss and a failed
directory flush after rename. Host recovery executes the selected worker's real
filesystem recovery. Readiness uses the real button and wacli producer paths,
AF_UNIX IPC, and broker dispatch with Cloud construction blocked. The kernel
peer identity and service PID remain synthetic. The staged recovery trial also
substitutes service state; it does not validate native service recovery.
The complete bootstrap shell test uses real current source, globs, file installs,
key and policy code, and layout conversion in a redirected root. Pi identity,
root ownership, accounts, and systemd remain host substitutes. A wacli first
install is rejected before controller mutation.
Tests do not prove native
service ordering, cgroup termination, timer recovery after repeated errors,
full ext4 blocks/inodes, or Pi SD flush behavior.

Before first installation, native Linux/systemd and real Pi power-cut acceptance
remain required. Physical Cloud and wacli send/play/reply, NFC, quiet hours,
listened notices, audio, GPIO, Wi-Fi and WhatsApp checks remain separate.
No Cloud request is part of local selection or publication.

On an isolated Linux system with systemd and cgroup v2, run the native forcing
case as root with `MSGBOX_NATIVE_SYSTEMD_TESTS=1` and
`python3 -m unittest discover -s tests -p test_controller_selector_systemd.py -v`.
It uses real `systemd-run`, exceeds the output limit, and checks that the
transient cgroup and a descendant that ignores SIGTERM stop before fallback.
This case skips on macOS; host checks do not establish that result.

BOX-10 bench list: verify native systemd ordering and sandbox restrictions;
verify cgroup termination of hung and killed trials; run the real timer through
repeated alternate-slot failures without reboot; repeat recovery with full blocks
and full inodes; cut Pi power during file and directory flushes, including retry
after a killed publisher, restored drop-in removal, and recovery-proof retirement.
Confirm application recovery and both transports without
SSH repair. SD-card flush guarantees and load timing remain unverified on this host.
