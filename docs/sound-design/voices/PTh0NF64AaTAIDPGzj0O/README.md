# Sending voice asset

`voice-sending.wav` says **Sending!** It is a temporary local-TTS placeholder,
not the Button Box ElevenLabs voice. The local generator was macOS `say`,
Samantha, 180 words per minute; `espeak-ng` was not installed on the build host.
The output was normalized with `loudnorm=I=-16:TP=-1.5:LRA=11` and exported as
48 kHz, mono, 16-bit PCM WAV. Measured loudness is -15.99 LUFS.

Replace the placeholder with a generated **Sending!** line using:

- Voice: `PTh0NF64AaTAIDPGzj0O`
- Model: `eleven_v3`
- Seed: `7`
- Post-processing: `loudnorm=I=-16:TP=-1.5:LRA=11`
- Output: 48 kHz, mono, 16-bit PCM WAV

Replace both copies with identical bytes:

1. `docs/sound-design/voices/PTh0NF64AaTAIDPGzj0O/voice-sending.wav`
2. `sounds/voices/buttonbox/voice-sending.wav`

Update the `voice-sending.wav` row's `sha256` and `seconds` in each adjacent
`manifest.json`. Set `temporary_placeholder` to `false`, including the source
manifest's top-level flag, and change the row's `source` to `ElevenLabs`.
Keep the generation settings in the manifest. Run `make check` in both
transports after replacement. Check the line on the physical USB speaker.

No ElevenLabs request was used to make this placeholder.
