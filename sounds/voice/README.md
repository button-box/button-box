# Button Box voice

19 Jessica voice lines, generated with ElevenLabs on a paid plan using `eleven_v3`, seed 7.
Commercial use is under ElevenLabs terms; these recordings are not covered by the repository source license.
Licence check (7 October 2026): paid ElevenLabs plans include a commercial licence, except for content made with Beta Services. Eleven v3 is generally available, not beta, and these lines were made on the paid plan, so they may ship ([licence](https://help.elevenlabs.io/hc/en-us/articles/13313564601361), [v3 GA](https://elevenlabs.io/blog/eleven-v3-is-now-generally-available)).

Jessica is an ElevenLabs "Default" voice. Default voices expire on 31 December 2026 ([source](https://help.elevenlabs.io/hc/en-us/articles/26942950589969)). After that date no new lines can be made in her voice; the files already made here are not affected.
Voice lines are normalized to -16 LUFS, true peak <= -1.5 dBTP.

All files are 48 kHz, 16-bit, mono PCM WAV. `manifest.json` records every filename,
SHA-256 checksum, duration, and transcript. Installation rejects missing, empty,
stereo, truncated, wrong-format or checksum-mismatched files. There is no synthesis fallback.

The B48 additions are `voice-card-prompt.wav` (generic recording invitation) and
`voice-card-saved.wav` (successful pairing), supplied on 9 October 2026. Their
transcripts were matched to the source manifests by SHA-256. Message playback
uses only `cue-msg_start.wav`; `voice-msg-start.wav` is retained for voice previews.
