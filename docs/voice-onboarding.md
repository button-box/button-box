# Voice setup on the Pi

Cloud boxes use the Button Box voice for first setup and the first reply.
The WhatsApp assistant can complete setup if the speaker fails. wacli boxes
keep their normal setup and message flow. Both modes use `buttonbox` as the
default voice for fresh settings and share the speaker lock. Existing saved
voice choices keep their value and revision in both modes, including `jessica`
with no selection receipt. Only a fresh install or settings with no `voice_pack`
key select `buttonbox`. Unsupported saved values use the Jessica audio fallback.

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
| Setup guide | Before claim, replay scan. After claim, replay the current names step if no note waits. After the first note, a new press starts the first reply. | Ignore presses during required speech. Optional greetings yield to a press or a real note. |
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

Cloud **MUST** gate `setup_greeting` on the box's **current heartbeat**:

- Require `capabilities.guided_setup_v1` to be the boolean `true` when creating
  a greeting and again when delivering an inbox response or redelivery.
- Replace the stored capabilities on each heartbeat. Do not merge a removed
  capability back from registration, an earlier heartbeat, or a fleet fixture.
  A missing, false, or invalid value does not permit a greeting.
- When a heartbeat stops reporting the capability, durably cancel every pending
  greeting before acknowledging that heartbeat. Do not deliver it later if the
  box upgrades again. Keep ordinary inbox commands available.

This is required for rollback safety. A power cut can occur after the Pi saves
its optional-audio request but before the `received` ACK is durable. The server
command can then remain pending for longer than the local 30-second audio
lifetime. The old runtime cannot parse `setup_greeting`. Its startup flow attempts
a heartbeat before polling the inbox; after acceptance, Cloud must use that capability
snapshot and cancel the greeting before returning the next inbox response.
Queue separation alone does not close this server redelivery case.

`test_current_heartbeat_contract_cancels_redelivery_after_pre_ack_rollback`
executes the historical startup and inbox reader against an executable Cloud
contract model. It covers a never-capable box and rollback before the ACK.
The test is not verification of the deployed Cloud implementation. The Cloud
repository must test and implement the same ordering and cancellation. No
Pi-side greeting retirement mechanism is added. Unknown command kinds remain
rejected.

**Failed heartbeat boundary — not closed by option (a)**

The historical startup loop catches a heartbeat error and can still poll with
its valid cached authorization. If the rollback heartbeat fails before Cloud
records it, the stored current capability can still be `true`. A read-only
probe with the frozen historical startup and inbox methods reproduced delivery
of the pending greeting, `cloud inbox item is invalid`, and zero calls to the
following ordinary command. The normal successful-heartbeat test above does
not prove safety for this failure case.

Cloud must address this remaining redelivery boundary before release acceptance.
One possible extension is to durably record a greeting's delivery attempt before
returning it, and never redeliver it after a lost ACK. A lost response would then
skip this optional line, as the guide already permits. This extension is proposed,
not implemented or verified here. A Pi-side audio expiry or retirement record
cannot prevent an old runtime from receiving and rejecting an unsupported
command in its downloaded inbox response. No Pi-side retirement was added.

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
that press; the normal button owner handles it. A greeting never waits behind
another speaker owner. It is skipped if the speaker is busy, and the button,
message queue, and quiet state are checked again after acquiring the lock.
The main loop checks the button before starting any further idle speech.

## Speaker access

`/run/messagebox-speaker/speaker.lock` is shared by the button service,
Cloud playback, local dashboard previews, and onboarding card/preview playback.
The audio-detect prerequisite creates the lock with tmpfiles before audio
services start. Its directory is root-owned, group `messagebox-settings`, mode
2770. Its file is mode 0660. Both strict onboarding services can write this
directory. The NFC service also has the settings group. Its settings directory
can remain read-only. Ordinary playback waits for the lock. Optional setup
greetings skip a busy speaker. Synchronous playback retries a busy ALSA
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
