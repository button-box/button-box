# Testing

## Repository tests

- `make test` runs the synthetic Python unit suite.
- `make lint` requires `uvx` (from `uv`) and `bunx` (from Bun). It runs Ruff for
  Python, ShellCheck for shell scripts, and Biome for frontend assets. `uvx` and
  `bunx` download these tools on first use.
- `make check` runs syntax checks, linting, and tests.

No project virtual environment or repository-local dependency installation is
required.

Synthetic tests cover routing, onboarding, redaction, pairing, NFC, and recovery
contracts. They do not replace physical Pi, phone, network, or hardware tests.

## Dedicated test rig

Stage One is an opt-in evidence and reproduction layer. It does not run Codex on
the Pi, modify code, deploy builds, or open pull requests automatically. Keep a
separate Pi and microSD card for this role; do not enable it on a household box.

After provisioning the exact test Pi, enable reporting by repeating its short
hostname explicitly:

```sh
hostname -s
sudo messagebox-test enable --device button-box-003
```

The command refuses a name that does not match the current device. Once enabled,
the onboarding portal and dashboard show a **Report problem** panel. The tester
can add a short, non-identifying observation, create the report, and either copy
its Markdown or download its JSON. The collector emits only allowlisted state,
service status, hardware-presence booleans, the boot ID, the installed Git
revision, and the current guided-run result. It does not read messages, audio,
contacts, credentials, network identifiers, NFC card IDs, or raw logs. Do not
put names, phone numbers, message text, or other customer information in the
optional note.

Disable the panel with:

```sh
sudo messagebox-test disable
```

### Guided daily scenarios

Start a run before the attended physical test:

```sh
sudo messagebox-test start smoke
sudo messagebox-test start hardware
sudo messagebox-test start onboarding
sudo messagebox-test start message-loop
sudo messagebox-test start full
```

The command prints the exact `record` commands for its scenario. Record each
observable gate as `pass`, `fail`, or `skip`, then finish the run:

```sh
sudo messagebox-test record message-received pass
sudo messagebox-test record message-played fail
sudo messagebox-test finish
```

`finish` succeeds only when every step passed or was explicitly skipped; any
failure or pending step returns a non-zero exit status. A report preserves the
partial transition—for example, received `true`, played `false`, replied
`false`—without including who sent the message or its content.

For a terminal-only bundle, run:

```sh
messagebox-test report --surface onboarding
messagebox-test report --surface dashboard --json
```

Attach the sanitized bundle to the GitHub issue or paste the Markdown into a
Codex task. Reproduce on the test rig and keep fixes on a branch until review;
production and household devices remain outside this workflow.

## Physical test scenarios

Use a spare Raspberry Pi 4 with a freshly imaged test microSD card. Confirm it
did not inherit state from a development or household device.

For installation and consumer onboarding, test:

- Clean installation and manufacturer handoff
- Wi-Fi success, failure, and recovery
- WhatsApp pairing and interruption
- From the WhatsApp-ready view, choose a recipient and wait at least 3.5 seconds
  (more than two 1.5-second poll cycles); the recipient chooser must remain open.
  Record this as `recipient-chooser-stable` before selecting a recipient.
- Empty-account recipient discovery and manual refresh
- Manual international-number selection and allow-listing, including invalid
  formats, duplicates, and default preservation when merely adding a recipient
- People displayed by valid international phone number, invalid direct-chat
  placeholders excluded, and refresh after the 100-message pairing bootstrap
  cap has been reached
- Initial default selection, switching the default among allowed recipients,
  protection from removing the current default, no-card routing after a switch,
  defer/resume, and recipient-manager recovery
- New voice note, physical playback, guided reply review, and accepted send
- Zero-tag Skip, person and group pairing, multiple tags per recipient,
  explicit reassignment, remove-after-beep behavior, Retry/Skip when the reader
  is unavailable, and the distinct read/success tones
- Reload within and after the two-minute pending-tag window, completion with and
  without mappings, and the onboarding-to-runtime service handoff
- Reboot and power loss during onboarding transitions

For the standalone developer flow and runtime, test:

- Explicit-default and multiple-recipient routing
- NFC enrollment, removal, and unknown cards
- Microphone, speaker, LED, and button behavior
- First send and reply
- Reboot and power-loss recovery
- Rollback to the previous working release
