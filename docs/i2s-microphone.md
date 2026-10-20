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

## How it works

- `/boot/firmware/config.txt` gets a managed block with `[all]`,
  `dtparam=i2s=on` and `dtoverlay=googlevoicehat-soundcard`. The ALSA card is
  `sndrpigooglevoi`: 48 kHz, S32_LE, two channels, signal in the left slot only.
- `/etc/alsa/conf.d/60-button-box-i2s-mic.conf` defines the ALSA PCM
  `buttonbox_mic`: it takes the left slot, applies a fixed gain (default
  +30 dB), and converts to the 16-bit mono 16 kHz stream that the recorder
  already uses. The gain stage saturates on overload; it does not wrap. There
  is no limiter, so the default gain leaves headroom (bench speech peaked near
  -14 to -20 dBFS at +33 dB).
- At boot, `messagebox-audio-detect` reads `MSGBOX_MIC_TYPE` from
  `/etc/messagebox/env`. For `i2s` it requires the `sndrpigooglevoi` capture
  card and the ALSA file, then writes `MSGBOX_MIC_PCM=buttonbox_mic` to
  `/run/messagebox-audio/audio.env`. The button records from `MSGBOX_MIC_PCM`
  when it is set. `MSGBOX_MIC_DEV` still names the raw card
  (`plughw:CARD=sndrpigooglevoi,DEV=0`) because the fixed updater health check
  accepts only that form.
- A USB box never selects the I2S card, even if the overlay is enabled.
- The recorded file format and the WhatsApp/Cloud upload are unchanged.
- The `amixer ... sset Mic` start step fails harmlessly on the I2S card (it is
  ignored, as before, when a control is missing).

## Switch a box

Install a software release that contains this change first. Older
`audio_config.py` versions ignore `MSGBOX_MIC_TYPE` and can pick the raw I2S
card once the overlay is active.

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

This removes only the managed `config.txt` block and the ALSA file. An overlay
line added by hand stays; remove it yourself.

A fresh developer install can do the same in one step:
`MSGBOX_MIC_TYPE=i2s ./scripts/provision.sh user@host` (or
`MSGBOX_MIC_TYPE=i2s ./scripts/setup.sh` on the Pi). Setup prints a reboot
note when `config.txt` changed.

## Box 4 install and verify checklist

Steps marked **sudo** need Dan's password on the box.

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
