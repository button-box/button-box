# Button Box sound design v1

- [`cues/README.md`](cues/README.md): 15 original motif cues and their checksums.
- [`voice/README.md`](voice/README.md): 17 ElevenLabs Jessica lines, transcripts,
  checksums and commercial-use terms.
- [`ringtones/README.md`](ringtones/README.md): the seven ringtone choices.

Setup and provisioning validate the bundled packs before system or SSH changes.
Every cue/voice WAV must be complete 48 kHz, 16-bit mono PCM with its declared
SHA-256; the press cue must be exactly 0.40 seconds. Missing or invalid assets
fail truthfully. No runtime sine generation or system voice substitution exists.

The "Swoosh sound" setting controls the 1.3-second `cue-sent.wav`. It still means
accepted for sending, not delivery. Send work continues independently of speaker
playback; the cue stops on a press, and stale success notices are discarded.
See [`docs/sound-design.md`](../docs/sound-design.md) for runtime behavior,
recording exclusions, quiet hours and real-speaker acceptance checks.
