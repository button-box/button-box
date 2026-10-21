"""Durable Cloud setup guide. Receipts precede speech; audio is optional."""

import fcntl
import json
import logging
import subprocess
import uuid
from pathlib import Path

from messagebox.cloud_device import CLOUD_DIR, atomic_json, open_private_lock

STATE_FILE = CLOUD_DIR / "guided-setup.json"
LINES = {"scan": "setup-scan", "press": "setup-press", "names": "setup-names",
         "your_turn": "setup-your-turn", "await_listened": "setup-sent"}
STAGES = {*LINES, "waiting_note", "recording", "review", "await_listened", "done"}


class SetupGuide:
    def __init__(self, path=None):
        self.path = Path(STATE_FILE if path is None else path)

    def load(self):
        try:
            state = json.loads(self.path.read_text())
            if (not isinstance(state, dict) or not isinstance(state.get("stage"), str)
                    or state["stage"] not in STAGES or not isinstance(state.get("spoken"), list)
                    or any(not isinstance(value, str) for value in state["spoken"])
                    or any(key in state and not isinstance(state[key], str)
                           for key in ("scope", "local_id", "message_id", "recipient"))
                    or any(key in state and type(state[key]) is not bool
                           for key in ("greeting_spoken", "listened_started", "sent_ready"))):
                raise ValueError("invalid guide state")
            return state
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError):
            logging.getLogger(__name__).warning("Invalid setup guide state; guide disabled")
            return {"stage": "done", "spoken": []}

    def change(self, action, **fields):
        try:
            return self._change(action, **fields)
        except (OSError, ValueError):
            return None  # Optional speech never blocks the WhatsApp setup path.

    def _change(self, action, **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open_private_lock(self.path.with_suffix(".lock")) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            if action == "start":
                if state is not None:
                    return state
                state = {"stage": "scan", "spoken": []}
            elif state is None:
                return None
            elif action == "advance":
                before = fields.pop("before")
                if state["stage"] not in before:
                    return None
                state.update(fields)
            elif action == "stage":
                if fields["stage"] not in STAGES:
                    raise ValueError("unknown setup stage")
                state.update(fields)
            elif action == "spoken":
                if state["stage"] in state["spoken"]:
                    return None
                state["spoken"].append(state["stage"])
            elif action == "fields":
                state.update(fields)
            atomic_json(self.path, state)
            return state

    def advance(self, before, after, **fields):
        if self.load() is None:
            return None
        return self.change("advance", before=before, stage=after, **fields)

    def speak(self, play, *, quiet=False, replay=False):
        state = self.load()
        if not state or quiet or state["stage"] not in LINES:
            return False
        if state["stage"] == "await_listened" and not state.get("sent_ready"):
            return False
        if not replay and not self.change("spoken"):
            return False
        try:
            play(LINES[state["stage"]])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        return True

    def sent(self, local_id, scope, status, *, ready=False):
        state = self.load()
        if (state and state["stage"] == "await_listened"
                and state.get("local_id") == local_id and state.get("scope") == scope):
            self.change("fields", message_id=status["message_id"],
                        sent_ready=bool(state.get("sent_ready") or ready))

    def first_reply(self):
        state = self.load()
        return bool(state and state["stage"] == "your_turn")

    def listened(self, metadata):
        state = self.load()
        return bool(state and metadata and state["stage"] == "await_listened"
                    and state.get("message_id") == metadata.get("message_id")
                    and state.get("scope") == metadata.get("account_scope"))


class ReplyOutbox:
    """Bind the guide before the sender can see the approved job."""
    def __init__(self, store, guide):
        self.store = store
        self.guide = guide

    def approve(self, *args, **kwargs):
        message_id = uuid.uuid4().hex
        self.guide.advance({"recording", "review"}, "await_listened",
                           local_id=message_id, scope=kwargs.get("account_scope"))
        return self.store.approve(*args, message_id=message_id, **kwargs)
