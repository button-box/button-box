# Button Box acceptance matrix

This is the canonical, reusable definition of the checks required before a box is accepted. Record each run with [the per-unit template](box-acceptance-run-template.md). Never include phone numbers, chat content, credentials, card UIDs, or recordings in a public run.

## Evidence and completion

- `TEST`: deterministic synthetic test; `UI`, `API`, and `LOG`: observed on the named live revision; `PHYSICAL`: a person operated the hardware; `ACOUSTIC`: a bounded measurement or listening check; `MOCK`: simulated hardware input.
- `Passed` applies only to the listed evidence layer. `Passed (TEST)` never proves physical behavior. Use `Blocked`, `Not run`, or `Inconclusive` honestly.
- A unit is accepted only when every Required case passes at its required layer on the same identified unit and revision, including a cold reboot. Optional NFC cases may be `Not fitted`; if NFC is fitted, all NFC cases are required.
- Run repository checks with `make check`. Run the guided device checks with `sudo -u messagebox -H /opt/messagebox/dev/hardware-test.sh`. Preserve sanitized timestamps and defect/PR links for failures.
- Validate a JSON run before delivery with `python3 scripts/dev/validate_acceptance_run.py RUN.json --expected-matrix-revision MATRIX_COMMIT`. The command rejects missing, non-passing, duplicate, insufficient-evidence, and stale revision records.

## Matrix

| ID | Required evidence | Expected result |
|---|---|---|
| BB-BUILD-01 | PHYSICAL | The recorded unit identity and final parts match the assembled box. The enclosure closes, components stay retained, cables remain intact with clearance and strain relief, and the specified power supply is used. |
| BB-HOME-01 | UI+API | Home reports Wi-Fi, WhatsApp, and runtime truthfully. |
| BB-HOME-02 | UI+LOG+ACOUSTIC | Ring control records one request and produces the selected signal. |
| BB-SETUP-01 | UI | Required and optional setup tasks render and navigate correctly. |
| BB-SETUP-02 | UI | A stale setup-only hash normalizes to the correct runtime view. |
| BB-NAV-01 | UI | Home, Setup, Settings, Activity, and Advanced hashes show matching views. |
| BB-NAV-02 | UI | Advanced WhatsApp management returns to Advanced. |
| BB-NAV-03 | UI | Advanced recipient management returns to Advanced. |
| BB-WIFI-01 | TEST | Initial scan/rescan normalizes safe access-point results. |
| BB-WIFI-02 | TEST+PHYSICAL | Failed candidate restores the working profile or safely opens setup. |
| BB-WIFI-03 | UI | Copy setup URL copies the displayed URL and reports success/failure. |
| BB-WIFI-04 | UI+API | Invalid change input is rejected without disconnecting the box. |
| BB-WA-01 | UI | Linked state is accurate and management is available. |
| BB-WA-02 | TEST+UI | Pair/start/code/copy/cancel/expiry/retry states are resumable and private. |
| BB-WA-03 | TEST+UI | Unlink requires confirmation; failure preserves account and routing. |
| BB-RECIP-01 | UI+API | Selecting the current default is idempotent and persistent. |
| BB-RECIP-02 | UI+API | The linked box account is rejected as a recipient. |
| BB-RECIP-03 | UI+API | Refresh preserves configured recipients. |
| BB-RECIP-04 | TEST+UI | Defer/resume returns to selection without state loss. |
| BB-RECIP-05 | UI+API | Re-adding an allowed recipient succeeds idempotently. |
| BB-RECIP-06 | UI+API | A malformed recipient is rejected without mutation. |
| BB-RECIP-07 | UI+API | Default can change between allowed recipients and survive reload. |
| BB-RECIP-08 | TEST | Rapid concurrent switching remains atomic and consistent. |
| BB-RECIP-09 | TEST+UI | Removing a non-default affects only that recipient. |
| BB-RECIP-10 | TEST+UI | Removing the default is rejected safely. |
| BB-RECIP-11 | TEST+UI+API | Naming or renaming a recipient preserves chat identity, default selection, and NFC mappings; names persist after reload. |
| BB-NFC-01 | UI | Waiting, back, skip, retry, and unavailable states remain coherent. |
| BB-NFC-02 | TEST+MOCK | Repeated, removed, alternating, stale, and unknown mock tags fail closed. |
| BB-NFC-03 | PHYSICAL | A real tag pairs, debounces, re-presents, reassigns, unpairs, and persists. |
| BB-NFC-04 | TEST+PHYSICAL | A fresh card selection takes priority over queued playback and recent replies in both recording modes; stale, unknown, and raced selections never route to another recipient. |
| BB-VOICE-01 | UI+LOG | One authorized voice note is received and queued once. |
| BB-VOICE-02 | ACOUSTIC | Arrival signal is distinct and matches settings/quiet hours. |
| BB-VOICE-03 | PHYSICAL | One press plays the intended queued note. |
| BB-VOICE-04 | PHYSICAL+UI | Record, stop, listen back, confirm, and send reach only the intended chat. |
| BB-VOICE-05 | TEST+PHYSICAL+LOG | Consecutive authorized messages remain in order while continuous sync stays connected during downloads and recipient refresh. |
| BB-VIDEO-01 | TEST+PHYSICAL+ACOUSTIC | An ordinary video with audio plays its soundtrack in queue order; unsupported or silent media does not block later messages. Circular video notes are not supported. |
| BB-RQ-001 | TEST+UI+PHYSICAL | Requeue retained voice and ordinary-video audio after existing messages with exact original chat labels and one replay per request. Active replays stay hidden from history until playback; restart, hold, trash, expiry, and pruning preserve identity and prevent duplicate playback. See the detailed replay sequence in [testing.md](testing.md). |
| BB-REPLY-01 | TEST+PHYSICAL+UI | In both recording modes, a standalone recording replies to the newest played chat within one hour, even if that message is requeued. An unavailable recent route blocks; expiry returns to the explicit default. NFC and queued guided replies keep their exact routes. |
| BB-AUDIO-01 | ACOUSTIC | Press acknowledgement is audible at the intended listening position. |
| BB-AUDIO-02 | UI+ACOUSTIC | Ringtone preview plays the selected sound. |
| BB-AUDIO-03 | TEST+PHYSICAL+ACOUSTIC | Startup and recording-ready cues are audible, first words are captured, and a new press interrupts the three-second send cue without being swallowed. |
| BB-SET-01 | UI+API | Unchanged save succeeds without unintended changes. |
| BB-SET-02 | UI+API | Valid change persists; original value restores; stale revision fails safely. |
| BB-ACT-01 | UI+API | Content-free counters, timeline, queue, hold, and trash render accurately. |
| BB-ACT-02 | TEST+UI | Hold/resume/trash move exactly one intended disposable message. |
| BB-ADV-01 | UI+API | Invalid listener data is rejected; valid CRUD preserves unrelated profiles. |
| BB-PERSIST-01 | UI+API | Browser reload preserves recipient, settings, and correct route. |
| BB-PERSIST-02 | API+LOG | Targeted service restart preserves state and returns healthy. |
| BB-PERSIST-03 | PHYSICAL+API+LOG | Cold reboot restores services, state, ring, and one message route. |
| BB-UPDATE-01 | TEST+API+LOG | The bounded update and rollback preserve configuration, contacts, identity, queues, permissions, and the exact prior boot selector and active services. Both setup and runtime mode migration are covered. |
| BB-TEST-01 | TEST+UI | On an explicitly enabled test rig, the report controls create, copy, and download a sanitized diagnostic bundle without exposing household data. |
| BB-LAYOUT-01 | UI | Desktop controls are visible, labeled, and usable. |
| BB-LAYOUT-02 | UI | Narrow mobile layout scrolls and exposes every active control. |
| BB-RECOVERY-01 | UI+API | API outage shows a clear error and clears it automatically after recovery. |

## Physical NFC sequence

Present a known unpaired tag; hold it to prove debounce; remove/re-present; rapidly alternate two assigned tags; present an unknown tag while idle, queued, and playing; cancel pairing before and after detection; then reassign, unpair, reboot, and verify persistence. Record latency and usable read range without recording UIDs.
