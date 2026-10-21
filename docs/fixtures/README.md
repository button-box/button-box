# Voice release fleet contracts

These are separate exports from clean, detached checkouts of exact commits:

| Step | Source commit | Fixture | Guided setup |
| --- | --- | --- | --- |
| 1 | `04d53cd8b4968ff435b21f5fa72e3d1c6ba3ff7c` | [Step 1](voice-onboarding-step1-04d53cd.json) | Not advertised; no `setup_greeting` command. |
| 2 | `9a3ae40259a21db53a625903004dca06546e1af2` | [Step 2](voice-onboarding-step2-9a3ae40.json) | `guided_setup_v1: true`; `setup_greeting` supported. |

For each commit, create a clean detached checkout and run its own exporter:

```sh
python3 scripts/dev/settings_contract.py --repo <exact-checkout>
```

The `build` field must match that checkout's commit. Do not replace the build
field in an export from another checkout. These files replace the incorrectly
labelled `voice-onboarding-fleet.json`; do not use the old file for the field
build. Copy each fixture to the Cloud repository's `tests/fixtures/box-builds/`
under its own build identity. Generate another fixture after the round-2 fixes
are committed; these files describe the two named historical commits exactly.
A fixture records supported features. Actual greeting delivery must use the
current heartbeat, including removal of a capability after rollback.
