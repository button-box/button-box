"""Cloud maintenance reservation, shared by interactions and the root updater."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

def transport_mode():
    return os.environ.get("MSGBOX_TRANSPORT", "wacli")


LOCK = Path('/run/messagebox-update/maintenance.lock')
INHIBIT = Path('/var/lib/button-box-update/install-inhibit')


@contextmanager
def interaction():
    if transport_mode() != 'cloud' or not LOCK.exists():
        yield True
        return
    descriptor = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            acquired = not INHIBIT.exists()
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(descriptor)
