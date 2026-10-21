# Recipient announcements

This feature applies to Cloud mode only. The strict boolean setting
`announce_recipient` defaults to `true`, including on field updates. wacli
recording, routing, playback, cards, quiet hours and listened notices do not
use this feature.

Field updates enable the setting by default. This is the product decision
from Dan on 21 October 2026. No fresh-provisioning marker is required.

The box reports `announce_recipient_v1: true` only when both local fallback
lines are valid for the selected voice pack. It also reports `voice_lines: 1`.
A missing fallback prevents this capability report and recipient speech.
There is no provisioning marker and no new inbox command kind.

The button service uses the recipient already passed to recording. It does
not select a card, default recipient or reply recipient again for speech.
Playback order is the recipient line, then `rec_go`, then microphone capture.
A press during a card prompt skips the recipient line. It uses the existing
start cue and capture path without another announcement or release wait.
For an accepted send, the order is the optional sent cue, then the named
`sent_recipient` line or the generic sent line. The durable sound request uses
the recipient stored in the outbox job. A missing or revoked subject uses the
generic line. An account change invalidates the old sound request.

Mute, volume zero and quiet controls suppress these voice lines. Taps during
the recording line have no action. A tap held through that line enters
capture. It must be released before a later press can stop capture. A press
during an idle sent line stops playback and returns to the button loop. The
same press can start recording.
Releasing hold mode during the recording line or start cue cancels capture.
Target a two-second line. Both downloaded and bundled lines have a four-second
limit. No request, download or synthesis runs at button-press time.

The poller preloads `recording_recipient` and `sent_recipient` with authenticated
`POST /device/voice-lines` requests. Each request contains exactly one of
`identity_id` and `link_id`. It uses the current pack only. A ready response
must match the requested template, subject kind and ID, account scope, pack,
and SHA-256 hash of the exact display name in the heartbeat snapshot. Invalid
responses use the local fallback and do not fail the heartbeat. Unknown
optional response fields are ignored.

Audio must be mono, 16-bit PCM WAV at 48 kHz, at most 384,044 bytes and four
seconds. The URL must use the exact HTTPS API origin and an allowed device
media path. The declared byte count and SHA-256 must match the downloaded
bytes. The cache checks the hash and audio format again before playback.

The separate cache is `/var/lib/messagebox-cloud/recipient-voice/`. It uses
`VoiceCache` with 300 files and a 128 MiB limit. This holds both templates
for all 150 contacts, including four-second clips. Its key contains account scope,
subject kind and ID, template, voice pack and name hash. The runtime state
stores retry times and audio hashes. One background worker handles optional
recipient and card preload. Each heartbeat cycle allows at most three jobs
and five seconds of total network time. Metadata and media use the same
absolute deadline. Late results are discarded. Heartbeat and inbox processing
do not wait for the worker. The poller checks the inbox before each next job.
Jobs rotate across contacts, templates and card lines. The default recipient
starts first. A failed request retries after 60 seconds; a valid line refreshes
after one hour. Eviction does not bypass these retry times.
The poller removes stale mappings and files after a name, contact, account or
pack change. It rechecks the snapshot after each download. Local lookup also
checks the current snapshot and pack, so stale audio cannot play before pruning.

## Bundled audio files

The 16 fallback WAVs are bundled in the existing directories below. Each pack
manifest records their exact text, duration and SHA-256 in `recipient_lines`.
The supplied audio bytes are preserved. `tata` uses French.
The recording line means “Recording!” and the sent line means “Sent!”.
Do not put a recipient name in either fallback.

| Existing pack directory | Required extra files |
| --- | --- |
| `sounds/voice/` (jessica) | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/pirate/` | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/alien/` | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/dj/` | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/robot/` | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/french/` | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/charlie/` | `voice-recording.wav`, `voice-sent.wav` |
| `sounds/voices/tata/` | `voice-recording.wav`, `voice-sent.wav` |

Each file must be mono, 16-bit PCM WAV at 48 kHz, have more than zero frames,
last at most four seconds, and be at most 2 MiB. Do not use symlinks. These are
optional extra files in existing pack directories. Do not add them to the
required `lines` manifest array. The release manifest includes extra WAVs and
the old updater checks their hashes. No updater change or new pack is needed.
There is no `buttonbox` pack in this field base.

## Validation and release checks

Run `make check`. The focused tests are in `tests/test_announce_recipient.py`.
The field test runs the local tagged v0.3.9, v0.3.10 and v0.3.12 updater code.
It installs the changed runtime modules and real fallback WAVs in all eight
pack directories, checks the installed bytes, and rolls back.

Generate the settings fixture with:

```sh
python3 scripts/dev/settings_contract.py > /tmp/announce-recipient-settings-contract.json
```

A local handoff copy is `tests/fixtures/box-builds/announce-recipient-uncommitted.json`.
Copy it to the Cloud repository at `tests/fixtures/box-builds/`.
The `build` field is the checkout's HEAD; an uncommitted export is not a
released build. Generate it again after the final audio files and release
revision exist. All eight packs now have both fallback files. Capability
output reflects the fallback files present at export time.

Before release, verify the Cloud companion response contract and rendered
names for people and connected boxes. Check each voice pack offline. Check
card, default, current-message reply and recent-reply routes. Rename and
remove a subject, change accounts and packs, corrupt a cache file, and restart.
Check rapid taps, a tap held across the line, a hold released during the line,
and a hold released during `rec_go`. Check Mute, volume zero and quiet hours.
Listen for clear speech, the intended name, a two-second target, and microphone
capture only after the cue. Check both sent-cue settings.

Keep the separate card-selection fix separate. This change does not alter the
selection or claim functions. Before a merge to `main`, use a physical wacli
box to send, play and reply to a voice note, tap a family card, and check quiet
hours and listened notices. Desktop tests do not establish physical acceptance.
