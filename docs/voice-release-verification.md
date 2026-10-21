# Voice release verification · 21 October 2026

Step 1 is committed at `04d53cd8b4968ff435b21f5fa72e3d1c6ba3ff7c` on
`feat/updater-voice-ready`, based on `origin/release-v0.3.12`. The voice branch
already contains that commit. Its reviewed step-2 source is
`9a3ae40259a21db53a625903004dca06546e1af2`, including the setup-page wording.
The earlier rebase is complete. No rebase command remains to be run.

Astra's confirmation source is `2a8608d`, which contains the prior contract
and setup-page corrections. This follow-up adds the mandatory per-poll feature
declaration and restricts greeting wire ACKs to the four agreed names. No push,
deployment, device, or hardware test was done. Review source files stay untracked.

**Current release order**

Release A is the self-upgrade bridge being built on `feat/updater-self-upgrade`.
It MUST ship before this Voice Release. The Voice Release MUST require the
self-upgrade controller delivered by Release A. Verify the bridge's final
controller version and set the Voice Release minimum updater requirement to
that version before building. Its exact version and field acceptance are
**unverified** here; the historical `1.2.0` minimum alone does not prove this gate.
Use **BOX-10 or BOX-11** as the canary, then use the normal release batches.

The earlier voice step-1 plan is **replaced by Release A**, not a separate
release to ship first. Its source and verification history remain below.

**Rule 2b follow-up proof**

| Change | Request or contract code | Test |
| --- | --- | --- |
| Declare the feature on every Cloud inbox poll | [messagebox/cloud_device.py:285](../messagebox/cloud_device.py#L285) | [tests/test_cloud_device.py:85](../tests/test_cloud_device.py#L85) |
| Require the current poll feature and protect the first offer after rollback | Mandatory rule 2b in [voice-onboarding.md](voice-onboarding.md) | [tests/test_voice_review_round2.py:210](../tests/test_voice_review_round2.py#L210); [tests/test_voice_review_round2.py:224](../tests/test_voice_review_round2.py#L224) |
| Accept exactly the four wire ACK names | GreetingContractServer.ack | [tests/test_voice_review_round2.py:295](../tests/test_voice_review_round2.py#L295); [tests/test_voice_review_round2.py:298](../tests/test_voice_review_round2.py#L298); [tests/test_voice_review_round2.py:301](../tests/test_voice_review_round2.py#L301) |
| Keep wacli on its local poll path | No shared runtime file changed | [tests/test_wacli_contract.py:89](../tests/test_wacli_contract.py#L89) |

**Round-2 findings and proof**

| Finding | Change | File | Test |
| --- | --- | --- | --- |
| 1. Mandatory greeting contract | Require the current poll feature, a fresh latest heartbeat, and an atomic pending-to-offered change before the response. Never redeliver. Cover failed rollback heartbeat, concurrent polls, expiry, ACK mapping, and retention. | [docs/voice-onboarding.md:68](voice-onboarding.md#L68) | [tests/test_voice_review_round2.py:210](../tests/test_voice_review_round2.py#L210); [tests/test_voice_review_round2.py:224](../tests/test_voice_review_round2.py#L224); [tests/test_voice_review_round2.py:199](../tests/test_voice_review_round2.py#L199); [tests/test_voice_review_round2.py:202](../tests/test_voice_review_round2.py#L202) |
| 2. Speaker contention | Skip an optional greeting immediately if the speaker is busy. Recheck button, note queue, and quiet state after lock acquisition. A cancellable wait is also available. Do not consume a press; check it before further idle speech. | [messagebox/speaker.py:21](../messagebox/speaker.py#L21); [messagebox/button_send.py:1824](../messagebox/button_send.py#L1824) | [tests/test_voice_review_round2.py:345](../tests/test_voice_review_round2.py#L345); [tests/test_voice_review_round2.py:369](../tests/test_voice_review_round2.py#L369); [tests/test_speaker.py:86](../tests/test_speaker.py#L86); [tests/test_speaker.py:119](../tests/test_speaker.py#L119) |
| 3. Saved wacli voice | Remove the ambiguous Jessica migration and selection-receipt requirement. Keep valid saved voice and revision. Use buttonbox only for a fresh install or a missing voice key. Unsupported/null choices fall back to Jessica. | [messagebox/settings.py:405](../messagebox/settings.py#L405); [messagebox/settings.py:39](../messagebox/settings.py#L39); [messagebox/cloud_runtime.py:1614](../messagebox/cloud_runtime.py#L1614) | [tests/test_voice_review_round2.py:406](../tests/test_voice_review_round2.py#L406); [tests/test_voice_review_round2.py:396](../tests/test_voice_review_round2.py#L396) |
| 4. Exact fleet fixtures | Replace the mislabelled export with separate exports from clean detached checkouts of 04d53cd and 9a3ae40. Step 1 has no guided setup capability or greeting command. | [docs/fixtures/README.md:3](../docs/fixtures/README.md#L3) | [tests/test_settings_contract.py:52](../tests/test_settings_contract.py#L52) |
| 5. Release evidence | Record the committed step identities and completed rebase. Replace the old embedded fixture with links to the two exact exports. Label prior test/size results as historical; keep physical and Cloud acceptance open. | [docs/voice-release-verification.md:3](voice-release-verification.md#L3) | Exact-commit export checks above; documentation review and git diff --check. |

The Cloud contract requires the current poll feature, plus one atomic `pending` → `offered` change before
the response, and no redelivery. The tests use SQLite and the actual historical
startup loop and strict inbox reader. After interruption before the ACK, both
a successful and a failed rollback heartbeat let the following ordinary command
run without a greeting redelivery. Concurrent requests return one greeting in
total. Restart, lost response, expiry, stale or removed capability, ACK mapping,
and 30-day retention are tested.

The negative old-runtime crash test remains as the reason for the rule. It
reproduces the invalid inbox error if Cloud breaks the no-redelivery contract.
These are tests of the required contract model, not the deployed Cloud service.
Cloud implementation and integration acceptance remain **unverified**. No
Pi-side retirement mechanism was added. Cloud MUST map the existing Pi ACKs
`applied` → `played` and `rejected` → `failed`; the requested Cloud row state
names and the Pi wire API are unchanged.

**First-offer protection:** rule 2b requires `features=guided_setup_v1` in
every current inbox poll, in addition to a fresh capable heartbeat. The test
runs the old startup and strict inbox reader after a failed rollback heartbeat:
it receives only the ordinary command. The same test then uses the real current
request code and receives the greeting once. Absent, empty, other, and
comma-separated feature lists are tested. The wacli main loop uses its local
poll command and does not start the Cloud runtime or call the Cloud inbox.

The simulated Cloud accepts exactly `received`, `applied`, `rejected`, and
`expired` on the wire. Tests confirm that `played` and `failed` do not change
an offered or received row. No greeting state names changed. The previous
first-offer failure is retained as the reason for rule 2b, not an open design
proposal. Production Cloud implementation and physical acceptance remain
**unverified**.

Current rule-2b follow-up `make check`: **PASS** on the changes to `2a8608d`.
Syntax and all lint checks passed. All 1,146 Python tests passed in 231.277
seconds; all 54 JavaScript tests passed. The 37 focused Cloud request, greeting,
and wacli tests also passed. `git diff --check` passed. These results are local
code and contract-model evidence; production Cloud and physical acceptance are
unverified.

**Historical mandatory-contract check**

The follow-up to `f09d2bc` passed 1,142 Python tests in 422.388 seconds and
54 JavaScript tests. Syntax, lint, and diff checks passed. It preceded rule 2b
and does not prove the current poll gate.

**Historical round-2 checks**

Round-2 focused checks passed: six delivery/speaker/voice regression tests,
six speaker tests, 21 settings tests, 11 setup tests, and four fleet-contract
tests. That `make check`: **PASS**. Syntax and all lint checks passed;
1,136 Python tests passed in 715.115 seconds, and 54 JavaScript tests passed.
`git diff --check` also passed. These results apply to the local round-2
corrections on HEAD `9a3ae40`. No physical acceptance is implied.

**Earlier findings and proof**

| Finding | Change | Code | Regression test |
| --- | --- | --- | --- |
| 1 | Prepare a separate updater release. Permit the new pack and setup WAVs. Raise the total sound limit to 160 MiB. Set controller version 1.2.0. Add a bounded download budget and an attended 1,800-second service drop-in. | [scripts/install/bounded_update.py:185](../scripts/install/bounded_update.py#L185); [scripts/install/update_discovery.py:76](../scripts/install/update_discovery.py#L76); [scripts/install/update-bootstrap.sh:56](../scripts/install/update-bootstrap.sh#L56) | [tests/test_voice_release_transition.py:46](../tests/test_voice_release_transition.py#L46); [tests/test_voice_release_transition.py:57](../tests/test_voice_release_transition.py#L57); [tests/test_update_discovery.py:93](../tests/test_update_discovery.py#L93); [tests/test_update_discovery.py:136](../tests/test_update_discovery.py#L136) |
| 2 | Put greetings and voice previews in a separate optional audio queue and state map. v0.3.12 ignores both. Do not write a new kind or buttonbox preview into the old strict queue. | [messagebox/cloud_runtime.py:218](../messagebox/cloud_runtime.py#L218); [messagebox/audio_requests.py:208](../messagebox/audio_requests.py#L208) | [tests/test_voice_review.py:30](../tests/test_voice_review.py#L30) |
| 3 | Require a nonempty string URL of at most 2,048 characters. Reject invalid greeting fields with an ACK error. Keep the poller running. | [messagebox/cloud_runtime.py:1443](../messagebox/cloud_runtime.py#L1443); [messagebox/cloud_device.py:338](../messagebox/cloud_device.py#L338) | [tests/test_voice_review.py:60](../tests/test_voice_review.py#L60) |
| 4 | Keep existing Jessica selections at their stored revision in both modes. Round 2 removes the ambiguous local-default migration. | [messagebox/settings.py:405](../messagebox/settings.py#L405) | [tests/test_voice_review.py:76](../tests/test_voice_review.py#L76); [tests/test_cloud_setup.py:49](../tests/test_cloud_setup.py#L49) |
| 5 | Use the shared lock under /run. Create its group permissions before audio starts. Give both strict onboarding services write access. Keep NFC settings read-only. | [messagebox/speaker.py:16](../messagebox/speaker.py#L16); [systemd/messagebox-audio-detect.service:7](../systemd/messagebox-audio-detect.service#L7); [systemd/onboarding/messagebox-onboarding-nfc.service:31](../systemd/onboarding/messagebox-onboarding-nfc.service#L31); [systemd/messagebox.tmpfiles.conf:3](../systemd/messagebox.tmpfiles.conf#L3) | [tests/test_speaker.py:52](../tests/test_speaker.py#L52) |
| 6 | Skip a greeting if a real note waits. Stop it if a new note or button press arrives. Keep the press for the button owner. | [messagebox/button_send.py:1824](../messagebox/button_send.py#L1824); [messagebox/button_send.py:1895](../messagebox/button_send.py#L1895) | [tests/test_voice_review.py:144](../tests/test_voice_review.py#L144); [tests/test_voice_review.py:163](../tests/test_voice_review.py#L163) |
| 7 | Check state structure before set membership. Invalid state disables the guide and writes a content-free log warning. | [messagebox/cloud_setup.py:22](../messagebox/cloud_setup.py#L22) | [tests/test_voice_review.py:87](../tests/test_voice_review.py#L87) |
| 8 | Include pack, text hash, and clip ID in the cache key. Require the Cloud greeting text_hash field. | [messagebox/cloud_voice.py:33](../messagebox/cloud_voice.py#L33) | [tests/test_voice_review.py:103](../tests/test_voice_review.py#L103) |

The rollback test loads the new state with the exact v0.3.12 `_load` method,
runs its `_finish_previews` method, and uses its complete strict `AudioRequests`
module. A pending greeting and a buttonbox voice preview do not stop a legacy
ringtone request. The frozen fixtures name their source commit. They do not
need network access or Git history in CI.

**Historical voice step-1 plan — replaced by Release A**

The earlier plan used committed step 1 (`04d53cd`) from
`feat/updater-voice-ready`, with minimum updater `1.1.0`. The actual v0.3.12
controller installed its complete payload in the test simulation. Its application
and sound files stayed at v0.3.12. Step 1 staged all six controller modules and
the discovery timeout drop-in; it did not activate the fixed controller.

That plan required attended promotion with the locking, atomic directory
exchange, read-back, and fallback procedure in `on-box-updates.md`. The voice
branch already contains step 1. The old step-2 tests used minimum updater
`1.2.0`; the v0.3.12 verifier rejected that minimum, and the step-1 controller
accepted the voice payload. These are historical results, not proof that the
self-upgrade bridge is installed or accepted.

**Release A replaces that attended step-1 release path.** Keep its evidence for
compatibility review. Do not ship the old plan as an additional prerequisite.
The current Voice Release must follow the bridge and require its self-upgrade
controller. Regenerate the final release fixture and measure the signed release
artifacts after the bridge and Voice Release identities are fixed. Test BOX-10
or BOX-11 first. Bridge promotion and recovery acceptance belong to Release A.

**Measured size and calculated transfer time**

These are historical, unsigned round-1 review archives. They are not measurements
of commits `04d53cd`, `9a3ae40`, or the round-2 corrected source. The production builder's `package`
function produced them with `gzip -n -9`, the explicit payload list, and a
manifest from `scripts/dev/release-manifest.py`. It included generated identity
files. It did not use the production tag-only build, sign, or publish. Temporary
measurement tags were `v0.3.13-canary.1` and `v0.3.14-canary.1`; they are not
reserved release versions. Release A has not been measured here. Rebuild and
measure the committed release artifacts.

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
link can still fail safely. Verify the effective timeout before the Voice Release.

The installer free-space check is twice the installed bytes plus 64 MiB. It runs
after extraction. Add the stored download and staging tree: the estimates are
525.36 MiB for step 1 and 579.33 MiB for step 2, before filesystem and tar padding
or other retained files. Actual peak Pi disk use is **unverified**.

**Historical round-1 commands and results**

- `make check`, updater worktree: PASS. 1,102 Python tests in 211.146 s;
  one step-2-only test skipped. 54 JavaScript tests passed.
- `MSGBOX_TEST_TRANSPORT=cloud make check`, voice worktree: PASS. 1,125 Python
  tests in 223.554 s; 54 JavaScript tests passed. This check preceded the last saved-state validation and test additions.
- `MSGBOX_TEST_TRANSPORT=wacli make check`, earlier round-1 source: PASS. 1,126
  Python tests in 214.108 s; 54 JavaScript tests passed. Syntax and all lint
  checks passed. This suite includes explicit Cloud and wacli contracts.
- `python3 -m unittest discover -s tests -p test_voice_review.py -v`: PASS,
  all eight focused regression tests.
- `python3 scripts/dev/settings_contract.py`: PASS. The earlier export was provisional and is superseded by the exact-commit fixtures below. The same command with `--repo` for the updater worktree also passed.
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
family-card, quiet-hours, and listened-notice paths. Existing local voice choices now keep their value and revision without any
selection receipt. Only fresh settings or a missing voice key get buttonbox. The shared speaker lock moves to the writable runtime
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

- BOX-10 or BOX-11: Release A self-upgrade bridge, controller requirement,
  effective timeout, slow link, low space, and controller recovery before the
  Voice Release.
- Audio quality, button press/release/bounce, speaker contention under actual
  systemd users, NFC setup sounds, missing speaker, and muted playback.
- Real note before or during a greeting. Absent, repeated, late, malformed, and
  changed-pack greetings. Cloud must supply the new `text_hash` field.
- Reboot and rollback with a pending greeting; reboot in each guide, recording,
  review, upload, and listened stage; hotspot return and quiet hours.
- Full physical wacli send, receive/play, reply, family card, quiet hours, and
  listened notice. No merge to main before this check passes.

**Changed files in round 2**

- Runtime: `messagebox/button_send.py`, `messagebox/cloud_runtime.py`,
  `messagebox/settings.py`, and `messagebox/speaker.py`.
- Documentation: `docs/cloud-settings.md`, `docs/voice-onboarding.md`,
  this report, and `docs/fixtures/README.md`.
- Fixtures: replace `docs/fixtures/voice-onboarding-fleet.json` with the two
  exact-commit exports below. Add the historical `settings.py`, `run.py.txt`,
  and `poll_once.py.txt` under `tests/fixtures/pi-v0.3.12/`; update its README.
- Tests: add `tests/test_voice_review_round2.py`; update `test_cloud_setup.py`,
  `test_settings.py`, `test_settings_contract.py`, and `test_speaker.py`.

**Earlier round-1 changed files**

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

The two exact-commit exports are separate files:

- [Step 1: 04d53cd](fixtures/voice-onboarding-step1-04d53cd.json).
  It does not advertise `guided_setup_v1` or accept `setup_greeting`.
- [Step 2: 9a3ae40](fixtures/voice-onboarding-step2-9a3ae40.json).
  It advertises `guided_setup_v1` and accepts `setup_greeting`.

See [fixture provenance and regeneration](fixtures/README.md). The incorrectly
labelled `voice-onboarding-fleet.json` is removed. The field build `3fb1c2a`
must never be assigned the step-2 capabilities. These exports describe the two
named historical commits, not the final Voice Release identity. Generate a new fixture from
the final committed release before Cloud handoff. Greeting delivery still
requires the current heartbeat; a fleet fixture cannot authorize redelivery.
