#!/usr/bin/env python3
"""Button Box incoming-voice-note poller (v0 production rig).

Polls the wacli DB for fresh voice notes in allowed chats, downloads their
media between sync bursts, and queues them as WAVs for the button service
to play (answering-machine model: nothing auto-plays; the button's lamp
signals waiting messages). Queue dir is persistent — survives reboots.
Config is loaded from /etc/messagebox/env by systemd.
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


def queue_message(message, seen):
    """Download and atomically queue one authorized audio message."""
    message_id = message["MsgID"]
    seen.add(message_id)
    save_seen(seen)
    started = time.time()
    print(
        f"NEW {message_id} from {message.get('SenderName')} "
        f"ts={message.get('Timestamp')}",
        flush=True,
    )
    os.makedirs(QUEUE_DIR, exist_ok=True)
    qwav = os.path.join(QUEUE_DIR, f"{next_queue_ms()}-{message_id}.wav")
    qtmp = qwav + ".part"
    media_tmp = qwav + ".media.part"
    try:
        # Continuous sync owns the writable wacli store. An explicit output
        # lets media download operate read-only without disconnecting sync,
        # so a second voice note cannot land in a download lock gap.
        dl = wacli(
            "--read-only",
            "media",
            "download",
            "--chat",
            message["ChatJID"],
            "--id",
            message_id,
            "--output",
            media_tmp,
            "--json",
        )
        try:
            downloaded = json.loads(dl.stdout or "{}")
        except json.JSONDecodeError:
            downloaded = {}
        if dl.returncode != 0 or downloaded.get("success") is not True:
            detail = (dl.stdout or dl.stderr)[-200:].strip()
            print(f"DOWNLOAD FAILED {detail}", flush=True)
            seen.discard(message_id)
            save_seen(seen)
            return False
        print(f"DOWNLOAD ok in {time.time()-started:.1f}s", flush=True)
        eq = ["-af", EQ_FILTER] if EQ_FILTER else []
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                media_tmp,
                *eq,
                "-ar",
                "48000",
                "-ac",
                "1",
                "-f",
                "wav",
                qtmp,
            ],
            check=True,
            timeout=120,
        )
        # Persist exact reply routing before exposing the WAV.  The button
        # service never infers or falls back to another chat.
        qmeta = qwav + ".json"
        qmeta_tmp = qmeta + ".part"
        with open(qmeta_tmp, "w") as handle:
            json.dump(
                {
                    "version": 1,
                    "chat": message["ChatJID"],
                    "msgid": message_id,
                    "sender_jid": message.get("SenderJID"),
                },
                handle,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(qmeta_tmp, qmeta)
        os.replace(qtmp, qwav)
        try:
            import wave as _w

            with _w.open(qwav) as wav_file:
                duration = wav_file.getnframes() / wav_file.getframerate()
        except Exception:
            duration = None
        log_event(
            type="received",
            chat=message["ChatJID"],
            sender=message.get("SenderName"),
            sender_jid=message.get("SenderJID"),
            msgid=message_id,
            file=os.path.basename(qwav),
            dur=duration,
        )
        print(
            f"QUEUED {os.path.basename(qwav)} total={time.time()-started:.1f}s",
            flush=True,
        )
        return True
    finally:
        try:
            os.unlink(media_tmp)
        except FileNotFoundError:
            pass


def poll_once(seen, authorizations):
    """Queue every unseen authorized audio message, oldest first."""
    result = wacli(
        "--read-only", "messages", "list", "--limit", "10", "--json", "--full"
    )
    data = json.loads(result.stdout or "{}")
    messages = (data.get("data") or {}).get("messages") or []
    queued_count = 0
    for message in reversed(messages):
        if message["MsgID"] in seen or message.get("FromMe"):
            continue
        if not message_is_authorized(message, authorizations):
            continue
        if message.get("MediaType") != "audio":
            continue
        if queue_message(message, seen):
            queued_count += 1
    return queued_count


def main():
    seen = load_seen()
    print(f"messagebox poller up: seen={len(seen)}", flush=True)
    while True:
        try:
            authorizations = load_contact_authorizations()
            # A normal listing initializes wacli.db even before an account is
            # linked, making the empty pairing destination look occupied.
            poll_once(seen, authorizations)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
