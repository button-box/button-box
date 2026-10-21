# Voice setup on the Pi

Cloud boxes use the Button Box voice for first setup and the first reply.
The WhatsApp assistant can complete setup if the speaker fails. wacli boxes
keep their normal setup and message flow. Both modes use `buttonbox` as the
local default voice and share the speaker lock. An explicit Cloud voice selection
keeps its value, including `jessica`. Only the old local wacli default migrates.

## Speech order

1. First Cloud connection before claim: `voice-setup-scan.wav`.
2. Pending claim: `voice-setup-press.wav`.
3. Completed claim: `voice-setup-names.wav`.
4. Optional Cloud greeting with the children's names.
5. First incoming note: normal ring and playback. No voice intro or age line.
6. After playback: `voice-setup-your-turn.wav`. Wait for a new press.
7. Record the first reply. At 40 seconds, play `voice-setup-remind-stop.wav`
   once. At 60 seconds, stop and keep the recording for review.
8. Play the review. Play `voice-setup-ask-send.wav`. A new press within ten
   seconds approves the exact saved recipient. No press deletes the recording
   and plays `voice-setup-deleted.wav`.
9. The normal successful-upload cue is followed by `voice-setup-sent.wav`.
10. The matching parent listened receipt uses `voice-listened-noname.wav`.

The normal Cloud send contract controls delivery. The upload cue is evidence
that Cloud accepted the message for its normal send process. It does not prove
that WhatsApp delivered the message or that the parent played it.

## Button actions

| State | Button action | Release rule |
| --- | --- | --- |
| Boot | The existing ten-second Wi-Fi reset gesture stays in force. | Runtime waits for a stable release. |
| Claim pending | Confirm the claim. The same press cannot start recording. | Release before another action. |
| Setup guide | Before claim, replay scan. After claim, replay the current names step if no note waits. After the first note, a new press starts the first reply. | Ignore presses during speech. Discard a held press when speech ends. |
| Recording | A new press stops tap recording. In the first reply, the 40-second reminder ignores presses; 60 seconds stops capture. Normal hold mode stops on release. | The start press must be released before a stop press. |
| Review | Play the whole recording and the setup send question. A new press in the next ten seconds sends. No press deletes. | The stop press and all playback presses must be released before approval. |
| Normal runtime | Keep the selected tap/hold, playback, family-card, and review actions. Cloud speech ignores presses. | A press consumed by playback, claim, or an update cannot enter another action. |

The boot reset gesture has a risk: a child or a stuck switch can hold the
button during boot and reset Wi-Fi. This change does not alter that gesture.
A physical test is required before a separate change to it.

## Durable state and recovery

`/var/lib/messagebox-cloud/guided-setup.json` stores the stage, spoken receipts,
account scope, exact first-reply recipient, and reply correlation. Its lock uses
the existing private-lock helper. Its writes use the existing atomic JSON helper.
A speech receipt is written before playback. A restart does not replay that
speech. A deliberate setup press can replay the current instruction once.
Returning to the hotspot does not reset the guide.

A new program install does not start the guide on an already claimed box.
A missing guide file starts only from the first setup-online event. A damaged
state file disables the guide. A restart during recording or review ends the
guide; normal outbox recovery keeps approved messages. Unapproved recordings
still use the existing temporary-file cleanup. The guide does not add automatic
recording or automatic approval after a restart.

Quiet hours defer setup instructions. Settings confirmations keep their short
lifetime and are discarded during quiet hours. Missing or failed guide audio is
silent. It does not prevent claim completion through WhatsApp.

## Cloud greeting contract

Send `setup_greeting` only to a box whose heartbeat advertises
`capabilities.guided_setup_v1: true`. Unknown command kinds remain rejected.
The command uses the normal operation ID, sequence, timestamps, and payload.
The payload uses the existing voice-line cache contract:

```json
{
  "voice_pack": "buttonbox",
  "clip_id": "<64 lowercase hex characters>",
  "sha256": "<SHA-256 of the WAV bytes>",
  "text_hash": "<SHA-256 of the greeting text>",
  "content_type": "audio/wav",
  "media_url": "<authorized device media URL>"
}
```

The text is: “Hi, {names}! I'm your Button Box. Now ask your grown-up to send
me a voice message on WhatsApp!” Cloud generates it in the selected box voice.
The box accepts only the current pack. It uses the existing authenticated media
download and WAV validation: 48 kHz, mono, 16-bit PCM, bounded size, and exact
hash. Cache names include the selected pack, text hash, and clip ID. The poller
queues the local clip through `optional-audio-requests`; it never plays it.
Voice previews also use this separate queue. v0.3.12 ignores this directory and
the `pending_optional_audio` state key, so rollback keeps its strict legacy
`audio-requests` reader safe.

The box acknowledges `received` after the cache and queue writes. Its normal
pending-audio receipts report `applied`, `expired`, or `rejected` after playback
or expiry. `applied` means local playback finished, not that a person heard it.
The greeting is optional. Its queue lifetime is at most 30 seconds. A missing,
late, expired, or invalid greeting is skipped. It cannot delay the first note.
The guide stores a receipt before the greeting, so another operation cannot
repeat it during the same setup. A queued real note skips the greeting. A new
note or button press stops an active greeting. The greeting does not consume
that press; the normal button owner handles it.

## Speaker access

`/run/messagebox-speaker/speaker.lock` is shared by the button service,
Cloud playback, local dashboard previews, and onboarding card/preview playback.
The audio-detect prerequisite creates the lock with tmpfiles before audio
services start. Its directory is root-owned, group `messagebox-settings`, mode
2770. Its file is mode 0660. Both strict onboarding services can write this
directory. The NFC service also has the settings group. Its settings directory
can remain read-only. Playback waits for the lock. Synchronous playback retries a busy ALSA
open once after 150 ms. A second busy error stops that action without a button
flow error. Incoming audio remains recoverable through its normal queue rules.
The lock stays held until an asynchronous player is reaped. Recording reserves
it too; only that capture's own reminder can play until recording ends.

## Physical acceptance still required

All of these results are unverified on hardware:

- Audio quality and level for each guide line and the new default pack.
- Press, release, bounce, held-switch, claim, review, and update transitions.
- Two services requesting the speaker at the same time, including settings saves.
- First-reply reminder at 40 seconds and review at 60 seconds.
- Reboot during each guide stage, recording, review, upload, and listened playback.
- Quiet hours, mute, hotspot return, missing greeting, and failed speaker.
- Cloud greeting generation and matching WhatsApp assistant text.
- Physical wacli send, receive/play, reply, family card, quiet hours, and listened notice.
