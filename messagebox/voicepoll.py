#!/usr/bin/env python3
"""Button Box incoming-audio poller (v0 production rig).

Polls the wacli DB for fresh voice notes and ordinary videos in allowed chats,
downloads their media between sync bursts, and queues the audio as WAVs for the
button service to play (answering-machine model: nothing auto-plays; the
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
QUEUE_DIR = str(DEFAULT_QUEUE_DIR)
EVENTS_FILE = str(STATE_DIR / "events.jsonl")
WACLI_BIN = "/usr/local/bin/wacli"
VIDEO_MAX_BYTES = int(os.environ.get("MSGBOX_VIDEO_MAX_BYTES", str(100 * 1024 * 1024)))
VIDEO_MAX_DURATION_S = float(os.environ.get("MSGBOX_VIDEO_MAX_DURATION_S", "600"))
PLAYABLE_MEDIA_TYPES = {"audio", "video"}


class MediaRejected(Exception):
    """A permanent media failure that should not be retried every poll."""


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
    except Exception:
        print("event log error", flush=True)
# EQ for the small boxy speaker: cut low-mid mud, lift presence/highs, then
# normalize loudness. Set to "" to disable, or override with any ffmpeg -af chain.
EQ_FILTER = os.environ.get("MSGBOX_EQ_FILTER",
    "highpass=f=150,treble=g=6:f=3000,loudnorm=I=-16:TP=-1.5")
STATE_FILE = str(STATE_DIR / "seen.json")
_last_queue_ms = 0


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


def next_queue_ms():
    """Return a strictly increasing millisecond queue prefix."""
    global _last_queue_ms
    _last_queue_ms = max(int(time.time() * 1000), _last_queue_ms + 1)
    return _last_queue_ms


def audio_duration(path):
    """Return the first audio stream duration, or ``None`` when unavailable."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=duration:format=duration", "-of", "json", path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise MediaRejected("invalid media")
    try:
        details = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise MediaRejected("invalid media") from exc
    streams = details.get("streams") or []
    if not streams:
        raise MediaRejected("video has no audio track")

    candidates = [streams[0].get("duration"), (details.get("format") or {}).get("duration")]
    for raw in candidates:
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration >= 0:
            return duration
    return None


def wav_duration(path):
    try:
        import wave
        with wave.open(path) as wav:
            return wav.getnframes() / wav.getframerate()
    except Exception:
        return None


def safe_unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def download_path(message):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    output_path = os.path.join(
        QUEUE_DIR, f".media-{os.getpid()}-{time.time_ns()}.part"
    )
    result = wacli(
        "--read-only", "media", "download", "--chat", message["ChatJID"],
        "--id", message["MsgID"], "--output", output_path, "--json",
    )
    payload = {}
    try:
        payload = json.loads(result.stdout or "{}")
        path = (payload.get("data") or {}).get("path")
    except (AttributeError, TypeError, ValueError):
        path = None
    if result.returncode != 0 or payload.get("success") is not True or not path:
        safe_unlink(output_path)
        return None
    return output_path


def queue_message(message, source_path):
    media_type = str(message.get("MediaType") or "").strip().lower()
    if media_type == "video":
        source_size = os.path.getsize(source_path)
        if source_size > VIDEO_MAX_BYTES:
            raise MediaRejected("video exceeds the configured size limit")
        duration = audio_duration(source_path)
        if duration is not None and duration > VIDEO_MAX_DURATION_S:
            raise MediaRejected("video exceeds the configured duration limit")

    os.makedirs(QUEUE_DIR, exist_ok=True)
    # Millisecond prefix keeps the queue sorted oldest-first.
    qwav = os.path.join(QUEUE_DIR, f"{next_queue_ms()}-{message['MsgID']}.wav")
    qtmp = qwav + ".part"
    qmeta = qwav + ".json"
    qmeta_tmp = qmeta + ".part"
    metadata_published = False
    try:
        eq = ["-af", EQ_FILTER] if EQ_FILTER else []
        audio_selection = ["-map", "0:a:0", "-vn"] if media_type == "video" else []
        # Probe metadata is advisory. Always cap video output so corrupt or
        # misreported input cannot expand until the process timeout or storage
        # exhaustion on the Pi.
        duration_limit = ["-t", str(VIDEO_MAX_DURATION_S)] if media_type == "video" else []
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", source_path,
                *audio_selection, *duration_limit, *eq,
                "-ar", "48000", "-ac", "1", "-f", "wav", qtmp,
            ],
            check=True,
            timeout=120,
        )
        duration = wav_duration(qtmp)
        if duration is None:
            raise MediaRejected("transcoded audio is invalid")
        if media_type == "video" and duration is not None and duration > VIDEO_MAX_DURATION_S:
            raise MediaRejected("video exceeds the configured duration limit")

        # Persist exact reply routing before exposing the WAV. The button
        # service never infers or falls back to another chat.
        with open(qmeta_tmp, "w") as f:
            json.dump({
                "version": 1,
                "chat": message["ChatJID"],
                "msgid": message["MsgID"],
                "sender_jid": message.get("SenderJID"),
                "media_type": media_type,
            }, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(qmeta_tmp, qmeta)
        metadata_published = True
        os.replace(qtmp, qwav)  # WAV appears only after routing metadata
        return qwav, duration
    except subprocess.TimeoutExpired:
        raise
    except subprocess.CalledProcessError as exc:
        raise MediaRejected("media audio could not be decoded") from exc
    finally:
        safe_unlink(qtmp)
        safe_unlink(qmeta_tmp)
        if metadata_published and not os.path.exists(qwav):
            safe_unlink(qmeta)


def process_message(message, seen):
    """Download and queue one authorized message without blocking later ones."""
    message_id = message["MsgID"]
    seen.add(message_id)
    save_seen(seen)
    started = time.time()
    print("NEW media message", flush=True)
    try:
        path = download_path(message)
    except Exception:
        path = None
    if path is None:
        print("DOWNLOAD FAILED", flush=True)
        seen.discard(message_id)  # Transient download failures retry next cycle.
        save_seen(seen)
        return
    print(f"DOWNLOAD ok in {time.time() - started:.1f}s", flush=True)
    try:
        try:
            queued_path, duration = queue_message(message, path)
        except MediaRejected as exc:
            print(f"SKIPPED media: {exc}", flush=True)
            log_event(type="receive_skipped", msgid=message_id, reason=str(exc))
            return
        except Exception:
            seen.discard(message_id)
            save_seen(seen)
            print("RETRY media: queue failure", flush=True)
            log_event(type="receive_retry", msgid=message_id, reason="queue failure")
            return
    finally:
        safe_unlink(path)

    log_event(
        type="received",
        chat=message["ChatJID"],
        sender=message.get("SenderName"),
        sender_jid=message.get("SenderJID"),
        msgid=message_id,
        file=os.path.basename(queued_path),
        dur=duration,
    )
    print(f"QUEUED media total={time.time() - started:.1f}s", flush=True)


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
            for m in reversed(msgs):  # oldest first (R9)
                if m["MsgID"] in seen or m.get("FromMe"):
                    continue
                if not message_is_authorized(m, authorizations):
                    continue
                media_type = str(m.get("MediaType") or "").strip().lower()
                if media_type not in PLAYABLE_MEDIA_TYPES:
                    continue
                process_message(m, seen)
        except Exception:
            print("poll error: processing failed", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
