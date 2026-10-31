"""Serialize bounded event appends across runtime processes."""

import fcntl
import json
import os
import time
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP = 2


def append_event(path, event, *, max_bytes=MAX_BYTES):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(event, sort_keys=True) + '\n').encode('utf-8')
    if len(payload) > max_bytes:
        raise ValueError('event exceeds log limit')
    # A sidecar lock survives rename so every writer rotates the same stream.
    descriptor = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size + len(payload) > max_bytes:
            for index in range(KEEP, 0, -1):
                source = path if index == 1 else Path(str(path) + f'.{index - 1}')
                if source.exists():
                    source.replace(str(path) + f'.{index}')
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'ab') as handle:
            handle.write(payload)


class UnavailableEvents:
    """Log transitions and one reminder per five minutes of continued failure."""

    def __init__(self, interval=300, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.last = {}

    def unavailable(self, key):
        now = self.clock()
        previous = self.last.get(key)
        if previous is not None and now - previous < self.interval:
            return False
        self.last[key] = now
        return True

    def available(self, key):
        self.last.pop(key, None)
