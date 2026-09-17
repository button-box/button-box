# Button Box dashboard · mobile-first visual proposal

**Review artifact only.** This proposal does not change the production dashboard, a live Button Box, device state, provisioning, a pull request, or a deployment.

Prepared 15 September 2026 from canonical repository `button-box/button-box` at `57c63bb7e0129b842bce476dcc3564dd520755da`. This archived prototype is separate from the implemented UI; see [implementation evidence](implementation.md).

## Review scope

1. **Brand fit:** should the local caregiver dashboard use this warm-paper, heavy-outline, high-contrast extension of the current button.box language?
2. **Information hierarchy:** does “ready / needs attention” belong first, followed by setup, recent activity, and caregiver controls?
3. **Navigation:** keep all five current canonical routes visible on mobile: Home, Setup, Settings, Activity, Advanced.
4. **Support identity placement:** keep the customer-facing **Box ID** out of the prominent header. Put it in a low-key footer support area with a Copy action and guidance to share it when asking for help. `BOX-DEMO` is sample prototype data; durable persistence and recovery after reimage are not implemented.
5. **Implementation boundary:** approve the visual/component system before production UI code changes. Route behavior, API contracts, privacy controls, safe routing, and recovery semantics remain unchanged unless separately reviewed.
6. **Owner community:** preserve the sticky five-route navigation and add the repository's existing WhatsApp community destination in the support footer.

Open [`prototype.html`](prototype.html) and use its route tabs and “Preview state” menu. Rendered reference screenshots live in [`screenshots/`](screenshots/).

### Rendered previews

- [`mobile-home.jpg`](screenshots/mobile-home.jpg) · 390 × 844
- [`mobile-setup.jpg`](screenshots/mobile-setup.jpg) · 390 × 844
- [`mobile-activity.jpg`](screenshots/mobile-activity.jpg) · 390 × 844
- [`mobile-advanced.jpg`](screenshots/mobile-advanced.jpg) · 390 × 844
- [`mobile-support-footer.jpg`](screenshots/mobile-support-footer.jpg) · 390 × 844
- [`mobile-home-full.jpg`](screenshots/mobile-home-full.jpg) · 390 × 2066
- [`desktop-home.jpg`](screenshots/desktop-home.jpg) · 1280 × 800

## Evidence used

### Canonical dashboard code

- `messagebox/onboarding/static/index.html` defines five canonical household routes and 24 rendered views.
- `messagebox/onboarding/static/app.js` owns state transitions, polling, loading, errors, focus movement, recipient/NFC flows, settings revision handling, activity queue actions, and support/recovery flows.
- `messagebox/dashboard/app.py` serves the household UI and its API: state, settings, WhatsApp, recipients, NFC, activity/audio queues, Wi-Fi change, listeners, ringtone, hold, trash, and reinstate operations.
- The repository README supplies the existing public WhatsApp community destination used by the proposal; no new invite URL was invented.

### What “Play the ringtone” demonstrates

The proposal renames “Ring Button Box now” to the more specific **Play the ringtone** and places its evidence boundary next to the action.

- A successful dashboard response demonstrates only that `POST /api/ring` created, or found, the idempotent `ring-request` marker and returned `202 queued`.
- The button service checks for that marker only while it is idle. It removes the marker, logs a dashboard-source ring event, plays the configured ringtone through the configured speaker with `aplay`, and flashes the lamp. A press can stop the ring.
- If a person standing near the named box actually hears the ringtone, that additionally demonstrates an audible speaker/ringtone-path check at that moment and can help identify the physical box.
- It does **not** demonstrate WhatsApp receipt, recipient routing, queue creation, incoming message playback, the physical-button interaction, microphone recording, review/approval, outgoing send, WhatsApp delivery, NFC selection, or reboot persistence.
- Full receive/play/reply acceptance remains the separate first-message proof flow on the named physical unit: receive a real message, play it, record a reply, approve/send it, and verify the intended destination receives it.

### Current live button.box language

Measured from the public site on 15 September 2026:

| Role | Live value | Proposed dashboard use |
| --- | --- | --- |
| Paper | `#fbf9f4` | app background |
| Ink | `#1a1a1a` | text and 2 px outlines |
| Muted | `#6f706a` | supporting copy |
| Button blue | `#5ea7ff` | product identity and primary household action |
| Signal yellow | `#ffc94d` | setup/in-progress and attention |
| WhatsApp green | `#25d366` | connected/complete states only |
| Error | `#a13326` | blocking failure and destructive emphasis |
| Body type | Avenir Next stack | labels, body, controls |
| Display type | Futura stack | short route titles and primary status |
| Button treatment | 2 px ink border, 11 px radius, 4 px hard shadow | primary actions; reduced to 2 px pressed state |

The dashboard deliberately does **not** copy the website’s oversized editorial layout. It borrows the recognizable palette, condensed display type, uppercase eyebrows, outlined pills, chunky buttons, direct family language, and paper-like warmth while keeping a calm operational density.

## Proposed component system

| Component | Purpose | Variants and states |
| --- | --- | --- |
| `AppHeader` | persistent product identity and current health | online, setup, attention; no physical identifier in the prominent header |
| `PrimaryNav` | five stable caregiver destinations | current, idle, keyboard focus; bottom-fixed on small screens |
| `HeroStatus` | one plain-language household answer | ready, setup in progress, needs attention, unavailable |
| `StatusTile` | Wi-Fi, WhatsApp, runtime, recipient/NFC health | checking, good, optional, attention, unknown |
| `TaskRow` | required/optional setup progress | complete, to do, active, blocked, optional |
| `ChoiceCard` | large touch-friendly settings/recipient choice | default, selected, disabled, saving, error |
| `Notice` | privacy, safety, recovery, or non-blocking warning | neutral, success, attention, danger |
| `ActionButton` | explicit state-changing or recovery action | primary, secondary, quiet, danger; busy and disabled |
| `RecipientCard` | recipient/default/card relationship | default, allowed, available, card count, pending action |
| `NfcStage` | waiting/detected/mapped/success/unavailable | all existing NFC stages with persistent Back/Skip/Done controls |
| `ActivityRow` | content-free interaction outcome | new message, reply, incomplete, delivered, attention |
| `AudioMessageCard` | queue playback and queue state transition | queue, on hold, trash; loading/claimed/conflict/error |
| `SettingsSection` | progressive grouping without capability loss | recording, after-listening, duration, ringtone/volume, arrival, quiet hours, NFC beep |
| `TechnicalDetails` | local support details inside Advanced | hostname and software revision; does not substitute for Box ID |
| `SupportFooter` | low-priority help and owner community | sample/persisted Box ID boundary, Copy result, canonical WhatsApp community link |
| `InlineStatus` | announce async feedback next to action | loading, saved, queued, retryable error, revision conflict |
| `Skeleton` | retain layout during fetch/poll | tiles, rows, cards; never presented as real data |

## Route and view inventory

This is the implementation-preservation checklist. A later production PR should map every row to existing tests or add focused contract coverage.

### Canonical routes

| Route | Existing capabilities that must remain | Proposed presentation | Required states |
| --- | --- | --- | --- |
| `#home` | Wi-Fi/WhatsApp/runtime status; setup attention; settings/setup shortcuts; ring-now action | one `HeroStatus`, three status tiles, useful next actions, explicit ringtone-check boundary | loading, ready, setup in progress, attention, ring queued, ring unavailable, global fetch error |
| `#setup` | required Wi-Fi, WhatsApp, default recipient, first message proof; optional NFC and personalization; resumable progress | checklist split Required/Optional with saved-state copy | loading, complete, active, attention, optional, blocked, route/API error |
| `#settings` | recording mode; after-listening; max duration; ringtone preview; volume; arrival signal; quiet hours/timezone; NFC beep; revisioned save | grouped cards with sticky Save on mobile and explicit unsaved/saved state | loading, loaded, dirty, saving, saved, invalid, API error, revision conflict, confirmation before immediate-send mode |
| `#activity` | content-free totals/timeline; queued audio; hold; trash; reinstate; setup-gated state | privacy notice, compact totals, interaction feed, segmented Queue/On hold/Trash | loading, no setup, empty, populated, playing, moving, claimed conflict, retryable error |
| `#advanced` | network-access disclosure; runtime/software health; WhatsApp/recipient shortcuts; Wi-Fi change with rollback copy; listener profiles; operator-only boundary | technical health first, then guarded recovery sections; Box ID remains in the global support footer | loading, setup mode, runtime, Box ID present/missing, profiles empty/populated/error, Wi-Fi changing/recovered/failed, destructive confirmations |
| `#whatsapp` | manage current link or begin/retry pairing | focused task stage preserving safe errors and unlink confirmation | idle, starting, code pending, bootstrapping, verifying, ready, expired, failed, cleanup required, unlink failed |
| `#continue` | resume exact current onboarding phase | same state components as the relevant onboarding stage | all onboarding phases below; no invented fallback |

### Existing rendered views/state machine

| Existing view ID | Trigger / meaning | Must preserve in implementation |
| --- | --- | --- |
| `home-view` | canonical home route | attention notice, health summary, three shortcuts, ring-now result |
| `setup-view` | resumable setup overview | required vs optional, durable completed state, correct resume links |
| `settings-view` | caregiver settings | every field and current confirmation/revision-conflict behavior |
| `activity-view` | privacy-safe activity and queues | no transcripts, raw NFC IDs, or internal message IDs; audio controls and recoverable queue actions |
| `advanced-view` | support/recovery | no-login network warning, runtime/software, Wi-Fi rollback, WhatsApp, recipients/NFC, listeners, operator-only boundary |
| `wifi-view` | `WIFI_SELECT` | scanning, no-networks manual entry, protected/open choice, setup URL copy, validation |
| `checking-view` | `WIFI_CONNECTING` or associated/checking | progress without false completion; reconnect/handoff guidance |
| `failed-view` | association/internet failure | distinct association vs internet copy, recheck, choose network, recover/start-over |
| `whatsapp-view` | WhatsApp idle | international-number instructions and start pairing |
| `code-view` | `code_pending` | code readability/copy, exact WhatsApp steps, waiting status, cancel |
| `pairing-progress-view` | starting/bootstrap/verify | stage-specific copy, keep-open status, cancel |
| `pairing-error-view` | expired/failed/safe error | bounded safe-error copy, retry or cleanup label, no false account-saved claim |
| `ready-view` | WhatsApp linked | masked account hint, eligible count, continue/manage/relink semantics |
| `recipients-view` | choose initial default | discovered recipients, manual number, empty state, refresh, defer, request status |
| `deferred-view` | recipient setup deferred | messaging/NFC remains off, resume choice |
| `voice-test-view` | recipient status `testing` | received → played → replied proof, live polling, one clear physical step at a time |
| `voice-success-view` | first flow complete | confirmed scope only, recipient label, next step to manager |
| `recipient-manager-view` | manage configured recipients | default, allow/remove, manual add, WhatsApp candidates, refresh, NFC enroll/unpair/cancel |
| `nfc-view` | waiting for tag | waiting/remove-tag instruction, mapped count, back, skip/done |
| `nfc-choose-view` | tag detected, choose recipient | allowed recipient list, sound warning, back |
| `nfc-mapped-view` | tag already paired | current recipient, keep pairing, explicit reassign, back |
| `nfc-success-view` | mapping saved | recipient confirmation, optional sound warning, another/done |
| `nfc-unavailable-view` | reader/API unavailable | preserved mappings distinction, retry, skip/done, back |
| `complete-view` | onboarding completion accepted | messaging active claim only after accepted backend result |
| global `page-error` | request or state error | assertive announcement, safe copy, retry path without hiding current context |

### Dynamic sub-states not represented by their own view

- Wi-Fi scan: scanning, results, no networks, scan error, manual SSID, open/protected, submit busy.
- Home: fetching, healthy, attention, ring queued, ring error.
- Recipients: loading, refresh loading, empty, configured/default, candidate, manual number, mutation busy/error, stale/changed state.
- Runtime NFC: idle, waiting, healthy/unhealthy, completed enrollment, cancel, presented-card required, unpair success/error.
- Settings: attention warning for last-valid fallback, loaded revision, preview busy/error, unsaved, save busy/success, validation error, revision conflict.
- Activity: setup-gated, totals empty/populated, timeline empty/populated, queue/hold/trash empty/populated, audio unavailable, move busy, file claimed, destination conflict.
- Advanced: setup-gated listener profiles, empty/populated, edit/remove/save busy/error; Wi-Fi change testing, reconnect guidance, rollback success/failure.
- Global polling: initial load, background refresh, temporarily unreachable with retry, recovery without resetting the current route.

## Accessibility acceptance

- Preserve one semantic `main`, labelled navigation, route-level `h1`, fieldsets/legends, associated labels, and native controls.
- Move focus to the new route/stage heading only when the view changes; do not steal focus on background polls.
- Keep tap targets at least 44 × 44 CSS px; the proposed mobile nav targets are 64 px high.
- Status is never color-only: icon/shape plus plain text (`Ready`, `To do`, `Needs attention`).
- Use `aria-current="page"`, `aria-live="polite"` for progress/saves, and `role="alert"` only for blocking failures.
- Maintain visible `:focus-visible` outlines that are not obscured by the hard shadow.
- Respect `prefers-reduced-motion`; no pulsing or decorative transition is needed to understand progress.
- Maintain useful layout to 200% zoom and a single 320 px column without horizontal page scrolling.
- Do not expose phone numbers, JIDs, raw NFC UIDs, internal message IDs, credentials, or unrestricted logs.
- Error and empty states keep the relevant recovery action adjacent to the explanation.

## Deliberate boundaries and open implementation questions

- **Box ID is customer-facing proposal language, not an implemented feature.** The backend must later define one durable canonical source installed during provisioning, retained across onboarding resets, reconciled after reimage, and kept distinct from hostname and enclosure/part serials. The sample `BOX-DEMO` value proves only the proposed footer placement and copy interaction.
- The prototype uses sample household data and does not call device APIs.
- The current CSP is compatible with same-origin CSS/JS only. A production version should keep assets local and avoid webfont dependencies.
- Bottom navigation is proposed for mobile and remains sticky. Desktop expands to a left rail while retaining the same five destinations and route hashes.
- The support footer uses the community invite already published in the repository README. Community membership, moderation, and destination changes remain outside this visual proposal.
- The current dashboard and onboarding share `index.html`, `app.js`, and `styles.css`. Implementation should first extract reusable visual components/tokens without changing endpoint contracts or the fail-closed state routing.
- Production acceptance still needs focused unit/UI contract tests plus real phone/Pi, Wi-Fi, WhatsApp, audio, NFC, physical-button, and recovery verification on the named unit. A rendered proposal is not device acceptance.

## Recommended implementation slice after review

1. Add design tokens and shared primitives behind the existing markup/state machine.
2. Restyle Home, global header/nav, notices, status tiles, buttons, form controls, and focus states without API changes.
3. Validate 320/390/768/desktop layouts plus keyboard and reduced-motion behavior.
4. Migrate Setup and onboarding views in small groups, preserving state behavior and tests.
5. Migrate Settings, Activity, and Advanced; add the Box ID support surface only after its separate data contract is approved and implemented.

No production work should start until the review gate is explicitly cleared.
