#!/usr/bin/env python3
"""Button Box incoming-audio poller (v0 production rig).

Polls the wacli DB for fresh voice notes and ordinary videos in allowed chats,
downloads their media between sync bursts, and queues their audio as WAVs for
the button service to play (answering-machine model: nothing auto-plays; the
button's lamp signals waiting messages). Queue dir is persistent — survives
reboots. Config is loaded from /etc/messagebox/env by systemd.
"""
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone

from messagebox.contacts import ContactError, ContactStore
from messagebox.runtime_paths import CONTACTS_FILE
from messagebox.runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
from messagebox.runtime_paths import STATE_DIR

POLL_S = float(os.environ.get("MSGBOX_POLL_S", "3"))
LOCK_WAIT = os.environ.get("MSGBOX_LOCK_WAIT", "60s")
QUEUE_DIR = str(DEFAULT_QUEUE_DIR)
EVENTS_FILE = str(STATE_DIR / "events.jsonl")
WACLI_BIN = "/usr/local/bin/wacli"
SUPPORTED_MEDIA_TYPES = frozenset(("audio", "video"))


def positive_setting(name, default, cast):
    value = cast(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


MAX_MEDIA_BYTES = positive_setting("MSGBOX_MEDIA_MAX_BYTES", 104_857_600, int)
MAX_MEDIA_SECONDS = positive_setting("MSGBOX_MEDIA_MAX_SECONDS", 1800, float)


class MediaRejected(Exception):
    """A permanent, public-safe reason not to queue downloaded media."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_contact_authorizations(path=CONTACTS_FILE):
    """Return validated ``ChatJID -> receive_after`` authorization rules."""
    try:
        contacts = ContactStore(path).load()["contacts"]
    except ContactError:
        return {}
    return {
        jid: contact["receive_after"]
        for jid, contact in contacts.items()
    }


def parse_wacli_timestamp(raw):
    """Parse timestamp forms emitted by wacli into Unix seconds."""
    if isinstance(raw, bool) or raw is None:
        return None

    if isinstance(raw, (int, float)):
        timestamp = float(raw)
    elif isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        try:
            timestamp = float(value)
        except ValueError:
            if value.endswith(("Z", "z")):
                value = value[:-1] + "+00:00"
            elif value.upper().endswith(" UTC"):
                value = value[:-4].rstrip()
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None

    if not math.isfinite(timestamp):
        return None
    # wacli versions have exposed Unix timestamps at differing precisions.
    while abs(timestamp) >= 100_000_000_000:
        timestamp /= 1000
    return timestamp


def message_is_authorized(message, authorizations):
    receive_after = authorizations.get(message.get("ChatJID"))
    if receive_after is None:
        return False
    timestamp = parse_wacli_timestamp(message.get("Timestamp"))
    return timestamp is not None and timestamp >= receive_after


def log_event(**ev):
    """Append an analytics event (best-effort; never breaks the pipeline)."""
    try:
        ev["ts"] = time.time()
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception as e:
        print(f"event log error: {e}", flush=True)
# EQ for the small boxy speaker: cut low-mid mud, lift presence/highs, then
# normalize loudness. Set to "" to disable, or override with any ffmpeg -af chain.
EQ_FILTER = os.environ.get("MSGBOX_EQ_FILTER",
    "highpass=f=150,treble=g=6:f=3000,loudnorm=I=-16:TP=-1.5")
STATE_FILE = str(STATE_DIR / "seen.json")


def wacli(*args):
    return subprocess.run([WACLI_BIN, *args], capture_output=True, text=True, timeout=300)


def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(seen), f)
    os.replace(tmp, STATE_FILE)


def download_media(message):
    result = wacli(
        "media", "download", "--chat", message["ChatJID"],
        "--id", message["MsgID"], "--lock-wait", LOCK_WAIT, "--json",
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError):
        return None
    if result.returncode != 0 or payload.get("success") is not True:
        return None
    path = (payload.get("data") or {}).get("path")
    return path if isinstance(path, str) and path else None


def probe_audio(path):
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise MediaRejected("invalid_media") from exc
    if size > MAX_MEDIA_BYTES:
        raise MediaRejected("media_too_large")

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,duration:format=duration", "-of", "json", path,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise MediaRejected("invalid_media") from exc

    streams = payload.get("streams") or []
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise MediaRejected("no_audio_track")

    raw_durations = [(payload.get("format") or {}).get("duration")]
    raw_durations.extend(stream.get("duration") for stream in audio_streams)
    durations = []
    for raw in raw_durations:
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            durations.append(duration)
    if not durations:
        raise MediaRejected("invalid_media")
    duration = max(durations)
    if duration > MAX_MEDIA_SECONDS:
        raise MediaRejected("media_too_long")
    return duration


def remove_if_present(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def queue_audio(message, path):
    duration = probe_audio(path)
    eq = ["-af", EQ_FILTER] if EQ_FILTER else []
    os.makedirs(QUEUE_DIR, exist_ok=True)
    # The poller visits messages oldest-first, so creation time preserves the
    # existing queue order without exposing private message timestamps.
    qwav = os.path.join(QUEUE_DIR, f"{int(time.time() * 1000)}-{message['MsgID']}.wav")
    qtmp = qwav + ".part"
    qmeta = qwav + ".json"
    qmeta_tmp = qmeta + ".part"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", path,
                "-map", "0:a:0", *eq, "-ar", "48000", "-ac", "1", "-f", "wav", qtmp,
            ],
            check=True,
            timeout=120,
        )
        # Persist exact reply routing before exposing the WAV. The button
        # service never infers or falls back to another chat.
        with open(qmeta_tmp, "w") as f:
            json.dump({
                "version": 1,
                "chat": message["ChatJID"],
                "msgid": message["MsgID"],
                "sender_jid": message.get("SenderJID"),
            }, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(qmeta_tmp, qmeta)
        os.replace(qtmp, qwav)  # WAV appears only after routing metadata
    except (OSError, subprocess.SubprocessError) as exc:
        for candidate in (qtmp, qmeta_tmp, qmeta, qwav):
            remove_if_present(candidate)
        raise MediaRejected("invalid_media") from exc
    return qwav, duration


def process_message(message, seen):
    seen.add(message["MsgID"])
    save_seen(seen)
    started = time.time()
    print(
        f"NEW {message['MsgID']} from {message.get('SenderName')} "
        f"ts={message.get('Timestamp')}",
        flush=True,
    )
    path = download_media(message)
    if path is None:
        print("DOWNLOAD FAILED", flush=True)
        seen.discard(message["MsgID"])
        save_seen(seen)
        log_event(type="receive_retry", msgid=message["MsgID"], reason="download_failed")
        return "retry"

    print(f"DOWNLOAD ok in {time.time() - started:.1f}s", flush=True)
    try:
        qwav, duration = queue_audio(message, path)
    except MediaRejected as exc:
        print(f"SKIPPED {exc.reason}", flush=True)
        log_event(type="receive_skipped", msgid=message["MsgID"], reason=exc.reason)
        return "skipped"

    log_event(
        type="received",
        chat=message["ChatJID"],
        sender=message.get("SenderName"),
        sender_jid=message.get("SenderJID"),
        msgid=message["MsgID"],
        file=os.path.basename(qwav),
        dur=duration,
    )
    print(f"QUEUED {os.path.basename(qwav)} total={time.time() - started:.1f}s", flush=True)
    return "queued"


def process_messages(messages, seen, authorizations):
    for message in reversed(messages):  # oldest first (R9)
        msgid = message.get("MsgID")
        if not msgid or msgid in seen or message.get("FromMe"):
            continue
        if not message_is_authorized(message, authorizations):
            continue
        if message.get("MediaType") not in SUPPORTED_MEDIA_TYPES:
            continue
        try:
            process_message(message, seen)
        except Exception:
            # A malformed message must not prevent later messages from being
            # considered. The ID stays seen if processing had already begun.
            print("MESSAGE FAILED invalid_media", flush=True)
            log_event(type="receive_skipped", msgid=msgid, reason="invalid_media")


def main():
    seen = load_seen()
    print(f"messagebox poller up: seen={len(seen)}", flush=True)
    while True:
        try:
            authorizations = load_contact_authorizations()
            # A normal listing initializes wacli.db even before an account is
            # linked, making the empty pairing destination look occupied.
            r = wacli("--read-only", "messages", "list", "--limit", "10", "--json", "--full")
            data = json.loads(r.stdout or "{}")
            msgs = (data.get("data") or {}).get("messages") or []
            process_messages(msgs, seen, authorizations)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
