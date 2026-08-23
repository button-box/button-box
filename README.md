# Button Box

Screen-free WhatsApp voice notes

WE ARE BUILDING IN PUBLIC

Some stuff doesn't work yet. Help us with a good PR.

## Repo

- `messagebox/`: importable device runtime, dashboard, and onboarding package
- `messagebox/dashboard/static/`: private dashboard assets
- `messagebox/onboarding/static/`: consumer onboarding portal assets
- `scripts/`: setup, provisioning, and command wrappers
- `scripts/dev/`: developer onboarding and internal Pi hardware checks
- `systemd/`: service and target definitions
- `config/`: minimal public configuration and pinned NFC dependencies
- `hardware/`: [prototype BOM](hardware/README.md), case and assembly files coming soon
- `tests/`: synthetic unit and contract tests
- `docs/`: architecture, installation, testing, and developer guidance

## Setup

Tested on Raspberry Pi 4B with Raspberry Pi OS Lite (64-bit). See
[`docs/installation.md`](docs/installation.md) for more details.

From host machine run:

```sh
./scripts/provision.sh admin@button-box-001.local
ssh -t admin@button-box-001.local messagebox-dev-onboard
```

## Support

Use GitHub issues. Before sharing
logs or screenshots, remove phone numbers, JIDs, Wi-Fi details, NFC identifiers,
recordings, private addresses, and authentication state.

## License

[MIT](LICENSE)

Button Box uses [wacli](https://github.com/openclaw/wacli). It is not
affiliated with or endorsed by WhatsApp or Meta.
