# Button Box sound design v1

The sound pack has 15 motif cues and 17 Jessica lines. All output uses the saved
master volume. "Swoosh sound", "Family confirmation beep" and every setting ID
retain their existing names and meanings. Hold and release has no countdown,
review or send-approval prompts.

| Brief moment | Implementation |
| --- | --- |
| Press, guided acknowledgement, claim press | `button_send.CUES`, `beep`, `acknowledge_guided_press`, `play_claim_cue`: `cue-press.wav`, exactly 0.40 s; unchanged `MIN_HOLD_S`. |
| Family confirmation / setup card read | `_play_nfc_prompt`, `onboarding.nfc.TonePlayer("read")`: `cue-card.wav`; saved Family confirmation beep setting still gates runtime confirmation. |
| Setup card saved | `TonePlayer("success")`: `cue-card_saved.wav`. |
| Changed Cloud settings applied | `maybe_play_cloud_sound`: `cue-card_saved.wav` at the new master volume; three-second debounce, 30-second expiry, silent on boot adoption and in quiet hours. |
| Runtime awake | `announce_runtime_ready`: `cue-ready.wav`. |
| Failure beep | All `beep("fail")` / claim failure paths: `cue-oops.wav`. |
| Online | `play_setup_online`, `maybe_play_connectivity`: connected cue followed by first-ever online voice, durable receipt before the voice. |
| Accepted send | `play_send_success_cue`: `cue-sent.wav`; independent sender, interruptible on press, unchanged setting and no delivery claim. |
| Guided countdown / approval / last chance / cancellation | `PROMPTS`, `PiGuidedIO.play_ordinary`, `sound_pack.next_send_prompt`: count-reply/new, ask-send-1/2/3 in durable rotation, last-chance, not-sent. Existing 10 s approval window and button handling stay. |
| Card needed | `prompt_for_token`: `voice-card-needed.wav`. |
| Unknown card | `nfc.Announcer`, `_play_nfc_prompt`: oops then card-unknown voice (or explicit operator override), acknowledged once per presentation. |
| Listened / listener announcement test | `play_pending_listened`: listened cue then saved family clip or `voice-listened.wav`; dashboard receipt ingestion uses the new default. All receipt playback shares this path. |
| Every incoming message starts | `GuidedSession.run` incoming intro paths; `play_next_legacy`; unroutable-but-authorized inbound path: msg_start then msg-start voice then family audio. |
| Last waiting message ends | Guided last-message `incoming_end_path`, legacy last-message path: msg_end before an invited reply countdown. |
| Microphone opens after countdown | `capture_guided_recording`: synchronous rec_go playback completes before `arecord` is created. Cue failure never opens capture. |
| Recording limit approaches | `RecordingLimitCue` in both capture modes: rec_limit at limit minus 5 s, nonblocking playback; it stays in the recording (Dan, 7 Oct: no words are cut). |
| Review starts | `PiGuidedIO.play_review_for_approval`: review voice then child recording. |
| Silent guided capture | `run_guided_once.session_event("guided_recording_empty")`: oops then empty voice. No outgoing job is created. |
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
message play at most once. The ask-send take counter survives restart.

## Installation and acceptance

Setup, provision and bounded-update preflight validate format, complete PCM,
manifest filenames, SHA-256 and exact press duration. Startup requires the packs;
claim confirmation remains available after a truthful asset warning. No runtime
synthesis or replacement system voice is used. The bounded updater includes
all 32 WAVs, manifests, READMEs and cue catalog. It retires only the five old
bundled prompts and old swoosh, rejecting symlinks/hardlinks, recording their
program rollback state and restoring them on failure or explicit rollback.

See [testing](testing.md#sound-design-v1) for offline and physical acceptance.
The Cloud assistant and Cloud dashboard/Flow Atlas are maintained separately;
no old spoken prompt quotes were found in the Pi setup screens. Their setting
labels remain unchanged. Historical release notes describe their original
releases and are not current sound instructions.
