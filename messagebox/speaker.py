"""One speaker reservation for all service users and both runtime modes."""

import fcntl
import subprocess
import tempfile
import threading
import time
import wave
from contextlib import ExitStack, contextmanager

from pathlib import Path
from messagebox.settings import _open_shared_lock

class SpeakerBusy(subprocess.SubprocessError):
    """The speaker stayed busy after a short retry."""


LOCK_FILE = Path("/run/messagebox-speaker/speaker.lock")
LEAD_IN_S = 0.3
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


@contextmanager
def _playback(args, *, lead_in_s=None):
    """Feed silence and the unchanged PCM through the same ALSA stream."""
    lead_in_s = LEAD_IN_S if lead_in_s is None else lead_in_s
    if not args or Path(args[0]).name != 'aplay' or not Path(args[-1]).is_file() or lead_in_s <= 0:
        yield args
        return
    try:
        source = wave.open(str(args[-1]), 'rb')
    except (EOFError, wave.Error):
        yield args
        return
    with source:
        params = source.getparams()
        if params.comptype != 'NONE' or params.sampwidth not in (1, 2, 3, 4):
            yield args
            return
        # Copy in small blocks; do not load a whole voice note into Python on
        # a 512 MB Pi. The private directory also works in the strict services.
        with tempfile.TemporaryDirectory(prefix='messagebox-lead-in-') as directory:
            path = Path(directory) / Path(args[-1]).name
            with wave.open(str(path), 'wb') as target:
                target.setparams(params)
                frames = int(params.framerate * lead_in_s)
                silence = b'\x80' if params.sampwidth == 1 else b'\0'
                target.writeframesraw(silence * frames * params.sampwidth * params.nchannels)
                while block := source.readframes(16384):
                    target.writeframesraw(block)
            yield [*args[:-1], str(path)]


def run(args, *, runner=None, lead_in_s=None, **kwargs):
    runner = subprocess.run if runner is None else runner
    with reservation(), _playback(args, lead_in_s=lead_in_s) as command:
        for attempt in range(2):
            try:
                return runner(command, **{**kwargs, "stderr": subprocess.PIPE})
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


def start_player(args, *, popen=None, cancel=None, wait=True, lead_in_s=None, **kwargs):
    """Keep the cross-process lock until the caller reaps this player."""
    context = ExitStack()
    try:
        context.enter_context(reservation(cancel=cancel, wait=wait))
        command = context.enter_context(_playback(args, lead_in_s=lead_in_s))
        if cancel is not None and cancel():
            raise SpeakerBusy("speaker playback cancelled")
        process = (subprocess.Popen if popen is None else popen)(command, **kwargs)
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
