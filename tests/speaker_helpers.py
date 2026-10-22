"""Keep speaker and setup receipts inside each test's temporary directory."""

import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

from messagebox import speaker, cloud_setup


def assert_padded_playback(test, played, source, *, lead_in_s=None):
    played, source = Path(played), Path(source)
    test.assertNotEqual(played, source)
    with wave.open(str(played), 'rb') as actual, wave.open(str(source), 'rb') as original:
        test.assertEqual(actual.getparams()[:3], original.getparams()[:3])
        lead = int(original.getframerate() * (speaker.LEAD_IN_S if lead_in_s is None else lead_in_s))
        silence = b'\x80' if original.getsampwidth() == 1 else b'\0'
        test.assertEqual(actual.getnframes(), original.getnframes() + lead)
        test.assertEqual(actual.readframes(lead),
                         silence * lead * original.getnchannels() * original.getsampwidth())
        test.assertEqual(actual.readframes(original.getnframes()), original.readframes(original.getnframes()))


def isolate_audio(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for module, name, value in ((speaker, "LOCK_FILE", root / "speaker.lock"),
                                (cloud_setup, "STATE_FILE", root / "guide.json")):
        replacement = patch.object(module, name, value)
        replacement.start()
        test.addCleanup(replacement.stop)
