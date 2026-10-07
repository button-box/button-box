"""Bundled sound contracts and durable first-use receipts; no synthesis fallback."""

import fcntl
import hashlib
import json
import wave
from pathlib import Path

from messagebox.cloud_device import atomic_json, open_private_lock
from messagebox.runtime_paths import APP_DIR, STATE_DIR

CUE_NAMES = (
    "press", "card", "rec_go", "rec_limit", "msg_start", "msg_end", "oops",
    "still_trying", "offline", "ready", "connected", "all_set", "sent", "listened", "card_saved",
)
VOICE_NAMES = (
    "all-set", "online", "msg-start", "count-reply", "count-new", "review",
    "ask-send-1", "ask-send-2", "ask-send-3", "last-chance", "not-sent", "empty",
    "listened", "card-needed", "card-unknown", "stuck", "fail",
)
SOUND_DIR = APP_DIR / "sounds"
SOUND_STATE = STATE_DIR / "sound-state.json"
ALL_SET_REQUEST = Path("/var/lib/messagebox-cloud/setup-complete-sound.json")


def cue_path(name):
    return SOUND_DIR / "cues" / f"cue-{name}.wav"


def voice_path(name):
    return SOUND_DIR / "voice" / f"voice-{name}.wav"


def validate_sounds(root=SOUND_DIR):
    root = Path(root)
    for pack, names, key, prefix in (("cues", CUE_NAMES, "cues", "cue"),
                                     ("voice", VOICE_NAMES, "lines", "voice")):
        try:
            manifest_path = root / pack / "manifest.json"
            if manifest_path.is_symlink():
                raise ValueError
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))[key]
            expected = {f"{prefix}-{name}.wav" for name in names}
            if len(rows) != len(expected) or {row["file"] for row in rows} != expected:
                raise ValueError
            for row in rows:
                path = root / pack / row["file"]
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
        except (OSError, EOFError, wave.Error, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Missing or invalid {pack} sound pack") from exc


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
