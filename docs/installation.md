# Installation

Validate installation changes on a spare Raspberry Pi 4 and microSD card. Do
not overwrite a working device without a tested backup.

## Manufacturer preparation

1. In Raspberry Pi Imager, install Raspberry Pi OS Lite 64-bit based on Debian
   13. Enable SSH and create a non-root sudo-capable administrator. Set the
   hostname to the box's zero-padded number, such as `message-box-001`, and use
   the same number on the physical label and in inventory.
2. Obtain a licensed guided-reply prompt set containing the five files listed
   in [`sounds/README.md`](../sounds/README.md). These recordings are required
   for the two-way voice proof but are intentionally not distributed in this
   repository.
3. From a repository clone on another computer, provision over SSH and supply
   the prompt directory explicitly:

   ```sh
   ./scripts/provision.sh --guided-prompts /path/to/guided-reply admin@message-box-NNN.local
   ```

   This transfers the installer's explicit source allowlist and runs setup on
   the Pi. The installer validates every prompt before making system changes.
   Alternatively, clone the repository onto the Pi, put the licensed files in
   `sounds/guided-reply/`, and run `./scripts/setup.sh` there as a non-root
   sudo-capable administrator. Neither method transfers device runtime state,
   pairs WhatsApp, or starts Message Box services.

   Setup installs the supported hardware defaults as `/etc/messagebox/env` only
   when that file does not already exist. Later updates preserve operator
   customization.

   The Pi needs internet access during setup. See
   [Internet access for a fresh card](developer-onboarding.md#internet-access-for-a-fresh-card).
4. Configure protected Wi-Fi onboarding with
   `sudo messagebox-init-wifi-onboarding`.
5. Print the displayed box number, hotspot name, password, and setup URL for the
   matching box. Treat the hotspot password as a credential: do not commit it,
   put it in shared logs, or retain an unprotected digital copy.
6. Run `sudo messageboxctl reset-wifi`. Verify the hotspot if needed, then run
   `sudo shutdown now` and package the printed insert with the powered-down box.
   Do not complete browser onboarding during manufacturing.

## Recipient onboarding

1. Power on the box.
2. Join its setup hotspot with the supplied password and open the printed URL.
3. Submit the home Wi-Fi credentials. The setup hotspot will disappear.
4. Reconnect the phone to home Wi-Fi and reopen the same URL, such as
   `http://message-box-001.local/`. The network switch may take up to two
   minutes; retry if the page is not ready.
5. Pair WhatsApp using a number beginning with `+` and its international country
   code.
6. Choose a recent WhatsApp person or group as the initial default recipient, or
   enter a person's international phone number beginning with `+`. On a clean
   account, the selected person or group must still send the linked number a
   new message before the voice proof. Choosing **Do this later** pauses setup
   and leaves messaging and NFC disabled.
7. Ask the selected recipient to send a new voice note. Press the box button to
   hear it, record the prompted reply, review it, and press again to approve the
   send. Messages received before selection are deliberately excluded.
8. After the two-way proof, optionally allow-list more recent people or groups
   in the recipient manager, or enter another international phone number
   manually. You can switch the default among allowed recipients in the manager;
   removing a recipient also removes that recipient's tag mappings.
9. Select **Continue to NFC setup**. Hold a tag over the reader until the box
   beeps, remove it, and choose the person or group it should represent. Pair as
   many tags as needed; multiple tags may point to one recipient.
10. An already-paired tag shows its current recipient and requires an explicit
    **Reassign** before it can move. The flow reads tag identifiers but never
    writes data to a tag.
11. Choose **Skip NFC setup** before the first tag or **Done** after pairing.
    Either action completes onboarding and activates messaging. If no tags are
    mapped, the default recipient works without the NFC reader.

The manufacturer must not complete these steps for the recipient.

If the reader is unavailable, Retry or skip NFC setup. Once any tag is mapped,
runtime routing remains fail-closed when the NFC reader is unhealthy; it never
guesses the default while mapped-card state is unsafe. Do not continue with
`messagebox-dev-onboard`; it remains an independent prototype workflow.

Before deployment, run the [physical test scenarios](testing.md#physical-test-scenarios)
on a spare device.
