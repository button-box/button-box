# Safety

Button Box is an open-source hardware and software project built around one
large illuminated button.

Automated tests and repository review do not prove a physical Pi, phone, Wi-Fi,
GPIO, NFC, audio, or WhatsApp journey. Please say exactly what you tested when
reporting success or opening a pull request.

Each detailed step in the [build guide](README.md#build-a-button-box) ends with
a **Done when** checkpoint. If the observed result differs, stop there and
troubleshoot instead of pushing ahead.

## Parts and enclosure

This is our current [reference build](README.md#parts-with-purchase-links).
Other USB speakers and microphones, and other GPIO-connected buttons, may work
electrically and with the software, but each substitution is unvalidated. The
printable enclosure was designed for the parts in this list. If you change the
speaker, microphone, or another part, the 3D-print designs may need a revision;
do not assume the substitute will fit the same case.

A shoebox or another sturdy, non-conductive box is an alternative to 3D
printing the enclosure. Whichever enclosure you use, secure the electronics,
provide ventilation, and protect the cables from strain and loose metal.

## Wiring and power

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

**Done when:** the unpowered assembly is mechanically secure, every connection
has been independently checked, and there are no loose conductors or shorts.

Do not bypass a board-safety check on a working device.

## Installation and updates

Writing an operating-system image erases the selected card. Confirm its
physical identity, capacity, and partitions before continuing.

Validate installation changes on a spare Raspberry Pi 4 and microSD card. Do
not overwrite a working device without a tested backup.

Before updating a working physical box:

1. Confirm the exact source commit.
2. Stop Button Box services.
3. Preserve rollback material.
4. Provision the reviewed tree.
5. Verify services and real device behavior.
6. Keep repository validation separate from physical acceptance.

## WhatsApp and private information

Button Box uses [wacli](https://github.com/openclaw/wacli), an unofficial
WhatsApp Web client. Button Box is not affiliated with or endorsed by WhatsApp
or Meta.

Button Box stores configuration, approved contacts, WhatsApp authentication
state, and queued audio locally on the Pi. These files must never be committed
to Git or copied into public diagnostics.

The dashboard has no login. Leave it disabled unless you understand the network
exposure. If enabled, bind it only to a private address you control.

Never include credentials, phone numbers, WhatsApp identifiers, NFC
identifiers, recordings, private addresses, or authentication files.
