# Button Box

**Button Box is a screen-free way for kids to send and receive WhatsApp voice
messages with family on their own.**

Button Box is an open-source hardware and software project built around one
large illuminated button. A child can record a voice note and send it to an
approved person or group. Incoming replies wait on the box until the child
presses the button to listen.

[Visit button.box](https://button.box/) ·
[Join the WhatsApp community](https://chat.whatsapp.com/FJ8LYL79k8zEMfoiyPjLPb?mode=gi_t) ·
[Report an issue](https://github.com/button-box/button-box/issues)


## Build a Button Box

The complete journey is:

1. Buy the parts.
2. Assemble the hardware.
3. Prepare a Raspberry Pi OS microSD card.
4. Download this repository.
5. Connect to and inspect the Pi.
6. Provision Button Box.
7. Link WhatsApp, choose approved recipients, and optionally pair NFC tags.
8. Test the physical hardware.
9. Send, receive, play, and repeat after a reboot.

Each detailed step below ends with a **Done when** checkpoint. If the observed
result differs, stop there and troubleshoot instead of pushing ahead.

## Step 1 — Buy your parts

### Choose a Raspberry Pi

| Board | Current status | What to expect |
| --- | --- | --- |
| **Raspberry Pi 4B** | Recommended for a first build | The public installation path has been physically exercised through Wi-Fi and WhatsApp readiness. |
| **Raspberry Pi Zero 2 W** | Supported device target; public installation gap | It needs an OTG USB hub for the USB microphone and speaker. On current `main`, provisioning stops at the Pi-4-only Comitup installer, so the fresh-card community path still needs validation. |

Do not bypass a board-safety check on a working device. If you want to help
finish the Zero 2 W path, please join the community or open a focused pull
request with the board and physical checks you performed.

### Parts with purchase links

Choose one Pi, its matching power supply, and the shared parts below. We are not
affiliated with these retailers.

- **Raspberry Pi Zero 2 W:** [official product and reseller page](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/?variant=raspberry-pi-zero-2-w)
- **Or Raspberry Pi 4B:** [PiShop.US](https://www.pishop.us/product/raspberry-pi-4-model-b-1gb/), [Vilros](https://vilros.com/products/raspberry-pi-4-model-b-1), or [CanaKit](https://www.canakit.com/raspberry-pi-4.html)
- **32 GB SanDisk Ultra A1 microSD card:** [Amazon](https://www.amazon.com/dp/B08L5HMJVW/)
- **TONOR G11 USB microphone:** [Amazon](https://www.amazon.com/dp/B07GVGMW59)
- **LIELONGREN 8 W USB speaker:** [Amazon](https://www.amazon.com/dp/B08QRYTPGH)
- **Pi Zero 2 W power supply:** [official Raspberry Pi 12.5 W Micro USB Power Supply](https://www.raspberrypi.com/products/micro-usb-power-supply/)
- **Or Pi 4B power supply:** [iUniker 5 V / 4 A USB-C supply](https://www.amazon.com/dp/B097P2NLVH)
- **100 mm illuminated arcade button:** [Amazon](https://www.amazon.com/dp/B072JLSH34)
- **HiLetgo PN532 NFC/RFID module kit:** [Amazon](https://www.amazon.com/dp/B01I1J17LC)
- **Optional M2.5 screws, nuts, and washers:** [Amazon](https://www.amazon.com/dp/B0FJ1XN2XP) — useful for mounting the Pi or NFC board inside a custom enclosure; not required for a shoebox prototype

You will also need:

- **NFC cards or tokens:** [Adafruit 13.56 MHz Classic 1K card](https://www.adafruit.com/product/359), which Adafruit states is tested with PN532 readers
- **Hook-up wire and insulated connectors:** [0.25-inch arcade-button wire pairs](https://www.adafruit.com/product/3838) and an [assorted 2.8/4.8/6.3 mm spade-connector kit](https://www.adafruit.com/product/4748) are examples; confirm the terminal sizes on your button before ordering
- **A microSD-card reader that fits your computer:** [USB-C example](https://www.adafruit.com/product/5212) or [USB-A example](https://www.adafruit.com/product/939)
- **For Pi Zero 2 W, a suitable OTG USB hub**, powered if your selected audio devices require it. This [micro-USB OTG mini hub](https://www.adafruit.com/product/2991) is an example only; it has not yet been physically validated with the reference microphone and speaker.
- **An enclosure:** use the [printable prototype enclosure](hardware/enclosure/README.md), a shoebox, or another sturdy, non-conductive container.
- **A computer with internet access** for preparing and provisioning the Pi
- **A new, dedicated phone number for the Button Box WhatsApp account.** Add a line or eSIM through your mobile provider, activate it in the WhatsApp mobile app on a phone, and then link Button Box as a companion device.

The current reference-parts list is approximately **$150 before the enclosure,
Zero 2 W adapters, NFC tokens, shipping, and taxes**. Retailer prices and
availability change.

This is our current reference build, not the only hardware that can work. Other
USB speakers and microphones, and other GPIO-connected buttons, may work too.
We encourage experimentation: treat substitutions as unvalidated, check their
electrical and software compatibility, and share what you learn.

### What the build takes

- Comfort following terminal commands one step at a time, or access to an AI
  coding agent that can guide you
- Raspberry Pi Imager and permission to erase a microSD card
- Temporary Wi-Fi, Ethernet, USB Ethernet, or Internet Sharing during setup
- Time to assemble, install, test each component, and troubleshoot an alpha
  build

> [!NOTE]
> The public product is **Button Box**. Some commands, package names, hostnames,
> and services still use the historical internal name `messagebox`. Use those
> exact names for now.

### Once your parts arrive — build with an agent

Point a capable coding agent at this repository and give it this prompt:

```text
Help me build a Button Box from this repository:
https://github.com/button-box/button-box

Read README.md and AGENTS.md before giving instructions. First ask whether I
have a Raspberry Pi Zero 2 W or Raspberry Pi 4B. Work one numbered step at a
time and wait for me to confirm each physical result.

The goal is to have a working box that sends and receives voice messages.
Do not ask me to paste credentials, phone numbers, WhatsApp identifiers, Wi-Fi
details, NFC identifiers, recordings, or authentication files into the chat.
Stop if my physical result differs from the README checkpoint.
```

A helpful agent should:

- identify the Pi model before choosing parts or commands
- explain each command before asking the builder to run it
- use each **Done when** result as a checkpoint
- stop when physical evidence differs from the guide
- never guess a disk, network address, GPIO connection, or WhatsApp recipient
- preserve a working card or device rather than experimenting on it
- redact private information before helping prepare an issue

Button Box uses [wacli](https://github.com/openclaw/wacli), an unofficial
WhatsApp Web client. Button Box is not affiliated with or endorsed by WhatsApp
or Meta.

## Step 2 — Assemble the hardware

A shoebox or another sturdy, non-conductive container is enough for a first
build. Secure the electronics, provide ventilation, and protect the cables from
strain and loose metal.

The default public GPIO configuration is:

| Connection | BCM/board name |
| --- | --- |
| Record button | BCM GPIO 17 |
| Button LED | BCM GPIO 26 |
| PN532 reset | D20 |
| PN532 request | D16 |
| PN532 data | I²C |

> [!CAUTION]
> A verified community wiring diagram is not in the repository yet. The pin
> list above is a software configuration reference, not a complete wiring
> diagram. Confirm button voltage, LED current limiting, connector sizes, Pi
> pin numbering, and PN532 I²C mode before applying power. Never connect or
> disconnect GPIO wiring while the Pi is powered.

For the printable prototype, download the [top](hardware/enclosure/button-box-enclosure-top.stl)
and [bottom](hardware/enclosure/button-box-enclosure-bottom.stl) enclosure files.

**Done when:** the unpowered assembly is mechanically secure, every connection
has been independently checked, and there are no loose conductors or shorts.

## Step 3 — Prepare the microSD card

Install the current [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
on your computer.

In Imager:

1. Select your exact Raspberry Pi model.
2. Select **Raspberry Pi OS Lite (64-bit)** based on Debian 13.
3. Choose the correct microSD card.
4. Set a hostname such as `button-box-001`. Existing `message-box-*` hostnames remain supported.
5. Create a non-root administrator such as `admin`.
6. Enable SSH, preferably with a dedicated public key.
7. Give the Pi temporary internet access through Imager Wi-Fi settings,
   Ethernet, USB Ethernet, or Internet Sharing.
8. Leave Raspberry Pi Connect disabled unless you deliberately need it.
9. Confirm the exact removable card before writing.

> [!CAUTION]
> Writing an operating-system image erases the selected card. Confirm its
> physical identity, capacity, and partitions before continuing.

Insert the verified card into the powered-off Pi, connect temporary networking,
and power it on.

**Done when:** the Pi boots and responds at its `.local` hostname.

## Step 4 — Download Button Box on your computer

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

## Step 5 — Connect to and inspect the Pi

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

## Step 6 — Install Button Box

> [!IMPORTANT]
> Continue with the automated public installer on Raspberry Pi 4B. On current
> `main`, the install stops on Pi Zero 2 W because `scripts/install/comitup.sh`
> is explicitly validated only on Pi 4. Zero 2 W runtime support remains, but
> its fresh-card installation path needs a tested repository change.

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

## Step 7 — Choose an onboarding path

### Path A: terminal-assisted DIY build

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

### Path B: browser Wi-Fi and WhatsApp onboarding

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

## Step 8 — Test the hardware

The terminal-assisted onboarding offers this test automatically. To run it
separately, connect to the Pi and run it as the `messagebox` service user:

```sh
ssh -t admin@button-box-001.local
sudo -u messagebox -H /opt/messagebox/dev/hardware-test.sh
```

The interactive test checks:

- internet access
- speaker
- microphone and playback
- button LED
- record button
- PN532 reader and an NFC card
- WhatsApp authentication

It does not send a WhatsApp message or reveal NFC identifiers.

**Done when:** every installed component passes, or any deliberately omitted
component is documented.

## Step 9 — Start the box and complete the first message loop

Check the selected services:

```sh
messageboxctl services
messageboxctl status
```

With an approved recipient expecting the test:

1. Hold the physical button and record a short message.
2. Release the button and follow the box's confirmation behavior.
3. Confirm that the message reaches the intended recipient.
4. Ask the recipient to reply with a voice note.
5. Confirm the box queues and plays the reply.
6. Reboot the Pi.
7. Repeat a short send-and-receive check.

**Done when:** the real button, LED, microphone, speaker, network, WhatsApp
account, recipient routing, send, reply, playback, and reboot behavior all pass
on the physical device.

## What works today

| Capability | Status | What that means |
| --- | --- | --- |
| Device runtime | **Experimental** | Recording, sending, receiving, playback, fail-closed recipient routing, and NFC support exist. |
| Raspberry Pi 4B | **Supported** | A brand-new-card installation has been physically completed through Wi-Fi and WhatsApp onboarding. |
| Raspberry Pi Zero 2 W | **Supported device target; install gap** | The device target is supported, but the current public provisioning path still contains a Pi-4-only Comitup gate and needs clean-install validation. |
| Wi-Fi and WhatsApp browser onboarding | **Experimental** | The physical Pi 4B flow has reached verified WhatsApp readiness. |
| Recipient and NFC browser onboarding | **Experimental** | Repository coverage is included; fresh-Pi NFC and final activation acceptance are still required. |
| Enclosure | **Prototype** | Printable [top and bottom STL files](hardware/enclosure/README.md) are available. |
| Complete first-message journey from public instructions | **In progress** | Physical proof is still needed for recipient setup, NFC where used, runtime startup, send, reply, playback, and reboot using only this README. |

Automated tests and repository review do not prove a physical Pi, phone, Wi-Fi,
GPIO, NFC, audio, or WhatsApp journey. Please say exactly what you tested when
reporting success or opening a pull request.

## Troubleshooting

### The Pi hostname does not resolve

Confirm that the Pi and computer are on a compatible network. Try the hostname
you set in Imager. If necessary, inspect your router's device list rather than
guessing a fixed IP address.

### SSH says `Permission denied (publickey)`

Confirm which SSH identity is being offered:

```sh
ssh -v admin@button-box-001.local
```

If you created a dedicated key, select it explicitly in your SSH configuration
or with `ssh -i`.

### Provisioning says `scripts/setup.sh: No such file or directory`

On macOS, confirm that `rsync --version` reports modern GNU rsync and rerun
provisioning with Homebrew first in `PATH`.

### The installer says Comitup is validated only on Raspberry Pi 4

You are installing on a different Pi model. This is the current public Zero 2 W
installation gap described above. Do not edit out the model check on a working
device; follow or help with the clean-install validation work instead.

### The setup hotspot does not appear

Keep a separate recovery connection when possible. Check onboarding services:

```sh
messageboxctl logs onboarding
```

Do not share unredacted output publicly.

### The wrong Wi-Fi password was entered

The onboarding flow should return to a retryable hotspot state. If recovery is
required, `sudo messageboxctl reset-wifi` removes saved infrastructure Wi-Fi
profiles and restarts onboarding.

### WhatsApp pairing expires

Return to the home-network setup page and request another code. An interrupted
attempt should not promote an unverified authentication store.

### A service will not start

Check:

```sh
messageboxctl services
messageboxctl status
```

Before posting logs, remove phone numbers, WhatsApp identifiers, Wi-Fi details,
NFC identifiers, recordings, private addresses, and authentication state.

## Privacy and security

Button Box stores configuration, approved contacts, WhatsApp authentication
state, and queued audio locally on the Pi. These files must never be committed
to Git or copied into public diagnostics.

The dashboard has no login. Leave it disabled unless you understand the network
exposure. If enabled, bind it only to a private address you control.

Do not put security-sensitive details in a public issue. Contact the maintainers
through the community and ask for a private disclosure channel first.

## Updating and recovery

Button Box uses fixed installed paths rather than running directly from the
repository checkout. Re-run the provisioning script to install a reviewed
update.

Before updating a working physical box:

1. Confirm the exact source commit.
2. Stop Button Box services.
3. Preserve rollback material.
4. Provision the reviewed tree.
5. Verify services and real device behavior.
6. Keep repository validation separate from physical acceptance.

## Get help

Use [GitHub Issues](https://github.com/button-box/button-box/issues) for
reproducible bugs and documentation problems, or
[join the community](https://chat.whatsapp.com/FJ8LYL79k8zEMfoiyPjLPb?mode=gi_t)
for builder discussion.

A useful issue includes:

- Pi model
- Raspberry Pi OS release
- Button Box commit
- the numbered build step
- expected result
- actual result
- sanitized error text

Never include credentials, phone numbers, WhatsApp identifiers, NFC identifiers,
recordings, private addresses, or authentication files.

## Contributing

Community contributions are welcome, especially:

- beginner-tested installation instructions
- Pi Zero 2 W and Pi 4B build photographs
- wiring and assembly diagrams
- enclosure designs
- accessibility improvements
- recovery and troubleshooting guides
- hardware compatibility reports
- focused tests and fixes

Before opening a pull request:

```sh
make check
```

State clearly which checks were automated and which Pi, phone, network, audio,
GPIO, NFC, Wi-Fi, or WhatsApp behaviors were physically tested.

## Repository map

- `messagebox/`: device runtime, dashboard, and onboarding package
- `messagebox/dashboard/static/`: private dashboard assets
- `messagebox/onboarding/static/`: household onboarding portal
- `scripts/`: installation, provisioning, and command wrappers
- `scripts/dev/`: experimental developer onboarding and hardware checks
- `systemd/`: device services and runtime target
- `config/`: public configuration and pinned dependencies
- `hardware/`: [hardware list and printable enclosure files](hardware/README.md)
- `sounds/`: audio licensing requirements and future assets
- `tests/`: synthetic unit and contract tests
- `docs/`: architecture, installation, testing, and developer documentation

## License and acknowledgements

Button Box software is licensed under the [MIT License](LICENSE). Third-party
hardware, software, and audio remain subject to their own licenses and terms.

Button Box uses [wacli](https://github.com/openclaw/wacli) and
[Comitup](https://github.com/davesteele/comitup). It is not affiliated with or
endorsed by WhatsApp or Meta.
