# Review, claim and settings flow fixes

## F313 deferred

Early approval during review is excluded from this batch. Review, send-question
playback, approval wait and held-playback-press discard are restored exactly to
`3d54ac7`, for ordinary and setup replies. The next attempt must observe the
button independently of blocking audio and filesystem work, for example GPIO
edge callbacks in a dedicated thread feeding one debounced state machine.
Audio phases must not transfer button ownership or consume approval edges.

Acceptance must cover all five review rounds, with exactly one approval/job
containing the original audio and recipient for every valid press, and none
for ignored holds or bounce. Run ordinary and setup replies where applicable:

1. Hold from 0.2 s through the ignore interval, open for only 5 ms at 0.6 s,
   then remain closed: require a stable release and a fresh press. Preserve a
   fresh press whose 80 ms confirmation spans either clip's end (press starts
   40 ms before completion, held for 240 ms). A stalled player must terminate
   by the 600 s deadline, be reaped and release speaker/maintenance reservations.
2. Question ends at 1.000 s; a 200 ms press starts at 1.100 s: accept it without
   a release-wait gap. A first review attempt failing with ALSA busy must retry
   once, preserve the recording and allow subsequent deliberate approval.
3. Last open sample at 0.995 s, clip ends at 1.000 s, press lasts
   0.996–1.250 s: preserve it for both clips. A failed optional setup question
   must leave approval available after audio recovers.
4. Question ends at 1.000 s, cleanup pauses 20 ms, press lasts
   1.010–1.210 s: preserve it without requiring another press.
5. Block audio startup/cleanup and filesystem work while a complete deliberate
   press occurs: independent observation must retain its edges and confirmation.
   No audio phase or blocking operation may create an unobserved interval.

Also retain the stop-recording hold exclusion, first 0.5 s ignore interval,
short-press rejection, no-press deletion, normal sending feedback and unchanged
wacli playback behavior. Verify real GPIO/audio timing before release.

## F314: pending-claim speech across owners

The HOME setup watcher checks in every five seconds when healthy. Only a
`claimed: true` result requests completion. The root completion service verifies
the claim again, waits its three-second response grace, stops the onboarding
voice target, removes the setup marker and starts the runtime. There is no
four-minute handover timer. The reported delay is consistent with waiting for
claim completion; the exact historical sequence requires device evidence.

Two local prompt-loss windows are covered. A pending claim arriving before the
online event was consumed could be discarded because the guide did not exist.
Also, `speak` could read `scan`, race a change to `press`, record `press` as
spoken, then play the stale scan line. Pending claims now initialize the guide,
and speech receipts compare the exact stage and pending claim under the lock.
A mismatch leaves the new prompt for the next idle pass. Both button owners
share this receipt. The completion gate also advances a verified completed
claim to names before handover, including phone-triggered completion.

Registration alone does not permit press speech. The pending claim must be
unexpired. Speech remains optional, with receipts before audio: power loss or
speaker failure cannot guarantee that the person hears the line.

## F315: silent settings during guided setup

`CloudRuntime._queue_settings_saved` skips queuing a saved confirmation while an
existing guide is before `done`. Settings application, volume application,
ignored-key reporting and ACKs are unchanged. Suppressed cues are not deferred.
After `done`, a new dashboard change queues its normal confirmation. Existing
boxes without a guide keep normal settings confirmations. No revision-number
or first-operation heuristic is needed.

## F316: what online speech proves (investigation only)

The connectivity checker requires managed wlan0 association, IPv4, a default
route, DNS and an interface-bound HTTPS 204 from the configured connectivity
host. This permits the setup page to proceed; it does not queue the voice.
The HOME watcher first registers if needed, then requires a successful
authenticated `/device/setup-checkin` response before writing `setup-online.json`.
The current setup line is `voice-setup-scan.wav`; the older `setup-online` and
`voice-online` assets remain bundled. Claimed runtime's `voice-online-ready`
requires a current-boot heartbeat snapshot at most 90 seconds old.

The setup event and guide speech receipt have no freshness bound or current-boot
check. An unconsumed event or unspoken scan stage can survive a restart and play
after connectivity has gone away. Thus setup speech proves a prior successful
Cloud response, not current connectivity. The inspected local Cloud service
updates `last_seen_at` during setup check-in. Tailscale and LAN discovery are
independent paths. No device or deployed Cloud evidence was fetched, so the
reported outage is not diagnosed from source alone.

A proposed follow-up is to persist the latest successful setup check-in's boot
ID and monotonic time and require a fresh same-boot observation before speaking
the online scan line, including replay. Let the existing watcher refresh the
proof; add no probe to the button/audio thread. Test stale events, restart and
reconnection before implementing this. F316 changes no runtime behavior.

## Cross-product review and companion work

Local Cloud source was inspected read-only; no deployed-state claim is made.
The assistant's instruction to press once and the Pi setup screens still match
the physical confirmation flow. The dashboard's Online status means Cloud
contact within 90 seconds, while the Wi-Fi setup page's Connected state proves
general internet access. Those meanings must remain distinct.

The inspected Cloud setup-checkin query filters live QR claims but does not
require `claimed_by_identity_id IS NOT NULL`; its legacy QR page can create a
claim before the WhatsApp message is sent. Cloud must ensure `pending_claim`
means a WhatsApp-bound claim before release, or the Pi cannot distinguish that
early QR claim using the existing payload. Verify/fix this in Cloud, with a
scan-without-WhatsApp regression test. No payload or identifier change is needed.
Assistant guidance must continue to request a fresh press after review and its
send question; early approval is deferred. It must not promise that optional
box speech was heard. No Cloud settings companion change is needed for the
timezone default.

Real-box acceptance remains required for normal approval timing, held playback
presses, audio level, claim handover, settings during setup, and Wi-Fi outage
reproduction. Physical wacli send, receive/play, reply, family-card, quiet-hours
and listened-notice checks remain required before a main release.
