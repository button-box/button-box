"""Bundled sound contracts and durable first-use receipts; no synthesis fallback."""

import fcntl
import hashlib
import json
import logging
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
    "all-set", "online", "msg-start", "count-reply", "count-new", "review",
    "ask-send-1", "ask-send-2", "ask-send-3", "last-chance", "not-sent", "empty",
    "listened", "card-needed", "card-unknown", "card-prompt", "card-saved", "stuck", "fail",
)
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
    if name not in VOICE_NAMES:
        raise ValueError("unknown voice line")
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
    _validate_pack(root / "voice", VOICE_NAMES, "lines", "voice")
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



if __name__ == "__main__":
    import sys
    try:
        validate_sounds(sys.argv[1])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
