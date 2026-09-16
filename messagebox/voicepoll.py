#!/usr/bin/env python3
"""Button Box incoming-audio poller (v0 production rig).

Polls the wacli DB for fresh voice notes and videos in allowed chats, downloads
their media between sync bursts, and queues the audio as WAVs for the button
service to play (answering-machine model: nothing auto-plays; the button's lamp
signals waiting messages). Queue dir is persistent — survives reboots. Config
is loaded from /etc/messagebox/env by systemd.
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
PLAYABLE_MEDIA_TYPES = frozenset({"audio", "video"})
MAX_MEDIA_BYTES = int(os.environ.get("MSGBOX_RECEIVE_MAX_BYTES", str(50 * 1024 * 1024)))
MAX_MEDIA_SECONDS = float(os.environ.get("MSGBOX_RECEIVE_MAX_SECONDS", "600"))


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


class MediaRejected(Exception):
    """The downloaded media is permanently unsuitable for the playback queue."""


def is_playable_media(message):
    return message.get("MediaType") in PLAYABLE_MEDIA_TYPES


def probe_audio(path):
    """Return media duration after verifying that an audio stream exists."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,duration", "-of", "json", path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise MediaRejected("invalid_media")
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise MediaRejected("invalid_media") from error

    audio_streams = [
        stream for stream in data.get("streams") or []
        if stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise MediaRejected("no_audio")

    durations = [
        (data.get("format") or {}).get("duration"),
        *(stream.get("duration") for stream in audio_streams),
    ]
    for raw in durations:
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration >= 0:
            return duration
    raise MediaRejected("invalid_duration")


def download_media(message):
    result = wacli(
        "media", "download", "--chat", message["ChatJID"], "--id", message["MsgID"],
        "--lock-wait", LOCK_WAIT, "--json",
    )
    response = {}
    try:
        response = json.loads(result.stdout or "{}")
        path = response["data"]["path"]
    except (KeyError, TypeError, ValueError):
        path = None
    if not response.get("success") or not isinstance(path, str) or not path:
        # Do not forward command output: it may contain private identifiers or
        # paths. The message remains unseen so the next cycle can retry it.
        raise RuntimeError("download failed")
    return path


def queue_media(message, path):
    try:
        size = os.path.getsize(path)
    except OSError as error:
        raise MediaRejected("invalid_media") from error
    if MAX_MEDIA_BYTES > 0 and size > MAX_MEDIA_BYTES:
        raise MediaRejected("too_large")

    duration = probe_audio(path)
    if MAX_MEDIA_SECONDS > 0 and duration > MAX_MEDIA_SECONDS:
        raise MediaRejected("too_long")

    eq = ["-af", EQ_FILTER] if EQ_FILTER else []
    os.makedirs(QUEUE_DIR, exist_ok=True)
    # The timestamp prefix keeps the queue sorted oldest-first.
    qwav = os.path.join(QUEUE_DIR, f"{int(time.time()*1000)}-{message['MsgID']}.wav")
    qtmp = qwav + ".part"
    qmeta = qwav + ".json"
    qmeta_tmp = qmeta + ".part"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vn", *eq,
                "-ar", "48000", "-ac", "1", "-f", "wav", qtmp,
            ],
            check=True,
            timeout=120,
        )
        # Persist exact reply routing before exposing the WAV. The button
        # service never infers or falls back to another chat.
        with open(qmeta_tmp, "w") as file:
            json.dump({
                "version": 1,
                "chat": message["ChatJID"],
                "msgid": message["MsgID"],
                "sender_jid": message.get("SenderJID"),
            }, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(qmeta_tmp, qmeta)
        os.replace(qtmp, qwav)  # WAV appears only after routing metadata
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise MediaRejected("invalid_media") from error
    finally:
        for partial in (qtmp, qmeta_tmp):
            try:
                os.unlink(partial)
            except FileNotFoundError:
                pass

    log_event(
        type="received",
        chat=message["ChatJID"],
        sender=message.get("SenderName"),
        sender_jid=message.get("SenderJID"),
        msgid=message["MsgID"],
        file=os.path.basename(qwav),
        dur=duration,
        source_media=message.get("MediaType"),
    )
    return qwav


def process_message(message, seen):
    """Download and queue one authorized message without affecting later ones."""
    msgid = message["MsgID"]
    seen.add(msgid)
    save_seen(seen)
    started = time.time()
    print(f"NEW {message.get('MediaType')} message", flush=True)
    try:
        path = download_media(message)
    except RuntimeError:
        print("DOWNLOAD FAILED", flush=True)
        seen.discard(msgid)  # download failures are safe to retry next cycle
        save_seen(seen)
        return False

    print(f"DOWNLOAD ok in {time.time()-started:.1f}s", flush=True)
    try:
        queue_media(message, path)
    except MediaRejected as error:
        reason = str(error)
        log_event(
            type="receive_skipped",
            chat=message["ChatJID"],
            sender_jid=message.get("SenderJID"),
            msgid=msgid,
            reason=reason,
            source_media=message.get("MediaType"),
        )
        print(f"SKIPPED media reason={reason}", flush=True)
        return False
    except Exception:
        # Local I/O failures may be transient, so retry without preventing
        # later messages in this poll from being handled.
        seen.discard(msgid)
        save_seen(seen)
        raise

    print(f"QUEUED media total={time.time()-started:.1f}s", flush=True)
    return True


def poll_once(seen, authorizations):
    # A normal listing initializes wacli.db even before an account is linked,
    # making the empty pairing destination look occupied.
    result = wacli("--read-only", "messages", "list", "--limit", "10", "--json", "--full")
    data = json.loads(result.stdout or "{}")
    messages = (data.get("data") or {}).get("messages") or []
    for message in reversed(messages):  # oldest first (R9)
        msgid = message.get("MsgID")
        if not msgid or msgid in seen or message.get("FromMe"):
            continue
        if not message_is_authorized(message, authorizations):
            continue
        if not is_playable_media(message):
            continue
        try:
            process_message(message, seen)
        except Exception:
            print("message error: processing failed", flush=True)


def main():
    seen = load_seen()
    print(f"messagebox poller up: seen={len(seen)}", flush=True)
    while True:
        try:
            authorizations = load_contact_authorizations()
            poll_once(seen, authorizations)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
