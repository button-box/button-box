import array
import hashlib
import json
import sys
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VOICE_DIRS = [ROOT / 'sounds/voice', *sorted(p for p in (ROOT / 'sounds/voices').iterdir() if p.is_dir())]
SOURCE_DIR = ROOT / 'docs/sound-design/voices/PTh0NF64AaTAIDPGzj0O'


class VoiceLeadInTests(unittest.TestCase):
    def test_every_shipped_voice_has_at_least_100_ms_before_two_percent_of_peak(self):
        for directory in VOICE_DIRS:
            paths = sorted(directory.glob('*.wav'))
            self.assertTrue(paths, directory)
            for path in paths:
                with self.subTest(path=path.relative_to(ROOT)), wave.open(str(path)) as audio:
                    self.assertEqual((audio.getsampwidth(), audio.getnchannels()), (2, 1))
                    samples = array.array('h', audio.readframes(audio.getnframes()))
                    if sys.byteorder != 'little':
                        samples.byteswap()
                    peak = max(map(abs, samples))
                    self.assertGreater(peak, 0)
                    onset = next(i for i, value in enumerate(samples) if abs(value) * 50 > peak)
                    self.assertGreaterEqual(onset / audio.getframerate(), .1)

    def test_manifest_checksums_and_durations_match_every_voice_asset(self):
        def rows(value):
            if isinstance(value, dict):
                if 'file' in value and 'sha256' in value:
                    yield value
                for item in value.values():
                    yield from rows(item)
            elif isinstance(value, list):
                for item in value:
                    yield from rows(item)

        for directory in [*VOICE_DIRS, SOURCE_DIR]:
            manifest = json.loads((directory / 'manifest.json').read_text())
            assets = set(directory.glob('*.wav'))
            for row in rows(manifest):
                path = directory / row['file']
                with self.subTest(path=path.relative_to(ROOT)), wave.open(str(path)) as audio:
                    self.assertEqual(row['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                    # Older manifests round durations to hundredths of a second.
                    self.assertAlmostEqual(row['seconds'], audio.getnframes() / audio.getframerate(), delta=.005)
                    assets.discard(path)
            self.assertFalse(assets, assets)

    def test_retained_sending_source_matches_shipped_clip(self):
        self.assertEqual((SOURCE_DIR / 'voice-sending.wav').read_bytes(),
                         (ROOT / 'sounds/voices/buttonbox/voice-sending.wav').read_bytes())
