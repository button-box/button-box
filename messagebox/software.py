"""Validated installed release identity, cached until the service restarts."""

from __future__ import annotations

import json
import os
import re
import stat
from functools import cache
from pathlib import Path


RELEASE_FILE = Path("/opt/messagebox/release.json")
_VERSION = re.compile(r"[0-9A-Za-z.+_-]{1,64}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")


@cache
def installed_release() -> tuple[str, str] | None:
    # The updater writes this record before restarting services. Do not inspect
    # a checkout or VERSION file, which may describe a different release.
    try:
        descriptor = os.open(RELEASE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            payload = source.read(4097)
        if len(payload) > 4096:
            return None
        document = json.loads(payload)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    version, commit = document.get("version"), document.get("commit")
    if (not isinstance(version, str) or not _VERSION.fullmatch(version)
            or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)):
        return None
    return version, commit


def software_fields() -> dict[str, str]:
    release = installed_release()
    if release is None:
        return {}
    version, commit = release
    return {"software_version": version, "software_commit": commit}


def software_footer() -> bytes:
    release = installed_release()
    if release is None:
        return b""
    version, commit = release
    return f'<p class="software-version">Software {version} ({commit[:7]})</p>'.encode("ascii")
