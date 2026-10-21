# Voice release verification · 21 October 2026

The eight review findings have code changes and regression tests. No commit,
push, PR change, release, device access, or hardware test was done. The review
source `.astra-pi189-r1.md` stays untracked and unchanged.

The step-1 branch exists as `feat/updater-voice-ready` in the temporary worktree
`/private/tmp/button-box-updater-voice-ready`. Its base is
`3fb1c2a3a4e25fccee6a2454f02f0675579a11dc` (`origin/release-v0.3.12`). Its
changes are uncommitted. The voice branch stays at `1416c10`, with uncommitted
corrections. An actual rebase needs a committed step-1 target. It is not done,
because this task prohibits commits. Review and commit each change set first;
then rebase the voice branch onto `feat/updater-voice-ready` and run checks again.
After both commits exist and both worktrees are clean, the rebase command is:

```sh
git rebase --onto feat/updater-voice-ready origin/release-v0.3.12 feat/voice-onboarding
```

This command was not run.

**Findings and proof**

| Finding | Change | Code | Regression test |
| --- | --- | --- | --- |
| 1 | Prepare a separate updater release. Permit the new pack and setup WAVs. Raise the total sound limit to 160 MiB. Set controller version 1.2.0. Add a bounded download budget and an attended 1,800-second service drop-in. | [scripts/install/bounded_update.py:185](../scripts/install/bounded_update.py#L185); [scripts/install/update_discovery.py:76](../scripts/install/update_discovery.py#L76); [scripts/install/update-bootstrap.sh:56](../scripts/install/update-bootstrap.sh#L56) | [tests/test_voice_release_transition.py:46](../tests/test_voice_release_transition.py#L46); [tests/test_voice_release_transition.py:57](../tests/test_voice_release_transition.py#L57); [tests/test_update_discovery.py:93](../tests/test_update_discovery.py#L93); [tests/test_update_discovery.py:136](../tests/test_update_discovery.py#L136) |
| 2 | Put greetings and voice previews in a separate optional audio queue and state map. v0.3.12 ignores both. Do not write a new kind or buttonbox preview into the old strict queue. | [messagebox/cloud_runtime.py:218](../messagebox/cloud_runtime.py#L218); [messagebox/audio_requests.py:208](../messagebox/audio_requests.py#L208) | [tests/test_voice_review.py:30](../tests/test_voice_review.py#L30) |
| 3 | Require a nonempty string URL of at most 2,048 characters. Reject invalid greeting fields with an ACK error. Keep the poller running. | [messagebox/cloud_runtime.py:1443](../messagebox/cloud_runtime.py#L1443); [messagebox/cloud_device.py:338](../messagebox/cloud_device.py#L338) | [tests/test_voice_review.py:60](../tests/test_voice_review.py#L60) |
| 4 | Keep an explicit Cloud Jessica selection at its stored revision. Limit the old-default migration to wacli. | [messagebox/settings.py:410](../messagebox/settings.py#L410) | [tests/test_voice_review.py:76](../tests/test_voice_review.py#L76); [tests/test_cloud_setup.py:49](../tests/test_cloud_setup.py#L49) |
| 5 | Use the shared lock under /run. Create its group permissions before audio starts. Give both strict onboarding services write access. Keep NFC settings read-only. | [messagebox/speaker.py:16](../messagebox/speaker.py#L16); [systemd/messagebox-audio-detect.service:7](../systemd/messagebox-audio-detect.service#L7); [systemd/onboarding/messagebox-onboarding-nfc.service:31](../systemd/onboarding/messagebox-onboarding-nfc.service#L31); [systemd/messagebox.tmpfiles.conf:3](../systemd/messagebox.tmpfiles.conf#L3) | [tests/test_speaker.py:52](../tests/test_speaker.py#L52) |
| 6 | Skip a greeting if a real note waits. Stop it if a new note or button press arrives. Keep the press for the button owner. | [messagebox/button_send.py:1824](../messagebox/button_send.py#L1824); [messagebox/button_send.py:1888](../messagebox/button_send.py#L1888) | [tests/test_voice_review.py:144](../tests/test_voice_review.py#L144); [tests/test_voice_review.py:163](../tests/test_voice_review.py#L163) |
| 7 | Check state structure before set membership. Invalid state disables the guide and writes a content-free log warning. | [messagebox/cloud_setup.py:22](../messagebox/cloud_setup.py#L22) | [tests/test_voice_review.py:87](../tests/test_voice_review.py#L87) |
| 8 | Include pack, text hash, and clip ID in the cache key. Require the Cloud greeting text_hash field. | [messagebox/cloud_voice.py:33](../messagebox/cloud_voice.py#L33) | [tests/test_voice_review.py:103](../tests/test_voice_review.py#L103) |

The rollback test loads the new state with the exact v0.3.12 `_load` method,
runs its `_finish_previews` method, and uses its complete strict `AudioRequests`
module. A pending greeting and a buttonbox voice preview do not stop a legacy
ringtone request. The frozen fixtures name their source commit. They do not
need network access or Git history in CI.

**Two-release plan**

1. Review and commit the updater branch. Build it with minimum updater 1.1.0.
   Use the next available version and release sequence. Test box 4 first. The
   v0.3.12 controller installs its complete payload in the test simulation.
   Its application and sound files remain those from v0.3.12.
2. In an attended maintenance window, promote all six reviewed fixed controller
   modules and install the discovery timeout drop-in. Use the locking, atomic
   directory exchange, read-back, and fallback procedure in `on-box-updates.md`.
   Do not rerun bootstrap on an installed box. An ordinary application update
   only stages these modules; it does not activate them. This maintenance step
   is necessary before the voice release can install.
3. Review and commit the voice corrections. Rebase the voice branch onto the
   committed updater branch. Build it with minimum updater 1.2.0 and a higher
   sequence. The signed-bundle test proves that the v0.3.12 verifier rejects this
   minimum. All six controller modules are byte-identical in the prepared
   worktrees. Test box 4 first, then use the normal release batches.

No single-step update was implemented. Fixed unit files stay unchanged. The
new drop-in is a signed payload file for attended maintenance. Fresh Cloud
bootstrap installs the same drop-in. Application rollback does not restore the
fixed controller or this drop-in; attended controller fallback must restore both.

**Measured size and calculated transfer time**

These are local, unsigned review archives. The production builder's `package`
function produced them with `gzip -n -9`, the explicit payload list, and a
manifest from `scripts/dev/release-manifest.py`. It included generated identity
files. It did not use the production tag-only build, sign, or publish. Temporary
measurement tags were `v0.3.13-canary.1` and `v0.3.14-canary.1`; they are not
reserved release versions. Rebuild and measure the committed release artifacts.

| Review payload | Compressed bytes | MiB | At 1 Mbit/s | At 5 Mbit/s | Required free space after extraction |
| --- | ---: | ---: | ---: | ---: | ---: |
| step1 | 103,508,074 | 98.71 | 828.1 s | 165.6 s | 305.64 MiB |
| step2 | 115,997,776 | 110.62 | 928.0 s | 185.6 s | 333.69 MiB |

Transfer time is calculated as compressed bytes × 8 ÷ link bits per second.
It excludes protocol overhead, retries, signature checks, extraction, and disk
writes. Actual Pi and network times are **unverified**. The first payload has
small code changes but still contains the complete old payload, as the old
installer requires. Its theoretical 1 Mbit/s transfer leaves about 72 seconds
inside the old 900-second service limit. Its real margin needs a box test.

The second payload has 140,352,636 sound bytes (133.85 MiB). Keep WAV format and
all installed packs. Raise the transfer budget to 1,500 seconds, checked between
64 KiB reads, and the service limit to 1,800 seconds. Keep the 30-second socket
timeout, 128 MiB compressed archive cap, 256 MiB expanded cap, per-file/count
limits, signature checks, and partial-file cleanup. A slow or failed transfer
can occupy discovery longer. There is no download resume support. A slower
link can still fail safely. Verify the effective timeout before step 2.

The installer free-space check is twice the installed bytes plus 64 MiB. It runs
after extraction. Add the stored download and staging tree: the estimates are
525.36 MiB for step 1 and 579.33 MiB for step 2, before filesystem and tar padding
or other retained files. Actual peak Pi disk use is **unverified**.

**Commands and results**

- `make check`, updater worktree: PASS. 1,102 Python tests in 211.146 s;
  one step-2-only test skipped. 54 JavaScript tests passed.
- `MSGBOX_TEST_TRANSPORT=cloud make check`, voice worktree: PASS. 1,125 Python
  tests in 223.554 s; 54 JavaScript tests passed. This check preceded the last saved-state validation and test additions.
- `MSGBOX_TEST_TRANSPORT=wacli make check`, final voice source: PASS. 1,126
  Python tests in 214.108 s; 54 JavaScript tests passed. Syntax and all lint
  checks passed. This suite includes explicit Cloud and wacli contracts.
- `python3 -m unittest discover -s tests -p test_voice_review.py -v`: PASS,
  all eight focused regression tests.
- `python3 scripts/dev/settings_contract.py`: PASS. Exact provisional output
  is below. The same command with `--repo` for the updater worktree also passed.
- `git diff --check`, both worktrees: PASS.
- Compared all six controller module bytes between worktrees: identical.
  Compared all historical fixtures with v0.3.12 source: exact, with only class
  indentation removed from the two method extracts.

**wacli impact**

Shared runtime changes are `button_send.py`, `settings.py`, and `speaker.py`.
`audio_requests.py` also changes, but only Cloud paths use its queue.
The optional audio queue is Cloud-only. Shared deployment changes are the
audio-detect service, tmpfiles configuration, and both onboarding service units.
wacli keeps its existing setup, recipient routing, send, receive/play, reply,
family-card, quiet-hours, and listened-notice paths. The approved local default
migration to buttonbox remains. An explicit local Jessica selection still uses
its selection receipt. The shared speaker lock moves to the writable runtime
directory. No Cloud requests are added to wacli. Updater bootstrap still requires
Cloud mode.

Proof includes the five tests in `test_wacli_contract.py`, the read-only NFC
sound test, the local-default test, and the Cloud-guide exclusion test in
`test_cloud_setup.py`. The suite also tests both recording modes and transports.
Physical wacli acceptance remains **unverified**.

**Button actions**

| State | Action | Release rule |
| --- | --- | --- |
| Boot | Keep the existing ten-second Wi-Fi reset gesture. | Wait for a stable release before runtime. |
| Claim pending | Confirm the claim. | Release before another action. |
| Setup guide | Replay the current instruction. A new press after the first note starts its reply. | Ignore presses during required speech. Optional greeting yields to a press or real note. |
| Recording | A new press stops the first reply. Remind at 40 s. Stop and keep it at 60 s. | Release the start press before a stop press. |
| Review | Play the recording and send question. A new press within 10 s sends. No press deletes. | Release stop/playback presses before approval. |
| Normal runtime | Keep the selected tap/hold, playback, card, and review actions. | A consumed press has no second action. |

The unchanged boot gesture can reset Wi-Fi if a child or a stuck switch holds
the button for ten seconds during boot. This risk remains.

**Physical tests still required — unverified**

- Box 4: exact field-controller update, fixed promotion, effective timeout,
  slow link, low space, and controller fallback.
- Audio quality, button press/release/bounce, speaker contention under actual
  systemd users, NFC setup sounds, missing speaker, and muted playback.
- Real note before or during a greeting. Absent, repeated, late, malformed, and
  changed-pack greetings. Cloud must supply the new `text_hash` field.
- Reboot and rollback with a pending greeting; reboot in each guide, recording,
  review, upload, and listened stage; hotspot return and quiet hours.
- Full physical wacli send, receive/play, reply, family card, quiet hours, and
  listened notice. No merge to main before this check passes.

**Changed files in this correction**

- `config/update-discovery-voice-ready.conf`
- `docs/on-box-updates.md`
- `docs/voice-onboarding.md`
- `docs/voice-release-verification.md`
- `messagebox/audio_requests.py`
- `messagebox/button_send.py`
- `messagebox/cloud_device.py`
- `messagebox/cloud_runtime.py`
- `messagebox/cloud_setup.py`
- `messagebox/cloud_voice.py`
- `messagebox/settings.py`
- `messagebox/speaker.py`
- `scripts/install/release_verify.py`
- `scripts/install/update-bootstrap.sh`
- `scripts/install/update_discovery.py`
- `scripts/release/payload.txt`
- `systemd/messagebox-audio-detect.service`
- `systemd/messagebox.tmpfiles.conf`
- `systemd/onboarding/messagebox-onboarding-home.service`
- `systemd/onboarding/messagebox-onboarding-nfc.service`
- `tests/fixtures/pi-v0.3.12/README.md`
- `tests/fixtures/pi-v0.3.12/audio_requests.py`
- `tests/fixtures/pi-v0.3.12/bounded_update.py`
- `tests/fixtures/pi-v0.3.12/finish_previews.py.txt`
- `tests/fixtures/pi-v0.3.12/release_verify.py`
- `tests/fixtures/pi-v0.3.12/runtime_state.py.txt`
- `tests/test_cloud_runtime.py`
- `tests/test_cloud_setup.py`
- `tests/test_release_manifest.py`
- `tests/test_speaker.py`
- `tests/test_update_discovery.py`
- `tests/test_voice_release_transition.py`
- `tests/test_voice_review.py`
- `tests/test_whatsapp_pairing.py`

The updater branch changes only updater code/bootstrap, its timeout config and
payload list, the updater documentation and tests, and frozen controller
fixtures. It does not change the runtime, voice selection, audio packs, or
application service units.

**Fleet fixture output**

`python3 scripts/dev/settings_contract.py` produced the exact output below.
The build field is still `1416c10`, because corrections are uncommitted. This is
a provisional handoff. Regenerate it from the final commit before it goes to
the Cloud repository at `tests/fixtures/box-builds/`. The greeting payload must
also include `text_hash`; the fleet fixture describes command kinds and
capabilities, not the payload field schema.

```json
{
  "build": "1416c10",
  "capabilities": {
    "announce_messages_voice": true,
    "audio": true,
    "box_link": true,
    "card_name_prompt": true,
    "guided_setup_v1": true,
    "holiday_mode": true,
    "kit_cards": 1,
    "listened_announcements": true,
    "message_age_phrase": true,
    "morning_ring": true,
    "mute": true,
    "nfc": false,
    "quiet_now": true,
    "quiet_periods": 3,
    "quiet_schedules": true,
    "reaction_cards": 1,
    "reaction_sent_voice": 1,
    "reactions": 1,
    "recording_modes": [
      "tap_review",
      "hold_release"
    ],
    "review_before_send": true,
    "ringtones": [
      "hello_piano",
      "sunshine",
      "bouncy",
      "sing_along",
      "island",
      "hello",
      "ukulele"
    ],
    "settings_unknown_keys": "ignore",
    "settings_version": 1,
    "software_update": {
      "modes": [
        "auto",
        "ask"
      ],
      "physical_confirmation": true,
      "protocol": 1
    },
    "swoosh_sound_enabled": true,
    "talk_modes": [
      "tap",
      "hold"
    ],
    "text_to_voice": 1,
    "voice_lines": 1,
    "voice_packs": [
      "jessica",
      "pirate",
      "alien",
      "dj",
      "robot",
      "french",
      "charlie",
      "tata",
      "buttonbox"
    ],
    "weekend_days": true
  },
  "command_kinds": [
    "audio",
    "card_prompt",
    "delete_message",
    "install_update",
    "listened",
    "mute",
    "nfc_cancel",
    "nfc_enroll",
    "nfc_unpair",
    "preview_ringtone",
    "queue_hold",
    "quiet_now",
    "reaction",
    "set_update_mode",
    "settings",
    "setup_greeting",
    "voice_preview"
  ],
  "keys": {
    "after_listening": {
      "type": "enum",
      "values": [
        "invite_reply",
        "play_only"
      ]
    },
    "announce_messages_voice": {
      "type": "bool"
    },
    "arrival_signal": {
      "type": "enum",
      "values": [
        "lamp_only",
        "ring_and_lamp",
        "ring_only",
        "silent"
      ]
    },
    "card_name_prompt": {
      "type": "bool"
    },
    "holiday_until": {
      "format": "YYYY-MM-DD",
      "nullable": true,
      "type": "string"
    },
    "master_volume_percent": {
      "max": 100,
      "min": 0,
      "type": "int"
    },
    "max_recording_seconds": {
      "type": "enum",
      "values": [
        30,
        60,
        120
      ]
    },
    "morning_ring": {
      "type": "bool"
    },
    "nfc_confirmation_beep": {
      "type": "bool"
    },
    "quiet_hours": {
      "type": "object"
    },
    "quiet_schedules": {
      "keys": [
        "school_days",
        "weekend"
      ],
      "nullable": true,
      "period_keys": [
        "start",
        "end"
      ],
      "periods_max": 3,
      "time_format": "HH:MM",
      "type": "object"
    },
    "recording_mode": {
      "type": "enum",
      "values": [
        "hold_release",
        "tap_review"
      ]
    },
    "review_before_send": {
      "type": "bool"
    },
    "revision": {
      "min": 0,
      "type": "int"
    },
    "ringtone_id": {
      "type": "enum",
      "values": [
        "bouncy",
        "cuckoo_clock",
        "ding_dong",
        "gentle_music_box",
        "hello",
        "hello_piano",
        "island",
        "playful_chiptune",
        "sing_along",
        "sunshine",
        "ukulele"
      ]
    },
    "say_reactions": {
      "type": "bool"
    },
    "swoosh_sound_enabled": {
      "type": "bool"
    },
    "talk_mode": {
      "type": "enum",
      "values": [
        "hold",
        "tap"
      ]
    },
    "timezone": {
      "max": 64,
      "type": "string"
    },
    "version": {
      "type": "enum",
      "values": [
        1
      ]
    },
    "voice_pack": {
      "type": "enum",
      "values": [
        "jessica",
        "pirate",
        "alien",
        "dj",
        "robot",
        "french",
        "charlie",
        "tata",
        "buttonbox"
      ]
    },
    "weekend_days": {
      "items": "int",
      "max": 6,
      "max_items": 7,
      "min": 0,
      "nullable": true,
      "type": "array",
      "unique": true
    }
  },
  "settings_version": 1,
  "unknown_keys": "ignore"
}
```
