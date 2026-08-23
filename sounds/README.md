# Audio assets

No voice recordings are included because the experimental system-voice files
do not have sufficiently documented redistribution rights.

Installation requires a licensed `guided-reply` directory containing these
mono WAV files:

- `reply-countdown.wav`
- `standalone-countdown.wav`
- `press-to-send.wav`
- `delete-warning.wav`
- `not-sent.wav`

Pass that directory to `scripts/provision.sh` with `--guided-prompts DIR`, or
place it at `sounds/guided-reply/` before running `scripts/setup.sh` locally on
the Pi. The installer fails before making system changes when any file is
missing, unreadable, empty, invalid, or not mono.

Before adding audio, record the prompt transcript, performer or generator,
consent and redistribution terms, technical format, checksum, and artifact
license. Tests and installation must fail truthfully when a feature requires a
missing prompt; they must not silently substitute an unlicensed system voice.
