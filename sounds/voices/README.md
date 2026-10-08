# Button Box voice packs

Pirate, Alien, DJ, Robot, French and Charlie each contain 17 ElevenLabs lines,
generated on a paid plan with `eleven_v3`, seed 7. Jessica remains in `../voice/`.
Each manifest retains the generation metadata, transcript and SHA-256 of every
shipped recording, including DJ's music bed and Robot's post effects.

Commercial use is under ElevenLabs terms; these recordings are not covered by
the repository source licence. As recorded for Jessica on 7 October 2026, paid
plans include a commercial licence except for Beta Services, and Eleven v3 is
generally available ([licence](https://help.elevenlabs.io/hc/en-us/articles/13313564601361),
[v3 GA](https://elevenlabs.io/blog/eleven-v3-is-now-generally-available)).

Charlie is a Default voice that expires for new generation on 31 December 2026;
existing files are unaffected ([Default voices](https://help.elevenlabs.io/hc/en-us/articles/26942950589969)).

All files are 48 kHz, 16-bit, mono PCM WAV, normalized to -16 LUFS with true peak
at most -1.5 dBTP. Installation validates the complete filename set, PCM data and
checksums for every pack. Runtime falls back to Jessica per missing file, logging
each missing line once. Capabilities list only complete, validated packs, with
Jessica always included. No audio is synthesized on the box.

## tata (Français · Tata)

Native French woman voice; all 17 lines in French (language fr). ElevenLabs voice yN0lwsGSD3mAgqilMvDS (saved), eleven_v3, seed 7, loudnorm I=-16. "carte famille" is the French for family card.
