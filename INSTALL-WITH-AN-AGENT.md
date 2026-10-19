# Install Button Box with an AI coding agent

This page is for an AI coding agent (Claude Code, Codex, Cursor, or similar)
that helps a person install Button Box. The person does the physical steps. The
agent runs or explains the commands, reads the output, and stops when the output
differs from this page. The human version is [INSTALL.md](INSTALL.md).

## Rules for the agent

1. **Privacy.** Never ask the person to paste, and never write into chat, files,
   commits, issues or logs: Wi-Fi names or passwords, the setup Wi-Fi password,
   phone numbers, WhatsApp names or IDs, family names, card IDs, recordings,
   SSH keys, tokens, or anything from `/var/lib/messagebox-cloud`,
   `/etc/messagebox/env`, `/etc/comitup.conf` or `~/.ssh`. If output contains
   one, do not repeat it; refer to it as "the value on your screen".
2. **Keys.** Never paste, generate, or replace a signing key. Never pass
   `--public-key` on a real box (it is for CI only). Never edit the pinned key
   in `install.sh`.
3. **No workarounds for safety checks.** Do not skip, edit or patch the model,
   OS, signature, or hash checks. If a check fails, follow the table below or stop.
4. **Ask before anything destructive:** flashing a card, `rm -rf`,
   `reset-wifi`, reformatting, or changing `/etc/messagebox/env`.
5. **One step at a time.** Wait for the person to confirm each physical result
   (card written, Pi powered, QR scanned, button pressed).
6. **Report honestly.** Say which steps passed, which were skipped, and what is
   not verified (for example, audio not tested because no speaker is plugged in).

## Step 1. Flash the card (the person does this)

Ask the person to flash **Raspberry Pi OS Lite (64-bit)**, Debian 13 (trixie),
with Raspberry Pi Imager. In Imager they set: hostname, a non-root user with
SSH, Wi-Fi, Wi-Fi country, and time zone. Supported boards: **Raspberry Pi 4
Model B** and **Raspberry Pi 3 Model A+**. Do not ask for the Wi-Fi details.

## Step 2. Open a terminal on the Pi

```sh
ssh <user>@<hostname>.local
```

Expected: a shell prompt on the Pi. If the name does not resolve, follow
[the README troubleshooting](README.md#the-pi-hostname-does-not-resolve).

## Step 3. Run the checks only

```sh
curl -fsSL https://raw.githubusercontent.com/button-box/button-box/<tag>/scripts/install/install.sh | sudo bash -s -- --check
```

Use the newest tag from <https://github.com/button-box/button-box/releases>.

Expected, in order:

```text
==> [hh:mm:ss] Checking this Pi
Pi model: Raspberry Pi 4 Model B Rev 1.5
OS: Raspberry Pi OS trixie, 64-bit
Internet: https://github.com is reachable
Disk: 25000 MB free
Clock: synchronised
Administrator: <user>
Mode: cloud
Preflight passed. Nothing was installed.
```

Exit code `0`. On exit code `3`, use the table in step 7.

## Step 4. Install

```sh
curl -fsSL https://raw.githubusercontent.com/button-box/button-box/<tag>/scripts/install/install.sh | sudo bash -s -- --mode cloud --updates auto
```

Ask the person whether updates should be `auto` (install by themselves at night)
or `ask` (the box asks; they hold its button). Use `--mode wacli` only if they
want a self-hosted box with their own WhatsApp account and no Cloud.

It takes 15 to 25 minutes. Expected milestones:

```text
==> ... Checking this Pi
==> ... Downloading and verifying the signed release
Verified vX.Y.Z (commit 0123456789ab)
==> ... Installing Button Box vX.Y.Z (scripts/setup.sh as <user>)
             ✅ BUTTON BOX SETUP COMPLETE ✅
==> ... Turning on signed updates (auto, stable)
Fixed updater installed. ...
==> ... Checking the box (sudo messageboxctl doctor)
Button Box doctor (Cloud box)
  PASS  ...
==> ... Connecting to Button Box Cloud
```

`BUTTON BOX SETUP COMPLETE` is followed by developer instructions from
`setup.sh`. Ignore them; the installer continues by itself.

Near the end, the installer shows the **setup Wi-Fi password** and asks
`Wi-Fi password recorded? [y/N]`. This is for the person. Tell them to write it
down and type `y` themselves. Do not read the password back, and do not type
`y` for them.

The run ends with a QR code and:

```text
Or open: https://button.box/s/<code>
The box is waiting for you. ...
Button Box is installed. Log: /var/log/button-box-install.log
```

## Step 5. Claim the box (the person does this)

The person scans the QR code with their phone, sends the prepared WhatsApp
message, follows the assistant, and presses the box's button when asked. The
setup link is not secret, but do not post it publicly.

## Step 6. Check the box

```sh
sudo messageboxctl doctor
```

Each line is `PASS`, `WARN` or `FAIL`, and every `WARN`/`FAIL` has a `Fix:`
line. Expected on a new Cloud box after the claim: no `FAIL`. A `WARN` for
`nfc` is normal when the card reader is not wired yet. Exit code `0` means no
`FAIL`.

| Area | Checks |
| --- | --- |
| `services` | No Button Box or Comitup unit failed; setup mode or the selected services are running. |
| `permissions` | The `messagebox` user can read its program, NFC venv, config and data files. |
| `audio` | The speaker and the microphone are detected. |
| `nfc` | The card reader answers on I2C (warning only). |
| `cloud` | The Cloud API is reachable over HTTPS (Cloud boxes). |
| `updates` | The signed updater is bootstrapped and its four units are enabled (Cloud boxes). |
| `wacli` | `wacli` is installed (self-hosted boxes). |

## Step 7. When something fails

The installer prints `ERROR: ...` and `Next: ...`. Do what `Next:` says. The
exit code tells you the stage:

| Exit | Meaning | What to do |
| --- | --- | --- |
| 2 | Bad option | Fix the command line. `--help` lists the options. |
| 3 | A check failed | See the messages below. Nothing was installed. |
| 4 | Download or signature failed | Nothing was installed. On `The release signature is not valid`, **stop** and tell the person to report it. Do not retry with other files. |
| 5 | An install step failed | Read the 30 lines above the error in `/var/log/button-box-install.log` (`sudo tail -n 60 /var/log/button-box-install.log`). Fix the cause, then run the same command again. It continues. |
| 6 | No Cloud setup link | Run `sudo messageboxctl doctor`. If `cloud` fails, fix the network. Then `sudo messageboxctl claim-link`. |
| 7 | Doctor found a `FAIL` | Follow each `Fix:` line, then `sudo messageboxctl doctor`. |

| Message | What to do |
| --- | --- |
| `Run the installer as root.` | Add `sudo`. |
| `Raspberry Pi Zero 2 W is not supported for Cloud updates.` | Stop. Tell the person the supported boards. A self-hosted Zero 2 W uses the manual install in INSTALL.md. |
| `This Pi is not supported` | Stop. Supported: Pi 4 Model B, Pi 3 Model A+. |
| `Raspberry Pi OS trixie is required` or `The 64-bit OS is required` | The person must flash Raspberry Pi OS Lite (64-bit), Debian 13. |
| `No internet connection` | `ping -c 3 github.com`. Ask the person to check Wi-Fi. |
| `Not enough free space` | `df -h /`. A 16 GB card or larger is needed. |
| `The clock is not synchronised.` | Wait one minute, `timedatectl status`, run again. |
| `User <user> needs passwordless sudo.` | Use the Imager user, or `--admin USER` for a user with passwordless sudo. Do not edit sudoers without asking. |
| `This box is already installed in <mode> mode.` | Run again with that `--mode`. Changing the mode is not supported. |
| `Release vX.Y.Z is older than this installer` | Use a newer `--version`. |

When you write an issue or a summary, include: Pi model, OS release, the
installer exit code, the `ERROR`/`Next` lines, and the doctor lines. Remove
every private value listed in rule 1 first.

## Running it again

Running the same install command again is safe. It repairs or does nothing.
To get the setup link again: `sudo messageboxctl claim-link`.
