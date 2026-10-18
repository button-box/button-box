# Pi OS install test in CI

The `Pi OS install` workflow (`.github/workflows/pi-os-image.yml`) installs
Button Box from zero inside a booted Raspberry Pi OS userspace, then checks the
result as the service users would see it. The normal `CI` workflow runs
`make check` on a plain Ubuntu machine. It does not run the install scripts on
Raspberry Pi OS, so it missed two install bugs:

- `nfc.sh` built the NFC venv under the administrator's strict umask, so the
  `messagebox` user could not read it.
- `remote_access.py` was missing from the `setup.sh` and `provision.sh` file
  lists, so the Cloud runtime could not import.

This job fails on both. The harness is in `scripts/dev/pi-os-ci/`.

## The image

- **Runner:** GitHub's native arm64 Linux runner (`ubuntu-24.04-arm`). It runs
  arm64 code at full speed. QEMU user emulation on an x86 runner works too, but
  apt, pip and `make check` are 5 to 10 times slower and do not fit the time
  budget.
- **Root filesystem:** the official Raspberry Pi OS Lite 64-bit image
  (trixie, 2026-09-15), pinned by URL and SHA-256 in `run.sh`. Both partitions
  are copied into a Docker image, so `/boot/firmware/config.txt`, the Pi apt
  repository, `raspi-config`, the `gpio` and `i2c` groups, NetworkManager and
  Avahi are the real Pi OS ones. A plain `debian:trixie` image has none of these.
- **Boot:** systemd runs as PID 1 in a privileged container. Units, journald,
  D-Bus, `systemctl enable --now`, `systemd-analyze verify` and
  `systemd-tmpfiles` behave as on a box.
- **Container changes** (`Dockerfile`): an empty `/etc/fstab`; masked
  first-boot, swap, console, EEPROM, root-grow and network-wait units; an
  `admin` user with passwordless sudo, like the user Raspberry Pi Imager
  creates.

The image download (about 540 MB) is cached by `actions/cache`. Each mode runs
as its own job, in parallel, in about 6 minutes: 1 minute to build the image,
1 to 1.5 minutes to install, a few seconds for the checks, and about 2.5
minutes for `make check`.

## What the job does

Both modes run with `umask 077` in the installing shell.

| Mode | Install path |
| --- | --- |
| `wacli` (self-hosted) | `scripts/provision.sh admin@ci-pi` from the runner, as an owner does from a laptop. `ssh` and `rsync` are stubs on `PATH` that reach the container. Then `scripts/install/tailscale.sh` through `sudo sh -s`, as `provision-tailscale.sh` runs it. |
| `cloud` | `git clone` on the Pi and `./scripts/setup.sh`, then `MSGBOX_TRANSPORT=cloud`, `minisign` and `espeak-ng`, and `scripts/install/update-bootstrap.sh auto stable`. Then `tailscale.sh`. |

`setup.sh` itself runs `comitup.sh`, `wacli.sh` (real download and checksum)
and `nfc.sh` (real `raspi-config`, venv and pip install).

After the install, `assert_install.py` checks, as root inside the box:

- every file under `/opt/messagebox`, the NFC venv, `/usr/lib/messagebox`,
  `/usr/share/messagebox`, `/etc/messagebox` and the data directories is
  readable (and executable where needed) by `messagebox`, using its real
  groups; the same for `messagebox-onboarding`, and for `messagebox-update` on
  Cloud boxes (bytecode caches are skipped; Python can run without them)
- `systemd-analyze verify` accepts every installed Button Box unit and the
  Comitup units
- for every installed service, the module or script in `ExecStart` imports with
  the unit's interpreter, user, groups, working directory and environment files
  (`gunicorn` included); the NFC venv imports `adafruit_pn532` and contains
  `board`, `busio`, `digitalio` and `lgpio` (these touch hardware on import)
- every file in the release mapping (`scripts/dev/release-manifest.py`) is in
  `scripts/release/payload.txt`, is installed, and matches its source; the
  program tree and unit directory have no unmapped files. Known gaps are
  listed with a reason in `assert_install.py`: the updater units exist only on
  Cloud boxes, and `setup.sh` does not install `messagebox/make_ringtones.py`
- `messagebox-audio-detect.service` writes its audio environment
- mode checks: the self-hosted box keeps the `setup.sh` layout and has `wacli`;
  the Cloud box runs from `/opt/messagebox-releases/` with the updater units
  enabled
- Tailscale is installed and held; no Button Box or Comitup unit failed

Finally, `make check` runs on Raspberry Pi OS arm64 as the administrator, with
`MSGBOX_TEST_TRANSPORT` set to the job's mode. It uses a second, uninstalled
container from the same image: several unit tests read real runtime paths such
as `/run/messagebox`, so they fail on a box that was just installed.

## What is stubbed

Only what cannot exist in a container:

- **Audio:** the runner kernel has no sound subsystem. `fake-sound.sh` lays a
  tmpfs over `/sys/class` (every real class is bind-mounted back) and adds one
  USB speaker and one microphone, so the real `audio_config.py` and
  `messagebox-audio-detect.service` run.
- **Pi model:** a container cannot have `/proc/device-tree`. `shim.sh` points
  the two readers (`comitup.sh`, `release_verify.py`) at
  `/run/ci-device-tree/model` ("Raspberry Pi 4 Model B") in the CI copy only.
  It fails if the number of readers changes.
- **Wi-Fi:** there is no `wlan0`. An `nmcli` wrapper reports it as managed for
  `comitup.sh`; all other `nmcli` calls are real. NetworkManager leaves the
  container's `eth0` and DNS alone.
- **SSH:** `provision.sh` reaches the box through `ssh` and `rsync` stubs.
- **Release identity:** `setup.sh` does not write `release.json`; the signed
  updater does. The Cloud job writes it from the checkout before bootstrap.

No service runtime starts, so nothing contacts WhatsApp or Button Box Cloud.
The install itself downloads from the Debian, Raspberry Pi, PyPI, GitHub and
Tailscale package servers, as a real install does.

## What it cannot cover

- the speaker, microphone, volume and playback quality
- the button, LEDs and GPIO timing
- the NFC card reader on I2C (the venv is checked, the reader is not)
- Wi-Fi, the setup hotspot, Comitup portals and the hand-off to home Wi-Fi
- WhatsApp pairing, sends and receives; Cloud claim, heartbeats and uploads
- boot timing, power cuts, SD card speed, memory on a Pi 3 A+, thermal limits
- the updater installing a signed release (the bootstrap is covered)

These still need the physical checks in [testing](testing.md) and
[box acceptance](box-acceptance.md).

## Later: a hardware runner on a test box

Not set up yet. The plan:

1. A spare Pi 4 with speaker, microphone, button and NFC reader on its own
   network, registered as a self-hosted GitHub runner with a dedicated label.
   It runs only on demand (`workflow_dispatch` or a release label), never for
   pull requests from forks.
2. Each run re-flashes or resets the SD card to a known Pi OS image, runs the
   same `provision.sh` install and `assert_install.py`, then starts the
   runtime.
3. Physical checks with loopback hardware: play a bundled cue and record it
   with the microphone, press the button through a GPIO-driven relay, read a
   fixed test card, and measure the button-to-sound delay.
4. No household data, real WhatsApp account or Cloud account on that box; Cloud
   checks use a test device against a staging Cloud.

## Run it yourself

On an arm64 Linux machine with Docker and passwordless sudo:

```sh
scripts/dev/pi-os-ci/run.sh build-image
scripts/dev/pi-os-ci/run.sh boot
scripts/dev/pi-os-ci/run.sh install wacli   # or cloud
scripts/dev/pi-os-ci/run.sh assert wacli
scripts/dev/pi-os-ci/run.sh check wacli
```

It changes only the container. Remove it with `docker rm -f button-box-pi`.
