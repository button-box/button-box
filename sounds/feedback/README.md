# Send confirmation

`sent-swoosh.wav` is an original, synthesized 1.25 second airy rising swoosh
with a playful three-note tail, 48 kHz,
16-bit mono PCM. It contains no sampled Apple or other third-party audio.
It is distributed under the repository's source-code license. Reproduce it with
`python3 scripts/dev/generate-send-swoosh.py`.

The button service queues this cue only after WhatsApp's send command succeeds
and local outbox completion succeeds, for both guided and hold/release modes.
It means accepted for sending, not delivered or listened to. The old immediate
hold/release "sent" beep at outbox enqueue is removed.

The main button loop plays the cue when idle, using the caregiver's current
speaker volume. It does not interrupt recording, prompts, or incoming playback.
Cues older than 30 seconds are discarded to avoid confusing them with a newer
recording. Pending cues are transient and are not replayed after restart.
A missing asset or playback failure does not retry the WhatsApp message.

Before release, check volume and perceived meaning on the actual speaker,
both recording modes, failed/offline sends, and sends completing during playback.
