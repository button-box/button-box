# Field OTA bridge and Cloud admission

Release A uses `min_updater_version: 1.1.0` and must fit the frozen 128 MiB
compressed, 256 MiB expanded and 128 MiB sound limits. The four field workers
install it through their normal signed application transaction. Fixed controller
files stay in slot A. The supervisor drop-in queues bounded bootstrap after the
worker exits. Bootstrap requires matching committed application, pointer and
known-good records and takes the worker locks. It flushes the permanent selector,
legacy A's GOOD, five routes and timer, then queues the bounded publication
service. Publication obtains a separate `promote_controller` permit before
changing the inactive slot. The normal A/B trial owns GOOD publication. Failed
trials retain A; the independent timer restores service without Cloud access.

## Permit flow

The candidate mapper runs from frozen `load_candidate` before release creation,
snapshot, stop or integration mutation. Its guard runs only at that boundary;
builds and normal mapping never contact Cloud. Denial raises the running worker's
own `UpdateError('not_idle')`. Automatic attempts retain the bundle and report
`waiting_for_window`. Ask mode still needs the existing physical approval.

The guard selects transport from the installed root-owned environment first.
wacli uses its existing signed local feed and physical/local policy, without
constructing a Cloud client. Controller publication also remains local in wacli.
Cloud requires synchronized root time and an exact signed permit binding:

- Box ID, device ID and current account scope, captured locally before the call.
- Purpose, channel, positive safe-integer release sequence and fresh root nonce.
- Manifest, archive, descriptor, source-set and target-set SHA-256 identities.
- Integer policy revision and Unix times in the heartbeat range, TTL <=300 seconds.

Root copies the standalone stdlib client to
`/run/messagebox-updater-admission/client.py` and writes `request.json`. Inputs
are root-owned 0644 in a root-owned 0755 directory. Only `out/` belongs to
`messagebox` (0700). Its single response must be a regular, single-linked 0600
file, at most 8192 bytes. A fixed-argv `systemd-run` starts the named admission
unit as `messagebox`, outside the worker's AF_UNIX restriction. Start/runtime
limits are 20 seconds, stop limit three seconds, with control-group kill. Root
waits at most 25 seconds and stops the unit on every outcome. The unit permits
AF_UNIX/INET/INET6 and writes only the output directory.

The client uses the existing private credential and HTTPS heartbeat endpoint.
It sends the installed application's software identity and the root's permit
request. It adds updater telemetry only after the selector is installed and
identified. It omits capabilities/settings and executes no returned commands.
Redirects are rejected. It neither registers a device nor changes account state.

The request has the protocol-1 Cloud fields exactly: `protocol`, `purpose`,
`nonce`, `channel`, `release_sequence`, `release_manifest_sha256`,
`release_archive_sha256`, `controller_descriptor_sha256`,
`target_controller_set_sha256`; maximum 1024 bytes. The signed payload adds
`schema`, `domain`, `action`, `key_id`, `box_id`, `device_id`, `account_scope`,
`source_controller_set_sha256`, `policy_revision`, `issued_at`, `expires_at`,
and omits `protocol`. Payload <=4096 bytes; signature <=512 ASCII bytes;
envelope <=8192. The envelope has exactly `schema`, `key_id`, `payload_b64`,
`signature_minisign`.

Root rejects duplicate/unknown fields, noncanonical JSON, wrong types or any
binding mismatch. Canonical bytes are sorted-key compact ASCII JSON plus LF.
Require `issued_at <= now < expires_at`, TTL <=300 seconds, and synchronized
local time. Unsigned server time has no authority. The complete four-line
signature must use legacy minisign `Ed` and fixed trusted comment
`buttonbox-updater-permit-v1`. Root runs the existing `minisign -V`, without `-H`.
Controller descriptor verification keeps its existing prehashed `-H` requirement.

`messagebox/updater_rollout_keys.py` is bounded literal data, parsed, never
imported: `{'schema': 1, 'keys': [<minisign base64 public-key line>, ...]}`.
At most eight distinct public pins support current/next overlap. Responses cannot
supply keys. The checked-in list is empty: Cloud admission denies and the builder
refuses publication until authorized public pins are supplied. No private key
ships. Production custody, online signing authorization and emergency rollout-pin
revocation remain release decisions; this PR does not deploy a Cloud signer.

Before installation, root atomically saves and flushes
`/var/lib/button-box-update/admission.json` (0600). It retains the maximum policy
revision and each purpose's accepted nonce, operation and exact request. This
file is outside application snapshots. Cached responses cannot match a new
nonce or authorize another operation. Promotion requires a fresh permit bound
to the committed manifest identity, retained archive identity and exact staged
descriptor/modules. Expiry is checked again at admission's end. Once local
mutation starts, recovery needs no permit or network. Rollback preserves the
receipt and policy floor.

## A/B heartbeat contract

The optional Cloud-only `updater` section replaces helper and controller-journal
phases. Maximum: 4096 bytes before extension removal, four container levels and
48 properties, including array elements. Unknown fields consume those budgets
and are stripped; an invalid known field drops only this section. Existing
`software_update` remains unchanged.

| Field | Contract |
|---|---|
| `schema`, `reporter` | Integer 1; `runtime` or `legacy_admission` |
| `controller_version` | Actual executing controller's `N.N.N`, <=32 characters |
| `selector_version` | `1.0.0` only for the known frozen selector bytes |
| `slots` | Object keyed by `a` and `b`; each has `slot_id`, `generation`, `state`, `controller_version`, `set_sha256` |
| Slot identity | Verified GOOD, or a retained descriptor and all checked modules for TRIED; otherwise `none`, generation zero and null version/hash |
| `active_slot`, `active_generation` | Current-boot selected slot and generation; must match its complete identity |
| `last_trial` | Null, or `{slot_id,generation,result,reason_code}`; results: `promoted`, `failed`, `rolled_back`, `none`; bounded reason or null |
| `discovery_timeout_seconds` | Observed effective systemd timeout, integer 1–3600 |
| `application` | Installed version (1–64 characters), commit (7–40 lowercase hex), state: `unknown`, `committed`, `rolled_back`, `rollback_failed` |
| `capabilities` | Exact booleans `permit_v1`, `controller_self_upgrade_v1`; report the installed adapter, including a retained legacy controller; they do not grant admission |

The active slot is GOOD or TRIED. Active TRIED requires a lower-generation GOOD
alternate. Trial `none` matches the active TRIED identity; promoted needs GOOD
for the same generation; failed or rolled-back needs an inactive non-GOOD slot
for that generation. Historical results can survive slot reuse or corruption.
The producer rechecks selection and both identified slots before emission.
It does not infer a trial result from a TRIED marker. Missing evidence is null.

The set hash covers sorted-key compact ASCII JSON plus LF, containing
`{path,size,sha256}` in this order: `bounded_update.py`, `health-probe.py`,
`release_verify.py`, `trust_update.py`, `update_controller.py`, `update_discovery.py`.
`path` is the basename. Source and target use the same definition, distinct from
descriptor identity. Runtime reports come from the selected root broker over
existing IPC. When the frozen A broker is retained, the existing bootstrap hook
writes root-owned `updater-status.json` under the update state directory. It
measures the broker's MainPID and fixed script path in procfs, rechecks MainPID,
and binds the cached report to the boot and monotonic time. The Cloud runtime
accepts it for at most 120 seconds. This also reports failed trials after fallback
to A. No new timer is added; wacli neither produces nor consumes this report.
Before A/B exists, the first client omits updater telemetry, as the schema-1
receiver requires. The Cloud admission implementation must bind this first
permit to an independently registered expected legacy source-set hash. Root
compares the signed hash with the actual files. It must not infer that hash
from application version. A policy that requires updater telemetry for this
first call will hold every field box; this remains a Cloud PR 5c integration
gate. The request's protocol field declares support for this exchange.

`tests/fixtures/updater-permit-v1.json` provides synthetic shared signature
vectors. The Cloud owner must implement this A/B schema before issuing permits.
An omitted permit means hold. No Cloud receiver, database, signer or rollout
was deployed by this Pi change.

## Durability and Release B

The discarded-write model found that the field snapshot code does not flush the
new snapshot entry in its parent. The admitted mapper adds a process-local
`create_backup` barrier. It also flushes post-chmod file metadata and newly created
integration directories. These wrappers leave fixed on-disk worker bytes
unchanged and run in wacli too. Before prepared, the old journal remains; after
prepared, the old boot worker has a durable complete rollback snapshot.

The staged 1.3.0 controller includes `voices/buttonbox` path support, a 160 MiB
sound allowance and a 1500-second monotonic transfer budget. A installs the
accepted discovery drop-in for an effective 1800-second service timeout. A ships
no new voice assets that violate the old worker limits.

Rebase `feat/voice-onboarding` onto the bridge before B. Build B with
`--min-updater-version 1.3.0`; the builder also emits
`required_updater_capabilities: ['controller_self_upgrade_v1']`. The mapper requires a
selected non-trial, nonlegacy GOOD controller >=1.3.0 before mutation. Version
1.2.0 or a staged 1.3.0 does not pass. Cloud must observe committed A, expected
active hash, passed trial, healthy runtime and effective 1800-second discovery.
Keep held and Ask boxes held. No voice branch or release was published here.

## Bench gates

Host tests execute frozen workers, real builder payloads, real minisign, the
copied client, rollback and selection. PID 1/systemd, HTTPS replies, identity and
ownership, readiness and hardware remain modeled. The storage model discards
unflushed data and entries; it does not measure ext4 or SD behavior.

- Confirm BOX-10 or BOX-11 is unshipped and available; never use a family box.
- Test native ordering, effective routes, sandbox permissions, client network
  access outside the worker restriction, cgroup cleanup and repeated timer retry.
- Cut Pi power during receipt, snapshot, file/directory flush, application switch
  and rollback, bootstrap retry, publication, trial and GOOD flush. Recover without
  SSH. Test full blocks/inodes and slow networks within the service deadlines.
- Use the real Cloud signer/receiver with both public-key generations: hold, stop,
  rotation/revocation, policy races and no new admission after expiry.
- Repeat two controller upgrades and one failed trial, then OTA B. Complete both
  transports' send/play/reply, quiet hours, listened notices, NFC, audio, GPIO and Wi-Fi.

Production rollout pins and custody are unresolved. These bench gates remain
unverified; host tests do not prove shipment readiness or Pi durability.
