#!/usr/bin/env python3
"""Detect attached audio devices and render Message Box configuration."""

import argparse
import ast
import contextlib
import fcntl
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
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
I2S_CAPABILITY = Path("/opt/messagebox/messagebox/mic_capture.py")
I2S_GUARD = Path("/etc/systemd/system/messagebox-button.service.d/zz-i2s-microphone.conf")
I2S_ALSA_CONFIG = Path("/etc/alsa/conf.d/60-button-box-i2s-mic.conf")
DEFAULT_I2S_GAIN_DB = 30.0
MAX_I2S_GAIN_DB = 40.0
I2S_ACTIVATION_TIMEOUT = 45.0
# BCM pins taken by I2S (18 clock, 19 frame, 20 data in, 21 data out) and by
# the overlay's amplifier SD_MODE output (16).
I2S_RESERVED_GPIOS = frozenset({16, 18, 19, 20, 21})
PIN_SETTINGS = {
    "MSGBOX_BUTTON_PIN": "17",
    "MSGBOX_LED_PIN": "26",
    "MSGBOX_NFC_RESET_PIN": "D20",
    "MSGBOX_NFC_REQUEST_PIN": "D16",
}
I2S_INSTALL_PINS = dict(PIN_SETTINGS, MSGBOX_NFC_RESET_PIN="D4", MSGBOX_NFC_REQUEST_PIN="D27")


class AudioConfigError(RuntimeError):
    pass


class I2SUnavailable(AudioConfigError):
    """Hardware unavailable after retrying; keep the button repair path alive."""


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

    A USB box prefers a dedicated USB microphone, then the lowest-numbered
    capture device. It never chooses the I2S card; an I2S box uses only that card.
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
                "I2S microphone not detected; restart audio detection to activate it"
            )
        return I2S_CARD, I2S_DEVICE

    captures = [capture for capture in captures if capture[2] != I2S_CARD]
    playback_cards = {int(match[1]) for path in sound_root.glob("pcmC*D*p")
                      if (match := PLAYBACK_NODE.fullmatch(path.name)) is not None}
    candidates = []
    for card_number, device_number, card_id in captures:
        if card_id is not None:
            # Preserve the dedicated USB microphone preference from this release.
            dedicated_usb = (card_number not in playback_cards
                             and _is_usb(sound_root / f"card{card_number}"))
            candidates.append((not dedicated_usb, card_number, device_number, card_id))
    if not candidates:
        if captures:
            raise AudioConfigError("could not read a valid ALSA capture card ID")
        raise AudioConfigError("no ALSA capture device detected; connect a microphone")

    _, _, device_number, card_id = min(candidates)
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
    *,
    prepare_install=False,
):
    replacements = {
        "MSGBOX_MIC_TYPE": "usb" if prepare_install else kind,
        "MSGBOX_MIC_DEV": microphone_device,
        "MSGBOX_SPK_DEV": speaker_device,
        "MSGBOX_MIC_CARD": microphone_card,
        "MSGBOX_SPEAKER_CARD": speaker_card,
    }
    if kind == "i2s":
        validate_i2s_pins(I2S_INSTALL_PINS)
        replacements.update(I2S_INSTALL_PINS)
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


def validate_i2s_pins(settings):
    conflicts = i2s_pin_conflicts(settings)
    if conflicts:
        raise AudioConfigError(
            "these pins are used by the I2S microphone: " + ", ".join(conflicts)
            + "; rewire and change them in /etc/messagebox/env first"
        )
    pins = []
    for key, default in PIN_SETTINGS.items():
        match = re.fullmatch(r"(?:D|GPIO)?([0-9]+)", settings.get(key) or default)
        if match is None or not 0 <= int(match[1]) <= 27:
            raise AudioConfigError(f"invalid GPIO setting: {key}")
        pins.append(int(match[1]))
    if len(set(pins)) != len(pins) or set(pins) & {2, 3}:
        raise AudioConfigError("box GPIOs must be distinct and leave I2C GPIO 2/3 free")


def check_i2s_application(capability_file=I2S_CAPABILITY):
    # Read declarative metadata without importing application code into an
    # installation helper. Versioned releases cannot inherit a stale marker.
    try:
        tree = ast.parse(Path(capability_file).read_text(encoding="utf-8"))
        values = [ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "I2S_CAPTURE_VERSION"
                          for target in node.targets)]
        if len(values) == 1 and type(values[0]) is int and values[0] == 1:
            return
    except (OSError, SyntaxError, ValueError, TypeError):
        pass
    raise AudioConfigError("application lacks I2S recording support; install a compatible application")


def render_i2s_guard():
    # This generated device configuration is outside release/rollback ownership.
    # Its zz filename sorts after the updater's allowed messagebox.conf drop-ins.
    # Check the selected card, allowing a legacy rollback to use a working USB
    # microphone even when the device's requested type remains I2S.
    return ("# Managed by audio_config.py; retained across application rollback.\n"
            "[Service]\n"
            f"ExecStartPre=/bin/sh -c 'test \"$${{MSGBOX_MIC_CARD:-}}\" != {I2S_CARD} && "
            f"test \"$${{MSGBOX_MIC_PCM:-}}\" != {I2S_PCM} || "
            "exec /usr/bin/python3 /usr/lib/messagebox/audio_config.py --check-i2s-application'\n")


def install_i2s_guards(guard_path):
    for path in (Path(guard_path), Path(guard_path).parent.with_name(
            "messagebox-onboarding-button.service.d") / Path(guard_path).name):
        expected = render_i2s_guard().encode("utf-8")
        try:
            info = path.lstat()
            unchanged = (stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o644
                         and info.st_uid == os.geteuid() and info.st_nlink == 1
                         and path.read_bytes() == expected)
        except FileNotFoundError:
            unchanged = False
        if unchanged:
            # Ordinary startup must not need free root-filesystem space just
            # to reinstall guards which are already correct.
            sync_existing_file(path)
        else:
            write_atomic(path, expected.decode("utf-8"))


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
    slave.rate 48000
}}
"""


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_existing_file(path):
    # An unchanged file may have just been edited outside this helper.
    with Path(path).open("rb") as source:
        os.fsync(source.fileno())
    fsync_directory(Path(path).parent)


def ensure_directory(path):
    path = Path(path)
    if not path.exists():
        ensure_directory(path.parent)
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        fsync_directory(path)
        fsync_directory(path.parent)


def write_atomic(path, content, mode=0o644, owner=None):
    path = Path(path)
    ensure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            if owner is not None:
                os.fchown(output.fileno(), *owner)
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def delete_durable(path):
    path = Path(path)
    path.unlink(missing_ok=True)
    if path.parent.exists():
        fsync_directory(path.parent)


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
    else:
        sync_existing_file(path)


def set_mic_type(
    kind,
    gain_db=DEFAULT_I2S_GAIN_DB,
    *,
    env_path=CONFIG_ENV,
    alsa_config=I2S_ALSA_CONFIG,
    guard_path=I2S_GUARD,
    capability_file=I2S_CAPABILITY,
):
    """Persist the microphone choice without modifying firmware configuration."""
    with audio_lock(env_path):
        settings = read_settings(env_path)
        if kind == "i2s":
            validate_i2s_pins(settings)
            check_i2s_application(capability_file)
            alsa_text = render_i2s_alsa_config(gain_db)
            # Guards and processed PCM must be durable before selecting I2S.
            install_i2s_guards(guard_path)
            write_atomic(alsa_config, alsa_text)
            # Set the type last, so a failed step leaves the box on its old microphone.
            set_env_value(env_path, "MSGBOX_MIC_TYPE", "i2s")
            return
        if kind != "usb":
            raise AudioConfigError(f"MSGBOX_MIC_TYPE must be one of: {', '.join(MIC_TYPES)}")
        set_env_value(env_path, "MSGBOX_MIC_TYPE", "usb")
        delete_durable(alsa_config)


@contextlib.contextmanager
def audio_lock(env_path):
    lock_path = Path(env_path).parent / ".audio-config.lock"
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def configured_mic_type(env_path=CONFIG_ENV):
    try:
        kind = mic_type(read_settings(env_path))
    except FileNotFoundError:
        return "usb"
    return kind


def activate_i2s(*, run=subprocess.run, sleep=time.sleep, monotonic=time.monotonic):
    """Prepare root-filesystem settings, then require the card in this boot.

    A reboot during updater probation is an interrupted install, not a resume.
    Live overlay loading lets the existing health/rollback transaction decide.
    No microphone is opened here.
    """
    with audio_lock(CONFIG_ENV):
        return _activate_i2s(run, sleep, monotonic)


def _activate_i2s(run, sleep, monotonic):
    # Preserve a gain selected by the operator; do not reset it each boot.
    settings = read_settings(CONFIG_ENV)
    if mic_type(settings) != "i2s":
        raise AudioConfigError("microphone selection changed; retry audio detection")
    validate_i2s_pins(settings)
    check_i2s_application(I2S_CAPABILITY)
    # Never edit or sync the firmware's VFAT volume: even rename+directory
    # fsync cannot guarantee a crash-safe config.txt replacement there.
    # Load the overlay at runtime on every boot which needs it instead.
    install_i2s_guards(I2S_GUARD)
    if not I2S_ALSA_CONFIG.exists():
        write_atomic(I2S_ALSA_CONFIG, render_i2s_alsa_config())
    else:
        sync_existing_file(I2S_ALSA_CONFIG)
    deadline = monotonic() + I2S_ACTIVATION_TIMEOUT
    loaded = False
    check_overlay = True
    retry_at = monotonic()
    while True:
        try:
            return detect_microphone(kind="i2s")
        except AudioConfigError:
            pass
        now = monotonic()
        if now >= deadline:
            raise I2SUnavailable(f"I2S microphone unavailable after {I2S_ACTIVATION_TIMEOUT:g} seconds; microphone health failed")
        if not loaded and now >= retry_at:
            try:
                if check_overlay:
                    # A previous invocation or timed-out loader may have applied it.
                    # Establish absence before risking another application.
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        continue
                    result = run(["/usr/bin/dtoverlay", "-l"], check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=min(10, remaining))
                    if "googlevoicehat-soundcard" in result.stdout:
                        loaded = True
                        continue
                    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                    absent = lines == ["No overlays loaded"] or (
                        bool(lines) and lines[0] == "Overlays (in load order):"
                        and all(re.fullmatch(r"[0-9]+:\s+\S+(?:\s+.*)?", line) for line in lines[1:]))
                    if not absent:
                        retry_at = monotonic() + 2
                        continue
                    check_overlay = False
                for command in (["/usr/bin/dtparam", "i2s=on"],
                                ["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]):
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    if command[0] == "/usr/bin/dtoverlay":
                        check_overlay = True
                    run(command, check=True, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=min(10, remaining))
                else:
                    # Loading twice can create duplicate overlays. Once accepted,
                    # only poll: slow enumeration is not another loader failure.
                    loaded = True
            except (OSError, subprocess.SubprocessError):
                retry_at = monotonic() + 2
            continue
        sleep(min(0.25, deadline - now))


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser()
    parser.add_argument("template", nargs="?")
    parser.add_argument("--runtime-output")
    parser.add_argument("--set-mic-type", choices=MIC_TYPES)
    parser.add_argument("--mic-type", choices=MIC_TYPES, default="usb",
                        help="microphone type for a rendered template")
    parser.add_argument("--mic-gain-db", type=float, default=DEFAULT_I2S_GAIN_DB)
    parser.add_argument("--prepare-install", action="store_true",
                        help="render a fresh environment without activating I2S yet")
    parser.add_argument("--check-i2s-pins", metavar="ENV_PATH")
    parser.add_argument("--check-i2s-application", action="store_true")
    options = parser.parse_args(arguments)
    if sum(map(bool, (options.template, options.runtime_output, options.set_mic_type,
                      options.check_i2s_pins, options.check_i2s_application))) != 1:
        parser.error("provide a template, runtime output, microphone switch or one check")
    if options.prepare_install and not options.template:
        parser.error("--prepare-install requires an environment template")
    try:
        if options.check_i2s_pins:
            validate_i2s_pins(read_settings(options.check_i2s_pins))
            return 0
        if options.check_i2s_application:
            if (os.environ.get("MSGBOX_MIC_CARD") == I2S_CARD
                    and os.environ.get("MSGBOX_MIC_PCM") != I2S_PCM):
                raise AudioConfigError("I2S capture requires the processed microphone PCM; restart audio detection")
            check_i2s_application(I2S_CAPABILITY)
            return 0
        if options.set_mic_type:
            set_mic_type(options.set_mic_type, options.mic_gain_db,
                                  env_path=CONFIG_ENV,
                                  alsa_config=I2S_ALSA_CONFIG, guard_path=I2S_GUARD,
                                  capability_file=I2S_CAPABILITY)
            print(f"Microphone type set to {options.set_mic_type}.")
            print("Restart messagebox-audio-detect and the recording services to apply it.")
            return 0
        kind = configured_mic_type() if options.runtime_output else options.mic_type
        if options.template and kind == "i2s":
            # Setup can run before the overlay is active; the boot detector checks it.
            microphone_card, microphone_device = I2S_CARD, I2S_DEVICE
        else:
            if options.runtime_output and kind == "i2s":
                try:
                    microphone_card, microphone_device = activate_i2s()
                except I2SUnavailable as exc:
                    # Keep the physical approval path alive on a committed box.
                    # The raw card remains absent in the existing health probe,
                    # so probation still rolls back an unhealthy candidate.
                    print(f"audio setup: {exc}; keeping button repair available", file=sys.stderr)
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
                    prepare_install=options.prepare_install,
                )
            )
    except (AudioConfigError, OSError, UnicodeError) as exc:
        print(f"audio setup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
