# Signed updater bridge

## Release artifacts and order

Build one application capsule with the normal release builder:
`--updater-bridge-base v0.3.12 --min-updater-version 1.1.0`. It accepts the
recorded v0.3.9, v0.3.10 and v0.3.12 applications (with or without a historical
`v` prefix), stepping the older two forward to the complete v0.3.12 payload.
It rejects newer/unknown applications before installed-file mutation. A later
corrective bridge can accept an installed capsule whose hash-bound manifest
records the same bridge base. The original app is restored if capsule health
fails; after a healthy capsule commit it is the controller trial's current app.
The existing `--boot-hotfix-base v0.3.12` remains repair-only.

Proposed stable tags are `v0.3.17` (bridge) and `v0.3.18` (full application);
these are examples to reserve, not created tags. Allocate bridge application
sequence S above **all** signed history. The capsule carries controller 1.3.0, its detached signed descriptor,
and a reserved controller sequence C; use a descriptor minimum application
version compatible with the capsule tag. The source is the reviewed bridge
commit; program/sounds come from the exact local base tag. The manifest records
that base. Each artifact has the usual four assets and both signature layers.

Then reissue the full v0.3.16 application as a fresh higher tag (for example
v0.3.18), minimum updater 1.3.0 and sequence F > S. Both the
box automatic-operation logic and owner offer display compare version names;
using v0.3.16 below the example bridge tags would suppress the normal offer.
Do **not** re-sign/replace an existing v0.3.16 release. Allocate a valid controller descriptor
sequence for that artifact; never reset either floor or move a tag. Application
sequence, rather than tag comparison, determines admission. No release sequence
in this note is an allocation. Signing and shipping remain separate operations.

1. Existing discovery downloads the exact eligible signed capsule. Ask/Install/
   Not now, physical confirmation, automatic quiet window, idle reservation and
   setup deferral are unchanged. See the inherited permission gap below.
2. Worker 1.1.0 verifies the outer signature, hashes, platform and sequence;
   validates the full v0.3.12 payload. Its authenticated staged mode check writes,
   fsyncs and verifies the B76 ordering drop-in and daemon-reload **before** any
   candidate creation, application write, snapshot or runtime quiescence. This
   narrow preflight uses the transaction's existing update/mode locks without
   acquiring them again. Only then does installation run.
   The capsule has a new identity and uses that app and its sounds unchanged.
3. After commit and worker exit, the existing supervisor hook queues bootstrap.
   Its first command rechecks the durable B76 repair. The preflight repair is
   outside the manifest's rollback destinations and survives application failure.
4. Bootstrap retains the exact 1.1.0 modules as slot A, durably publishes its
   GOOD record and the permanent selector, then installs the five fixed routes
   and enables the independent selector timer. The discovery route durably sets
   and verifies TimeoutStartSec=1800 before selecting the new downloader
   (transfer budget 1500 seconds). No fixed worker is overwritten.
5. After bootstrap releases its locks, a bounded systemd child verifies retained
   controller proof/current trust/floors, writes only slot B, consumes TRIED and
   runs the existing recovery, offline self-tests and live readiness trial.
6. Only the selector publishes B's GOOD after fresh broker, button-owner and
   poller evidence and health checks. The service marks completion only after
   promotion succeeds. The next signed full application uses normal 1.3.0 OTA.

## Failure and restart contract

| Boundary | Failure or interruption | Result / next action |
| --- | --- | --- |
| Discovery, owner policy, signature, sequence, platform, base preflight | Missing local approval, invalid/replayed/incompatible artifact | Reject/defer without changing installed app or fixed worker; retain normal owner policy. |
| Authenticated repair preflight | Write/fsync/reload failure | Abort before application mutation or quiescence. Retry from the running original app. Before the repair is durable, a power cut retains the inherited fleet boot risk. |
| Capsule transaction before commit | Health failure or power loss | Durable B76 repair remains. Frozen boot recovery restores the prior app and activators; sequence remains consumed when applicable. Runtime and original updater remain reachable. |
| Initial selector installation | Partial selector/GOOD/routes, reload or timer failure | Any published route has an already durable selector and accepted A. Other routes still use unchanged A directly. Retry completes integration. |
| Descriptor admission / inactive copy | Bad inner signature, controller floor, storage, copy or parser failure | A remains accepted; app remains the base capsule; independent selector timer and supervisor remain available. |
| Trial, including power loss and failed health | B has no durable GOOD | Timer, or boot recovery, selects A and restores shared state/runtime; TRIED prevents repeating the failed candidate. A corrected signed capsule needs a fresh descriptor/sequence and tag. |
| GOOD/completion publication | Cut before directory fsync or completion | Boot selects a durable accepted slot; A remains usable. Retry settles publication; completion is scheduling evidence, never selection authority. |
| Later full application | Rejection, failed health or interrupted install | Normal selected-worker journal rollback; B76 and accepted controller slots survive. |

No artifact can repair a box before it executes. A power cut before repair code
runs, or while its first write is not yet durable, retains the original cycle.
No application mutation or quiescence is allowed in that interval. Host tests
record that interval as an uncovered boot risk, never a successful restart.
A box already silent after that failed boot cannot receive normal OTA: its
runtime/approval gate is inactive. This bridge adds no rescue button, remote shell
or permission bypass. Operator recovery/retrieval remains an external obligation;
there is no family-only OTA recovery claim for an already-dead box.

## Coverage and acceptance

Running Cloud v0.3.9, v0.3.10 and v0.3.12 with the recorded original 1.1.0
controller receive the bridge directly; the older two first step to its v0.3.12
application. The repair-only capsule still accepts v0.3.12 only. An already repaired
v0.3.12 is eligible; repair is idempotent. Newer voice apps with a frozen fallback,
unknown fixed-controller bytes, unbootstrapped/offline boxes, unsupported OS/CPU,
and already-dead runtimes remain outside coverage. wacli has no Cloud OTA bridge;
its ordinary messaging and physical release acceptance gates stay unchanged.

Host tests exercise actual packaged base artifacts and disposable test-key
signatures through recorded workers, installed hooks, real controller self-tests,
admission, recovery and journal paths. Systemd/PID credentials, hardware, platform,
elapsed time and network heartbeat success are explicit host boundaries. They do
not prove Pi OS PID 1 boot or physical power-loss behavior.

Host regression evidence: both `MSGBOX_TEST_TRANSPORT=cloud make check` and
`MSGBOX_TEST_TRANSPORT=wacli make check`, run through `agent-storage run --heavy`,
passed 1,412 Python cases (1,415 run, three native/field-only skips) and 54
JavaScript cases each, plus syntax and lint. Per base, the cut audit has 189
durable hook boundaries, six application journal boundaries, three early
application-write boundaries and eight durable preflight outcomes with reachable
runtime **and** updater: 618 distinct covered cut outcomes across the three bases.
Four preflight cuts per base precede durable repair: those 12 outcomes are
explicitly **uncovered**, not counted as successful boots. Hook cuts also require
a successful cold boot after retry or consumed-trial fallback. A separate real
signed corrective capsule succeeds after failed promotion on every base.

Before rollout on Pi OS arm64: run the signed OTA from each supported base;
exercise Ask/Not now/Install and automatic windows through the real Cloud;
verify B76 persistence and effective dependencies before power cycling; cut at
repair, selector/routes, slot copy, trial, GOOD and completion boundaries; force
failed controller/app health; verify runtime, heartbeat, selection and a following
full OTA after each recovery. Export the exact systemd graph and test runtime and
setup boot. Verify audio/GPIO/NFC/Wi-Fi/WhatsApp, plus physical wacli send, play,
reply, card, quiet hours and listened notices before a release merge.

## Coordinator and Cloud contract

Neither frozen 1.1.0 nor v0.3.16's 1.3.0 controller verifies a Cloud-signed permit
bound to box identity and artifact. `automatic_if_ready` consumes a release-signed
candidate plus local mode/window policy; `start_install` writes a local root ticket.
Ask requests arrive over the existing authenticated Cloud transport and retain
physical confirmation, but that is not an artifact-bound Cloud signature. The
bridge preserves these paths and adds no permit protocol. Missing, expired,
wrong-box, wrong-artifact and replayed Cloud permits cannot be claimed as enforced.
**The release owner must decide this inherited gap separately before release.**

The persistent discovery route is the bridge at stable `/latest`. Publish any
following full stable release with GitHub's `make_latest=false`; leave the bridge
as latest while **any** unpinned original updater is outstanding, including
offline/unknown boxes. This hold has no expiry. Do not move `/latest` merely
because an online canary or a rollout batch succeeds. There are two cohorts:

- Frozen stable discovery without an emergency trust pin reads only `/latest`.
  It finds the bridge while this hold remains, even with the full release present.
  If latest moves to the incompatible full release, it finds no eligible update.
- With an emergency pin, frozen discovery scans bounded release history and can
  find the signed bridge beside the incompatible full release.

The existing approved exact-version request discovers the immutable bridge tag
through `/tags/<tag>` for unpinned clients. On pinned frozen clients, use history
without an exact-version request: the frozen requested-plus-history path duplicates
the same tag and can delete its selected download. This inherited discovery bug
is covered by a rejection test; it is not repaired on the frozen worker.
Before moving latest with unpinned stragglers, Cloud must durably retain/reoffer
that prerequisite request until promotion is verified, with normal owner policy; this companion change and its
acceptance are required first. It is not implemented by this public repository.
Until then the permanent latest hold is mandatory. Promoted pinned clients can
find the compatible non-latest full release in history. Promoted unpinned clients
wait until the hold can safely end; do not assume current Cloud can offer a tag
the box has not reported as available. Exact-version discovery exists in the
controller, but persistent Cloud offering still needs its companion acceptance.
Never infer the selected worker from application version or `protocol=1`: current heartbeat does
not report controller selection. Application commit alone is not promotion proof.
Track bridge installation and verified controller readiness separately before
offering the full release. Owner-facing notes should say “Software update” and
explain the intermediate update; retain Install / Not now and exact-version
approval binding. Dashboard version/history must show the actual capsule tag and
result, without claiming that the full application is installed. Selected-worker
telemetry/readiness and persistent prerequisite discovery require companion work;
they are not permission to bypass a failed trial or an owner's decision.

The bridge's application contract is **v0.3.12**, regardless of its higher release
tag. Regenerate the fleet fixture with `scripts/dev/settings_contract.py --repo`
against the verified base checkout, then associate that contract with the actual
signed bridge build in the release handoff. It has the same 22 setting keys as
the full app, but its `voice_pack` enum/capability excludes `buttonbox`, and its
command allowlist excludes `setup_greeting`; it does not advertise `guided_setup_v1`.
Cloud must use reported capabilities
and the actual build fixture for those features; a version-number threshold is
insufficient. Do not send those values/commands until the full app is installed,
or weaken rejection to make them pass. The existing `box_update_ready_v1`
template can retain its wire format; owner copy and prerequisite offering need
the companion changes above. This does not resolve the inherited signed-permission gap.

Future GitHub/website handoffs must perform the standing public-output review of
all outgoing surfaces and their actual audience, scans plus semantic review,
sanitization/recheck and a private receipt. No tag, signature, push, upload,
publication or device action is authorized by preparing this bridge.
