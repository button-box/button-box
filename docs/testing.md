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

## Physical test scenarios

Use a spare Raspberry Pi 4 with a freshly imaged test microSD card. Confirm it
did not inherit state from a development or household device.

For installation and consumer onboarding, test:

- Clean installation and manufacturer handoff
- Wi-Fi success, failure, and recovery
- WhatsApp pairing and interruption
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
- Reboot and power loss during onboarding transitions

For the standalone developer flow and runtime, test:

- Explicit-default and multiple-recipient routing
- NFC enrollment, removal, and unknown cards
- Microphone, speaker, LED, and button behavior
- First send and reply
- Reboot and power-loss recovery
- Rollback to the previous working release
