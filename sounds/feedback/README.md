# Send confirmation

`sent-swoosh.wav` is an original, synthesized three-second paper-plane swoosh:
a soft intake, sustained airy acceleration and a playful three-note landing, 48 kHz,
16-bit mono PCM. It contains no sampled Apple or other third-party audio.
It is distributed under the repository's source-code license. Reproduce it with
`python3 scripts/dev/generate-send-swoosh.py`.
The signal is band-limited and normalized to at most -6 dBFS peak, with smooth
endpoints and an audible tail rather than silence padding. This limits digital
peaks; perceived loudness and comfort still need an actual-speaker check.

The button service queues this cue only after WhatsApp's send command succeeds
and local outbox completion succeeds, for both guided and hold/release modes.
It means accepted for sending, not delivered or listened to. The old immediate
hold/release "sent" beep at outbox enqueue is removed.

The main button loop plays the cue when idle, using the caregiver's current
speaker volume. The sender worker never waits for the sound. A new button press
stops this cue and passes through the normal press debounce, without requiring
a second press. The player is stopped/reaped before the next audio operation;
a five-second watchdog bounds a stuck player. This cue does not interrupt
recording, prompts, or incoming playback. Their normal press handling is unchanged.
Cues older than 30 seconds are discarded to avoid confusing them with a newer
recording. Pending cues are transient and are not replayed after restart.
A missing asset or playback failure does not retry the WhatsApp message.

Before release, check volume and perceived meaning on the actual speaker,
both recording modes, failed/offline sends, and sends completing during playback.

## Candidate audition (after explicit test-box deployment approval)

1. Verify the exact candidate file hashes and an idle box, with no recording,
   prompt, or incoming playback active. Keep the existing release unchanged
   until that separate approval; a Mac preview is not a box-speaker pass.
2. Start a bounded room capture with three quiet baseline seconds. Send one
   synthetic test reply to the authorized test chat using the normal flow.
3. Let the success swoosh finish: it should sound like one smooth, roughly
   three-second flight, with no harsh peak or repeated notification.
4. Repeat one authorized test send. During its swoosh, press the button once
   deliberately. The cue should stop promptly and that same press should begin
   the normal configured interaction; check that audio does not overlap and
   delivery was not delayed or duplicated. Finish/cancel the resulting test
   interaction normally, preserving queued messages.
5. Record actual phone delivery separately from the accepted-send cue. Stop
   capture and retain only sanitized measurements, then delete room audio.
