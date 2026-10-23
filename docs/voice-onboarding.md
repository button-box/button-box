# Voice setup on the Pi

Cloud boxes use the Button Box voice for first setup and the first reply.
The WhatsApp assistant can complete setup if the speaker fails. wacli boxes
keep their normal setup and message flow. Both modes use `buttonbox` as the
default voice for fresh local installs and share the speaker lock. Existing
saved voice choices keep their value and revision in both modes, including
`jessica` with no selection receipt. A Cloud settings document with no
`voice_pack` keeps the current applied voice. Only an explicit Cloud buttonbox
choice or a fresh local install can select it in Cloud mode. The wacli default
rules are unchanged. Unsupported explicit values use the Jessica audio fallback.

## Speech order

In Cloud HOME mode, the setup watcher registers the box before its first
check-in once home Wi-Fi is ready. It uses the same locked, resumable claim
start as the phone page. No phone request is needed for the online cue.
Clock-not-ready and network errors keep the existing bounded retry backoff;
logs contain only the error type. The phone page and physical claim press
keep their existing roles.

1. First Cloud connection before claim: `voice-setup-scan.wav`.
2. Pending claim: `voice-setup-press.wav`.
3. Completed claim: `voice-setup-names.wav`.
4. Optional Cloud greeting with the children's names.
5. First incoming note: normal ring and playback. No voice intro or age line.
   A short press during the ring stops it and plays the waiting note. The
   normal hold-to-record and family-card rules still apply.
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

A confirmed pending claim can create the guide before the online cue is
consumed. Both button owners use the same receipt. Speech records the stage
and pending claim it read; a concurrent stage change leaves the new prompt
unconsumed for the next idle pass. Registration alone never arms the press line.
The root handover gate advances to names only after its own successful claim
verification, so the runtime cannot inherit a stale press instruction when a
phone completion request overtakes the setup watcher.

A new program install does not start the guide on an already claimed box.
A missing guide file starts from the first setup-online event or a confirmed
pending claim. A damaged
state file disables the guide. A restart during recording or review ends the
guide; normal outbox recovery keeps approved messages. Unapproved recordings
still use the existing temporary-file cleanup. The guide does not add automatic
recording or automatic approval after a restart.

Quiet hours defer setup instructions. Cloud settings still apply and are
acknowledged during setup, but their saved confirmation is not queued until the
guide is `done`. Suppressed confirmations are not replayed later. Settings
confirmations keep their short lifetime and are discarded during quiet hours.
Missing or failed guide audio is
silent. It does not prevent claim completion through WhatsApp.

## Optional greeting download and voice

The poller handles real notes before greeting commands. A greeting download runs
in one background worker, with a three-second monotonic deadline. The poll thread
never waits for it. Transfer reads use the remaining time budget. Late results
are rejected. A stalled system resolver can keep that single worker occupied;
no further greeting workers start until it exits. Real-note polling continues.

Only the poll thread writes playback requests and ACKs. Before accepting a
finished clip, it checks the deadline, current account, voice and expiry again.
The queue record includes `voice_pack`. The button owner checks that field
against the current voice before playback. Missing or different voices are
rejected, including old queue records without a voice. Real notes and presses
still cancel optional playback.

## Cloud greeting contract

Cloud **MUST** implement all of these rules for `setup_greeting`. They are a
release requirement, not an optional improvement.

1. Store each greeting as one row. Use the states `pending` → `offered` →
   `received` | `played` | `failed` | `expired` | `cancelled`. Store `offered_at`
   and `expires_at`. Set `expires_at` no later than ten minutes after creation.
2. Offer a greeting only when its state is `pending` and the box's **latest**
   heartbeat was received within `BOX_ONLINE_SECONDS` and reports boolean
   `capabilities.guided_setup_v1: true`. Replace the capability snapshot on each
   heartbeat. Do not merge an older capability from registration, a previous
   heartbeat, or a fleet fixture. Change `pending` to `offered` with one atomic
   `UPDATE … WHERE state='pending' RETURNING` operation. Commit this change and
   `offered_at` **before** sending the inbox response. The heartbeat check and
   offer must not race a heartbeat that removes the capability.

   **Rule 2b — current poll (mandatory).** The new runtime MUST send
   `features=guided_setup_v1` in **every** inbox poll query. The parameter name
   is exactly `features`; its value is a comma-separated list of feature names.
   Later releases can add names to that list. Cloud MUST offer a greeting only
   if the **current poll** contains the exact `guided_setup_v1` token AND the
   heartbeat rule above holds. An absent parameter, an empty list, or another
   feature does not permit a greeting. Do not reuse a feature from an earlier
   poll or use the heartbeat as a substitute. Keep ordinary commands available.
   The old runtime never sends this parameter, so it cannot receive a greeting.

3. **Never redeliver an offered row.** Concurrent polls, repeated cursors,
   server restarts, a lost HTTP response, and a lost ACK must not offer it again.
   A lost ACK leaves the row `offered` until `expires_at`, then `expired`, unless
   rule 4 cancels it first. Known box ACKs record `received`, `played`, or
   `failed`; an expiry ACK records `expired`. Ignore unknown ACK states and
   unknown operation IDs. A late ACK must not reverse a final result or revive
   a cancelled or expired row. A `received` receipt can later record the final
   playback result, but it can never be offered again.
4. If the latest heartbeat stops reporting `guided_setup_v1`, or there is no
   fresh heartbeat, change all `pending` and `offered` rows to `cancelled` and
   offer nothing. A missing, false, or invalid capability does not permit an
   offer. Persist cancellation before acknowledging a capability-removing
   heartbeat. Refreshing the heartbeat or upgrading again must not revive a
   cancelled row. Keep ordinary inbox commands available.
5. Delete terminal receipt rows after 30 days. Store only the clip reference
   and delivery metadata. Do not store greeting text or names in these rows.

`BOX_ONLINE_SECONDS` is the Cloud online window. This document does not set a
new Pi timeout or guess the Cloud configuration value. The executable test
model uses its own explicit test window.

**Existing Pi ACK names**

Cloud MUST accept exactly these greeting wire ACK names and map them to the
requested row states. Wire ACK names `played` and `failed` are unknown and MUST
be ignored:

| Pi wire ACK | Cloud row state |
| --- | --- |
| `received` | `received` |
| `applied` | `played` |
| `rejected` | `failed` |
| `expired` | `expired` |

The row state names above do not change the Pi command API. The Pi already
sends these ACKs. The ACK wire names and the wacli path are unchanged. `played` means
local playback finished; it does not prove that a person heard the greeting.

**Why at-most-once delivery is mandatory**

The old runtime cannot parse `setup_greeting`. Its startup loop catches a
heartbeat error and can still poll with valid cached authorization. If that
heartbeat fails before Cloud records the rollback, Cloud can still hold the
previous `guided_setup_v1: true` value. A power cut after the new Pi saves its
optional audio request but before the `received` ACK can then cause redelivery.
Local audio expiry and queue separation cannot protect that old inbox reader.

`test_old_runtime_crashes_if_cloud_breaks_the_no_redelivery_rule` keeps this
failure reproducible with the frozen historical startup loop and inbox reader.
It verifies `cloud inbox item is invalid` and no following ordinary command.
The positive tests offer the greeting once, interrupt before the ACK, and then
run the historical code with either a successful or a failed rollback heartbeat.
Both flows reach the following ordinary command without another greeting offer.
A separate concurrent-poll test checks the atomic state change, a lost response,
restart, and expiry. Other tests cover cancellation, ACK mapping, and retention.

These tests simulate the mandatory Cloud contract with SQLite. They do not
verify the production Cloud implementation. Cloud must implement and test the
same transaction and retention rules before release. A lost response can skip
this optional line. That is permitted. No Pi-side retirement mechanism is added.
Unknown command kinds remain rejected.

**First offer after rollback — protected by rule 2b**

A fresh heartbeat alone cannot prove which runtime made the current poll.
Without rule 2b, a row can remain `pending` through a rollback and a failed
heartbeat, then be offered to the old runtime. Its strict reader rejects
`setup_greeting` before it can process a following ordinary command.

`test_first_offer_cannot_reach_old_runtime_after_failed_rollback_heartbeat`
starts with that pending row and a fresh new-runtime heartbeat, then runs the
frozen historical startup and inbox reader with a failed rollback heartbeat.
The poll has no `features` parameter. The greeting stays pending and the old
reader processes the ordinary command. The test then uses the real current
`CloudDeviceClient.inbox` request code: its feature declaration permits one
first offer, and a repeat poll gets no greeting. Separate tests cover absent,
empty, unknown, and comma-separated feature lists, and prove that the wacli
poller never starts the Cloud runtime or calls the Cloud inbox.

This closes the first-offer case in the required contract model. Production
Cloud implementation and real-box acceptance remain unverified. No Pi-side
retirement mechanism or new greeting state is required.

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

All application PCM WAV playback through `speaker.run` and `start_player`
adds leading silence in the same `aplay` stream. `messagebox/speaker.py` has
the default tuning constant, `LEAD_IN_S = 0.3`. This covers voices, cues, notes
and previews in both transports. Cloud playback uses the same path with
`cloud_quiet.VOICE_LINE_LEAD_IN_S = 0.6` for cached rendered lines in
`voice-lines`, `listened-clips`, and `card-prompts`. This adds USB wake-up
margin for the 48 kHz name lines. Bundled voices, button cues, and wacli
keep the 300 ms default. The temporary copy preserves the format and
every original PCM byte. Files in the sound pack stay unchanged. Copying uses
small blocks for low-memory boxes. The copy is removed after playback or
failure; asynchronous playback retains it until the player is reaped.
Non-WAV inputs retain their existing player behavior. Check the first word
on the actual USB speaker after a cold start before the handout. The Cloud
setup greeting, listened name line, and card name line must each keep their
first word. The bench failure occurred with the existing 300 ms shared pad;
the longer Cloud margin still needs confirmation on BOX-10.

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
