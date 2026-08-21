# Testing

## Repository tests

- `make test` runs the synthetic Python unit suite.
- `make lint` requires `uv` and Bun. It runs pinned Ruff, ShellCheck, and Biome
  versions through `uvx` and `bunx`; the first run downloads them.
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
- Reboot and power loss during onboarding transitions

For the standalone developer flow and runtime, test:

- Single- and multiple-recipient routing
- NFC enrollment, removal, and unknown cards
- Microphone, speaker, LED, and button behavior
- First send and reply
- Reboot and power-loss recovery
- Rollback to the previous working release
