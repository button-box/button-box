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
PN532 on I2C with reset `D4` and request `D5` (physical pin 29, GPIO 5)
do not conflict. The existing request setting `D27` also remains valid.

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
- During OTA activation and boot, an I2S configuration validates the pins and
  application capability, persists the service guards, boot overlay and ALSA
  PCM, and requires the I2S capture card. Existing gain configuration is kept.
  If the card is not already present, it runs `/usr/bin/dtparam i2s=on` and
  `/usr/bin/dtoverlay googlevoicehat-soundcard`, each with a 10-second timeout,
  then waits at most five seconds for enumeration. It never opens the microphone.
- Missing tools, loader errors or a missing card fail audio detection; there is
  no USB fallback in the candidate. The existing updater health reports the
  failed audio service or missing capture device and restores the prior
  application. Persistent boot/ALSA configuration and guards survive rollback.
  A previous USB application may use its working USB microphone; a previous
  application selecting raw I2S is blocked. With no working previous microphone,
  the box stays visibly unhealthy rather than recording raw audio.
- The recorded file format and the WhatsApp/Cloud upload are unchanged.
- No Cloud or dashboard setting, command, capability flag or wording change is
  required. Microphone type and gain are local installation settings.
- The `amixer ... sset Mic` start step fails harmlessly on the I2S card (it is
  ignored, as before, when a control is missing).

## Signed OTA activation

Provision the hardware choice and conflict-free pin settings before shipment:
`MSGBOX_MIC_TYPE=i2s`, NFC reset `D4` and request `D5` for GPIO 5 wiring.
Do not change the USB defaults on existing USB boxes. Microphone choice is a
local hardware setting, not a Cloud settings command.

OTA requires a working source application and its live button readiness proof.
A bare v0.3.17 installation with neither a USB microphone nor an active I2S
overlay cannot satisfy that existing updater gate. For a first OTA test on such
a bench installation, temporarily connect USB and establish working audio and
button services before requesting the update; remove USB after verifying I2S
selection. New boxes can instead be provisioned with the I2S-capable application
before shipment. Neither is a manual recovery step for a family box.

The normal signed application update installs the helper and I2S-capable
recorder, switches the application pointer, and restarts audio detection before
recording services. Detection prepares persistent configuration and loads the
overlay in the running boot if necessary. Enumeration and service readiness
must pass the existing updater probation before the release commits. No SSH
command or manual reboot is part of family OTA activation. On the next boot,
the persisted overlay should enumerate the card before detection; detection
revalidates the prerequisites and writes the processed PCM runtime settings.

A reboot during updater probation is deliberately **not** requested: the
existing controller treats that as an interrupted update and rolls back.
If this kernel/overlay cannot load live, activation fails and rolls back; there
is no unsafe reboot fallback. Successful live loading on the supported Pi and
kernel remains a physical release acceptance requirement. Until that passes,
OTA support is software-tested, not hardware-accepted.

For developer setup, `MSGBOX_MIC_TYPE=i2s ./scripts/setup.sh` (or provisioning
with the same variable) prepares the overlay and safe explicit pins. Fresh
setup retains its existing request pin `D27`; GPIO 5 wiring must explicitly
set `MSGBOX_NFC_REQUEST_PIN=D5` in the installed environment before use.

Operator-only gain adjustment remains available:

```sh
sudo python3 /usr/lib/messagebox/audio_config.py --set-mic-type i2s --mic-gain-db 33
sudo systemctl restart messagebox-audio-detect messagebox-button
```

Gain is 0 to 40 dB; changing gain needs no reboot. To return a bench installation
to USB, reconnect its USB microphone, run the helper with `--set-mic-type usb`,
and restart audio detection and the button service. The helper removes its boot
block and ALSA file. An `[all]` boundary keeps subsequent settings in global
scope. A manually added overlay stays, and a running overlay remains until the
next reboot. USB detection excludes that card. Persistent service guards permit
USB capture but still reject a legacy application selecting raw I2S.

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
   NFC must use free pins, such as reset `D4` / request `D5` (GPIO 5 wiring),
   not `D20`/`D16` or missing.
2. Install the release that contains this change (normal signed update path).
   Confirm: `grep -c set-mic-type /usr/lib/messagebox/audio_config.py` prints
   a number greater than 0.
3. Power off, wire the INMP441 as above, power on.
4. On a bench installation, set `MSGBOX_MIC_TYPE=i2s` in the local environment
   before requesting the signed OTA. Verify activation without a manual reboot.
   Repeat with an already enabled overlay to verify the first subsequent boot.
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
   `--mic-gain-db` using the operator-only gain command above (no reboot) if needed.
9. Play it on the speaker: `aplay -D "$(grep ^MSGBOX_SPK_DEV= /run/messagebox-audio/audio.env | cut -d= -f2)" /tmp/i2s.wav`,
   then `rm /tmp/i2s.wav`.
10. **sudo** `sudo systemctl start messagebox-button`.
11. Record a real message with the button to a test recipient. Check that it
    arrives and plays in WhatsApp, is clear, and is not cut off as silence.
12. **sudo** Confirm the updater health probe still passes:
    `sudo python3 /usr/lib/messagebox-update/health-probe.py --current`
    (expect `PASS audio:MSGBOX_MIC_DEV:enumerated`).

To undo on the bench: reconnect USB, use `--set-mic-type usb`, then restart
audio detection and the button service.

## Updater compatibility

The v0.3.17 controller (1.3.0) and the retained frozen controller (1.1.0)
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

Enabling I2S durably creates `zz-i2s-microphone.conf` drop-ins for both
`messagebox-button.service` and `messagebox-onboarding-button.service`.
This device configuration is outside normal application manifest/rollback
ownership. Before either recording service starts with the I2S card or PCM selected, it calls
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
I2S-capable application. An I2S-only installation needs an I2S-capable rollback target. A working USB
rollback remains allowed when the old detector selects USB.

This is activation protection, not pre-install manifest admission: when setup
mode or an unselected button service makes health omit that service, an
incompatible candidate can remain selected but cannot record. A corrupted or
manually replaced incompatible rollback target is also blocked and can leave
recovery requiring operator action. Install a compatible application to recover;
do not remove the guard to make a legacy application run on raw I2S audio.
Manual execution outside the managed service and privileged changes can bypass
this guard. Capability declarations are a trusted release contract, not a
proof of audio quality or protection against dishonest signed software.
