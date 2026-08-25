import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "install" / "audio_config.py"
SPEC = importlib.util.spec_from_file_location("audio_config", HELPER)
audio_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio_config)


class AudioConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sound_root = self.root / "class" / "sound"
        self.sound_root.mkdir(parents=True)

    def add_device(self, card, device, card_id, kind, *, usb=False):
        bus_path = self.root / "devices" / f"bus-{card}"
        interface = bus_path / f"{card}:1.0"
        card_path = interface / "sound" / f"card{card}"
        card_path.mkdir(parents=True, exist_ok=True)
        (card_path / "id").write_text(f"{card_id}\n", encoding="ascii")
        device_link = card_path / "device"
        if not device_link.exists():
            device_link.symlink_to(interface, target_is_directory=True)
        if usb:
            (bus_path / "idVendor").write_text("1234\n", encoding="ascii")
            (bus_path / "idProduct").write_text("5678\n", encoding="ascii")

        card_link = self.sound_root / f"card{card}"
        if not card_link.exists():
            card_link.symlink_to(card_path, target_is_directory=True)
        pcm_path = card_path / f"pcmC{card}D{device}{kind}"
        pcm_path.touch()
        (self.sound_root / pcm_path.name).symlink_to(pcm_path)

    def add_capture(self, card, device, card_id):
        self.add_device(card, device, card_id, "c")

    def add_playback(self, card, device, card_id, *, usb=False):
        self.add_device(card, device, card_id, "p", usb=usb)

    def test_returns_card_and_device_config_values(self):
        self.add_capture(4, 0, "microphone")

        self.assertEqual(
            audio_config.detect_microphone(self.sound_root),
            ("microphone", "plughw:CARD=microphone,DEV=0"),
        )

    def test_fallback_uses_lowest_numeric_card_and_device(self):
        self.add_capture(10, 0, "later")
        self.add_capture(2, 3, "first")
        self.add_capture(2, 1, "first")

        self.assertEqual(
            audio_config.detect_microphone(self.sound_root),
            ("first", "plughw:CARD=first,DEV=1"),
        )

    def test_no_capture_device_fails_clearly(self):
        with self.assertRaisesRegex(
            audio_config.AudioConfigError, "connect a microphone"
        ):
            audio_config.detect_microphone(self.sound_root)

    def test_speaker_uses_lowest_usb_playback_and_ignores_hdmi(self):
        self.add_playback(0, 0, "hdmi")
        self.add_playback(10, 0, "later", usb=True)
        self.add_playback(2, 1, "speaker", usb=True)

        self.assertEqual(
            audio_config.detect_speaker(self.sound_root),
            ("speaker", "plughw:CARD=speaker,DEV=1"),
        )

    def test_no_usb_playback_device_fails_clearly(self):
        self.add_playback(0, 0, "hdmi")

        with self.assertRaisesRegex(audio_config.AudioConfigError, "USB speaker"):
            audio_config.detect_speaker(self.sound_root)

    def test_renders_audio_settings_and_preserves_mixer_volume(self):
        template = (ROOT / "config" / "env.example").read_text(encoding="utf-8")

        rendered = audio_config.render_environment(
            template,
            "microphone",
            "plughw:CARD=microphone,DEV=0",
            "speaker",
            "plughw:CARD=speaker,DEV=1",
        )

        self.assertIn("MSGBOX_MIC_DEV=plughw:CARD=microphone,DEV=0\n", rendered)
        self.assertIn("MSGBOX_MIC_CARD=microphone\n", rendered)
        self.assertIn("MSGBOX_SPK_DEV=plughw:CARD=speaker,DEV=1\n", rendered)
        self.assertIn("MSGBOX_SPEAKER_CARD=speaker\n", rendered)
        self.assertIn("MSGBOX_SPEAKER_VOLUME=50%\n", rendered)

    def test_template_requires_each_audio_setting_once(self):
        with self.assertRaisesRegex(
            audio_config.AudioConfigError, "exactly once"
        ):
            audio_config.render_environment(
                "MSGBOX_MIC_DEV=one\nMSGBOX_MIC_DEV=two\n",
                "microphone",
                "plughw:CARD=microphone,DEV=0",
                "speaker",
                "plughw:CARD=speaker,DEV=0",
            )


if __name__ == "__main__":
    unittest.main()
