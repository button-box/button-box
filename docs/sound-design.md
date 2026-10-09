# Button Box sound design v1

The sound pack has 16 motif cues and 19 Jessica lines. All output uses the saved
master volume. "Swoosh sound", "Family confirmation beep" and every setting ID
retain their existing names and meanings. Both talk modes use a start cue and optional playback review, without
countdown, review, approval or deletion voices.

| Brief moment | Implementation |
| --- | --- |
| Press, guided acknowledgement, claim press | `button_send.CUES`, `beep`, `acknowledge_guided_press`, `play_claim_cue`: `cue-press.wav`, exactly 0.40 s; unchanged `MIN_HOLD_S`. |
| Family confirmation / setup card read | `_play_nfc_prompt`, `onboarding.nfc.TonePlayer("read")`: `cue-card.wav`; Family confirmation beep gates the runtime cue. Runtime taps then play the cached name invitation or current pack's generic `voice-card-prompt.wav` when `card_name_prompt` is enabled. |
| Family card saved | Runtime `enrolled` handoff and `TonePlayer("success")`: `cue-card_saved.wav` then current pack's `voice-card-saved.wav`, independently of tap feedback settings. |
| Changed Cloud settings applied | `maybe_play_cloud_sound`: `cue-card_saved.wav` at the new master volume; three-second debounce, 30-second expiry, silent on boot adoption and in quiet hours. |
| Runtime awake | `announce_runtime_ready`: `cue-ready.wav`. |
| Failure beep | All `beep("fail")` / claim failure paths: `cue-oops.wav`. |
| Online | `play_setup_online`, `maybe_play_connectivity`: connected cue followed by first-ever online voice, durable receipt before the voice. |
| Accepted send | `play_send_success_cue`: `cue-sent.wav`; independent sender, interruptible on press, unchanged setting and no delivery claim. |
| Recording cancellation / too short / silent | `GuidedSession`: delete the recording and play `cue-deleted.wav`. Review allows one fresh tap in the 10 s window after playback. |
| Card needed | `prompt_for_token`: `voice-card-needed.wav`. |
| Unknown card | `nfc.Announcer`, `_play_nfc_prompt`: oops then card-unknown voice (or explicit operator override), acknowledged once per presentation. |
| Listened / listener announcement test | `play_pending_listened`: listened cue then saved family clip or `voice-listened.wav`; dashboard receipt ingestion uses the new default. All receipt playback shares this path. |
| Every incoming message starts | Guided, legacy and unroutable-but-authorized paths play `cue-msg_start.wav` then family audio. `voice-msg-start.wav` remains bundled for voice previews only. |
| Last waiting message ends | Guided last-message `incoming_end_path`, legacy last-message path: msg_end before an invited reply. |
| Microphone opens after start cue | `capture_guided_recording`: synchronous rec_go playback completes before `arecord` is created. Cue failure never opens capture. |
| Recording limit approaches | `RecordingLimitCue` in both capture modes: rec_limit at limit minus 5 s, nonblocking playback; it stays in the recording (Dan, 7 Oct: no words are cut). |
| Review starts | `GuidedSession` plays the child recording, discarding presses during playback, then waits for a fresh tap. |
| Short or silent capture in either mode | `guided_recording_empty`: delete and play the deletion cue. No outgoing job is created. |
| Three send failures | `sender_loop` counts by current message in both modes, queues a notice; idle `maybe_play_still_trying` checks job still pending, then still_trying + stuck voice once durably per job. |
| Recipient unavailable | `block_unavailable_recipient`: oops + fail voice when no default/family is available; card-selection asks for a card, and unknown-card routing stays fail-closed. |
| Setup finishes | Root `onboarding.completion` queues a sound request after successful handoff; idle `announce_all_set` plays all_set + all-set voice once per box. |
| Cloud unreachable | `maybe_play_connectivity` reads existing current-boot snapshot verification age; offline once after 180 s, resets on freshness, durable outage receipt. No additional network probes. |

## Recording rules

The go tick (rec_go) plays to completion before the microphone opens, so it is
never recorded. The 5 s limit warning (rec_limit) plays while capture continues
and stays in the recording as two soft taps: removing it would also remove the
child's words that overlap it (Dan, 7 October 2026). The hardware acceptance
check confirms the go tick is absent from sent audio.

A press during the card invitation stops and reaps its player before the start
cue. It skips hold classification, while still waiting for rec_go to end before
opening the microphone. Hold to talk ends on release; tap to talk ignores the
starting press until release and ends on a later tap. Without that tap before
the recording limit, tap to talk deletes the recording. Both modes reject
recordings shorter than 1.5 seconds (measured before silence trimming) and
recordings without detected voice. The same card mapping is revalidated before
capture; a changed or expired selection never falls back to another person.

Incoming audio in both modes uses
`highpass=f=100,equalizer=f=2500:t=q:w=1:g=2,loudnorm=I=-16:TP=-2:LRA=11:linear=true,alimiter=limit=0.79:level=disabled`.
`MSGBOX_EQ_FILTER` still overrides the chain, including an empty disable value.

## Quiet hours and first-use receipts

Unsolicited listened, still-trying, offline, connected and setup-complete audio
waits until quiet hours end. Runtime awake is suppressed in quiet hours. Prompt,
card and press responses still sound. Recording, guided sessions and an active
press defer idle notifications. The sender only queues notifications; it never
owns the speaker. Listened receipts remain pending during quiet hours. Persisted receipts naming
the former bundled default are explicitly mapped to the new default; a missing
custom family clip still reports unavailable and stays pending.

`/var/lib/messagebox/state/sound-state.json` is private, locked and atomically
written. First-use receipts are written before playback to prevent replay after
a crash, including a crash during audio playback; audio failure is reported and
is not proof that a child heard the line. Online voice, all-set and each stuck
message play at most once. Legacy voice assets and receipts remain installed for release compatibility.

## Installation and acceptance

Setup, provision and bounded-update preflight validate format, complete PCM,
manifest filenames, SHA-256 and exact press duration. Startup requires Jessica and cues; optional packs can fall back to Jessica;
claim confirmation remains available after a truthful asset warning. No runtime
synthesis or replacement system voice is used. The bounded updater includes
all bundled WAVs, manifests, READMEs and cue catalog, including the six optional
voice choices in `sounds/voices/` (required in a complete release). It retires only the five old
bundled prompts and old swoosh, rejecting symlinks/hardlinks, recording their
program rollback state and restoring them on failure or explicit rollback.

See [testing](testing.md#sound-design-v1) for offline and physical acceptance.
The Cloud assistant and Cloud dashboard/Flow Atlas are maintained separately;
no old spoken prompt quotes were found in the Pi setup screens. The local setup page offers Tap to talk, Hold to talk and the independent
"Let them hear it before sending" switch. Historical release notes describe their original
releases and are not current sound instructions.


## Voice pack acceptance

Installation and bounded preflight require all seven voice sets to validate.
Capabilities exclude any incomplete or corrupt optional set; missing choices
do not prevent a runtime restart. Jessica is the
per-file runtime fallback, and the originals remain in `sounds/voice/`.
See [pack licensing](../sounds/voices/README.md) and
[settings behavior](cloud-settings.md#voice-packs).

Start-cue playback is synchronous in both modes. Recording time starts after
the cue ends. Legacy countdown and approval voice assets remain bundled but
are no longer used by the recording flow.

On a real box, listen to Jessica, Pirate, Alien, DJ, Robot, French and Charlie on
the speaker. For each, preview and switch the setting; confirm the sample, volume
and dark lamp. Exercise all four talk/review combinations and the go tick,
checking that no start cue is captured and the deletion cue is clear. Verify a matched Cloud
name clip and a queued clip after changing packs, plus Robot/DJ fixed listened
lines. Confirm volume-only and ringtone-only previews retain their behavior.
Offline checks do not establish speaker quality or physical microphone timing.
