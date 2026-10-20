#!/usr/bin/env python3
"""Detect attached audio devices and render Message Box configuration."""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path


CAPTURE_NODE = re.compile(r"pcmC([0-9]+)D([0-9]+)c\Z")
PLAYBACK_NODE = re.compile(r"pcmC([0-9]+)D([0-9]+)p\Z")
CARD_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
USB_ID = re.compile(r"[0-9A-Fa-f]{4}\Z")

CONFIG_ENV = Path("/etc/messagebox/env")
MIC_TYPES = ("usb", "i2s")
# The INMP441 I2S microphone uses the googlevoicehat-soundcard overlay, whose
# ALSA card ID is fixed. MSGBOX_MIC_DEV keeps naming this raw card, because the
# fixed updater health check accepts only plughw:CARD=...,DEV=... values;
# recording uses the processed ALSA PCM in MSGBOX_MIC_PCM instead.
I2S_CARD = "sndrpigooglevoi"
I2S_PCM = "buttonbox_mic"
I2S_DEVICE = f"plughw:CARD={I2S_CARD},DEV=0"
I2S_ALSA_CONFIG = Path("/etc/alsa/conf.d/60-button-box-i2s-mic.conf")
BOOT_CONFIG = Path("/boot/firmware/config.txt")
BOOT_BLOCK_START = "# Button Box I2S microphone (managed by audio_config.py)"
BOOT_BLOCK_END = "# End Button Box I2S microphone"
BOOT_LINES = ("[all]", "dtparam=i2s=on", "dtoverlay=googlevoicehat-soundcard")
DEFAULT_I2S_GAIN_DB = 30.0
MAX_I2S_GAIN_DB = 40.0
# BCM pins taken by I2S (18 clock, 19 frame, 20 data in, 21 data out) and by
# the overlay's amplifier SD_MODE output (16).
I2S_RESERVED_GPIOS = frozenset({16, 18, 19, 20, 21})
PIN_SETTINGS = {
    "MSGBOX_BUTTON_PIN": "17",
    "MSGBOX_LED_PIN": "26",
    "MSGBOX_NFC_RESET_PIN": "D20",
    "MSGBOX_NFC_REQUEST_PIN": "D16",
}


class AudioConfigError(RuntimeError):
    pass


def _card_id(sound_root, card_number):
    try:
        card_id = (sound_root / f"card{card_number}" / "id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError):
        return None
    return card_id if CARD_ID.fullmatch(card_id) else None


def _is_usb(card_path):
    try:
        device_path = (card_path / "device").resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    for path in (device_path, *device_path.parents):
        try:
            vendor = (path / "idVendor").read_text(encoding="ascii").strip()
            product = (path / "idProduct").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        return bool(USB_ID.fullmatch(vendor) and USB_ID.fullmatch(product))
    return False


def read_settings(path=CONFIG_ENV):
    settings = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            settings[key.strip()] = value.strip().strip("'\"")
    return settings


def mic_type(settings):
    """Return the configured microphone type; boxes without the setting use USB."""
    value = settings.get("MSGBOX_MIC_TYPE", "") or "usb"
    if value not in MIC_TYPES:
        raise AudioConfigError(f"MSGBOX_MIC_TYPE must be one of: {', '.join(MIC_TYPES)}")
    return value


def detect_microphone(sound_root=Path("/sys/class/sound"), kind="usb"):
    """Return (card ID, plughw device) for the configured microphone type.

    A USB box keeps the original rule: the lowest-numbered capture device. The
    I2S card is never chosen for a USB box, and an I2S box uses only that card.
    """
    captures = []
    for capture_path in sound_root.glob("pcmC*D*c"):
        match = CAPTURE_NODE.fullmatch(capture_path.name)
        if match is None:
            continue
        card_number, device_number = (int(value) for value in match.groups())
        captures.append((card_number, device_number, _card_id(sound_root, card_number)))

    if kind == "i2s":
        if not any(card_id == I2S_CARD and device == 0 for _, device, card_id in captures):
            raise AudioConfigError(
                "I2S microphone not detected; enable it with --set-mic-type i2s and reboot"
            )
        return I2S_CARD, I2S_DEVICE

    captures = [capture for capture in captures if capture[2] != I2S_CARD]
    candidates = [capture for capture in captures if capture[2] is not None]
    if not candidates:
        if captures:
            raise AudioConfigError("could not read a valid ALSA capture card ID")
        raise AudioConfigError("no ALSA capture device detected; connect a microphone")

    _, device_number, card_id = min(candidates)
    return card_id, f"plughw:CARD={card_id},DEV={device_number}"


def detect_speaker(sound_root=Path("/sys/class/sound")):
    candidates = []
    for playback_path in sound_root.glob("pcmC*D*p"):
        match = PLAYBACK_NODE.fullmatch(playback_path.name)
        if match is None:
            continue
        card_number, device_number = (int(value) for value in match.groups())
        card_path = sound_root / f"card{card_number}"
        card_id = _card_id(sound_root, card_number)
        if card_id is not None and _is_usb(card_path):
            candidates.append((card_number, device_number, card_id))

    if not candidates:
        raise AudioConfigError(
            "no USB ALSA playback device detected; connect a USB speaker"
        )

    _, device_number, card_id = min(candidates)
    return card_id, f"plughw:CARD={card_id},DEV={device_number}"


def render_environment(
    template,
    microphone_card,
    microphone_device,
    speaker_card,
    speaker_device,
    kind="usb",
):
    replacements = {
        "MSGBOX_MIC_TYPE": kind,
        "MSGBOX_MIC_DEV": microphone_device,
        "MSGBOX_SPK_DEV": speaker_device,
        "MSGBOX_MIC_CARD": microphone_card,
        "MSGBOX_SPEAKER_CARD": speaker_card,
    }
    counts = dict.fromkeys(replacements, 0)
    rendered = []
    for line in template.splitlines():
        key = line.split("=", 1)[0]
        if key in replacements:
            counts[key] += 1
            line = f"{key}={replacements[key]}"
        rendered.append(line)

    invalid = [key for key, count in counts.items() if count != 1]
    if invalid:
        raise AudioConfigError(
            "configuration template must define each audio setting exactly once"
        )
    return "\n".join(rendered) + "\n"


def render_runtime_environment(
    microphone_card, microphone_device, speaker_card, speaker_device, kind="usb"
):
    """Render volatile settings whose ALSA names can change across boots."""
    values = [
        ("MSGBOX_MIC_DEV", microphone_device),
        ("MSGBOX_SPK_DEV", speaker_device),
        ("MSGBOX_MIC_CARD", microphone_card),
        ("MSGBOX_SPEAKER_CARD", speaker_card),
    ]
    if kind == "i2s":
        values.append(("MSGBOX_MIC_PCM", I2S_PCM))
    return "".join(f"{key}={value}\n" for key, value in values)


def i2s_pin_conflicts(settings):
    """Return box pin settings that would share a GPIO with the I2S microphone."""
    conflicts = []
    for key, default in PIN_SETTINGS.items():
        value = settings.get(key) or default
        match = re.fullmatch(r"(?:D|GPIO)?([0-9]+)", value)
        if match and int(match[1]) in I2S_RESERVED_GPIOS:
            conflicts.append(f"{key}={value}")
    return conflicts


def render_i2s_alsa_config(gain_db=DEFAULT_I2S_GAIN_DB):
    if not 0 <= gain_db <= MAX_I2S_GAIN_DB:
        raise AudioConfigError(f"microphone gain must be 0 to {MAX_I2S_GAIN_DB:g} dB")
    factor = 10 ** (gain_db / 20)
    return f"""\
# Button Box I2S microphone (INMP441). Written by
# /usr/lib/messagebox/audio_config.py --set-mic-type i2s; local edits are replaced.
# The microphone sends 24-bit audio in the LEFT slot of a 48 kHz S32_LE stereo
# stream at a low level. route takes the left slot and applies a fixed gain
# ({gain_db:g} dB); it saturates instead of wrapping on loud input. plug then
# converts to the recorder's 16-bit mono 16 kHz format.
pcm.{I2S_PCM}_raw {{
    type hw
    card {I2S_CARD}
    device 0
}}
pcm.{I2S_PCM}_gain {{
    type route
    slave {{
        pcm "{I2S_PCM}_raw"
        channels 2
        format S32_LE
    }}
    ttable.0.0 {factor:.4f}
}}
pcm.{I2S_PCM} {{
    type plug
    slave.pcm "{I2S_PCM}_gain"
}}
"""


def boot_config_with_i2s(text):
    """Return config.txt text with the managed overlay block, and if it changed."""
    lines = text.splitlines()
    if BOOT_BLOCK_START in lines:
        return text, False
    active = {line.strip() for line in lines if not line.lstrip().startswith("#")}
    if BOOT_LINES[2] in active:
        # Already enabled by hand; do not load the overlay twice.
        return text, False
    block = [BOOT_BLOCK_START, *BOOT_LINES, BOOT_BLOCK_END]
    prefix = text if not text or text.endswith("\n") else text + "\n"
    return prefix + "\n".join(block) + "\n", True


def boot_config_without_i2s(text):
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n") for line in lines]
    if BOOT_BLOCK_START not in stripped or BOOT_BLOCK_END not in stripped:
        return text, False
    start = stripped.index(BOOT_BLOCK_START)
    end = stripped.index(BOOT_BLOCK_END, start)
    return "".join(lines[:start] + lines[end + 1:]), True


def write_atomic(path, content, mode=0o644, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if owner is not None:
            os.chown(temporary_name, *owner)
        # The FAT boot partition cannot store modes; pass None there.
        if mode is not None:
            os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def set_env_value(path, key, value):
    """Set one KEY=value line, keeping the file's other lines, owner and mode."""
    path = Path(path)
    status = path.stat()
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = [f"{key}={value}" if line.split("=", 1)[0] == key else line for line in lines]
    if f"{key}={value}" not in updated:
        updated.append(f"{key}={value}")
    if updated != lines:
        write_atomic(path, "\n".join(updated) + "\n", status.st_mode & 0o7777,
                     (status.st_uid, status.st_gid))


def set_mic_type(
    kind,
    gain_db=DEFAULT_I2S_GAIN_DB,
    *,
    env_path=CONFIG_ENV,
    boot_config=BOOT_CONFIG,
    alsa_config=I2S_ALSA_CONFIG,
):
    """Switch the box microphone type. Return True when a reboot is required."""
    settings = read_settings(env_path)
    boot_text = Path(boot_config).read_text(encoding="utf-8")
    if kind == "i2s":
        conflicts = i2s_pin_conflicts(settings)
        if conflicts:
            raise AudioConfigError(
                "these pins are used by the I2S microphone: " + ", ".join(conflicts)
                + "; rewire and change them in /etc/messagebox/env first"
            )
        write_atomic(alsa_config, render_i2s_alsa_config(gain_db))
        boot_text, changed = boot_config_with_i2s(boot_text)
        if changed:
            write_atomic(boot_config, boot_text, mode=None)
        # Set the type last, so a failed step leaves the box on its old microphone.
        set_env_value(env_path, "MSGBOX_MIC_TYPE", "i2s")
        return changed
    if kind != "usb":
        raise AudioConfigError(f"MSGBOX_MIC_TYPE must be one of: {', '.join(MIC_TYPES)}")
    set_env_value(env_path, "MSGBOX_MIC_TYPE", "usb")
    boot_text, changed = boot_config_without_i2s(boot_text)
    if changed:
        write_atomic(boot_config, boot_text, mode=None)
    Path(alsa_config).unlink(missing_ok=True)
    return changed


def configured_mic_type(env_path=CONFIG_ENV, alsa_config=I2S_ALSA_CONFIG):
    try:
        kind = mic_type(read_settings(env_path))
    except FileNotFoundError:
        return "usb"
    if kind == "i2s" and not Path(alsa_config).is_file():
        raise AudioConfigError(
            f"{alsa_config} is missing; run audio_config.py --set-mic-type i2s"
        )
    return kind


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser()
    parser.add_argument("template", nargs="?")
    parser.add_argument("--runtime-output")
    parser.add_argument("--set-mic-type", choices=MIC_TYPES)
    parser.add_argument("--mic-type", choices=MIC_TYPES, default="usb",
                        help="microphone type for a rendered template")
    parser.add_argument("--mic-gain-db", type=float, default=DEFAULT_I2S_GAIN_DB)
    options = parser.parse_args(arguments)
    if sum(map(bool, (options.template, options.runtime_output, options.set_mic_type))) != 1:
        parser.error("provide ENV_TEMPLATE, --runtime-output PATH or --set-mic-type TYPE")
    try:
        if options.set_mic_type:
            reboot = set_mic_type(options.set_mic_type, options.mic_gain_db)
            print(f"Microphone type set to {options.set_mic_type}.")
            if reboot:
                print(f"{BOOT_CONFIG} changed: reboot the box to apply it.")
            return 0
        kind = configured_mic_type() if options.runtime_output else options.mic_type
        if options.template and kind == "i2s":
            # Setup can run before the overlay is active; the boot detector checks it.
            microphone_card, microphone_device = I2S_CARD, I2S_DEVICE
        else:
            microphone_card, microphone_device = detect_microphone(kind=kind)
        speaker_card, speaker_device = detect_speaker()
        if options.runtime_output:
            write_atomic(
                options.runtime_output,
                render_runtime_environment(
                    microphone_card,
                    microphone_device,
                    speaker_card,
                    speaker_device,
                    kind,
                ),
            )
        else:
            template = Path(options.template).read_text(encoding="utf-8")
            sys.stdout.write(
                render_environment(
                    template,
                    microphone_card,
                    microphone_device,
                    speaker_card,
                    speaker_device,
                    kind,
                )
            )
    except (AudioConfigError, OSError, UnicodeError) as exc:
        print(f"audio setup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
