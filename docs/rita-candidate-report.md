# Integrated candidate report

Date: 22 October 2026. Branch: `release/rita-candidate`.
Base: `origin/release-v0.3.12`, commit `3fb1c2a`.
All changes are local. No push, publication, device install or external report
was made. `feat/announce-recipient` is excluded.

## Commits

| Local commit | Change |
| --- | --- |
| `5a3165d` | Merge A/B updater `72aa00f`. No conflicts. |
| `3eceedc` | Merge voice onboarding `169dfb0`. Resolve six conflicts. |
| `939e8c3` | Merge card routing `50546ee`. Resolve two conflicts. |
| `484b1dd` | Fix the install allowlists and add the full signed bench/rollback proof. |
| `f3a0161` | Record the initial candidate guide and test report. |
| `fbdfd0c` | Prove unattended future controller replacement through the existing supervisor. |
| `bc91d64` | Move greeting downloads off the poll path and bind queued greetings to a voice. |

Use `git log --first-parent origin/release-v0.3.12..HEAD`
to get their exact identities.

## Conflict decisions

| File | Resolution |
| --- | --- |
| `docs/on-box-updates.md` | Keep A/B recovery. Remove the replaced attended fixed-controller promotion and old two-step voice release procedure. Document the voice limits on A/B. |
| `scripts/install/release_verify.py` | Keep A/B verifier version 1.3.0. Do not downgrade it to step 1's version 1.2.0. |
| `scripts/install/update-bootstrap.sh` | Keep the A/B unit and drop-in loops. Also install the voice discovery timeout drop-in. |
| `scripts/install/update_discovery.py` | Keep A/B key-path injection. Add the voice transfer deadline and monotonic clock injection. |
| `scripts/setup.sh` | Install both A/B modules and both new voice modules. Keep the buttonbox sound directory. |
| `tests/test_release_manifest.py` | Use the combined payload: 702 source entries and 687 installed paths. Keep the A/B proof and trust exclusions. |
| `messagebox/button_send.py` | Keep the Cloud SetupGuide and first-reply flow. Add durable card selection and its selection ID. Remove the obsolete stale-card state. Keep the new expired-card block. |
| `tests/test_button_routing.py` | Keep speaker isolation and the card tests' file-lock imports. |

The merged updater differs from `72aa00f` only where voice installation needs
support: buttonbox sound paths, the 160 MiB sound total, the 1,500-second transfer
budget and the 1,800-second discovery service limit. A/B verification, recovery,
selection and admission remain in place. The old attended-promotion procedure
is not part of the bench path.

## Integration fixes

- Install `remote_access.py` through setup and SSH provisioning. The runtime
  imports it. The field install allowlists omitted it.
- Test the complete setup shell on an empty device tree. Use real file installs,
  sound validation, signatures, extraction, journals, pointers and rollback.
- Copy the discovery timeout input into the bootstrap test source.
- Remove the test's obsolete step-1 controller hash. Test the integrated
  controller instead. Keep the frozen old-controller rejection test.
- Isolate the frozen worker's staged imports from the current test application.
  Test the exact reviewed bridge against all four frozen field controllers.
- Patch the runtime's actual Cloud module in voice tests. Prevent module reloads
  in the controller readiness harness from invalidating those patches.
- Correct stale release documentation. Require minimum updater 1.3.0 for the
  integrated voice candidate.

## Initial candidate test evidence (f3a0161)

Both initial `make check` commands exited 0. Syntax and all lint checks passed.

| Check | Result |
| --- | --- |
| `MSGBOX_TEST_TRANSPORT=cloud make check` | PASS. 1,288 Python tests passed; one native test skipped on Mac. All 54 JavaScript tests passed. Python time: 1,040.145 s. |
| `MSGBOX_TEST_TRANSPORT=wacli make check` | PASS. 1,288 Python tests passed; one native test skipped on Mac. All 54 JavaScript tests passed. Python time: 1,028.048 s. |
| Native Linux/systemd test | PASS. One test, 5.329 s. This covers the test skipped by both Mac runs. |
| Latest fresh bench proof | PASS. One test, 51.104 s. Includes fresh service state, old-controller rejection, sequence floor and quarantine checks. |
| Full bridge package | PASS with all four frozen workers. Publication and initialization cuts also pass. |
| Bench guide | All ten shell blocks pass `sh -n`. |
| Git diff | `git diff --check` passes. |

Each complete matrix run covers 80 A/B publication cuts and 99 signed
initial-controller write cuts. Local logs are `/tmp/rita-cloud-final.log`,
`/tmp/rita-wacli-final.log`, `/tmp/rita-bench-final.log` and
`/tmp/rita-native-systemd.log`. These logs were not uploaded.
The exported settings contract for source `484b1dd` is
[`fixtures/rita-candidate-484b1dd.json`](fixtures/rita-candidate-484b1dd.json).
It is a local handoff. No Cloud fixture was changed.

The complete bench test covers this sequence:

1. Run `scripts/setup.sh` with OS boundaries replaced.
2. Record the exact source as a local `bench-install` tree.
3. Run `update-bootstrap.sh` in Cloud mode.
4. Verify and commit the first signed application with the installed controller.
5. Bootstrap A/B. Check GOOD, routes, timer activation and slot A selection.
6. Import the installed voice runtime in a separate process. Validate buttonbox
   sounds. Use its speaker lock and persistent setup receipt.
7. Send a later signed application through the permanent A/B install route.
   The release changes one harmless application file.
8. Interrupt probation after the application pointer changes.
9. Run selector recovery. Require the first application, rollback receipt,
   unchanged routes, retained sequence floor and quarantine of the failed app.
10. Restart the installed voice code. Require the saved receipt and no repeat
    setup line.

The controller-slot tests also cover real signed publication, both controller
slots, failed trials, corruption, write cuts, bounded child processes and recovery.
The reviewed bridge package passes all four frozen field workers. Its full
package publication test covers 80 write-cut points.

## wacli impact

Shared runtime changes are in `audio_requests.py`, `button_send.py`,
`contacts.py`, `guided_reply.py`, `nfc.py`, `nfc_state.py`, `settings.py`,
`sound_pack.py`, the new `speaker.py`, the onboarding modules, and the dashboard.
Setup and provisioning also change. Card selection now survives a mispress in both modes. Fresh
settings select buttonbox. Saved voice choices remain in place. The speaker
lock prevents simultaneous playback in both modes. Cloud setup and greeting behavior stay
behind the Cloud path.

`test_wacli_contract.py` checks person/card routing, playback/reply, quiet hours,
listened notices and the absence of Cloud calls. The routing, NFC, settings,
speaker, recording and voice tests protect the shared changes. Both full matrix
runs exercise these tests. No merge into `main` was made. The required physical
wacli check is still open.

## Review fixes

`fbdfd0c` proves the future unattended path. No candidate activation code or
fixed controller unit changes were needed. The test future application itself
adds the trigger and changes its updateable bootstrap unit. The first install
still uses controller 1.3.0 and minimum updater 1.3.0.

`bc91d64` moves the optional greeting fetch into one background worker. It gives
transfer reads and result acceptance a three-second monotonic deadline. Real
notes run first. The poll thread owns all ACK and queue writes and checks the
current account, voice and expiry before accepting a result. A stuck resolver
can occupy the one worker; it cannot create more workers or block real notes.
The queue saves `voice_pack`. Playback rejects a different or missing voice.

The unattended test loads the installed future verifier without using cached
test-runner modules. It includes a simulated readiness delay beyond 60 seconds,
actual publication locks, real signatures, GOOD publication, runtime timer
recovery, reboot selection and another OTA.
The selector's standard-library test resolves paths before comparison. This
keeps the same restriction when Homebrew exposes the library through a symlink.

| Software finding | Acceptance test |
| --- | --- |
| Unattended controller replacement | `test_update_bootstrap.BootstrapCommandTests.test_signed_future_ota_automatically_replaces_controller_and_survives_failed_trial` |
| Greeting must not delay real notes | `test_voice_review.VoiceReviewTests.test_stalled_greeting_never_blocks_a_real_note_or_another_poll`, `test_real_notes_run_before_a_greeting_in_the_same_poll`, and `test_cloud_device.CloudIdentityTests.test_optional_media_has_a_total_deadline_even_with_regular_bytes` |
| Greeting must use the current voice | `test_voice_review.VoiceReviewTests.test_greeting_queued_under_an_old_voice_is_skipped` |

Both complete review checks exited 0. Syntax and all lint checks passed.

| Check | Result |
| --- | --- |
| `MSGBOX_TEST_TRANSPORT=cloud make check` | PASS. 1,293 Python tests passed; one native systemd test skipped on Mac. All 54 JavaScript tests passed. Python time: 933.876 s. |
| `MSGBOX_TEST_TRANSPORT=wacli make check` | PASS. 1,293 Python tests passed; one native systemd test skipped on Mac. All 54 JavaScript tests passed. Python time: 934.205 s. |

Each Python run reports 1,294 tests in total. The single skipped test has the
separate Linux evidence below; it was not rerun in this fix round. All five
acceptance tests listed above pass in both full runs. Local logs are
`/tmp/rita-astra-pass-cloud.log` and `/tmp/rita-astra-pass-wacli.log`.
They were not uploaded. Both bench-guide shell syntax and `git diff --check`
also pass.

The restarted Mac selected Xcode tools with a pending licence. The first runs
failed when subprocesses launched system Python or Git (exit 69). Selecting the
already installed Command Line Tools fixes this host issue without a licence or
source change. The focused unattended test then passed in 37.338 seconds.
The complete checks use Python 3.13 and this local environment:

```sh
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
export UV_PYTHON=/opt/homebrew/opt/python@3.13/bin/python3.13
export PATH=/opt/homebrew/opt/python@3.13/libexec/bin:$PATH
MSGBOX_TEST_TRANSPORT=cloud make check
MSGBOX_TEST_TRANSPORT=wacli make check
```

Settings keys, command kinds and advertised capabilities remain unchanged.
The settings exporter was rerun and compared with the saved fixture, excluding
only the source build ID. No Cloud fixture or external service was changed.

## Shipment limits

A future signed application can add unattended controller activation through the
existing supervisor → bootstrap path. No change to the candidate's activation
code is required. The new acceptance test carries changed, signed controller
bytes in an application installable by controller 1.3.0. It calls publication
after bootstrap releases its locks, raises the future bootstrap unit's limit
from 60 to 300 seconds, and retains the 180-second publication service bound.
A trial with readiness delayed by more than 60 simulated seconds passes. The
test checks new GOOD, reboot selection, failed-trial recovery by the independent
selector timer, and a further application OTA through the new controller.
The future trigger is test payload code; it is not activated in this candidate.
`self_upgrade: false` does not prohibit a later signed release from adding it.
This path is no longer an unresolved software capability gap.

The native Linux/systemd cleanup test passed separately on local Debian 13
aarch64, systemd 257.13 and cgroup v2 (5.329 seconds). It used the real transient
service and confirmed cleanup of a child that ignores SIGTERM. The VM had no
host mounts. Its temporary test source and added updater group were removed.
The VM was returned to its prior stopped state. The normal Mac matrix skips
this test by platform. On an isolated Linux host, after bootstrap has created
the `messagebox-update` group, run:

```sh
sudo env MSGBOX_NATIVE_SYSTEMD_TESTS=1 PYTHONPATH=tests \
  python3 -m unittest -v test_controller_selector_systemd
```

The bench operator must complete these checks on the spare Pi before ship mode:

- Native signed future application/controller OTA, actual systemd ordering,
  GOOD and slot selection, delayed trial, failed-trial timer recovery, another
  application OTA, reboot and power-cut recovery.
- Live Cloud current-poll features plus fresh heartbeat gating, atomic single
  greeting offer, no redelivery after lost ACK or rollback, and all four ACK
  mappings. The Cloud server used by the tests is a model.
- Stalled greeting followed by a real note; voice change before queued greeting
  playback; USB audio, setup, send/play/reply, NFC cards and mispresses, quiet
  hours, listened notices and Wi-Fi return.
- The physical wacli check remains required before a merge into `main`.

No live Cloud or physical acceptance was performed during this local task.
The Astra note is a source review. It does not close these checks.
The candidate has no production signatures or reserved
release sequences from this work. Complete signing before the bench install.

Use [the bench guide](rita-bench-install.md). Keep wired Ethernet for SSH after
Wi-Fi reset. The hotspot firewall blocks SSH. Disable SSH and Tailscale only at
the final ship-mode step. Keep the buyer's box unclaimed during manufacture.
