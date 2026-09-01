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
LOCK_WAIT = os.environ.get("MSGBOX_LOCK_WAIT", "60s")
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


def main():
    seen = load_seen()
    print(f"messagebox poller up: seen={len(seen)}", flush=True)
    while True:
        try:
            authorizations = load_contact_authorizations()
            r = wacli("messages", "list", "--limit", "10", "--json", "--full")
            data = json.loads(r.stdout or "{}")
            msgs = (data.get("data") or {}).get("messages") or []
            for m in reversed(msgs):  # oldest first (R9)
                if m["MsgID"] in seen or m.get("FromMe"):
                    continue
                if not message_is_authorized(m, authorizations):
                    continue
                if m.get("MediaType") != "audio":
                    continue
                seen.add(m["MsgID"])
                save_seen(seen)
                t0 = time.time()
                print(f"NEW {m['MsgID']} from {m.get('SenderName')} ts={m.get('Timestamp')}", flush=True)
                dl = wacli("media", "download", "--chat", m["ChatJID"], "--id", m["MsgID"],
                           "--lock-wait", LOCK_WAIT, "--json")
                out = dl.stdout or ""
                if '"success":true' not in out.replace(" ", ""):
                    print(f"DOWNLOAD FAILED {(out or dl.stderr)[-200:].strip()}", flush=True)
                    seen.discard(m["MsgID"])  # retry next cycle
                    save_seen(seen)
                    continue
                print(f"DOWNLOAD ok in {time.time()-t0:.1f}s", flush=True)
                path = json.loads(out)["data"]["path"]
                eq = ["-af", EQ_FILTER] if EQ_FILTER else []
                os.makedirs(QUEUE_DIR, exist_ok=True)
                # ms timestamp prefix keeps the queue sorted oldest-first
                qwav = os.path.join(QUEUE_DIR, f"{int(time.time()*1000)}-{m['MsgID']}.wav")
                qtmp = qwav + ".part"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, *eq,
                                "-ar", "48000", "-ac", "1", "-f", "wav", qtmp],
                               check=True, timeout=120)
                # Persist exact reply routing before exposing the WAV.  The
                # button service never infers or falls back to another chat.
                qmeta = qwav + ".json"
                qmeta_tmp = qmeta + ".part"
                with open(qmeta_tmp, "w") as f:
                    json.dump({
                        "version": 1,
                        "chat": m["ChatJID"],
                        "msgid": m["MsgID"],
                        "sender_jid": m.get("SenderJID"),
                    }, f, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(qmeta_tmp, qmeta)
                os.replace(qtmp, qwav)  # WAV appears only after routing metadata
                try:
                    import wave as _w
                    with _w.open(qwav) as wf:
                        dur = wf.getnframes() / wf.getframerate()
                except Exception:
                    dur = None
                log_event(type="received", chat=m["ChatJID"], sender=m.get("SenderName"),
                          sender_jid=m.get("SenderJID"), msgid=m["MsgID"],
                          file=os.path.basename(qwav), dur=dur)
                print(f"QUEUED {os.path.basename(qwav)} total={time.time()-t0:.1f}s", flush=True)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
