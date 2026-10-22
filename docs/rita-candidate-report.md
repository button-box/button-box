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

The final documentation commit follows these four commits. Use `git log --first-parent origin/release-v0.3.12..HEAD`
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

## Test evidence

Both final `make check` commands exited 0. Syntax and all lint checks passed.

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

## Shipment limits

**The reviewed A/B branch has no automatic OTA controller-promotion trigger.**
It has a signed local publication API. The descriptor still declares
`self_upgrade: false`. Application OTA uses the selected controller, but an
application commit alone does not publish a new controller. The branch's
`on-box-updates.md` states this limit. Do not treat local publication tests as
proof of unattended controller self-upgrade. Resolve this gap before shipment
if unattended self-upgrade is the shipment requirement.

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

Physical Pi service ordering, power-cut, audio, button, LED, NFC, Wi-Fi, live Cloud rule-2b and
wacli checks remain open. The Astra note is a source-review pass. It does not
close these checks. The candidate has no production signatures or reserved
release sequences from this work. Complete signing before the bench install.

Use [the bench guide](rita-bench-install.md). Keep wired Ethernet for SSH after
Wi-Fi reset. The hotspot firewall blocks SSH. Disable SSH and Tailscale only at
the final ship-mode step. Keep the buyer's box unclaimed during manufacture.
