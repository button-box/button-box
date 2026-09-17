# Testing

## Repository tests

- `make test` runs the synthetic Python unit and JavaScript UI-contract suites.
- `make lint` requires `uvx` (from `uv`) and `bunx` (from Bun). It runs Ruff for
  Python, ShellCheck for shell scripts, and Biome for frontend assets. `uvx` and
  `bunx` download these tools on first use.
- `make check` runs syntax checks, linting, and tests.

No project virtual environment or repository-local dependency installation is
required.

Synthetic tests cover routing, onboarding, redaction, pairing, NFC, and recovery
contracts. They do not replace physical Pi, phone, network, or hardware tests.

## Regression test matrix

Matrix case IDs are permanent. Add new cases with a new descriptive ID; do not
renumber or reuse an existing ID.

Every confirmed product bug must add a matrix case or explicitly update an
existing case and its regression test before the fix is complete. If automation
is impossible, record the reason in the pull request and keep a precise manual
assertion in this matrix.

| Case ID | Regression | Synthetic setup and expected result | Software evidence | Separate physical assertion |
| --- | --- | --- | --- | --- |
| `BB-RQ-001` | Caregiver requeues recently played media | Archive synthetic voice audio and an ordinary video soundtrack after playback. The dashboard returns safe newest-first metadata and an opaque handle. Each replay appends after existing waiting messages using a fresh queue identity while preserving its original history identity and routing sidecar. Repeated and concurrent requests create one playable WAV; restart preserves real queued state, while an interrupted pre-publication attempt retries without a phantom duplicate. Expired, missing or policy-pruned media cannot stream or requeue. | `tests/test_played_history.py::PlayedHistoryTests`, `tests/test_dashboard_queue_hold.py::DashboardQueueHoldTests::test_recently_played_is_newest_first_safe_and_requeues_once`, and the recently-played cases in `tests/onboarding-ui.test.js` | On a test box at the exact candidate revision, leave two synthetic messages waiting, then requeue a retained voice note and ordinary video soundtrack. Confirm both append in request order, refresh and restart before playback, then verify exactly one playback each and that reply routing remains bound to the original chats. Confirm a deliberately expired or pruned fixture cannot stream or requeue. Do not treat circular video notes as supported. |

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
