# Installation

Validate installation changes on a spare Raspberry Pi 4 and microSD card.
Production/customer updates need a recovery plan. A disposable development box
may explicitly waive rollback backups; never include its private state in a
release artifact.

## Manufacturer preparation

1. In Raspberry Pi Imager, install Raspberry Pi OS Lite 64-bit based on Debian
   13. Enable SSH and create a non-root sudo-capable administrator. Set the
   hostname to the box's zero-padded number, such as `button-box-001`, and use
   the same number on the physical label and in inventory.
2. Review the included licensed guided-reply prompt set described in
   [`sounds/README.md`](../sounds/README.md). These recordings are required for
   the two-way voice proof.
3. From a repository clone on another computer, provision over SSH:

   ```sh
   ./scripts/provision.sh admin@button-box-NNN.local
   ```

   This transfers the installer's explicit source allowlist and runs setup on
   the Pi. The installer validates every prompt before making system changes.
   To use an alternate licensed prompt set, pass its directory with
   `--guided-prompts DIR`. Alternatively, clone the repository onto the Pi and
   run `./scripts/setup.sh` there as a non-root sudo-capable administrator.
   Neither method transfers device runtime state, pairs WhatsApp, or starts
   Button Box services.

   Updates migrate boot selection without changing the onboarding marker or
   runtime component selection. The installer validates the staged generator,
   removes the non-selected legacy boot link first, atomically installs the
   generator, then removes the selected legacy link. It enables the
   marker/reconciliation path only after a daemon reload proves that exactly
   one generated dependency matches the marker. Repeating the migration is
   safe. An unsafe marker or unexpected legacy link stops the update before
   either legacy link is removed.

   Connect the microphone and USB speaker before running setup. On a fresh
   installation, setup selects the lowest-numbered ALSA capture device and USB
   playback device, then records their card names in `/etc/messagebox/env`;
   setup fails if either is unavailable. Later updates preserve the existing
   file and operator customization.

   The installed audio-detection service discovers current ALSA card names at
   boot and supplies four audio-device overrides through
   `/run/messagebox-audio/audio.env`. Runtime and setup audio consumers wait
   for detection. With one microphone and one USB speaker, moving their USB
   ports while powered off should not require editing configuration. Live
   hot-plug recovery and choosing among multiple microphones/speakers are not
   guaranteed; power down before moving devices and repeat the physical check.

   The Pi needs internet access during setup. See
   [Internet access for a fresh card](developer-onboarding.md#internet-access-for-a-fresh-card).
   For an optionally shipped box that needs private, consented remote
   maintenance, complete the [Tailscale remote-support procedure](remote-support.md)
   while LAN access is still available. Tailscale is independent of the Button
   Box application install and does not replace ordinary OpenSSH keys.
4. Configure protected Wi-Fi onboarding with
   `sudo messagebox-init-wifi-onboarding`.
5. Print the displayed box number, hotspot name, password, and setup URL for the
   matching box. Treat the hotspot password as a credential: do not commit it,
   put it in shared logs, or retain an unprotected digital copy.
6. Run `sudo messageboxctl reset-wifi`. Verify the hotspot if needed, then run
   `sudo shutdown now` and package the printed insert with the powered-down box.
   Do not complete browser onboarding during manufacturing.

## Recipient setup

1. Power on Button Box.
2. Join its setup hotspot with the supplied password and open the printed URL.
3. Copy the setup URL before submitting Wi-Fi credentials. The setup hotspot
   will disappear, and an iPhone may close its captive setup window.
4. In the phone's Wi-Fi settings, join the exact network selected for the box
   (including a separate IoT network), then open the copied URL in Safari or
   Chrome. Closing the setup window does not prove the box connected. If the
   address does not open, verify the phone's network and retry. If the setup
   hotspot returns, reconnect to it and check the Wi-Fi name and password.
5. Pair WhatsApp using a number beginning with `+` and its international country
   code.
6. Choose a recent WhatsApp person or group as the initial default recipient, or
   enter a person's international phone number beginning with `+`. On a clean
   account, the selected person or group must still send the linked number a
   new message before the voice proof. Choosing **Do this later** pauses setup
   and leaves messaging and NFC disabled.
7. Ask the selected recipient to send a new voice note. Press the Button Box button to
   hear it, record the prompted reply, review it, and press again to approve the
   send. Messages received before selection are deliberately excluded.
8. After the two-way proof, optionally allow-list more recent people or groups
   in the recipient manager, or enter another international phone number
   manually. You can switch the default among allowed recipients in the manager;
   removing a recipient also removes that recipient's tag mappings.
9. Select **Continue to NFC setup**. Hold a tag over the reader until Button Box
   beeps, remove it, and choose the person or group it should represent. Pair as
   many tags as needed; multiple tags may point to one recipient.
10. An already-paired tag shows its current recipient and requires an explicit
    **Reassign** before it can move. The flow reads tag identifiers but never
    writes data to a tag.
11. Choose **Skip NFC setup** before the first tag or **Done** after pairing.
    Either action completes setup and activates messaging. If no tags are
    mapped, the default recipient works without the NFC reader.

The manufacturer must not complete these steps for the recipient.

After setup, the same printed URL opens the canonical Button Box dashboard.
Home shows connection and runtime health; Setup preserves the completed task
list; Settings controls button behavior, message length, ringtone, volume,
arrival signal, quiet hours, time zone, and the NFC confirmation beep. Activity
contains the privacy-sensitive timeline, browser audio, and queue controls.
Advanced contains concise health and listener-profile information.

The dashboard intentionally has no login. Anyone on household Wi-Fi can change
settings and play queued audio. Do not forward port 80 or publish the dashboard
through a tunnel. Runtime binds only to the Wi-Fi interface.

If the reader is unavailable, Retry or skip NFC setup. Once any tag is mapped,
runtime routing remains fail-closed when the NFC reader is unhealthy; it never
guesses the default while mapped-card state is unsafe. Do not continue with
`messagebox-dev-onboard`; it remains an independent prototype workflow.

Before deployment, run the [physical test scenarios](testing.md#physical-test-scenarios)
on a spare device.

## Boot-mode recovery

Use these checks only after stopping Button Box runtime and setup services.
The marker must be either absent for runtime or a root-owned regular mode-0600
file containing exactly `enabled` plus a newline for setup. Symlinks, devices,
directories, unexpected content, and unreadable markers intentionally select
neither mode.

After correcting an authorized marker or restoring reviewed unit files, run:

```sh
sudo systemctl daemon-reload
sudo systemctl start messagebox-mode-reconcile.path
sudo systemctl start messagebox-mode-reconcile.service
systemctl status messagebox-mode-reconcile.path messagebox-mode-reconcile.service
```

Runtime mode should have `messagebox.target` active, Comitup inactive, and all
enabled target components active. Setup mode should have the target inactive
and Comitup active; verify either its connected home portal or its setup
hotspot portal before calling recovery usable. Do not recreate either legacy
`multi-user.target.wants` link. The generator-owned link under `/run` is
ephemeral and must match the marker after every daemon reload.
