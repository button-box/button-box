# Frozen field updater contract

These files are test data. Do not edit them to match a new controller.
Add a new snapshot if another field controller has different bytes.

## Files added

- `tests/field_updater.py`: the host harness.
- `tests/test_field_updater_contract.py`: 22 contract tests.
- `tests/fixtures/field-updater/provenance.json`: source and hash records.
- `tests/fixtures/field-updater/README.md`: this report.
- 44 source files under `tests/fixtures/field-updater/shared/`. The hash table lists every path.

## Source records

The 44 files in `shared/` have identical bytes and Git modes in all four
commits below. Each release maps to that snapshot. `provenance.json` records
its complete commit ID and each file's Git blob ID, Git mode and SHA-256.
Fixture files use mode 0644 on the host. Original modes remain in the record.

| Release | Commit |
|---|---|
| v0.3.9 | `ae49eceeebd8049afe2f682b36b42f3aae9a771a` |
| v0.3.10 | `eb7f713788b4fc091695d03215602c507f4d641b` |
| v0.3.11 | `1aa2216f73586250e790c2f5745c0d09c1d4d145` |
| v0.3.12 | `3fb1c2a3a4e25fccee6a2454f02f0675579a11dc` |

The tests verify all hashes and compare all four source sets. If the commit
objects are present, the tests also compare every byte and mode with Git.
On a shallow or source-only checkout, only this Git comparison is skipped.
The hash and cross-version tests still run. Field device identity is unverified.
An application version alone does not identify its installed controller.

## Host harness

`../../field_updater.py` builds a synthetic payload and a temporary device root.
It imports the frozen worker, verifier, controller and trust code by file path.
It does not import the current product updater. The default candidate mapper
and mode migration inputs are frozen sources. Tests can change the candidate.

The harness runs real validation, extraction, locks, atomic file writes,
journals, application switches and restoration. The pipeline tests enter at
`update_controller.worker()`. Only host boundaries are replaced: systemd,
platform and installed identity, boot identity, time, the minisign process result,
and successful health. The candidate sound validator is a minimal function;
the frozen worker's own path, WAV, size and count checks still run.

A positive signature result is synthetic. This harness does not prove signature
cryptography, download behavior, broker socket permissions, real probation,
boot order, power-loss recovery, or physical device health. The archive size
checks use the original constants and real files. The compressed-size test
uses a sparse file in the signed envelope; extraction uses real gzip archives.

Run the focused contract:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_field_updater_contract.py -v
```

Run the required checks for both transports:

```sh
MSGBOX_TEST_TRANSPORT=cloud make check
MSGBOX_TEST_TRANSPORT=wacli make check
```

## Contract assertions

All citations below refer to the frozen files under `shared/`. Test comments
carry the same citations. The byte comparisons bind them to all four releases.

| Assertion | Frozen source |
|---|---|
| All 44 files have identical bytes and modes in all four releases; hashes and Git objects match. | `provenance.json`; all 44 frozen paths |
| The supervisor `messagebox.conf` drop-in installs with each field worker, has mode 0644, survives commit, and causes a daemon reload. | `scripts/install/bounded_update.py:257-258,1815-1819,1882-1887`; `scripts/dev/release-manifest.py:30-31` |
| The candidate mapper is imported and called before the first file installation. | `scripts/install/bounded_update.py:547-552,1804,1828-1833,1882-1887` |
| A mapper exception leaves installed files and the application pointer unchanged. No transaction journal or systemd action starts. | `scripts/install/bounded_update.py:547-552,1804` |
| All six changed controller modules are copied to the release's `updater/` directory with mode 0644. The fixed controller bytes remain unchanged. | `scripts/install/bounded_update.py:1834-1844` |
| Invalid controller syntax rejects the candidate and removes its staged release. | `scripts/install/bounded_update.py:1837-1840,1861-1863` |
| Nested application `.py`, `.sh`, `.html`, `.js` and `.css` files install. New `messagebox-*` services, paths, targets and timers install. A generic service drop-in installs. | `scripts/install/bounded_update.py:248-263,1828-1833,1882-1887` |
| Unsupported application extensions, installer destinations, unrelated units, socket units, other drop-in names, redirected destinations and unsafe source paths reject before installed-file changes. | `scripts/install/bounded_update.py:242-263,486-490` |
| Changed or missing fixed main units reject. Changed NFC requirements reject. Unchanged fixed files pass. | `scripts/install/bounded_update.py:1258-1263,1815-1819` |
| Failed host health invokes real recovery: it restores the old pointer and files, removes a previously absent drop-in and records `rolled_back`. | `scripts/install/bounded_update.py:1400-1415,1433-1496,1924-1927` |
| The frozen controller worker verifies the envelope, extracts the archive, compares manifests, installs the supervisor drop-in and removes its downloads and staging tree after success. | `scripts/install/update_controller.py:561-590,611-619`; `scripts/install/release_verify.py:50-151` |
| A candidate mapper can raise the frozen `UpdateError('not_idle')` before mutation. Automatic work retains the candidate and availability, clears staging and records a retry after 60 seconds. A second attempt follows the same path. | `scripts/install/bounded_update.py:547-552,1804`; `scripts/install/update_controller.py:591-600,611-615` |
| A rejected signature result and an archive/external manifest mismatch reject before installation. They record verification failure and remove the candidate. | `scripts/install/release_verify.py:63-77`; `scripts/install/update_controller.py:579-584,607-615` |
| Extra WAVs pass validation only in allowed existing packs. A new `buttonbox` pack, unknown pack, nested filename, uppercase name, arbitrary JSON and code file reject. Selected JSON paths pass. | `scripts/install/bounded_update.py:185-188,242-246,486-490,497-504` |
| A 2 MiB sound file passes. One extra byte rejects. An invalid WAV rejects. | `scripts/install/bounded_update.py:189,372-388,504` |
| Exactly 128 MiB of sound data passes. Added sound data rejects. | `scripts/install/bounded_update.py:190,497-503` |
| Exactly 2,000 sound files pass. File 2,001 rejects. | `scripts/install/bounded_update.py:191,497-503` |
| Exactly 10,000 archive entries pass. Entry 10,001 rejects before extraction. | `scripts/install/release_verify.py:15,118-151` |
| Exactly 256 MiB expanded data passes. One extra byte rejects before extraction. | `scripts/install/release_verify.py:14,138-140` |
| Symlinks, hardlinks, FIFOs, character and block devices, duplicate names, traversal, absolute paths and noncanonical paths reject before extraction. | `scripts/install/release_verify.py:130-140` |
| Exactly 128 MiB compressed-envelope bytes pass. One extra byte rejects. | `scripts/install/release_verify.py:13,85-90` |

## Results and limits

Validation on 21 October 2026:

| Command | Result |
|---|---|
| `MSGBOX_TEST_TRANSPORT=cloud make check` | PASS: syntax, lint, 1,120 Python tests and 54 JavaScript tests. |
| `MSGBOX_TEST_TRANSPORT=wacli make check` | PASS: syntax, lint, 1,120 Python tests and 54 JavaScript tests. |

All 22 new contract tests passed in each run. The Git source comparison ran;
it was not skipped. An independent code review found no blocking defect.

All three required bridge facts have executable tests. No tested bridge fact
contradicts the source design. This does not prove a complete self-upgrade.
A queued promotion service, retained controller proof, independent recovery,
controller exchange, activation, rollout permits, and unattended repeat upgrades
are not implemented here. Their device acceptance remains unverified.

The candidate mapper retry test proves exception propagation only. It does not
implement or verify a Cloud admission client, hold, expiry, or timeout policy.
No bootstrap script or systemd unit is executed on the host. The fixtures are
source records, not captured field installation images or bootstrap output.

### wacli impact

None. This change adds test files only. No shared runtime file changes.
Both transport check runs include the existing runtime mode tests. Physical
send, receive/play, reply, card taps, quiet hours and listened notices remain
unverified. No device is contacted by the new tests.

Before any later push or publication, inspect every outgoing surface and its
audience under the standing public-output privacy policy. Keep the review
receipt private. Local test success does not satisfy that publication review.

## SHA-256 table

One row covers the same path in all four releases. Do not regenerate these
hashes from the current product source.

| Frozen path | SHA-256 |
|---|---|
| `messagebox/wifi_watchdog.py` | `62ec582d7a53858e0652aa16e114ac9ead50002b8afda78409549a3a8d1fe545` |
| `scripts/dev/release-manifest.py` | `706ea90678fcb7db4d7e11799cd87509a462249a897790e5cec0205e9d41bbd4` |
| `scripts/install/bounded_update.py` | `49c285b51f745a6894ee2533df516d681e9e9992fb72c84d5017b0cb799b8425` |
| `scripts/install/health-probe.py` | `e9079c51a8c92e495012b516ba84357a8a221d759dcedeb94d58c970a29b324a` |
| `scripts/install/messagebox-mode-migrate.py` | `7a73bff609f2ce0c581ac992b54f0eebdcb46abce20da8d1c738831bbba2d24e` |
| `scripts/install/release_verify.py` | `e1aa79e5564230e0503850f6a5c1540f71a70ef17910c83a253fcddc0781a9b2` |
| `scripts/install/trust_update.py` | `99502b79d0663c3d07e2a549c3c21d18cbaa31b28363b51f2f58de2d4878ca62` |
| `scripts/install/update-bootstrap.sh` | `2a46a5df941e2ff01b5dc122f0eed494f3aab64b1374ee0edc6332e8e2036741` |
| `scripts/install/update_controller.py` | `d9aac78e61ba538f847ff3145443dcae2b752fc3ba1b9693d51fbfafa6321a3f` |
| `scripts/install/update_discovery.py` | `f1dbd4e0d5eaad0fe8a99e58c9b9e31e2407868dd0f09be96acc9f730685f540` |
| `systemd/messagebox-audio-detect.service` | `5ddaee2b3dffa547d7c165684731963c9677f8b305f180ff651e3d6f7226d3df` |
| `systemd/messagebox-button.service` | `4cf519edf2ddc51d9e096f13810e5d18a2870f68518bdc879771ec88b4b7a255` |
| `systemd/messagebox-dash.service` | `af64608aee990eaa361a126e046eed5d9c92d2cd1b95bb1fbefc5090b1a424da` |
| `systemd/messagebox-mode-generator` | `db2f3261b1bd7b074d983c0cbdc877eb07f313eefb3e1f7211b26f7c10de2fd5` |
| `systemd/messagebox-mode-reconcile.path` | `e65614dc574dc455a1746d52cf889a38019326f1f300cca0339b37a0b409ea11` |
| `systemd/messagebox-mode-reconcile.service` | `2b3f4aa6b4af029efb46cf1b7b56ed32abd0124af48215e9b960442365a905ed` |
| `systemd/messagebox-nfc.service` | `00fe653b072bcf31a4dfeeb613fd5eca13d6865dc0819c4278c4421a50d774e4` |
| `systemd/messagebox-poller.service` | `7a9090b41eb51fa856baf110dc0475221e05715d99b707093419f4ad0f26c8bd` |
| `systemd/messagebox-sync.service` | `320e82db2438f8e3a269938480d2a684654c9a8f9cc1e85cd1cdbd3856d78034` |
| `systemd/messagebox-update-controller.service` | `1329e7e718d3f9b12b6049d3c4b5c5bc913153d74afd4b527e0179b57c5f0643` |
| `systemd/messagebox-update-discovery.service` | `c2594001cf9d21bedfdb8ecd6568b311a9af423f7b6c900c4b975da42cde7f11` |
| `systemd/messagebox-update-discovery.timer` | `460400ed0bb0745f8638dbee0ef7d26e35ef9bc078b4419d8e590590dc44e2e5` |
| `systemd/messagebox-update-install.service` | `5a15d0fa55439687c228474c36a3c65c69f6a4264f7ca7beec529eb70ff92a09` |
| `systemd/messagebox-update-recovery.service` | `248cc26ff3add0701d98d5c8f0355dc5dbef83417e86f8d1e33e5ceea3a74863` |
| `systemd/messagebox-update-supervisor.service` | `39123442b4f1c019627c288cd71bfb430e1c334d816e0a0ce132597a6b22e5d3` |
| `systemd/messagebox-update-supervisor.timer` | `b190e18046eab5a10b4a9f63b4c9dd1b50b0cecc6d3c69db5e7afee1aab00148` |
| `systemd/messagebox-wifi-change.path` | `87e33d22d208db29e0e65cbb3f811346dca55efe9e06a22acb43f1020661f560` |
| `systemd/messagebox-wifi-change.service` | `b9001ce3b2742e0be988dfa2b7c5d6ebe6a3d35178be7a4c73acf4884ec996ea` |
| `systemd/messagebox-wifi-watchdog.service` | `ffee977f683e88aa286ec2154ca2c5720ffaf3b24aeab65f5695229b832ad11f` |
| `systemd/messagebox-wifi-watchdog.timer` | `10ccb76f8cc6783844307d0380faa77c57b35457152c3ef71f1fb6337c1a0daa` |
| `systemd/messagebox.target` | `261f3c509959ebbe5dca4b13782cef042d736a573371c246c8e013763e8f0912` |
| `systemd/messagebox.tmpfiles.conf` | `85778a074ce34629e4fabd5925e12bb2dd56027bee4288ee1339ec75384db88e` |
| `systemd/onboarding/comitup-web.service.d/messagebox.conf` | `8cdac1c2be407d8764bf20d82eeffedfbc2c4d317a03f5406c70bf24cf0f1945` |
| `systemd/onboarding/comitup.service.d/messagebox.conf` | `58cb9ef0a5450b3215a4e9e5dc212e975222adcf31c53298c215a67c3fd57fe1` |
| `systemd/onboarding/messagebox-onboarding-button.service` | `6da576be98eecb7d3a4cb9bf7319a1e830d13833bece34835f39c199c4770d86` |
| `systemd/onboarding/messagebox-onboarding-complete.path` | `c46f1fe8047d9e08ba808bb363392c0dd23e50897351708007ee73cc2a5f1283` |
| `systemd/onboarding/messagebox-onboarding-complete.service` | `f204ba0df2ecd9686d494a8d73e6a013213d648a19c01335ab11e880ee4678e5` |
| `systemd/onboarding/messagebox-onboarding-home.service` | `503854ab931e563209194f56155fab3a11d919776fdbe77752261c7fd1062e24` |
| `systemd/onboarding/messagebox-onboarding-nfc.service` | `3ed4511582a92d4919696f50d4b8c7c4b5c81fe927908465a5874d4e87cd70a8` |
| `systemd/onboarding/messagebox-onboarding-voice-gate.service` | `a26fdf8683c40d3d5c5d778578c3ac2260c11ca563c09afc63f9385771379649` |
| `systemd/onboarding/messagebox-onboarding-voice.path` | `2ddd45a560a18052d4e452666477730bbf989b2b84195c2e0ee6b2a59b77de04` |
| `systemd/onboarding/messagebox-onboarding-voice.target` | `92c91486a79df2c76f9e7b6ae21bab76b11ab1927367a5dee3c0a616d03ff2ea` |
| `systemd/onboarding/messagebox-whatsapp-pairing.service` | `f9df5a0a35530dbc7c912c0df8ec83e6590dda6b334a42474fee21e3828025fa` |
| `systemd/onboarding/messagebox-wifi-reset.service` | `293f465f80e6197ab60a0614ed74bf397882f966b6363aa8bb987ee3408db574` |
