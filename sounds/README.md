# Audio assets

The licensed `guided-reply` prompt set is included in this repository. It
contains these mono WAV files:

- `reply-countdown.wav`
- `standalone-countdown.wav`
- `press-to-send.wav`
- `delete-warning.wav`
- `not-sent.wav`

`scripts/provision.sh` and `scripts/setup.sh` use this directory by default. The
installer fails before making system changes when any file is missing,
unreadable, empty, invalid, or not mono. An alternate licensed set can still be
passed to `scripts/provision.sh` with `--guided-prompts DIR`.

See [`guided-reply/LICENSE.md`](guided-reply/LICENSE.md) for the prompt
transcripts, production details, checksums, and separate asset-license notice.
Tests and installation must fail truthfully when a feature requires a missing
prompt; they must not silently substitute another system voice.
