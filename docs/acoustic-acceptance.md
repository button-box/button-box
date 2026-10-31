# Acknowledgement acoustic acceptance

Sound design v1 uses `sounds/cues/cue-press.wav` (exactly 0.40 s) for button
acknowledgement, `cue-card.wav` for setup/runtime card reads and
`cue-card_saved.wav` for setup success. These are distinct motif cues. The
bundled cue manifest records checksums, durations and loudness targets; there
is no generated-tone refresh. NFC debouncing and caregiver settings stay intact.

Software playback success is not acoustic acceptance. For each named box,
record a bounded local 16-bit mono PCM WAV from the agreed room microphone:
three quiet seconds, exactly one physical action, then two quiet seconds.
Keep recording under 60 seconds. Avoid unrelated conversation, keep room audio
private, and do not run another playback test during a child's recording.

Compare the capture with the exact installed reference WAV. Check one audible
cue at the physical action, comfortable loudness above room noise, no clipping
or duplicated cue, and correct cue identity. Check setup detection and normal
runtime scans separately. Hold a card in place for several seconds: one cue.
Remove/re-present it: one fresh cue. Scan a different card: one fresh cue and
the correct recipient. Check start/stop/approval acknowledgements separately
from accepted-send feedback and at low/normal/high master volume.

`scripts/dev/check-acoustic-cue.py` remains a single-frequency experimental
tone analyzer. Its frequency/duration test does not certify these motif cues;
use the installed references and physical listening for v1 acceptance.

For recording, verify the go tick is absent from the sent audio and the limit
warning's entire exclusion window is silent. Check acoustic decay and ALSA
buffering against the 250 ms guard on both boxes. See
[sound design](sound-design.md#recording-exclusions) and
[testing](testing.md#sound-design-v1) for the full acceptance sequence.

Retain only sanitized measurements, microphone/placement, candidate revision
and action time. Delete temporary room audio after measurement. Synthetic PCM
and unit tests do not prove physical speaker acceptance; if capture is
unavailable, leave that gate explicitly untested.
