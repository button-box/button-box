"""Read the operator-assigned, non-secret inventory identity (never a hostname)."""

import os
import re
import stat
from pathlib import Path


BOX_ID_PATH = Path("/etc/messagebox-box-id")
BOX_COLOR_PATH = Path("/etc/messagebox-box-color")
BOX_COLORS = ("yellow", "pink-red", "green", "blue", "white", "black", "beige")


def read_box_id(path=BOX_ID_PATH):
    """Return only a valid BOX-number, or None; do not expose file contents/errors."""
    try:
        # Avoid following links or blocking on an accidentally installed FIFO.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            data = source.read(33)
            if len(data) > 32:
                return None
            value = data.decode("ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if re.fullmatch(r"BOX-[1-9][0-9]{0,8}", value) else None


def read_box_color(path=BOX_COLOR_PATH):
    """Return an allowed shell color, defaulting safely without exposing contents/errors."""
    try:
        # Match the identity reader: links and non-regular files are never read.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return "yellow"
            data = source.read(33)
            if len(data) > 32:
                return "yellow"
            value = data.decode("ascii").strip()
    except (OSError, UnicodeError):
        return "yellow"
    return value if value in BOX_COLORS else "yellow"
