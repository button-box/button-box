"""Keep speaker and setup receipts inside each test's temporary directory."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from messagebox import speaker, cloud_setup


def isolate_audio(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for module, name, value in ((speaker, "LOCK_FILE", root / "speaker.lock"),
                                (cloud_setup, "STATE_FILE", root / "guide.json")):
        replacement = patch.object(module, name, value)
        replacement.start()
        test.addCleanup(replacement.stop)
