# Button Box sound design v1

The sound pack has 17 motif cues, 21 Jessica lines and 20 lines in each other
pack. The setup handoff line is Jessica-only. All output uses the saved
master volume. "Swoosh sound", "Family confirmation beep" and every setting ID
retain their existing names and meanings. Cloud boxes with the Button Box voice
use spoken review, approval, empty and deletion lines in normal use. Other packs
and wacli keep their existing cue and review behavior. No Cloud voice request is
needed for these bundled lines. Ordinary speech discards held presses; approval
requires a fresh press after the prompt.

| Brief moment | Implementation |
| --- | --- |
| Press, guided acknowledgement, claim press | `button_send.CUES`, `beep`, `acknowledge_guided_press`, `play_claim_cue`: `cue-press.wav`, exactly 0.40 s; unchanged `MIN_HOLD_S`. |
| Family confirmation / setup card read | `_play_nfc_prompt`, `onboarding.nfc.TonePlayer("read")`: `cue-card.wav`; Family confirmation beep gates the runtime cue. Runtime taps then play the cached name invitation or current pack's generic `voice-card-prompt.wav` when `card_name_prompt` is enabled. |
| Family card saved | Runtime `enrolled` handoff and `TonePlayer("success")`: `cue-card_saved.wav` then current pack's `voice-card-saved.wav`, independently of tap feedback settings. |
| Changed Cloud settings applied | `maybe_play_cloud_sound`: `voice-setting-saved.wav` at the new master volume; three-second debounce, 30-second expiry, silent during guided setup, on boot adoption and in quiet hours. Settings still apply and ACK while setup is active. |
| Runtime awake | `announce_runtime_ready`: `cue-ready.wav`. |
| Failure beep | All `beep("fail")` / claim failure paths: `cue-oops.wav`. |
| Online | Cloud `play_setup_online`: setup scan instruction once after an authenticated setup check-in; the press instruction waits for a pending claim. Claimed `maybe_play_connectivity`: connected cue then current pack's online-ready line after boot or after an outage of at least ten minutes, at most once per hour. Shorter heartbeat gaps stay silent. Durable receipts precede playback. Legacy online voice stays bundled for wacli. |
| Accepted send | `play_send_success_cue`: optional `cue-sent.wav`, then Cloud Button Box `voice-setup-sent.wav` ("Sent!"). The voice is independent of the swoosh setting. It confirms Cloud acceptance, not delivery. Quiet controls and a press suppress idle playback. |
| Send starts | Cloud Button Box approval wakes the existing sender. The approval beep is nonblocking. After preflight, a live upload gets `voice-sending.wav` after a 150 ms immediate-failure check. The released-press guard can play it before its normal 500 ms settle ends. A rejection, already completed upload, quiet state, changed account, old approval, or background retry does not play it. Normal hold release without review uses the same durable approval event. |
| Recording cancellation / too short / silent | `GuidedSession`: delete the recording and play `cue-deleted.wav`. Review allows one fresh tap in the 10 s window after playback. |
| Card needed | `prompt_for_token`: `voice-card-needed.wav`. |
| Unknown card | `nfc.Announcer`, `_play_nfc_prompt`: oops then the current pack's card-unknown voice, resolved on each tap so a voice pack change applies at once (or explicit operator override), acknowledged once per presentation. |
| Listened / listener announcement test | Listened cue, then cached family/Cloud voice. Missing, evicted, wrong-pack or failed Cloud clips use the bundled voice: Button Box `voice-listened-noname.wav` ("Your message was heard!"), otherwise `voice-listened.wav`. Failures log only the exception type. Mute still suppresses fallback. wacli family clips and fallback stay unchanged. |
| Every incoming message starts | Guided, legacy and unroutable-but-authorized paths play `cue-msg_start.wav` then family audio. With the Cloud setting `announce_messages_voice` on and outside quiet controls, the current pack's `voice-msg-start.wav` plays between the cue and the message. It is also used for voice previews. Then, on Cloud boxes, the message age phrase (`voice-age-<key>.wav`, for example "This one's from last night!") plays when the message is old enough; see `docs/architecture.md`. wacli plays the cue only. |
| Last waiting message ends | Guided last-message `incoming_end_path`, legacy last-message path: msg_end before an invited reply. |
| Recording starts | Cloud Button Box tap/card start: lamp on, finish cue-rec_start (150 ms silence + the existing 120 ms rec_go tick), then open the microphone. Nominal cue delay is 270 ms. Normal hold keeps the original 120 ms press cue and 400 ms gesture check, without a second go cue. Other voices and wacli keep synchronous rec_go before capture. |
| Recording limit approaches | Cloud Button Box: the bundled stop reminder at 40 s for a 60 s capture, including normal standalone recordings. Other voices and wacli keep rec_limit at limit minus 5 s. Playback is nonblocking and stays in the recording. |
| Review starts | Cloud Button Box plays `voice-review.wav` ("Listen!"), the recording, then a rotating `voice-ask-send-1/2/3.wav` (for example "Love it! Press to send."). Other modes play the recording only. All require a fresh approval tap. |
| Short or silent capture in either mode | Delete and play the deletion cue. Cloud Button Box adds `voice-empty.wav`; declined reviews add `voice-not-sent.wav`. The capture limit always offers review of meaningful audio; Cloud Button Box then uses the full `voice-setup-ask-send.wav` instruction, including waiting to delete. Only a fresh approval creates an outgoing job. |
| Three send failures | `sender_loop` counts by current message in both modes, queues a notice; idle `maybe_play_still_trying` checks job still pending, then still_trying + stuck voice once durably per job. |
| Recipient unavailable | `block_unavailable_recipient`: oops + fail voice when no default/family is available; card-selection asks for a card, and unknown-card routing stays fail-closed. |
| Setup finishes | Root `onboarding.completion` queues a sound request after successful handoff; idle `announce_all_set` plays all_set + all-set voice once per box. |
| Cloud unreachable | `maybe_play_connectivity` reads existing current-boot snapshot verification age; offline once after 10 minutes without a good heartbeat, resets on freshness, durable outage receipt. No additional network probes. |

## Recording rules

The bundled `sounds/voices/buttonbox/voice-sending.wav` is the selected ElevenLabs
take; see [Sending voice asset](sound-design/voices/PTh0NF64AaTAIDPGzj0O/README.md).
All shipped voice clips have at least 100 ms before the first sample above 2%
of peak amplitude. Clips below that threshold receive only prepended PCM silence
to reach 150 ms, retaining their original samples, format and metadata. This
asset padding is additional to the shared playback pre-roll.

Cloud Button Box finishes its start cue before opening the microphone.
For tap and card starts, `cue-rec_start.wav` contains 150 ms of silence followed
by the same 120 ms tick previously played from `cue-rec_go.wav`. The clip limit
covers the complete 270 ms asset, with no additional playback pre-roll. This
raises the nominal cue delay from 120 ms to 270 ms so the child hears the beep
before talking. Actual device/startup latency still needs physical measurement.
Nonzero playback exits or a five-second timeout prevent capture; stalled players
are terminated and reaped. Capture and VAD have no start-cue overlap or masking.
Normal hold classification keeps the original unpadded 120 ms press cue and
400 ms gesture check. Other voices and wacli keep their original synchronous
`rec_go` asset and playback path.
The 5 s limit warning (rec_limit) plays while capture continues
and stays in the recording as two soft taps: removing it would also remove the
child's words that overlap it (Dan, 7 October 2026). The hardware acceptance
check confirms the legacy go tick is absent from sent audio and that the
Cloud tick is audible before capture and words spoken just after it are kept.

A press during the card invitation stops and reaps its player before the start
cue. It skips hold classification. Cloud Button Box waits for its padded
rec_start; other voices and wacli wait for rec_go. Hold to talk ends on release; tap to talk ignores the
starting press until release and ends on a later tap. At the recording limit,
both talk modes stop and keep meaningful audio for review,
even when review is normally disabled. A fresh approval sends it; ten seconds
without approval deletes it. Both transports use this rule. Both modes reject
recordings shorter than 1.5 seconds (measured before silence trimming) and
recordings without detected voice. The same card mapping is revalidated before
capture; a changed or expired selection never falls back to another person.

Incoming audio in both modes uses
`highpass=f=100,equalizer=f=2500:t=q:w=1:g=2,loudnorm=I=-16:TP=-2:LRA=11:linear=true,alimiter=limit=0.79:level=disabled`.
`MSGBOX_EQ_FILTER` still overrides the chain, including an empty disable value.

## Quiet hours and first-use receipts

Unsolicited listened, still-trying, offline and setup-complete audio
waits until quiet hours end. Cloud runtime connected transitions during quiet
hours or mute are consumed silently. Setup handoff waits until quiet hours end.
Runtime awake is suppressed in quiet hours. Prompt,
card and press responses still sound. Recording, guided sessions and an active
press defer idle notifications. The sender only queues notifications; it never
owns the speaker. Listened receipts remain pending during quiet hours. Persisted receipts naming
the former bundled default are explicitly mapped to the new default; a missing
custom family clip still reports unavailable and stays pending.

`/var/lib/messagebox/state/sound-state.json` is private, locked and atomically
written. First-use receipts are written before playback to prevent replay after
a crash, including a crash during audio playback; audio failure is reported and
is not proof that a child heard the line. Setup voice, all-set and each stuck
message play at most once. Runtime online edges retain their boot ID and last
playback time; same-boot rate limits use monotonic time, and reboots use wall time
(a backwards wall clock suppresses the announcement). Legacy voice assets and
receipts remain installed for release compatibility.

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

Installation and bounded preflight require all nine voice sets to validate.
Capabilities exclude any incomplete or corrupt optional set; missing choices
do not prevent a runtime restart. Jessica is the
per-file runtime fallback, and the originals remain in `sounds/voice/`.
See [pack licensing](../sounds/voices/README.md) and
[settings behavior](cloud-settings.md#voice-packs).

Recording time starts when the microphone opens, after successful start-cue
completion. All modes use a sequential start. Legacy countdown and approval voice assets remain bundled but
are no longer used by the recording flow.

On a real box, listen to Jessica, Pirate, Alien, DJ, Robot, French and Charlie on
the speaker. For each, preview and switch the setting; confirm the sample, volume
and dark lamp. Exercise all four talk/review combinations and the go tick,
checking that the start cue is audible and excluded from capture in every mode,
and that words spoken just after it are kept. Confirm the deletion cue is clear. Verify a matched Cloud
name clip and a queued clip after changing packs, plus Robot/DJ fixed listened
lines. Confirm that volume and ringtone changes each say "Setting saved!" once.
Offline checks do not establish speaker quality or physical microphone timing.

With `announce_messages_voice=false`, arrival has a ringtone/lamp and playback
has the message cues; no new-message voice is expected. With `after_listening=play_only`,
normal playback ends without recording. The first setup note still speaks
`setup-your-turn` and waits for a separate press.
