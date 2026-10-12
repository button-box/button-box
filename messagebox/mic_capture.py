"""One microphone entry point, scoped to a physical button interaction.

This is an application invariant, not a sandbox against root or injected code.
"""

from contextlib import contextmanager
import subprocess
import threading
import time

from gpiozero import Button


class CaptureDenied(RuntimeError):
    pass


_active_permits = set()


class _PressPermit:
    def __init__(self):
        self.thread = threading.get_ident()
        self.active = True
        self.used = False


@contextmanager
def capture_session(button):
    # Check the actual GPIO switch before and after the debounce interval.
    if not isinstance(button, Button) or not button.is_pressed:
        raise CaptureDenied("physical button press required")
    time.sleep(0.06)
    if not button.is_pressed:
        raise CaptureDenied("physical button press required")
    permit = _PressPermit()
    _active_permits.add(permit)
    try:
        yield permit
    finally:
        permit.active = False
        _active_permits.discard(permit)


def require_permit(permit):
    if (not isinstance(permit, _PressPermit) or permit not in _active_permits
            or not permit.active or permit.used
            or permit.thread != threading.get_ident()):
        raise CaptureDenied("active physical button session required")


def open_microphone(permit, device, *, popen=subprocess.Popen):
    require_permit(permit)
    # Consume before starting the child, including when starting it fails.
    permit.used = True
    return popen(
        ["arecord", "-q", "-D", device, "-t", "raw", "-f", "S16_LE",
         "-r", "16000", "-c", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
