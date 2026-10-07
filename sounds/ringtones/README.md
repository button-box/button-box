# Ringtones

Seven original compositions, repository license. The default is `hello_piano`.
All WAVs are 48 kHz, 16-bit mono PCM. The approved masters are normalized to
−15 LUFS integrated with peak ≤ −1 dBFS. Each `<id>.lamp.json` contains timed
`lamp_on` spans in seconds; the lamp lights for 0.14 seconds per note onset.

| ID | Picker label | Approximate length | WAV SHA-256 |
| --- | --- | --- | --- |
| `hello_piano` | Hello (piano) | 14.1 s | `d5c53d091066567967731d768cc7f5553e57e0ae42a9993fa9e06019c053a830` |
| `sunshine` | Sunshine | 15.5 s | `22d08cbd174449872f26c5f58d0910244a4e67307cfcabb64661d9776fac2471` |
| `bouncy` | Bouncy | 15.1 s | `02bd3e24d2faff181b75cb63b5e51e3d73b6ad2fe3715c4aea1875f51cbc7b34` |
| `sing_along` | Sing-along | 14.2 s | `ae5a0f3ce1dc81c1e076f3d41338bc98bdd981e0b671e4ee372304c8d8f96e5f` |
| `island` | Island | 13.4 s | `3f18590e1b8cbc11dc5e89f89e65e88d820ddd1984c711bf4578614c6f03d004` |
| `hello` | Hello (short) | 9.2 s | `96a5f9ddb665401e401236abfeb18d43a5f1e5a2b2afcfe283e346f3bd9f6792` |
| `ukulele` | Ukulele | 9.0 s | `4673f77b16bf07740608447002fd8da81abcca20c29233c4b483ba9ca06fd7fb` |

Setup and provisioning validate the complete pack before installation. WAVs,
lamp schedules and the manifest install under `/opt/messagebox/ringtones/`
with root ownership and mode 0644. Bounded releases install the same files,
including on boxes previously using generated ringtones. MIDI and generator
sources are developer assets and are excluded from bounded runtime releases.

Saved settings and Cloud commands using any of the four legacy ringtone IDs
map to `hello_piano`; other settings and revisions are preserved. A missing or
invalid lamp schedule uses the previous blinking pattern. Ring playback remains
interruptible by a button press. Preview deadlines allow at least 17 seconds;
local setup/dashboard speaker commands retain their 30-second timeout.

The retained generators are in `scripts/dev/make_ringtones_v2.py`,
`make_ringtones_melodic.py` and `make_ringtones_piano.py`; MIDI sources are in
`midi/`. Generators use NumPy/SciPy (and mido for MIDI), output working renders
under their original `ring-*` names, and include unused composition variants.
They do not regenerate the normalized masters or lamp schedules byte for byte.
The bundled masters and `manifest.json` are the installation source of truth.

Before release, test every choice in the local and Cloud dashboards on a real
box: full playback, lamp timing, and immediate press interruption. Test the
WhatsApp assistant with “change the ringtone to sunshine”. Also test an old
software box with the Cloud legacy choices. The coordinating Cloud change owns
its assistant/dashboard vocabulary and Flow Atlas update.
