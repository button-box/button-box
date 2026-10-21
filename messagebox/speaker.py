"""One speaker reservation for all service users and both runtime modes."""

import fcntl
import subprocess
import threading
import time
from contextlib import contextmanager

from pathlib import Path
from messagebox.settings import _open_shared_lock

class SpeakerBusy(subprocess.SubprocessError):
    """The speaker stayed busy after a short retry."""


LOCK_FILE = Path("/run/messagebox-speaker/speaker.lock")
_capture = threading.local()


@contextmanager
def reservation(*, cancel=None, wait=True):
    if cancel is not None and cancel():
        raise SpeakerBusy("speaker playback cancelled")
    if getattr(_capture, "active", False):
        yield
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _open_shared_lock(LOCK_FILE) as lock:
        if cancel is None and wait:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            while True:
                if cancel is not None and cancel():
                    raise SpeakerBusy("speaker playback cancelled")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait:
                        raise SpeakerBusy("speaker unavailable") from None
                    time.sleep(0.02)
        try:
            if cancel is not None and cancel():
                raise SpeakerBusy("speaker playback cancelled")
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def busy(error):
    text = str(error or "").lower()
    return any(word in text for word in ("busy", "cannot set freq", "set_params"))


def run(args, *, runner=None, **kwargs):
    runner = subprocess.run if runner is None else runner
    with reservation():
        for attempt in range(2):
            try:
                return runner(args, **{**kwargs, "stderr": subprocess.PIPE})
            except subprocess.CalledProcessError as exc:
                if not busy(exc.stderr):
                    raise
                if attempt:
                    raise SpeakerBusy("speaker unavailable") from None
                time.sleep(0.15)


class SpeakerPlayer:
    def __init__(self, process, context):
        self.process = process
        self.context = context
        self.speaker_released = threading.Event()
        self.release_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.process, name)

    def release(self):
        with self.release_lock:
            if not self.speaker_released.is_set():
                self.context.__exit__(None, None, None)
                self.speaker_released.set()

    def poll(self):
        code = self.process.poll()
        if code is not None:
            self.release()
        return code

    def wait(self, *args, **kwargs):
        code = self.process.wait(*args, **kwargs)
        self.release()
        return code

    def communicate(self, *args, **kwargs):
        result = self.process.communicate(*args, **kwargs)
        self.release()
        return result


def start_player(args, *, popen=None, cancel=None, wait=True, **kwargs):
    """Keep the cross-process lock until the caller reaps this player."""
    context = reservation(cancel=cancel, wait=wait)
    context.__enter__()
    try:
        process = (subprocess.Popen if popen is None else popen)(args, **kwargs)
    except BaseException:
        context.__exit__(None, None, None)
        raise
    return SpeakerPlayer(process, context)


def is_busy():
    if not LOCK_FILE.exists():
        return False
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _open_shared_lock(LOCK_FILE) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


@contextmanager
def capture_owner():
    """Keep unrelated service audio out of capture; allow its own reminder."""
    with reservation():
        _capture.active = True
        try:
            yield
        finally:
            _capture.active = False
