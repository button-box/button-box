import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def test_prefers_capture_only_microphone_over_speaker_capture_input(self):
        # A USB speaker enumerated first behind a hub also exposes capture.
        self.add_capture(0, 0, "UACDemoV10")
        self.add_playback(0, 0, "UACDemoV10", usb=True)
        self.add_capture(1, 0, "UsbMic")

        self.assertEqual(
            audio_config.detect_microphone(self.sound_root),
            ("UsbMic", "plughw:CARD=UsbMic,DEV=0"),
        )
        self.assertEqual(
            audio_config.detect_speaker(self.sound_root),
            ("UACDemoV10", "plughw:CARD=UACDemoV10,DEV=0"),
        )

    def test_combined_audio_device_is_used_when_it_is_the_only_capture(self):
        self.add_capture(0, 0, "headset")
        self.add_playback(0, 0, "headset", usb=True)

        self.assertEqual(
            audio_config.detect_microphone(self.sound_root),
            ("headset", "plughw:CARD=headset,DEV=0"),
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

    def test_runtime_output_contains_only_detected_audio_settings(self):
        path = self.root / "run" / "audio.env"
        with (
            mock.patch.object(audio_config, "detect_microphone", return_value=("Mic", "plughw:CARD=Mic,DEV=0")),
            mock.patch.object(audio_config, "detect_speaker", return_value=("Speaker", "plughw:CARD=Speaker,DEV=0")),
        ):
            self.assertEqual(audio_config.main(["--runtime-output", str(path)]), 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(path.read_text().splitlines(), [
            "MSGBOX_MIC_DEV=plughw:CARD=Mic,DEV=0",
            "MSGBOX_SPK_DEV=plughw:CARD=Speaker,DEV=0",
            "MSGBOX_MIC_CARD=Mic",
            "MSGBOX_SPEAKER_CARD=Speaker",
        ])

    def test_failed_runtime_detection_preserves_previous_output(self):
        path = self.root / "audio.env"
        path.write_text("previous\n")
        with (
            mock.patch.object(audio_config, "detect_microphone", side_effect=audio_config.AudioConfigError("no microphone")),
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(audio_config.main(["--runtime-output", str(path)]), 1)
        self.assertEqual(path.read_text(), "previous\n")

    def test_atomic_output_failure_preserves_previous_file_and_cleans_stage(self):
        path = self.root / "audio.env"
        path.write_text("previous\n")
        with mock.patch.object(audio_config.os, "replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                audio_config.write_atomic(path, "new\n")
        self.assertEqual(path.read_text(), "previous\n")
        self.assertEqual(list(self.root.glob(".audio.env.*")), [])

    def test_audio_services_require_detector_and_load_override_last(self):
        for name in (
            "messagebox-button.service", "messagebox-nfc.service", "messagebox-dash.service",
            "onboarding/messagebox-onboarding-home.service",
            "onboarding/messagebox-onboarding-nfc.service",
            "onboarding/messagebox-onboarding-button.service",
        ):
            with self.subTest(service=name):
                unit = (ROOT / "systemd" / name).read_text()
                requires = next(line for line in unit.splitlines() if line.startswith("Requires="))
                self.assertIn("messagebox-audio-detect.service", requires)
                self.assertGreater(unit.index("EnvironmentFile=/run/messagebox-audio/audio.env"),
                                   unit.index("EnvironmentFile=/etc/messagebox/env"))
        installer = (ROOT / "scripts/setup.sh").read_text()
        self.assertIn('"$REPO_DIR/scripts/install/audio_config.py" /usr/lib/messagebox/audio_config.py', installer)
        self.assertIn("messagebox-dash messagebox-audio-detect; do", installer)


if __name__ == "__main__":
    unittest.main()
