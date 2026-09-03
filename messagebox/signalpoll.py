#!/usr/bin/env python3
"""Message Box incoming-voice-note poller for Signal.

Mirrors voicepoll.py's answering-machine model (nothing auto-plays; the
button's lamp signals waiting messages) but polls signal-cli-rest-api
instead of wacli. Queue dir and sidecar schema are shared with voicepoll.py
so the button service plays and routes both channels identically; each
queued item's sidecar carries ``channel`` so a reply goes back out on the
same channel it arrived on.
"""
import json
import os
import subprocess
import time

from messagebox.contacts import ContactError, ContactStore
from messagebox.providers import SignalProvider
from messagebox.runtime_paths import CONTACTS_FILE
from messagebox.runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
from messagebox.runtime_paths import STATE_DIR

POLL_S = float(os.environ.get("MSGBOX_SIGNAL_POLL_S", "3"))
QUEUE_DIR = str(DEFAULT_QUEUE_DIR)
EVENTS_FILE = str(STATE_DIR / "events.jsonl")
STATE_FILE = str(STATE_DIR / "signal-seen.json")

# Same EQ chain as voicepoll.py, kept independently overridable per channel.
EQ_FILTER = os.environ.get(
    "MSGBOX_EQ_FILTER", "highpass=f=150,treble=g=6:f=3000,loudnorm=I=-16:TP=-1.5"
)


def load_contact_authorizations(path=CONTACTS_FILE):
    """Return validated ``identifier -> receive_after`` rules for Signal contacts."""
    try:
        contacts = ContactStore(path).load()["contacts"]
    except ContactError:
        return {}
    return {
        jid: contact["receive_after"]
        for jid, contact in contacts.items()
        if contact.get("channel") == "signal"
    }


def message_is_authorized(envelope, authorizations):
    source = envelope_source(envelope)
    receive_after = authorizations.get(source)
    if receive_after is None:
        return False
    timestamp = envelope_timestamp(envelope)
    return timestamp is not None and timestamp >= receive_after


def envelope_source(envelope):
    """Return the chat identifier: a group ID if present, else the sender."""
    inner = envelope.get("envelope") or envelope
    data_message = inner.get("dataMessage") or {}
    group_info = data_message.get("groupInfo") or {}
    group_id = group_info.get("groupId")
    if isinstance(group_id, str) and group_id:
        return f"group.{group_id}"
    source = inner.get("sourceNumber") or inner.get("source")
    return source if isinstance(source, str) else None


def envelope_timestamp(envelope):
    inner = envelope.get("envelope") or envelope
    raw = inner.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    timestamp = float(raw)
    while abs(timestamp) >= 100_000_000_000:
        timestamp /= 1000
    return timestamp


def voice_attachment(envelope):
    inner = envelope.get("envelope") or envelope
    data_message = inner.get("dataMessage") or {}
    for attachment in data_message.get("attachments") or []:
        content_type = str(attachment.get("contentType") or "")
        if content_type.startswith("audio/"):
            return attachment
    return None


def log_event(**event):
    """Append an analytics event (best-effort; never breaks the pipeline)."""
    try:
        event["ts"] = time.time()
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        with open(EVENTS_FILE, "a") as handle:
            handle.write(json.dumps(event) + "\n")
    except Exception as exc:
        print(f"event log error: {exc}", flush=True)


def load_seen():
    try:
        with open(STATE_FILE) as handle:
            return set(json.load(handle))
    except (FileNotFoundError, ValueError):
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(sorted(seen), handle)
    os.replace(tmp, STATE_FILE)


def main():
    provider = SignalProvider()
    seen = load_seen()
    print(f"signal poller up: seen={len(seen)}", flush=True)
    while True:
        try:
            authorizations = load_contact_authorizations()
            for envelope in provider.receive():
                inner = envelope.get("envelope") or envelope
                message_id = str(inner.get("timestamp") or "")
                if not message_id or message_id in seen:
                    continue
                if not message_is_authorized(envelope, authorizations):
                    continue
                attachment = voice_attachment(envelope)
                if attachment is None:
                    continue
                seen.add(message_id)
                save_seen(seen)
                t0 = time.time()
                source = envelope_source(envelope)
                print(f"NEW {message_id} from {source}", flush=True)
                attachment_id = attachment.get("id")
                try:
                    raw = provider.fetch_attachment(str(attachment_id))
                except Exception as exc:
                    print(f"DOWNLOAD FAILED {exc}", flush=True)
                    seen.discard(message_id)  # retry next cycle
                    save_seen(seen)
                    continue
                os.makedirs(QUEUE_DIR, exist_ok=True)
                raw_path = os.path.join(QUEUE_DIR, f".signal-{message_id}.raw")
                with open(raw_path, "wb") as handle:
                    handle.write(raw)
                eq = ["-af", EQ_FILTER] if EQ_FILTER else []
                qwav = os.path.join(QUEUE_DIR, f"{int(time.time() * 1000)}-signal-{message_id}.wav")
                qtmp = qwav + ".part"
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path, *eq,
                         "-ar", "48000", "-ac", "1", "-f", "wav", qtmp],
                        check=True, timeout=120,
                    )
                finally:
                    try:
                        os.remove(raw_path)
                    except OSError:
                        pass
                qmeta = qwav + ".json"
                qmeta_tmp = qmeta + ".part"
                inner_sender = inner.get("sourceNumber") or inner.get("source")
                with open(qmeta_tmp, "w") as handle:
                    json.dump({
                        "version": 1,
                        "channel": "signal",
                        "chat": source,
                        "msgid": message_id,
                        "sender_jid": inner_sender,
                    }, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(qmeta_tmp, qmeta)
                os.replace(qtmp, qwav)  # WAV appears only after routing metadata
                log_event(type="received", channel="signal", chat=source,
                          sender_jid=inner_sender, msgid=message_id,
                          file=os.path.basename(qwav))
                print(f"QUEUED {os.path.basename(qwav)} total={time.time()-t0:.1f}s", flush=True)
        except Exception as exc:
            print(f"poll error: {exc}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
