"""Bundled sound contracts and durable first-use receipts; no synthesis fallback."""

import fcntl
import hashlib
import json
import logging
import time
import wave
from pathlib import Path

from messagebox.cloud_device import atomic_json, open_private_lock
from messagebox.runtime_paths import APP_DIR, STATE_DIR
from messagebox.settings import SettingsReader, VOICE_PACKS, normalize_voice_pack

CUE_NAMES = (
    "press", "card", "rec_go", "rec_limit", "msg_start", "msg_end", "oops", "deleted",
    "still_trying", "offline", "ready", "connected", "all_set", "sent", "listened", "card_saved",
)
VOICE_NAMES = (
    "all-set", "online", "online-ready", "msg-start", "count-reply", "count-new", "review",
    "ask-send-1", "ask-send-2", "ask-send-3", "last-chance", "not-sent", "empty",
    "listened", "card-needed", "card-unknown", "card-prompt", "card-saved", "stuck", "fail",
)
SETUP_VOICE_NAMES = ("setup-online",)
# Reaction-card emoji (variation selectors and skin tones removed) to its
# bundled "You just sent ..." line. Words follow Cloud's emoji words and card
# names. Any other emoji uses the generic "You just sent a reaction!" line.
REACTION_SENT_LINES = {
    "\u2764": "heart", "\U0001f602": "big-laugh", "\U0001f44f": "applause", "\U0001f60d": "heart-eyes",
    "\U0001f44d": "thumbs-up", "\U0001f525": "fire", "\U0001f389": "celebration", "\U0001f618": "kiss",
    "\U0001f62e": "surprise", "\U0001f622": "tear", "\U0001f64c": "hooray", "\U0001f60a": "smile",
}
GENERIC_REACTION_SENT = "reaction"
MAX_OPTIONAL_LINE_BYTES = 2 * 1024 * 1024
SOUND_DIR = APP_DIR / "sounds"
SOUND_STATE = STATE_DIR / "sound-state.json"
ALL_SET_REQUEST = Path("/var/lib/messagebox-cloud/setup-complete-sound.json")


def cue_path(name):
    return SOUND_DIR / "cues" / f"cue-{name}.wav"


_settings = SettingsReader()
_missing_voice_files = set()


def current_voice_pack():
    return normalize_voice_pack(_settings.snapshot().get("voice_pack"))


def voice_path(name, pack=None):
    if name not in VOICE_NAMES + SETUP_VOICE_NAMES:
        raise ValueError("unknown voice line")
    if name in SETUP_VOICE_NAMES:
        return SOUND_DIR / "voice" / f"voice-{name}.wav"
    pack = current_voice_pack() if pack is None else normalize_voice_pack(pack)
    fallback = SOUND_DIR / "voice" / f"voice-{name}.wav"
    if pack == "jessica":
        return fallback
    path = SOUND_DIR / "voices" / pack / fallback.name
    if path.is_file() and not path.is_symlink():
        return path
    key = (pack, name)
    if key not in _missing_voice_files:
        _missing_voice_files.add(key)
        logging.getLogger(__name__).warning("Missing %s voice line %s; using jessica", pack, name)
    return fallback


def _playable_wav(path):
    """Accept only a local, bounded 48 kHz mono 16-bit PCM file."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OPTIONAL_LINE_BYTES:
            return False
        with wave.open(str(path), "rb") as sound:
            frames = sound.getnframes()
            return (sound.getnchannels() == 1 and sound.getsampwidth() == 2
                    and sound.getframerate() == 48000 and sound.getcomptype() == "NONE"
                    and frames > 0 and len(sound.readframes(frames)) == frames * 2)
    except (OSError, EOFError, wave.Error):
        return False


def reaction_sent_key(emoji):
    plain = "".join(character for character in str(emoji or "")
                    if character not in "\ufe0e\ufe0f" and not "\U0001f3fb" <= character <= "\U0001f3ff")
    return REACTION_SENT_LINES.get(plain, GENERIC_REACTION_SENT)


def reaction_sent_path(emoji, pack=None):
    """The "You just sent ..." line for a reaction card, or None.

    Order: this emoji in the current pack, then in Jessica, then the generic
    line in the current pack, then in Jessica. Optional lines are never
    required for a pack to be installed.
    """
    pack = current_voice_pack() if pack is None else normalize_voice_pack(pack)
    for name in dict.fromkeys((reaction_sent_key(emoji), GENERIC_REACTION_SENT)):
        filename = f"voice-sent-{name}.wav"
        candidates = [SOUND_DIR / "voice" / filename]
        if pack != "jessica":
            candidates.insert(0, SOUND_DIR / "voices" / pack / filename)
        for path in candidates:
            if _playable_wav(path):
                return path
    return None


def _validate_pack(directory, names, key, prefix):
    try:
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if directory.parent.name == "voices" and manifest.get("voice_key") != directory.name:
            raise ValueError
        rows = manifest[key]
        expected = {f"{prefix}-{name}.wav" for name in names}
        if len(rows) != len(expected) or {row["file"] for row in rows} != expected:
            raise ValueError
        for row in rows:
            path = directory / row["file"]
            if path.is_symlink() or not path.is_file():
                raise ValueError
            with wave.open(str(path), "rb") as sound:
                frames = sound.getnframes()
                if (frames <= 0 or sound.getnchannels() != 1 or sound.getsampwidth() != 2
                        or sound.getframerate() != 48000 or sound.getcomptype() != "NONE"
                        or len(sound.readframes(frames)) != frames * 2
                        or (path.name == "cue-press.wav" and frames != 19200)):
                    raise ValueError
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError
    except (OSError, EOFError, wave.Error, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Missing or invalid {directory.name} sound pack") from exc


def installed_voice_packs(root=None):
    root = SOUND_DIR if root is None else Path(root)
    installed = ["jessica"]
    for pack in VOICE_PACKS[1:]:
        try:
            _validate_pack(root / "voices" / pack, VOICE_NAMES, "lines", "voice")
        except ValueError:
            continue
        installed.append(pack)
    return installed


def validate_sounds(root=None, *, require_voice_packs=True):
    root = SOUND_DIR if root is None else Path(root)
    _validate_pack(root / "cues", CUE_NAMES, "cues", "cue")
    _validate_pack(root / "voice", VOICE_NAMES + SETUP_VOICE_NAMES, "lines", "voice")
    if require_voice_packs:
        for pack in VOICE_PACKS[1:]:
            _validate_pack(root / "voices" / pack, VOICE_NAMES, "lines", "voice")


def consume_moment(name, path=SOUND_STATE, *, token=True):
    """Receipt before playback prevents duplicate welcomes after a crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open_private_lock(path.with_suffix(".lock")) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = {}
        if not isinstance(document, dict):
            raise ValueError("sound receipts are invalid")
        if document.get(name) == token:
            return False
        document[name] = token
        atomic_json(path, document)
        return True


def next_send_prompt(path=SOUND_STATE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open_private_lock(path.with_suffix(".lock")) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = {}
        if not isinstance(document, dict):
            raise ValueError("sound receipts are invalid")
        index = document.get("send_take", 0)
        if type(index) is not int or not 0 <= index < 3:
            raise ValueError("send prompt receipt is invalid")
        document["send_take"] = (index + 1) % 3
        atomic_json(path, document)
    return voice_path(f"ask-send-{index + 1}")


def online_transition(boot_id, online, *, audible=True, mono=None, wall=None, path=SOUND_STATE):
    """Consume Cloud online edges durably, including silent or rate-limited edges."""
    mono = time.monotonic() if mono is None else mono
    wall = time.time() if wall is None else wall
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open_private_lock(path.with_suffix(".lock")) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = {}
        if not isinstance(document, dict):
            raise ValueError("sound receipts are invalid")
        previous = document.get("online_ready", {})
        if not isinstance(previous, dict):
            raise ValueError("online receipt is invalid")
        if previous.get("boot_id") == boot_id and previous.get("online") is online:
            return False
        elapsed = 600
        if "played_wall" in previous:
            # Same-boot rate limits tolerate wall-clock corrections; reboot uses UTC.
            elapsed = (mono - previous["played_mono"] if previous.get("played_boot") == boot_id
                       else wall - previous["played_wall"])
        play = online and audible and elapsed >= 600
        receipt = {**previous, "boot_id": boot_id, "online": online}
        if play:
            receipt.update(played_boot=boot_id, played_mono=mono, played_wall=wall)
        document["online_ready"] = receipt
        atomic_json(path, document)
        return play



if __name__ == "__main__":
    import sys
    try:
        validate_sounds(sys.argv[1])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
