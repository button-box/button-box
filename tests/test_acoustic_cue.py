import importlib.util
import math
import random
import struct
import tempfile
import unittest
import wave
from pathlib import Path

spec = importlib.util.spec_from_file_location("acoustic_cue", Path(__file__).resolve().parents[1] / "scripts/dev/check-acoustic-cue.py")
acoustic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acoustic)


class AcousticCueTests(unittest.TestCase):
    def capture(self, tones, *, amplitude=5000):
        random_source = random.Random(4)
        rate = 16000
        samples = []
        for i in range(rate * 6):
            t = i / rate
            value = random_source.randint(-40, 40)
            for start, duration, hz in tones:
                if start <= t < start + duration:
                    value += amplitude * math.sin(2 * math.pi * hz * t)
            samples.append(max(-32768, min(32767, round(value))))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.wav"
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            return acoustic.measure(path, frequency=880, minimum=0.32, maximum=0.48)

    def test_detects_expected_tone_above_room_baseline(self):
        result = self.capture([(3.4, 0.4, 880)])
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["onset_s"], 3.4, places=2)
        self.assertAlmostEqual(result["duration_s"], 0.4, places=2)

    def test_rejects_silence_wrong_pitch_short_or_duplicate_cues_and_clipping(self):
        for tones in ([], [(3.4, 0.4, 400)], [(3.4, 0.07, 880)], [(3.4, 0.4, 880), (4.4, 0.4, 880)], [(0.4, 0.4, 880), (3.4, 0.4, 880)]):
            with self.subTest(tones=tones):
                self.assertFalse(self.capture(tones)["passed"])
        self.assertFalse(self.capture([(3.4, 0.4, 880)], amplitude=50000)["passed"])
