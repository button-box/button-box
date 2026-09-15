import importlib.util
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path


class SendSwooshAssetTests(unittest.TestCase):
    def test_asset_is_reproducible_long_and_not_clipped(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("swoosh", root / "scripts/dev/generate-send-swoosh.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "swoosh.wav"
            module.render(generated)
            self.assertEqual(generated.read_bytes(), (root / "sounds/feedback/sent-swoosh.wav").read_bytes())
            with wave.open(str(generated), "rb") as source:
                self.assertEqual(source.getnchannels(), 1)
                self.assertEqual(source.getsampwidth(), 2)
                self.assertEqual(source.getframerate(), 48000)
                self.assertAlmostEqual(source.getnframes() / source.getframerate(), 3.0)
                raw = source.readframes(source.getnframes())
            values = struct.unpack(f"<{len(raw) // 2}h", raw)
            self.assertLess(max(abs(v) for v in values), 18000)
            self.assertGreater(math.sqrt(sum(v * v for v in values) / len(values)), 2500)
            self.assertLess(abs(values[0]), 10)
            self.assertLess(abs(values[-1]), 10)
            # A three-second file must not just pad the old short cue with silence.
            for second in range(3):
                window = values[second * 48000:(second + 1) * 48000]
                self.assertGreater(math.sqrt(sum(v * v for v in window) / len(window)), 1200)
            tail = values[int(2.7 * 48000):int(2.9 * 48000)]
            self.assertGreater(math.sqrt(sum(v * v for v in tail) / len(tail)), 100)
