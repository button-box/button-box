# Testing

## Repository tests

- `make test` runs the synthetic Python, loopback HTTP, and JavaScript UI-contract
  suites. `uv` provides Gunicorn 23.0.0, matching the pinned device package.
  Dependencies may download on first use; the tests themselves use no external
  network, credentials or hardware.
- `make lint` requires `uvx` (from `uv`) and `bunx` (from Bun). It runs Ruff for
  Python, ShellCheck for shell scripts, and Biome for frontend assets. `uvx` and
  `bunx` download these tools on first use.
- `make check` runs syntax checks, linting, and tests.

No project virtual environment or repository-local dependency installation is
required.

Synthetic tests cover routing, onboarding, redaction, pairing, NFC, and recovery
contracts. They do not replace physical Pi, phone, network, or hardware tests.

## Developer simulated inputs

`scripts/dev/simulate-inputs.py` validates a private timed plan by default. Importing
it does not access hardware or application state. Example plan (use synthetic card
IDs here; keep actual plans outside Git with permissions `0600`):

```json
{"duration": 5, "events": [
  {"at": 1, "type": "press"},
  {"at": 1.2, "type": "release"},
  {"at": 2, "type": "nfc-present", "uid": "04:A1:00:FF"},
  {"at": 3, "type": "nfc-repeat"},
  {"at": 4, "type": "nfc-removed"}
]}
```

Supported events are `press`, `release`, `nfc-present` (mapped card),
`nfc-unknown` (unmapped card), `nfc-repeat` and `nfc-removed`. The interval between
press and release is the hold duration. Events must increase, leave at least
0.8 seconds before/after the sequence, and end released with no card present.
Plans last 1–120 seconds and contain at most 256 events. Removal/repeat retain
the normal NFC grace, refresh, unknown-card and one-shot selection semantics.

```sh
python3 scripts/dev/simulate-inputs.py /private/path/plan.json
python3 scripts/dev/simulate-inputs.py /private/path/plan.json --execute \
  --authorization /private/path/authorization.json --timeout 180 \
  --scratch-state-root /private/path/empty-run-state
```

Execute only through the existing sole device owner in an already authorized
test session, as the normal runtime user with the normal service environment
(including detected audio devices). This command does not stop services, configure
credentials or change transport. It requires the button, NFC and poller
(`messagebox-poller.service`) services to be verified inactive. Receive the verified test audio first, then the
sole owner stops the queue producer before simulation. It also requires no
pending claim/enrollment/NFC state, and empty
outbox, recording-temp and listened-receipt directories. Use an explicitly provided
`--scratch-state-root` when production has retained work: it must be a separate,
empty, `0700` directory owned by the runtime user. Only outbox, recording-temp and
listened-receipt paths change; contacts, settings, incoming queue, account and
transport stay authoritative. The driver never copies, moves or deletes household
jobs. Scratch output is retained; use a fresh root per run. Existing or unknown
work in the selected paths fails closed and is retained. Resolve it through the ordinary owner/recovery
workflow; do not delete it to make the driver run.

The private `0600` authorization manifest contains exactly `transport` (`cloud`
or `wacli`), `contacts_sha256`, `settings_sha256`, `account_sha256`, and
`recipients` (the authorized test JIDs, a subset of authoritative contacts). The owner verifies
the account and counterpart before creating it. Hash the exact contact file
selected by that transport and `/var/lib/messagebox-settings/settings.json`.
`account_sha256` hashes a compact JSON array: Cloud uses `[api_url, device_id]`
from the existing validated identity; wacli uses `[resolved_store_path, linked_jid]`
from the normal runtime's bounded `wacli --read-only --json auth status` lookup.
The driver caches this proof until the local identity/store fingerprint changes;
it never creates credentials or starts sync. The owner still verifies the actual
account and counterpart independently. All queued audio routes must be authorized.
Planned mapped cards must belong to an authorized test contact. Validation wrappers
call the normal routing functions unchanged and reject an unauthorized returned
route before recording/presence; guided capture has the same recipient guard.
Hashes, account identity and queued routes are rechecked before each interaction
and before sending.
Neither the manifest nor the plan belongs in a repository or public report.

Execution replaces only button input/status lamp hardware, uses the application's
shared confirmed-press handler and `NfcRuntime.observe`, and keeps recording,
playback, authorization, queue transitions and routing real. It does not run
startup recovery or the permanent sender thread. Ordinary wacli recording
presence and playback reactions remain real for the verified test recipients.
Approved audio remains in the outbox unless `--send-generated` is explicitly
added; that option sends only work generated after the empty-outbox preflight,
using the normal transport functions, with no automatic retry. It checks durable final
state rather than treating a handled return value as send success: failed,
uncertain or quarantined recordings fail. Private JSON outcomes use hashed job
keys and distinguish Cloud queued/waiting/accepted/provider-delivered progress
from independently verified counterpart delivery. Neither a queued upload nor
an accepted transport operation is an account delivery pass. NFC enrollment,
unpairing, account identity, physical switches/readers/lamp, and delivered or
acoustically correct audio are separate assertions.

Budget `--timeout` for the full configured workflow, including recording,
playback, review/approval and transport completion. Its 1–300 second limit
supervises the whole process group, including audio/presence children; ending
the input timeline alone does not interrupt an active handler. Timing starts after
audio setup. Collapsed button transitions, events more than 50 ms late, or an
incomplete schedule fail rather than silently skipping inputs. Successful private
JSON output includes planned/processed counts and maximum scheduling lateness. Timeout, handler
error, uncertain send or unverifiable cleanup is a failure. A timeout can leave
claimed queue/audio/outbox or NFC artifacts: the owner must reconcile them and
restore the saved service state before retrying. Normal completion clears only
the simulator's newly created selection/announcement/health files. It preserves
recorded/queued output, receipts, logs, unknown jobs and enrollment. A zero exit
means the driver completed, not that every expected behavior, acoustic result
or message delivery passed; collect those assertions separately.

Execution also emits private `nfc_observation` and `route_observation` JSON
records. These report actual `NfcRuntime.observe` return action/announce flags,
reader state, selection/claimed/unknown markers, stored announcement action,
and observed selection age/TTL status. Initial state and meaningful changes are
included. A removal input sample and the later grace-completed `removed` result
are separate records. A `refreshed` result does not prove that a consumed
selection was re-armed; inspect selection presence and the claimed marker.
Alternating cards have distinct card hashes even when they route to one person.

Route records come only from the real application's routing/capture guard calls:
`allowed`, `unavailable`, or `rejected`, with an authorized-recipient boolean.
The observer never calls routing/status helpers to infer a choice or consume
state. NFC-only rows therefore show stored effects; proving an actual outbound
route rejection requires the appropriate normal simulated interaction. TTL
status describes the observed timestamp, not a full route-authorization verdict.
File snapshots are not transactional with concurrent application transitions.

Trace identifiers use keyed SHA-256 hashes stable within one run, with a new
unpublished key for each run. Raw cards, recipient/account/device identifiers,
labels, clips and settings are omitted. Idle snapshot reads are limited to ten
per second; unchanged snapshots are suppressed. Handler returns and explicit
NFC events remain visible. Output stops at 2,048 records with an explicit
truncation record; the completion summary reports record count and truncation.
Truncation or missing route records means the trace cannot establish that case,
even if the driver completed. Capture traces and run metadata privately; software
trace evidence does not certify the physical reader, acoustic output or delivery.

Focused contracts are in `tests/test_input_simulator.py`. The script is developer
source, not an installed service or public endpoint. Review changes to the shared
handler and this driver together before any authorized test deployment.

## Regression test matrix

Matrix case IDs are permanent. Add new cases with a new descriptive ID; do not
renumber or reuse an existing ID.

Every confirmed product bug must add a matrix case or explicitly update an
existing case and its regression test before the fix is complete. If automation
is impossible, record the reason in the pull request and keep a precise manual
assertion in this matrix.

| Case ID | Regression | Synthetic setup and expected result | Software evidence | Separate physical assertion |
| --- | --- | --- | --- | --- |
| `BB-WEB-001` | Idle browser connections or a slow operation freeze setup | Launch both portal service commands with the pinned server and a synthetic app. Hold six empty TCP connections open; pages, static assets and state must still respond within one second. Hold a separate operation open and repeat, then release it. HTML/static loading must not run connectivity checks. | `tests/test_onboarding_http.py::OnboardingHTTPTests`, `tests/test_onboarding_api.py::OnboardingAPITests::test_home_page_loads_before_state_request_proves_connectivity`, and the shared-proxy serialization test in `tests/test_comitup_adapter.py` | On the exact installed revision, navigate Home, Setup, Connect Wi-Fi and Link WhatsApp in a real phone browser on both home Wi-Fi and the hotspot. Verify that tabs and links respond promptly while a scan runs. Record HTTP timing separately from the person's observed touch result. |
| `BB-RX-001` | Consecutive inbound voice notes | Provide two allowed audio messages from different synthetic senders in one poll while sync remains active. Both queue exactly once in oldest-first order with unique queue filenames; media retrieval is read-only and does not make continuous sync release the store lock. | `tests/test_voicepoll.py::PollingStoreTests::test_consecutive_senders_queue_oldest_first_without_store_write_lock` and `tests/test_whatsapp_pairing.py::WhatsAppFrontendAndServiceContractTests::test_service_separates_web_user_from_live_store_and_keeps_runtime_stopped` | On a test box, send two voice notes from separate test accounts in quick succession while the first is downloading. Verify both appear and play once in send order. Record device, revision, observed times, and result without storing message content or account identifiers. |
| `BB-RQ-001` | Caregiver requeues recently played media | Archive synthetic voice audio and an ordinary video soundtrack after playback. The dashboard returns safe newest-first metadata and an opaque handle. Each replay appends after existing waiting messages using a fresh queue identity while preserving its original history identity, exact sender/chat display identity, and routing sidecar. An active replay is absent from Recently played until it is played again, including while held or in trash. Repeated and concurrent requests create one playable WAV; claim, release, recovery, hold, trash, and replay scans observe complete transitions. Restart preserves real queued state, while interrupted publication or archive commits recover without a phantom duplicate. Expired, missing or policy-pruned media cannot stream or requeue. | `tests/test_played_history.py::PlayedHistoryTests`, `tests/test_dashboard_queue_hold.py::DashboardQueueHoldTests::test_recently_played_is_newest_first_safe_and_requeues_once`, `tests/test_dashboard_queue_hold.py::DashboardQueueHoldTests::test_dashboard_move_waits_for_replay_history_transition`, and the recently-played cases in `tests/onboarding-ui.test.js` | On a test box at the exact candidate revision, leave two synthetic messages waiting, then requeue a retained voice note and ordinary video soundtrack. Confirm both append in request order with the original sender/chat labels and disappear from Recently played while queued, held or in trash. Refresh and restart before playback, then verify exactly one playback each, that each returns to Recently played only after playback, and that reply routing remains bound to the original chats. Confirm a deliberately expired or pruned fixture cannot stream or requeue. Do not treat circular video notes as supported. |
| `BB-RT-001` | Recent-sender routing cannot drift during replay or bypass NFC | Archive an older message from sender A and a newer message from sender B. Requeue B and move it through queue, in-flight, hold, and trash; B remains the recent route even while hidden from Recently played. A removed or invalid fresh B route blocks instead of choosing A or the default. With no card or claimed inbound, standalone hold-release and tap-review sessions use fresh B; a fresh card still wins, and unknown-card or unhealthy-reader state blocks. No/expired history alone permits the configured default. | `tests/test_played_history.py::PlayedHistoryTests::test_active_newest_replay_remains_the_recent_route_in_every_queue_state`, `tests/test_played_history.py::PlayedHistoryTests::test_invalid_or_removed_newest_route_never_selects_an_older_sender`, and the recent-routing cases in `tests/test_button_routing.py` | On the exact candidate revision, play messages from two test chats, requeue the newer one, and exercise both recording modes. Verify replies stay bound to the newer chat through refresh/restart/hold/trash, a fresh NFC choice overrides it, and removed-contact, unknown-card, and unavailable-reader states block without sending to an older/default chat. |

## Physical test scenarios

Use a spare Raspberry Pi 4 with a freshly imaged test microSD card. Confirm it
did not inherit state from a development or household device.

For installation and consumer onboarding, test:

- Clean installation and manufacturer handoff
- Wi-Fi success, failure, and recovery
- WhatsApp pairing and interruption
- Empty-account recipient discovery and manual refresh
- Manual international-number selection and allow-listing, including invalid
  formats, duplicates, and default preservation when merely adding a recipient
- People displayed by valid international phone number, invalid direct-chat
  placeholders excluded, and refresh after the 100-message pairing bootstrap
  cap has been reached
- Initial default selection, switching the default among allowed recipients,
  protection from removing the current default, no-card routing after a switch,
  defer/resume, and recipient-manager recovery
- New voice note and ordinary video with speech, physical playback, guided reply
  review, and accepted send. Confirm a no-audio video is skipped and the next
  valid message still plays. Circular instant video notes remain an explicit
  unsupported case with the pinned wacli 0.17.1 client.
- Zero-tag Skip, person and group pairing, multiple tags per recipient,
  explicit reassignment, remove-after-beep behavior, Retry/Skip when the reader
  is unavailable, and the distinct read/success tones
- Reload within and after the two-minute pending-tag window, completion with and
  without mappings, and the onboarding-to-runtime service handoff
- After both Skip and Done, verify from another device on the same Wi-Fi that
  the printed `.local` hostname resolves to the box's current Wi-Fi IPv4 address
  and that the dashboard opens. Repeat after the advertised mDNS records expire
  from client caches and after a cold reboot. A listening port or a successful
  request made on the box itself is insufficient for this check.
- Reboot and power loss during onboarding transitions

For the standalone developer flow and runtime, test:

- Explicit-default and multiple-recipient routing
- NFC enrollment, removal, and unknown cards
- Microphone, speaker, LED, and button behavior
- First send and reply
- Reboot and power-loss recovery
- Rollback to the previous working release

# Recipient and activity checks during setup

Navigation refetches server state, including after the setup-to-runtime handoff.
Runtime needing attention must not be labeled "Setup in progress". Verify Home
and Activity after completion without reloading the tab.

After scanning an unpaired tag, allow a new international number directly on
"Who is this tag for?". The scan and existing mappings remain intact; adding
does not change the default or assign the tag. Choose the new recipient to
assign explicitly. Check invalid/self numbers, retry after an uncertain response,
and background polling while typing. Existing allowed-recipient validation applies.

Activity is read-only during setup and shows content-free recorded events or
an empty state. It uses the existing group-restricted pairing-worker socket:
the portal does not gain runtime-store access. Test before WhatsApp linking,
after receive/play/send events, with no history, and with the worker unavailable.
Never complete setup or expose audio/recipient identifiers just to view events.

# Early send during own-recording review

In tap/review mode, a fresh deliberate press during review of the user's own
recording stops preview and approves that recording once. It skips the later
send prompt, approval timeout and delete warning. Incoming-message listening
is unchanged. A held recording-stop press must be released before review can
accept a new press. With no review press, the existing prompt, timeout, warning
and cancellation flow remains unchanged. Approval queues a send; only actual
send success may trigger the successful-send sound.

On a named box, verify early review approval for both standalone and reply
flows, a held recording-stop press, a short bounce, no approval/cancellation,
and exactly one outgoing voice note in the intended chat. Keep software tests
and physical switch/audio/delivery evidence separate.

## Cloud mode

Run the [combined-release cloud acceptance checks](cloud-runtime.md#combined-release-acceptance)
in addition to the existing standalone checks. Validate both modes on the exact
candidate revision before accepting a combined release.
