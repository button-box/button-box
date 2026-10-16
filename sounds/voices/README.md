# Button Box voice packs

Pirate, Alien, DJ, Robot, French and Charlie each contain 19 ElevenLabs lines,
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

Every pack includes `voice-online-ready.wav` for claimed Cloud reconnects.
`voice-setup-online.wav` is only in the default Jessica directory, because setup
always uses the default voice. Both are registered with PCM durations and
SHA-256 checksums in their manifests.

Every pack includes the B48 generic `voice-card-prompt.wav` and pairing
`voice-card-saved.wav`; transcripts match the source manifests by SHA-256.
Cloud name invitations use the same pack and are cached separately from listened
clips. Robot and DJ always use their bundled generic invitation with its effects.

## tata (Français · Tata)

Native French woman voice; the original 19 lines are in French (language fr).
The online-ready handoff added here uses the supplied English copy. ElevenLabs
voice yN0lwsGSD3mAgqilMvDS (saved), eleven_v3, seed 7, loudnorm I=-16.
"carte famille" is the French for family card.

## Reaction-card lines

Every pack, and Jessica in `../voice/`, has 13 `voice-sent-<key>.wav` lines such
as "You just sent a heart!", listed under `reaction_lines` in its manifest with
transcript, seed and SHA-256. All voices speak the same English text (Tata
included), `eleven_v3` with the same post chains. Most takes use seed 7; takes
that said the wrong word use the seed in the manifest. These lines are optional:
a pack without them still installs, and the box falls back to Jessica, then to
"You just sent a reaction!", then to the card cue.

## Message age phrases

Every pack, and Jessica in `../voice/`, has 12 `voice-age-<key>.wav` lines that
say when an old message was sent, such as "This one's from last night!". They
are listed under `age_lines` in its manifest with transcript, seed and SHA-256.
Each pack says them in its own character, in English (Tata included), with
`eleven_v3` and the same post chains; the full script is in
`docs/message-age-phrase-lines.md`. Most takes use seed 7; retakes use the seed
in the manifest. These lines are optional: a pack without them still installs,
the box falls back to Jessica, and without a Jessica line it says nothing.
