# I2S microphone (INMP441)

A box can use an INMP441 I2S MEMS microphone on the GPIO header instead of a
USB microphone. The speaker stays on USB. USB boxes are unchanged: a missing
`MSGBOX_MIC_TYPE` means `usb`.

## Wiring

| INMP441 | Raspberry Pi signal | Physical pin |
| --- | --- | ---: |
| VDD | 3.3 V | 17 |
| GND | GND | 14 |
| SCK | BCM GPIO 18 (I2S clock) | 12 |
| WS | BCM GPIO 19 (I2S frame) | 35 |
| SD | BCM GPIO 20 (I2S data in) | 38 |
| L/R | GND (left channel) | 20 |

The `googlevoicehat-soundcard` overlay also drives BCM GPIO 16 as an output
(the amplifier SD_MODE line of the original HAT) and reserves GPIO 21 for I2S
data out. It defines no button or LED pins. Button GPIO 17, lamp GPIO 26 and a
PN532 on I2C with reset `D4` and request `D27` do not conflict.

The repository NFC defaults `D20`/`D16` (Waveshare HAT jumpers) **do
conflict**. `--set-mic-type i2s` refuses to run while any box pin setting,
including a missing NFC setting that falls back to `D20`/`D16`, uses GPIO 16,
18, 19, 20 or 21.

Fresh setup with `MSGBOX_MIC_TYPE=i2s` explicitly uses NFC reset/request
`D4`/`D27`, button GPIO 17 and lamp GPIO 26. It validates these pins before
changing services or installing files. Existing configurations must already
contain valid, distinct pins outside the I2S and I2C GPIOs. USB setup keeps
the existing `D20`/`D16` NFC defaults.

## How it works

- `/boot/firmware/config.txt` gets a managed block with `[all]`,
  `dtparam=i2s=on` and `dtoverlay=googlevoicehat-soundcard`. The ALSA card is
  `sndrpigooglevoi`: 48 kHz, S32_LE, two channels, signal in the left slot only.
- `/etc/alsa/conf.d/60-button-box-i2s-mic.conf` defines the ALSA PCM
  `buttonbox_mic`: it takes the left slot, applies a fixed gain (default
  +30 dB), and converts the fixed 48 kHz hardware rate to the 16-bit mono
  16 kHz stream that the recorder already uses. The gain stage saturates on
  overload; it does not wrap. There is no limiter, so the default gain leaves
  headroom (bench speech peaked near
  -14 to -20 dBFS at +33 dB).
- At boot, `messagebox-audio-detect` reads `MSGBOX_MIC_TYPE` from
  `/etc/messagebox/env`. With the `sndrpigooglevoi` capture card and the ALSA
  file present, it writes `MSGBOX_MIC_PCM=buttonbox_mic` to
  `/run/messagebox-audio/audio.env`. All button recording flows (hold, guided
  tap/reply, family-card recording and the first setup reply) and the developer
  hardware test record from `MSGBOX_MIC_PCM` when it is set. Setup greetings
  are playback only. `MSGBOX_MIC_DEV` still names the raw card
  (`plughw:CARD=sndrpigooglevoi,DEV=0`) because the fixed updater health check
  accepts only that form.
- A USB box never selects the I2S card, even if the overlay is enabled.
- If the configured I2S card is absent at boot, detection logs a warning and
  tries the normal USB selection. It removes the processed PCM from the new
  runtime output. With no USB microphone either, detection fails and the
  recording service does not start. The configured type stays `i2s` so the
  next boot tries I2S again. A present I2S card without its ALSA configuration
  fails closed rather than recording raw audio.
- The recorded file format and the WhatsApp/Cloud upload are unchanged.
- No Cloud or dashboard setting, command, capability flag or wording change is
  required. Microphone type and gain are local installation settings.
- The `amixer ... sset Mic` start step fails harmlessly on the I2S card (it is
  ignored, as before, when a control is missing).

## Switch a box

Start with an I2S-capable application. Enabling I2S checks its installed
`messagebox/mic_capture.py` for `I2S_CAPTURE_VERSION = 1`; the declaration means
that every recording path honors `MSGBOX_MIC_PCM`. A missing, invalid or
unsupported declaration is refused before changing audio configuration.
Do not use a legacy USB bridge as an I2S migration route.

```sh
sudo python3 /usr/lib/messagebox/audio_config.py --set-mic-type i2s
sudo reboot
```

Change the gain (0 to 40 dB) by running the same command again with
`--mic-gain-db 33`, then restart the button service:
`sudo systemctl restart messagebox-button`. A gain change does not need a
reboot.

Switch back to a USB microphone:

```sh
sudo python3 /usr/lib/messagebox/audio_config.py --set-mic-type usb
sudo reboot
```

This removes the managed overlay block and the ALSA file. An `[all]` boundary
remains so settings appended below the block keep their original global
scope. An overlay line added by hand stays; remove it yourself. The application
start guard remains installed and bypasses its I2S check when USB is selected.

A fresh developer install can do the same in one step:
`MSGBOX_MIC_TYPE=i2s ./scripts/provision.sh user@host` (or
`MSGBOX_MIC_TYPE=i2s ./scripts/setup.sh` on the Pi). Setup prints a reboot
note when `config.txt` changed.

## Interrupted configuration and manual boot settings

Microphone switches are serialized by `/etc/messagebox/.audio-config.lock`.
Enabling first persists the application start guard, so even a later legacy
install cannot record raw I2S after an interruption. It then writes boot
configuration, the ALSA configuration, and activates `MSGBOX_MIC_TYPE=i2s` last. Each file
and its containing directory is synced before proceeding, including newly
created parent directories. Disabling commits USB mode before removing the
overlay and ALSA file; deletion also syncs the containing directory. This
ordering covers separate boot and root filesystems. Repeating the switch
after an interruption completes it safely.

An unmanaged overlay is accepted only with an unconditional `dtparam=i2s=on`
and one unparameterized overlay line in global scope. Conditional overlays,
includes, duplicate overlays, unclear I2S parameters and incomplete or edited
managed blocks are refused. Resolve these manually before retrying. The helper
does not guess which Pi model or nested include will make a line effective.

## Install and verify checklist

Steps marked **sudo** require administrator access.

1. Confirm the box pins do not use I2S GPIOs:
   `grep -E '^MSGBOX_(BUTTON|LED|NFC_RESET|NFC_REQUEST)_PIN=' /etc/messagebox/env`.
   NFC must be `D4`/`D27` (or other free pins), not `D20`/`D16` or missing.
2. Install the release that contains this change (normal signed update path).
   Confirm: `grep -c set-mic-type /usr/lib/messagebox/audio_config.py` prints
   a number greater than 0.
3. Power off, wire the INMP441 as above, power on.
4. **sudo** `sudo python3 /usr/lib/messagebox/audio_config.py --set-mic-type i2s`
   then **sudo** `sudo reboot`.
5. Check the card and the runtime settings:
   `arecord -l | grep sndrpigooglevoi` and
   `cat /run/messagebox-audio/audio.env` (expect
   `MSGBOX_MIC_PCM=buttonbox_mic` and
   `MSGBOX_MIC_DEV=plughw:CARD=sndrpigooglevoi,DEV=0`).
   `systemctl status messagebox-audio-detect messagebox-button` must be active.
6. **sudo** Stop the button so it does not hold the microphone:
   `sudo systemctl stop messagebox-button`.
7. **sudo** Record five seconds of normal speech at 30 cm through the same
   device the button uses:
   `sudo -u messagebox arecord -D buttonbox_mic -f S16_LE -r 16000 -c 1 -d 5 /tmp/i2s.wav`
8. Check the level: `sox /tmp/i2s.wav -n stat 2>&1 | grep -E 'Maximum amplitude|RMS'`
   if `sox` is installed, otherwise
   `ffmpeg -i /tmp/i2s.wav -af volumedetect -f null - 2>&1 | grep -E 'max_volume|mean_volume'`.
   Target: speech peaks about -6 to -20 dBFS, no 0 dBFS peaks. Adjust with
   `--mic-gain-db` (step 4 command, no reboot) if needed.
9. Play it on the speaker: `aplay -D "$(grep ^MSGBOX_SPK_DEV= /run/messagebox-audio/audio.env | cut -d= -f2)" /tmp/i2s.wav`,
   then `rm /tmp/i2s.wav`.
10. **sudo** `sudo systemctl start messagebox-button`.
11. Record a real message with the button to a test recipient. Check that it
    arrives and plays in WhatsApp, is clear, and is not cut off as silence.
12. **sudo** Confirm the updater health probe still passes:
    `sudo python3 /usr/lib/messagebox-update/health-probe.py --current`
    (expect `PASS audio:MSGBOX_MIC_DEV:enumerated`).

To undo: **sudo** `--set-mic-type usb`, reconnect the USB microphone, reboot.

## Updater compatibility

The v0.3.16 controller (1.3.0) and the retained frozen controller (1.1.0)
health probes validate the raw `MSGBOX_MIC_DEV` card and device, enumerate it
with `arecord -l`, and check read/write access to its capture node as the
`messagebox` user. They do not open the microphone or require a USB capture
card. Keeping the raw `plughw` value lets an I2S card pass these checks.
The 1.3.0 controller self-test imports the probe and checks manifest/path/hash
validation without opening or checking a microphone; the frozen 1.1.0
controller has no `self-test` command. Discovery self-tests use local fixtures,
without microphone checks.
These checks do not verify that the processed PCM records usable audio;
the recording and physical checks above are still required.

## Legacy applications and rollback

Enabling I2S durably creates
`/etc/systemd/system/messagebox-button.service.d/zz-i2s-microphone.conf`.
This device configuration is outside normal application manifest/rollback
ownership. Before the button service starts with I2S configured or selected, it calls
`audio_config.py --check-i2s-application` against the selected application's
capability declaration. Legacy helpers reject that option; newer helpers
reject missing or unsupported declarations. Neither case starts the button
application or its recorder. A selected I2S card also requires
`MSGBOX_MIC_PCM=buttonbox_mic`; stale raw-only runtime settings are refused.
This also covers a legacy detector selecting I2S during an interrupted switch
to USB. The guard remains effective if an older
application replaces the main service unit.

No updater/controller code is changed. A candidate can be staged and its
pointer switched before the guard rejects its service start. A monitored
running button service then fails health and the updater restores the prior
I2S-capable application. Keep the first installation and every intended
rollback target I2S-capable.

This is activation protection, not pre-install manifest admission: when setup
mode or an unselected button service makes health omit that service, an
incompatible candidate can remain selected but cannot record. A corrupted or
manually replaced incompatible rollback target is also blocked and can leave
recovery requiring operator action. Install a compatible application to recover;
do not remove the guard to make a legacy application run on raw I2S audio.
Manual execution outside the managed service and privileged changes can bypass
this guard. Capability declarations are a trusted release contract, not a
proof of audio quality or protection against dishonest signed software.
