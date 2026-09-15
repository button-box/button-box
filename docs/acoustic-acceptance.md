# Acknowledgement acoustic acceptance

The press cue is 880 Hz for 400 ms; NFC detection is 1760 Hz for 280 ms
in setup and runtime. Generated assets are refreshed/versioned so an update
cannot silently keep the old quiet cue. Existing NFC presentation debouncing
and caregiver sound settings remain intact.

Software playback success is not acoustic acceptance. For each named box,
record a bounded local **16-bit mono PCM WAV** from the agreed room microphone:
three quiet seconds, exactly one physical action, then two quiet seconds.
Keep recording under 60 seconds. Do not record unrelated conversation or upload
room audio. Do not run another audio test while the box is recording or playing.

Run the deterministic check on that capture:

```sh
python3 scripts/dev/check-acoustic-cue.py capture.wav --frequency 880 --minimum .32 --maximum .48
python3 scripts/dev/check-acoustic-cue.py nfc-capture.wav --frequency 1760 --minimum .22 --maximum .36
```

The check requires one sustained cue with the expected frequency and duration,
at least 10 dB above the baseline, and under 1% clipped samples. It rejects
duplicate cues, silence, wrong pitch, short blips and a contaminated baseline.
This is a controlled-test threshold, not a calibrated loudness measurement or
proof that a similar sound from elsewhere was emitted by this box. Correlate
the physical action and device event timestamp; independently listen for comfort
and clarity on the actual speaker.

Check setup detection and normal runtime scans separately. Hold a card in place
for several seconds: one cue only. Remove/re-present it: one fresh cue. Scan a
different card: one fresh cue and the correct recipient. Check a press to start,
stop and approve recording separately from successful-send feedback.

Retain only sanitized measurements, microphone/placement, revision and action
time in the run record. Stop the microphone and delete the temporary room WAV
after measurement. No physical or acoustic pass may be inferred from synthetic
unit tests; if capture is unavailable, mark that gate untested.
