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
                self.assertAlmostEqual(source.getnframes() / source.getframerate(), 1.25)
                raw = source.readframes(source.getnframes())
            values = struct.unpack(f"<{len(raw) // 2}h", raw)
            self.assertLess(max(abs(v) for v in values), 18000)
            self.assertGreater(math.sqrt(sum(v * v for v in values) / len(values)), 2500)
            self.assertLess(abs(values[0]), 10)
            self.assertLess(abs(values[-1]), 10)
