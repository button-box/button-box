# Button Box cues

17 motif cues. Original compositions and generator code are covered by the repository source license.
The loudest 100 ms is matched to -13 dBFS RMS, with peak <= -1 dBFS.
Regenerate with `python3 scripts/dev/make_cues.py OUT_DIR` (developer-only NumPy/SciPy dependencies).

All files are 48 kHz, 16-bit, mono PCM WAV. `manifest.json` records every filename,
SHA-256 checksum, duration, and meaning. Installation rejects missing, empty,
stereo, truncated, wrong-format or checksum-mismatched files. There is no synthesis fallback.
`cue-press.wav` contains exactly 19,200 frames (0.40 s). `cues.json` is the compact cue catalog.

## v1.1 taps (2026-10-07)

card, press, oops and rec_go were replaced with louder v1.1 versions (denser chord plus a wooden click in 1.8–3.3 kHz, levelled through a small-speaker model). Dan approved them by ear on box 4. press stays exactly 0.40 s; rec_go is 0.32 s and still plays fully before the microphone opens. Generator: make_cues_v11.py (sound design session).

## Sequential Button Box recording start

`cue-rec_start.wav` is 270 ms: 7,200 zero-valued frames (150 ms), then the
first 5,760 original PCM frames (120 ms) of `cue-rec_go.wav`. Its format and
tick samples are unchanged. Cloud Button Box tap/card starts play this entire
asset before opening the microphone, with no extra playback pre-roll.
The leading silence protects the tick from USB speaker wake-up loss.
Normal hold classification keeps its original unpadded 120 ms press clip;
other voices and wacli retain `cue-rec_go.wav`. Neither original asset changed.
The asset contract test checks the padding, exact tick samples and played length.

## Deletion cue

`cue-deleted.wav` is a 0.40 s descending G5/E5 tone with rounded attacks.
Its loudest 100 ms matches the bundled `cue-oops.wav`; it uses a gentler timbre.
Regenerate only this asset and its catalog/manifest entry with
`python3 scripts/dev/make_deleted_cue.py`. The existing cue generator predates
the approved tap replacements; do not regenerate those assets for this change.

SHA-256: `e95cf8bfd2f53507adeccdf21c6f0096aafb2a5710bb0c27732469f7773c1894`.
Speaker acceptance is still required.
