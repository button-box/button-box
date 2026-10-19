# Install Button Box

This is the short path: flash a microSD card, boot the Pi, run **one command**
on it, and scan the QR code. It takes about 30 minutes, most of it waiting.

Using an AI coding agent? Give it [INSTALL-WITH-AN-AGENT.md](INSTALL-WITH-AN-AGENT.md).

## Before you start

- **Pi:** Raspberry Pi 4 Model B or Raspberry Pi 3 Model A+. These are the two
  boards with signed software updates. A Pi Zero 2 W is not supported for Cloud
  updates; use the [manual install](#manual-install-from-your-computer).
- **Card:** a microSD card of 16 GB or more.
- **Network:** Wi-Fi (or Ethernet) with internet access.
- **Hardware:** the box can be assembled or not. The installer only warns when
  the card reader is not wired yet.

## 1. Flash the microSD card

Install the current [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
on your computer. In Imager:

1. Select your exact Raspberry Pi model.
2. Select **Raspberry Pi OS Lite (64-bit)** based on Debian 13 (trixie).
3. Choose the correct microSD card.
4. Set a hostname such as `button-box-001`.
5. Create a non-root user, for example `admin`. Imager gives it passwordless
   `sudo`; the installer needs that.
6. Set your Wi-Fi network, Wi-Fi country, and time zone.
7. Enable SSH, preferably with a public key.
8. Confirm the exact removable card before writing.

> [!CAUTION]
> Writing an operating-system image erases the selected card. Confirm its
> physical identity, capacity, and partitions before continuing.

> [!IMPORTANT]
> Imager remembers the Wi-Fi name and password from earlier runs. When you
> flash again after a Pi did not appear, type the Wi-Fi name again.

**Done when:** the card is written and verified.

## 2. Boot the Pi and open a terminal on it

Put the card in the powered-off Pi, then power it on. Wait about two minutes.
From your computer (replace the user and hostname with yours):

```sh
ssh admin@button-box-001.local
```

**Done when:** you see a prompt such as `admin@button-box-001:~ $`.

## 3. Run the installer

On the Pi, run (replace `<tag>` with the newest release tag on the
[Releases page](https://github.com/button-box/button-box/releases), for example `v0.3.13`):

```sh
curl -fsSL https://raw.githubusercontent.com/button-box/button-box/<tag>/scripts/install/install.sh | sudo bash -s -- --mode cloud --updates auto
```

From a checkout of this repository, the same is:

```sh
sudo sh scripts/install/install.sh --mode cloud --updates auto
```

The installer:

1. Checks the Pi: root, model, OS (trixie, 64-bit), internet, free disk space,
   and the clock. Each failure prints what to do next.
2. Downloads the latest signed stable release from GitHub (or `--version`) and
   verifies it with the release key pinned in the installer. It never runs code
   that fails this check.
3. Installs it exactly as a box is installed today (`scripts/setup.sh`): the
   same files, services, users, and permissions.
4. Turns on signed updates (`update-bootstrap.sh auto stable`).
5. Installs Tailscale for remote support. It does not enrol it.
6. Runs `sudo messageboxctl doctor`.
7. Registers the box with Button Box Cloud and prints its setup link,
   `https://button.box/s/<setup code>`, with a QR code.

The first run takes 15 to 25 minutes. It logs everything, without secrets, to
`/var/log/button-box-install.log` (readable by root only).

**Done when:** the last lines show the QR code, `Or open: https://button.box/s/...`,
and `Button Box is installed.`

### The setup Wi-Fi password

At the end, the installer runs `messagebox-init-wifi-onboarding` once. It shows
the password of the box's own **setup Wi-Fi** and asks you to confirm that you
wrote it down. The box only opens this Wi-Fi when it cannot reach your home
Wi-Fi, so you can fix the Wi-Fi from your phone later. The password is shown
only on your terminal. It is never logged.

## 4. Scan the QR code

Scan the QR code with your phone (or open the link). WhatsApp opens with a
prepared message. Send it, and follow the Button Box assistant. When the box
asks, press its button. That proves you have the box in your hands.

**Done when:** WhatsApp confirms that your Button Box is set up.

## Options

| Option | Meaning |
| --- | --- |
| `--mode cloud` | Button Box Cloud with signed updates (default for a new box). |
| `--mode wacli` | Self-hosted: the box uses your own linked WhatsApp account. No Cloud. |
| `--updates auto` | Cloud updates install by themselves at night (default). |
| `--updates ask` | The box asks first; you hold its button to install. |
| `--version vX.Y.Z` | Install this signed release instead of the latest. |
| `--timezone Europe/Lisbon` | Set the time zone first. |
| `--admin USER` | The sudo user that runs setup (default: the user who ran `sudo`). |
| `--check` | Run the checks in step 1 only. Installs nothing. |

Exit codes: `0` done, `2` bad option, `3` a check failed, `4` download or
signature failed (nothing was installed), `5` an install step failed, `6` the
Cloud setup link failed, `7` installed, but the doctor found a problem.

## Run it again, check the box, get the link again

- **Run the installer again** at any time. It repairs what is missing and does
  nothing when the box is fine. On a Cloud box with signed updates on, it does
  not reinstall: updates come from the signed updater.
- **Check the box:** `sudo messageboxctl doctor` prints one line per area
  (services, permissions, audio, card reader, Cloud, updates), each `PASS`,
  `WARN` or `FAIL`, with a `Fix:` line for each problem.
- **Get the setup link again:** `sudo messageboxctl claim-link`.

## Self-hosted box (`--mode wacli`)

```sh
curl -fsSL https://raw.githubusercontent.com/button-box/button-box/<tag>/scripts/install/install.sh | sudo bash -s -- --mode wacli
```

The box does not use Button Box Cloud and has no signed updates. When the
installer finishes, link WhatsApp and choose the people with
`messagebox-dev-onboard` ([Path A](#path-a-terminal-assisted-diy-build)) or the
browser setup ([Path B](#path-b-browser-wi-fi-and-whatsapp-onboarding)).

## Install without a keyboard (cloud-init)

The installer runs unattended as root. Add this line to the `runcmd:` section
of the card's cloud-init `user-data`:

```yaml
runcmd:
  - [sh, -c, "curl -fsSL https://raw.githubusercontent.com/button-box/button-box/<tag>/scripts/install/install.sh | sh -s -- --mode cloud --updates auto"]
```

The installer waits up to two minutes for the network and one minute for the
clock. The user-data must create a user with passwordless sudo (as Imager
does). Without a terminal, the installer cannot show the setup Wi-Fi password,
so it registers the box and logs its setup link, but does not open claim setup.
To finish, open a terminal on the box and run `sudo messageboxctl claim-link`.
The output is in `/var/log/button-box-install.log` and
`/var/log/cloud-init-output.log`.

## Troubleshooting

| Message | What to do |
| --- | --- |
| `Raspberry Pi Zero 2 W is not supported for Cloud updates.` | Use a Pi 4 Model B or Pi 3 Model A+, or the [manual install](#manual-install-from-your-computer). |
| `Raspberry Pi OS trixie is required` / `The 64-bit OS is required` | Flash Raspberry Pi OS Lite (64-bit), Debian 13. |
| `No internet connection` | Check Wi-Fi: `ping -c 3 github.com`. Run the installer again. |
| `Not enough free space` | Use a larger card, or remove files. |
| `The clock is not synchronised.` | Wait a minute after the Pi joins the network: `timedatectl status`. |
| `User admin needs passwordless sudo.` | Use the user Imager created, or pass `--admin USER`. |
| `The release signature is not valid.` | Stop. Do not install these files. Report it in an issue. |
| `Release vX.Y.Z is older than this installer` | Use a newer release with `--version`. |
| `Button Box setup failed.` | Read the lines above it in the log, fix the cause, and run the installer again. |
| A `FAIL` line in the doctor | Follow its `Fix:` line, then run `sudo messageboxctl doctor`. |

When you ask for help, never post Wi-Fi names or passwords, phone numbers, keys,
or the device files in `/var/lib/messagebox-cloud`.

## Manual install from your computer

Use this path for a Pi Zero 2 W, for a self-hosted box you install from a
laptop, or for development. It installs from a checkout, not a signed release,
so a Cloud box installed this way has no signed updates until you run
`update-bootstrap.sh` (see [on-box updates](docs/on-box-updates.md)).

Flash and boot the Pi as in steps 1 and 2 above, then:

### M1. Download Button Box on your computer

On the computer that will provision the Pi:

```sh
git clone https://github.com/button-box/button-box.git
cd button-box
```

Confirm the checkout and required host commands:

```sh
git status --short --branch
ssh -V
rsync --version
```

On macOS, use a modern GNU rsync rather than the built-in `openrsync`:

```sh
brew install rsync
PATH="/opt/homebrew/bin:$PATH" rsync --version
```

**Done when:** the repository is on your computer and SSH plus modern rsync are
available.

### M2. Connect to and inspect the Pi

Replace `admin` and the hostname below if you chose different values:

```sh
ssh admin@button-box-001.local
```

On the Pi, verify the model, architecture, and operating-system release:

```sh
tr -d '\0' </proc/device-tree/model
uname -m
grep -E '^(PRETTY_NAME|VERSION_CODENAME)=' /etc/os-release
```

Expected architecture: `aarch64`

Expected Debian codename: `trixie`

Exit the Pi:

```sh
exit
```

**Done when:** the detected model matches your chosen board and the supported
64-bit OS is running.

### M3. Install Button Box

> [!IMPORTANT]
> Continue with the automated public installer on Raspberry Pi 4B. The
> installer (`scripts/install/comitup.sh`) also accepts Raspberry Pi 3
> (A+/B/B+) and Pi Zero 2 W, but those fresh-card installations are not yet
> physically validated. The signed software updater supports Raspberry Pi 4
> Model B and Pi 3 Model A+ only; it does not support Pi Zero 2 W yet.

From the repository root on your computer:

```sh
./scripts/provision.sh admin@button-box-001.local
```

On macOS with Homebrew rsync:

```sh
PATH="/opt/homebrew/bin:$PATH" ./scripts/provision.sh admin@button-box-001.local
```

The script transfers an explicit set of installation files, installs Button Box
in fixed system paths, and leaves runtime and onboarding services stopped.

**Done when:** setup prints `BUTTON BOX SETUP COMPLETE` without an error.

### M4. Choose an onboarding path

#### Path A: terminal-assisted DIY build

This is currently the shortest path to a working experimental box. It uses the
Pi's temporary network connection and does not use the consumer Wi-Fi portal.

Run from your computer:

```sh
ssh -t admin@button-box-001.local messagebox-dev-onboard
```

The guided workflow will:

1. Link or verify WhatsApp.
2. Display recent chats with their exact WhatsApp identifiers.
3. Configure the first approved recipient.
4. Optionally configure the private dashboard.
5. Offer to test the physical hardware.
6. Enable and start the selected runtime services.

Enter phone numbers and other private values directly into the terminal when
prompted. Do not paste them into an agent conversation or GitHub issue.

**Done when:** the workflow reports WhatsApp ready, the exact intended recipient
is configured, the selected hardware tests pass, and the selected services are
enabled and started.

#### Path B: browser Wi-Fi and WhatsApp onboarding

This path is the intended household experience from Wi-Fi through normal
runtime activation.

Initialize the protected onboarding identity:

```sh
ssh -t admin@button-box-001.local sudo messagebox-init-wifi-onboarding
```

Record the displayed hotspot name, hotspot password, and setup URL privately.
Then arm onboarding:

```sh
ssh -t admin@button-box-001.local sudo messageboxctl reset-wifi
```

On a phone:

1. Join the Button Box setup hotspot.
2. Open the supplied setup URL.
3. Select home Wi-Fi and enter its password.
4. Rejoin home Wi-Fi when the setup hotspot disappears.
5. Open the same `http://button-box-001.local/` address.
6. Link WhatsApp with the displayed phone code.
7. Choose a default recipient and complete the guided two-way voice test.
8. Allow any additional recipients and optionally pair NFC tags.
9. Skip NFC setup or choose Done to activate messaging.

NFC is optional. A zero-tag setup routes new standalone messages to the default
recipient without depending on the reader. Once any tag is mapped, unsafe NFC
health or unknown tag state blocks rather than silently choosing the default.
Path A remains the terminal-assisted developer workflow; the two paths are not
continuations of one another.

**Done when:** the browser reports that Button Box is ready and the intended
default and optional tag mappings pass the physical scenarios below.
