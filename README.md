# Message Box

Message Box is an experimental, screen-free Raspberry Pi device for exchanging
voice messages with approved WhatsApp contacts.

> [!WARNING]
> Message Box controls physical hardware and local network services. Test changes
> on a spare Raspberry Pi and microSD card before relying on them.

## Project status

This repository contains the device runtime, Raspberry Pi setup,
systemd services, Wi-Fi and WhatsApp onboarding, NFC routing, and automated
tests. Raspberry Pi 4 enclosure and assembly files will be added after their
designs, licenses, and physical fit have been validated. Audio whose
redistribution rights are not established is not included.

The consumer web flow reaches verified Wi-Fi and WhatsApp readiness, then
stops. Recipient selection, NFC enrollment, and runtime startup are not yet
implemented in that flow.

`messagebox-dev-onboard` is a separate, standalone developer workflow for
configuring and starting an installed prototype from an interactive shell. It
does not configure Wi-Fi and is not the next step after the consumer web flow.
See the [developer onboarding guide](docs/developer-onboarding.md).

## Repository map

- `messagebox/`: importable device runtime, dashboard, and onboarding package
- `messagebox/dashboard/static/`: private dashboard assets
- `messagebox/onboarding/static/`: consumer onboarding portal assets
- `scripts/`: setup, explicit-input provisioning, and installed command wrappers
- `scripts/dev/`: developer onboarding, internal Pi hardware checks, and macOS dhcp script
  for connecting directly to a Pi over Ethernet
- `systemd/`: service and target definitions
- `config/`: minimal public configuration and pinned NFC dependencies
- `hardware/`: status of future Raspberry Pi 4 hardware files
- `tests/`: synthetic unit and contract tests
- `docs/`: architecture, privacy, testing, and maintainer guidance

## Development checks

Run the repository test suite without touching attached hardware:

```sh
make test
```

Run all local checks available on the machine:

```sh
make check
```

Provision a prepared Pi, open its installed dev flow, or start the macOS direct
Ethernet helper with the repository scripts. Shipped boxes use a zero-padded
three-digit number that matches the physical label:

```sh
./scripts/provision.sh admin@message-box-001.local
./scripts/dev/reprovision.sh admin@message-box-001.local
ssh -t admin@message-box-001.local messagebox-dev-onboard
sudo ./scripts/dev/macos-direct-ethernet.sh en8
```

`reprovision` is a destructive dev shortcut for a disposable box reachable by
SSH, including through routed Ethernet, macOS Internet Sharing, or the direct
Ethernet helper at `10.77.77.77`. It clears consumer onboarding and WhatsApp
test credentials before deploying the current tree. It does not replace a
clean-card installation test.

Flash Raspberry Pi OS with Raspberry Pi Imager rather than a Make target so the
target disk, hostname, SSH, and administrator settings stay explicit.

`/opt/messagebox/dev/hardware-test.sh` is a separate internal interactive
hardware test on an installed Raspberry Pi. `messagebox-dev-onboard` runs it as
the `messagebox` service user. It is not the repository unit-test command.

## Installation status

The installer targets Raspberry Pi 4 and Raspberry Pi OS Lite 64-bit based on
Debian 13. The setup path is documented in
[`docs/installation.md`](docs/installation.md).

Dev setup, direct Ethernet access, contact routing, and dashboard
exposure are documented separately in
[`docs/developer-onboarding.md`](docs/developer-onboarding.md).

## Privacy and safety

Do not put WhatsApp authentication stores, Wi-Fi credentials, phone numbers,
contact records, NFC identifiers, recordings, private URLs, or device state in
this repository. See [`docs/privacy.md`](docs/privacy.md).

Message Box currently uses an unofficial WhatsApp Web client. It is not
affiliated with or endorsed by WhatsApp or Meta. Account access may stop
working if the upstream service changes.

## License

Project software is licensed under the [MIT License](LICENSE). Third-party
components retain their own licenses. Hardware, documentation, and future
audio assets require explicit artifact-level licensing before distribution.
