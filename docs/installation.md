# Installation

Validate installation changes on a spare Raspberry Pi 4 and microSD card. Do
not overwrite a working device without a tested backup.

## Manufacturer preparation

1. In Raspberry Pi Imager, install Raspberry Pi OS Lite 64-bit based on Debian
   13. Enable SSH and create a non-root sudo-capable administrator. Set the
   hostname to the box's zero-padded number, such as `button-box-001`, and use
   the same number on the physical label and in inventory.
2. From a repository clone on another computer, provision over SSH:

   ```sh
   ./scripts/provision.sh admin@button-box-NNN.local
   ```

   This transfers the installer's explicit source allowlist and runs setup on
   the Pi. Alternatively, clone the repository onto the Pi and run
   `./scripts/setup.sh` there as a non-root sudo-capable administrator. Neither
   method transfers device runtime state, pairs WhatsApp, or starts Button Box
   services.

   The Pi needs internet access during setup. See
   [Internet access for a fresh card](developer-onboarding.md#internet-access-for-a-fresh-card).
3. Configure protected Wi-Fi onboarding with
   `sudo messagebox-init-wifi-onboarding`.
4. Print the displayed box number, hotspot name, password, and setup URL for the
   matching box. Treat the hotspot password as a credential: do not commit it,
   put it in shared logs, or retain an unprotected digital copy.
5. Run `sudo messageboxctl reset-wifi`. Verify the hotspot if needed, then run
   `sudo shutdown now` and package the printed insert with the powered-down box.
   Do not complete browser onboarding during manufacturing.

## Recipient onboarding

1. Power on the box.
2. Join its setup hotspot with the supplied password and open the printed URL.
3. Submit the home Wi-Fi credentials. The setup hotspot will disappear.
4. Reconnect the phone to home Wi-Fi and reopen the same URL, such as
   `http://button-box-001.local/`. The network switch may take up to two
   minutes; retry if the page is not ready.
5. Pair WhatsApp using a number beginning with `+` and its international country
   code.

The manufacturer must not complete these steps for the recipient.

The consumer flow stops at verified WhatsApp readiness. Recipient selection,
NFC enrollment, and runtime startup are not yet included. Do not continue with
`messagebox-dev-onboard`; it is an independent prototype workflow documented in
[Developer onboarding](developer-onboarding.md).

Before deployment, run the [physical test scenarios](testing.md#physical-test-scenarios)
on a spare device.
