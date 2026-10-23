# Sending voice asset

`voice-sending.wav` says **Sending!** in the Button Box voice (ElevenLabs).
Rendered 22 Oct 2026 with:

- Voice: `PTh0NF64AaTAIDPGzj0O`
- Model: `eleven_v3`, seed `7`, text `[cheerfully] Sending!`
- Post-processing: leading/trailing silence trim, `loudnorm=I=-16:TP=-1.5:LRA=11`,
  then prepend silence to put the first sample above 2% of peak at 150 ms
- Output: 48 kHz, mono, 16-bit PCM WAV, approximately 1.19 s

Both copies have identical bytes:

1. `docs/sound-design/voices/PTh0NF64AaTAIDPGzj0O/voice-sending.wav`
2. `sounds/voices/buttonbox/voice-sending.wav`

Dan chose this take (22 Oct). Check the line on the physical USB speaker.
