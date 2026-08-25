#!/usr/bin/env python3
"""Render initial Message Box configuration for attached audio devices."""

import re
import sys
from pathlib import Path


CAPTURE_NODE = re.compile(r"pcmC([0-9]+)D([0-9]+)c\Z")
PLAYBACK_NODE = re.compile(r"pcmC([0-9]+)D([0-9]+)p\Z")
CARD_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
USB_ID = re.compile(r"[0-9A-Fa-f]{4}\Z")


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


def detect_microphone(sound_root=Path("/sys/class/sound")):
    candidates = []
    capture_found = False
    for capture_path in sound_root.glob("pcmC*D*c"):
        match = CAPTURE_NODE.fullmatch(capture_path.name)
        if match is None:
            continue
        capture_found = True
        card_number, device_number = (int(value) for value in match.groups())
        card_id = _card_id(sound_root, card_number)
        if card_id is not None:
            candidates.append((card_number, device_number, card_id))

    if not candidates:
        if capture_found:
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
):
    replacements = {
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


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: audio_config.py ENV_TEMPLATE", file=sys.stderr)
        return 2
    try:
        microphone_card, microphone_device = detect_microphone()
        speaker_card, speaker_device = detect_speaker()
        template = Path(arguments[0]).read_text(encoding="utf-8")
        sys.stdout.write(
            render_environment(
                template,
                microphone_card,
                microphone_device,
                speaker_card,
                speaker_device,
            )
        )
    except (AudioConfigError, OSError, UnicodeError) as exc:
        print(f"audio setup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
