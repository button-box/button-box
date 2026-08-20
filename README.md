# Message Box

Message Box is an experimental, screen-free Raspberry Pi device for exchanging
voice messages with approved WhatsApp contacts.

> [!WARNING]
> This is a private, disposable release candidate. It is not ready for public
> use, has not passed the clean-card physical acceptance gates, and must not be
> installed over a working Message Box device.

## Current candidate

This clean-history candidate contains the device runtime, Raspberry Pi setup,
systemd services, Wi-Fi and WhatsApp onboarding, NFC routing, and automated
tests. It intentionally excludes private history, production state, family
records, obsolete enclosure files, personalized assembly material, and audio
whose redistribution rights are not established.

The current web flow reaches verified Wi-Fi and WhatsApp readiness. Recipient
selection and NFC enrollment still use operator tooling, so the required fully
self-service Wi-Fi, WhatsApp, and NFC journey is not complete.

## Repository map

- `src/`: device runtime and onboarding application
- `scripts/`: installation, provisioning, recovery, and hardware checks
- `systemd/`: service and target definitions
- `config/`: minimal public configuration and pinned NFC dependencies
- `tests/`: synthetic unit and contract tests
- `docs/`: architecture, privacy, testing, and maintainer guidance
- `release/`: source allowlist, provenance, and file-by-file disposition

## Development checks

Run the repository test suite without touching attached hardware:

```sh
make test
```

Run all local checks available on the machine:

```sh
make check
```

`scripts/test.sh` is a separate interactive hardware test intended for an
installed Raspberry Pi. It is not the repository unit-test command.

## Installation status

The installer targets Raspberry Pi 4 and Raspberry Pi OS Lite 64-bit based on
Debian 13. The candidate setup path is documented in
[`docs/installation.md`](docs/installation.md), but publication remains blocked
until it succeeds on a clean card using only public instructions.

## Privacy and safety

Do not put WhatsApp authentication stores, Wi-Fi credentials, phone numbers,
contact records, NFC identifiers, recordings, private URLs, or device state in
this repository. See [`docs/privacy.md`](docs/privacy.md) and
[`SECURITY.md`](SECURITY.md).

Message Box currently uses an unofficial WhatsApp Web client. It is not
affiliated with or endorsed by WhatsApp or Meta. Account access may stop
working if the upstream service changes.

## License

Project software is licensed under the [MIT License](LICENSE). Third-party
components retain their own licenses. Hardware, documentation, and future
audio assets require explicit artifact-level licensing before publication.
