# Installation

Use a spare Raspberry Pi 4 and microSD card when validating installation
changes. Do not overwrite a working household device without a tested backup.

## Manufacturer preparation

1. In Raspberry Pi Imager, install Raspberry Pi OS Lite 64-bit based on Debian
   13. Set the hostname to `message-box-NNN`, using the box's zero-padded
   three-digit number, for example `message-box-001`. Put the same number on the
   physical box and record it in the device inventory. Enable SSH and create a
   normal sudo-capable administrator. The software accepts other lowercase IDs
   for development, but shipped boxes should use the numbered convention.
2. Clone this repository onto the Pi as a normal sudo-capable administrator.
3. Run `./scripts/setup.sh`, or provision from another computer with
   `./scripts/provision.sh admin@message-box-NNN.local`. Provisioning transfers
   only explicit installation inputs. Neither path copies credentials, pairs
   WhatsApp, or starts Message Box services. The Pi needs internet access while
   setup installs packages. Configure temporary Wi-Fi in Raspberry Pi Imager,
   use routed Ethernet, or follow the macOS Internet Sharing instructions in
   [Developer onboarding](developer-onboarding.md#internet-access-for-a-fresh-card).
4. Configure protected Wi-Fi onboarding with
   `sudo messagebox-init-wifi-onboarding`.
5. Print the displayed box number, hotspot name, Wi-Fi password, and setup URL
   as a setup insert packaged with the box. Then run
   `sudo messageboxctl reset-wifi`.
6. Optionally verify that the setup hotspot appears, then run
   `sudo shutdown now`.

Do not complete the browser flow during manufacturing. Package the printed
setup insert with the matching powered-down box. Treat its Wi-Fi password as a
credential: do not commit it, paste it into shared logs, or retain an
unprotected digital copy.

## Recipient onboarding

The recipient powers on the box, joins its setup hotspot with the supplied
password, and opens the printed setup URL. After submitting home Wi-Fi details,
the setup hotspot disappears. The recipient reconnects the phone to home Wi-Fi,
waits up to two minutes, and opens the same printed URL, such as
`http://message-box-001.local/`, to continue with WhatsApp pairing. The WhatsApp
number must begin with `+` and its international country code. The manufacturer
should not perform these steps on the recipient's behalf.

The consumer flow currently stops at verified WhatsApp readiness. Recipient
selection, NFC enrollment, and runtime startup are not yet implemented there.
Do not continue with `messagebox-dev-onboard`; that installed command is a
separate, standalone developer workflow for configuring a prototype from an
interactive shell. See [`developer-onboarding.md`](developer-onboarding.md) for
that flow, macOS Internet Sharing, contact routing, and dashboard precautions.

The installed Python code is the top-level `messagebox/` package and is invoked
with `python3 -m messagebox...` from `/opt/messagebox`; installation does not
depend on a project package build or `PYTHONPATH`.

Validate the complete consumer journey and recovery cases on physical hardware
before deploying a changed installation to another box.
