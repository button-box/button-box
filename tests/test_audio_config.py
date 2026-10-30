import importlib.util
import io
import os
import stat
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

    def add_capture(self, card, device, card_id, *, usb=False):
        self.add_device(card, device, card_id, "c", usb=usb)

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

    def test_dedicated_usb_microphone_wins_in_either_card_order(self):
        for mic, speaker in ((2, 1), (1, 2)):
            with self.subTest(mic=mic, speaker=speaker):
                # Give each order its own fake /sys/class/sound tree.
                self.sound_root = self.root / str(mic) / 'class/sound'
                self.sound_root.mkdir(parents=True)
                self.add_capture(0, 0, audio_config.I2S_CARD)
                self.add_capture(speaker, 0, 'UACDemoV10', usb=True)
                self.add_playback(speaker, 1, 'UACDemoV10', usb=True)
                self.add_capture(mic, 0, 'UACDemoV10_1', usb=True)
                self.assertEqual(audio_config.detect_microphone(self.sound_root),
                    ('UACDemoV10_1', 'plughw:CARD=UACDemoV10_1,DEV=0'))
                self.assertEqual(audio_config.detect_speaker(self.sound_root),
                    ('UACDemoV10', 'plughw:CARD=UACDemoV10,DEV=1'))

    def test_single_combined_usb_device_remains_a_valid_microphone(self):
        self.add_capture(3, 0, 'combined', usb=True)
        self.add_playback(3, 0, 'combined', usb=True)
        self.assertEqual(audio_config.detect_microphone(self.sound_root),
                         ('combined', 'plughw:CARD=combined,DEV=0'))

    def test_non_usb_capture_only_card_does_not_override_existing_fallback(self):
        self.add_capture(1, 0, 'combined', usb=True)
        self.add_playback(1, 0, 'combined', usb=True)
        self.add_capture(2, 0, 'onboard')
        self.assertEqual(audio_config.detect_microphone(self.sound_root),
                         ('combined', 'plughw:CARD=combined,DEV=0'))

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

    def test_usb_box_never_selects_the_i2s_card(self):
        self.add_capture(0, 0, audio_config.I2S_CARD)
        self.add_capture(2, 0, "mic")
        self.assertEqual(audio_config.detect_microphone(self.sound_root),
                         ("mic", "plughw:CARD=mic,DEV=0"))

        (self.sound_root / "pcmC2D0c").unlink()
        with self.assertRaisesRegex(audio_config.AudioConfigError, "connect a microphone"):
            audio_config.detect_microphone(self.sound_root)

    def test_i2s_box_uses_only_the_i2s_card(self):
        self.add_capture(0, 0, "mic")
        with self.assertRaisesRegex(audio_config.AudioConfigError, "I2S microphone not detected"):
            audio_config.detect_microphone(self.sound_root, kind="i2s")

        self.add_capture(3, 0, audio_config.I2S_CARD)
        self.assertEqual(
            audio_config.detect_microphone(self.sound_root, kind="i2s"),
            ("sndrpigooglevoi", "plughw:CARD=sndrpigooglevoi,DEV=0"),
        )

    def test_mic_type_defaults_to_usb_and_rejects_unknown_values(self):
        self.assertEqual(audio_config.mic_type({}), "usb")
        self.assertEqual(audio_config.mic_type({"MSGBOX_MIC_TYPE": ""}), "usb")
        self.assertEqual(audio_config.mic_type({"MSGBOX_MIC_TYPE": "i2s"}), "i2s")
        with self.assertRaisesRegex(audio_config.AudioConfigError, "usb, i2s"):
            audio_config.mic_type({"MSGBOX_MIC_TYPE": "gpio"})

    def test_existing_env_without_mic_type_stays_usb(self):
        env = self.root / "env"
        env.write_text("MSGBOX_MIC_DEV=plughw:CARD=mic,DEV=0\n# MSGBOX_MIC_TYPE=i2s\n")
        self.assertEqual(audio_config.configured_mic_type(env), "usb")
        self.assertEqual(audio_config.configured_mic_type(self.root / "missing-env"), "usb")

    def test_i2s_recording_requires_installed_alsa_config(self):
        env = self.root / "env"
        env.write_text("MSGBOX_MIC_TYPE=i2s\n")
        self.assertEqual(audio_config.configured_mic_type(env), "i2s")
        with mock.patch.object(audio_config, "configured_mic_type", return_value="i2s"), \
             mock.patch.object(audio_config, "detect_microphone", return_value=(audio_config.I2S_CARD, audio_config.I2S_DEVICE)), \
             mock.patch.object(audio_config, "I2S_ALSA_CONFIG", self.root / "missing.conf"), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(audio_config.main(["--runtime-output", str(self.root / "audio.env")]), 1)
        self.assertIn("ALSA configuration is missing", error.getvalue())

    def test_i2s_runtime_output_adds_processed_pcm_and_keeps_raw_device(self):
        rendered = audio_config.render_runtime_environment(
            audio_config.I2S_CARD, audio_config.I2S_DEVICE, "Speaker",
            "plughw:CARD=Speaker,DEV=0", "i2s",
        )
        self.assertEqual(rendered.splitlines(), [
            # The fixed updater health check accepts only plughw:CARD=...,DEV=...
            "MSGBOX_MIC_DEV=plughw:CARD=sndrpigooglevoi,DEV=0",
            "MSGBOX_SPK_DEV=plughw:CARD=Speaker,DEV=0",
            "MSGBOX_MIC_CARD=sndrpigooglevoi",
            "MSGBOX_SPEAKER_CARD=Speaker",
            "MSGBOX_MIC_PCM=buttonbox_mic",
        ])

    def test_boot_detection_writes_i2s_pcm_and_removes_it_when_switched_to_usb(self):
        output = self.root / "audio.env"
        alsa = self.root / "mic.conf"
        alsa.write_text(audio_config.render_i2s_alsa_config())
        for kind, card in (("i2s", audio_config.I2S_CARD), ("usb", "mic")):
            with self.subTest(kind=kind), \
                 mock.patch.object(audio_config, "I2S_ALSA_CONFIG", alsa), \
                 mock.patch.object(audio_config, "configured_mic_type", return_value=kind), \
                 mock.patch.object(audio_config, "detect_microphone",
                                   return_value=(card, f"plughw:CARD={card},DEV=0")) as detect, \
                 mock.patch.object(audio_config, "detect_speaker",
                                   return_value=("Speaker", "plughw:CARD=Speaker,DEV=0")):
                self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 0)
                detect.assert_called_once_with(kind=kind)
                self.assertIn(f"MSGBOX_MIC_DEV=plughw:CARD={card},DEV=0\n", output.read_text())
                self.assertEqual("MSGBOX_MIC_PCM=buttonbox_mic\n" in output.read_text(), kind == "i2s")

    def test_i2s_alsa_config_takes_left_channel_with_bounded_gain(self):
        config = audio_config.render_i2s_alsa_config()
        self.assertIn("pcm.buttonbox_mic {", config)
        self.assertIn("card sndrpigooglevoi", config)
        self.assertIn("format S32_LE", config)
        self.assertIn("slave.rate 48000", config)
        self.assertIn("ttable.0.0 31.6228", config)
        self.assertNotIn("ttable.1", config)
        self.assertIn("ttable.0.0 10.0000", audio_config.render_i2s_alsa_config(20))
        for gain in (-1, 41):
            with self.subTest(gain=gain), self.assertRaisesRegex(audio_config.AudioConfigError, "0 to 40 dB"):
                audio_config.render_i2s_alsa_config(gain)

    def make_i2s_box(self, pins="MSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D27\n"):
        env = self.root / "env"
        env.write_text("MSGBOX_MIC_TYPE=usb\nMSGBOX_BUTTON_PIN=17\n" + pins)
        env.chmod(0o640)
        boot = self.root / "config.txt"
        boot.write_text("dtparam=audio=on\n[cm4]\notg_mode=1")
        alsa = self.root / "alsa" / "60-button-box-i2s-mic.conf"
        capability = self.root / "mic_capture.py"
        capability.write_bytes((ROOT / "messagebox/mic_capture.py").read_bytes())
        return {"env_path": env, "boot_config": boot, "alsa_config": alsa,
                "guard_path": self.root / "units" / "zz-i2s-microphone.conf",
                "capability_file": capability}

    def test_set_mic_type_i2s_is_idempotent_and_keeps_env_lines_and_mode(self):
        paths = self.make_i2s_box()
        self.assertTrue(audio_config.set_mic_type("i2s", 33, **paths))
        boot = paths["boot_config"].read_text()
        self.assertTrue(boot.startswith("dtparam=audio=on\n[cm4]\notg_mode=1\n"))
        # [all] keeps the overlay out of the preceding model-specific section.
        self.assertIn("\n[all]\ndtparam=i2s=on\ndtoverlay=googlevoicehat-soundcard\n", boot)
        self.assertIn("ttable.0.0 44.6684", paths["alsa_config"].read_text())
        env = paths["env_path"]
        self.assertEqual(env.read_text(), "MSGBOX_MIC_TYPE=i2s\nMSGBOX_BUTTON_PIN=17\n"
                         "MSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D27\n")
        self.assertEqual(env.stat().st_mode & 0o777, 0o640)

        self.assertFalse(audio_config.set_mic_type("i2s", **paths))
        self.assertEqual(paths["boot_config"].read_text(), boot)
        self.assertIn("ttable.0.0 31.6228", paths["alsa_config"].read_text())

    def test_set_mic_type_i2s_keeps_an_overlay_enabled_by_hand(self):
        paths = self.make_i2s_box()
        manual = "dtparam=i2s=on\ndtoverlay=googlevoicehat-soundcard\n"
        paths["boot_config"].write_text(manual)
        self.assertFalse(audio_config.set_mic_type("i2s", **paths))
        self.assertEqual(paths["boot_config"].read_text(), manual)

    def test_set_mic_type_i2s_refuses_conflicting_pins_without_changes(self):
        # Missing NFC pins mean the code defaults D20/D16, which I2S uses.
        paths = self.make_i2s_box(pins="")
        env, boot = paths["env_path"].read_text(), paths["boot_config"].read_text()
        with self.assertRaisesRegex(audio_config.AudioConfigError,
                                    "MSGBOX_NFC_RESET_PIN=D20, MSGBOX_NFC_REQUEST_PIN=D16"):
            audio_config.set_mic_type("i2s", **paths)
        self.assertEqual(paths["env_path"].read_text(), env)
        self.assertEqual(paths["boot_config"].read_text(), boot)
        self.assertFalse(paths["alsa_config"].exists())

    def test_each_reserved_gpio_is_refused_for_every_box_pin(self):
        for key in audio_config.PIN_SETTINGS:
            for pin in (16, 18, 19, 20, 21):
                for prefix in ("", "D", "GPIO"):
                    with self.subTest(key=key, pin=pin, prefix=prefix):
                        settings = {"MSGBOX_NFC_RESET_PIN": "D4",
                                    "MSGBOX_NFC_REQUEST_PIN": "D27", key: f"{prefix}{pin}"}
                        self.assertEqual(audio_config.i2s_pin_conflicts(settings),
                                         [f"{key}={prefix}{pin}"])

    def test_set_mic_type_usb_reverses_the_i2s_switch(self):
        paths = self.make_i2s_box()
        original_boot = paths["boot_config"].read_text()
        audio_config.set_mic_type("i2s", **paths)
        self.assertTrue(audio_config.set_mic_type("usb", **paths))
        self.assertEqual(paths["boot_config"].read_text(), original_boot + "\n[all]\n")
        self.assertFalse(paths["alsa_config"].exists())
        self.assertIn("MSGBOX_MIC_TYPE=usb\n", paths["env_path"].read_text())
        self.assertFalse(audio_config.set_mic_type("usb", **paths))

    def test_setup_template_for_i2s_does_not_need_the_card_before_reboot(self):
        output = io.StringIO()
        with (
            mock.patch.object(audio_config, "detect_microphone", side_effect=AssertionError),
            mock.patch.object(audio_config, "detect_speaker", return_value=("Speaker", "plughw:CARD=Speaker,DEV=0")),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(audio_config.main(
                ["--mic-type", "i2s", str(ROOT / "config" / "env.example")]), 0)
        self.assertIn("MSGBOX_MIC_TYPE=i2s\n", output.getvalue())
        self.assertIn("MSGBOX_MIC_DEV=plughw:CARD=sndrpigooglevoi,DEV=0\n", output.getvalue())

    def test_prepared_i2s_install_has_safe_explicit_pins_but_does_not_activate_type(self):
        rendered = audio_config.render_environment(
            (ROOT / "config/env.example").read_text(), audio_config.I2S_CARD, audio_config.I2S_DEVICE,
            "speaker", "plughw:CARD=speaker,DEV=0", "i2s", prepare_install=True)
        for value in ("MSGBOX_MIC_TYPE=usb", "MSGBOX_NFC_RESET_PIN=D4", "MSGBOX_NFC_REQUEST_PIN=D27",
                      "MSGBOX_BUTTON_PIN=17", "MSGBOX_LED_PIN=26"):
            self.assertIn(value + "\n", rendered)

    def test_conditional_duplicate_include_and_malformed_boot_configs_are_refused_before_writes(self):
        cases = ("[pi4]\ndtoverlay=googlevoicehat-soundcard\n",
                 "[none]\ndtoverlay=googlevoicehat-soundcard\n",
                 "[all]\ndtoverlay=googlevoicehat-soundcard\n[all]\ndtoverlay=googlevoicehat-soundcard\n",
                 "include custom.txt\n", "include\tcustom.txt\n",
                 "[all]\ndtoverlay=googlevoicehat-soundcard\n",
                 "[all]\ndtoverlay=googlevoicehat-soundcard,unknown=1\n",
                 "dtparam=i2s=off\ndtoverlay=googlevoicehat-soundcard\n",
                 audio_config.BOOT_BLOCK_START + "\n",
                 audio_config.BOOT_BLOCK_END + "\n",
                 audio_config.BOOT_BLOCK_START + "\n[pi4]\n" + audio_config.BOOT_BLOCK_END + "\n")
        for content in cases:
            with self.subTest(content=content):
                paths = self.make_i2s_box()
                paths["boot_config"].write_text(content)
                with mock.patch.object(audio_config, "write_atomic", side_effect=AssertionError("write before validation")), \
                     self.assertRaises(audio_config.AudioConfigError):
                    audio_config.set_mic_type("i2s", **paths)
                self.assertIn("MSGBOX_MIC_TYPE=usb", paths["env_path"].read_text())

    def test_disabling_i2s_keeps_later_settings_in_global_scope(self):
        original = "[cm4]\notg_mode=1\n"
        configured, _ = audio_config.boot_config_with_i2s(original)
        configured += "dtparam=i2c_arm=on\n[pi4]\narm_boost=1\n"
        restored, changed = audio_config.boot_config_without_i2s(configured)
        self.assertTrue(changed)
        self.assertEqual(restored, original + "[all]\ndtparam=i2c_arm=on\n[pi4]\narm_boost=1\n")

    def test_missing_i2s_card_falls_back_to_usb_and_logs_without_selecting_raw_i2s(self):
        output = self.root / "audio.env"
        output.write_text("MSGBOX_MIC_PCM=buttonbox_mic\n")
        self.add_capture(2, 0, "mic", usb=True)
        detect = audio_config.detect_microphone
        with mock.patch.object(audio_config, "configured_mic_type", return_value="i2s"), \
             mock.patch.object(audio_config, "detect_microphone", side_effect=lambda **kw: detect(self.sound_root, **kw)), \
             mock.patch.object(audio_config, "detect_speaker", return_value=("speaker", "plughw:CARD=speaker,DEV=0")), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 0)
        self.assertIn("I2S card unavailable; trying USB", error.getvalue())
        self.assertIn("MSGBOX_MIC_DEV=plughw:CARD=mic,DEV=0", output.read_text())
        self.assertNotIn("MSGBOX_MIC_PCM", output.read_text())

    def test_power_loss_at_each_write_sync_replace_and_delete_keeps_prerequisites_and_allows_retry(self):
        from test_controller_slots import DurableView

        class PowerLoss(BaseException):
            pass

        def trial(kind, cut=None):
            with tempfile.TemporaryDirectory() as directory:
                previous = self.root
                self.root = Path(directory)
                try:
                    paths = self.make_i2s_box()
                    if kind == "usb":
                        audio_config.set_mic_type("i2s", **paths)
                    disk = DurableView(self.root)
                    events = []
                    crashed = [False]
                    real_fsync, real_replace, real_unlink = os.fsync, os.replace, os.unlink

                    def step(event):
                        events.append(event)
                        if not crashed[0] and len(events) - 1 == cut:
                            crashed[0] = True
                            raise PowerLoss()

                    def fsync(fd):
                        event = "directory sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file sync"
                        step("before " + event)
                        real_fsync(fd)
                        disk.synced(fd)
                        step("after " + event)

                    def replace(source, target):
                        step("before replace")
                        real_replace(source, target)
                        step("after replace")

                    def unlink(path, *args, **kwargs):
                        step("before delete")
                        real_unlink(path, *args, **kwargs)
                        step("after delete")

                    with mock.patch.object(audio_config.os, "fsync", side_effect=fsync), \
                         mock.patch.object(audio_config.os, "replace", side_effect=replace), \
                         mock.patch.object(audio_config.os, "unlink", side_effect=unlink):
                        try:
                            audio_config.set_mic_type(kind, **paths)
                        except PowerLoss:
                            pass
                    disk.power_loss()
                    if "dtoverlay=googlevoicehat-soundcard" in paths["boot_config"].read_text():
                        self.assertEqual(paths["guard_path"].read_text(), audio_config.render_i2s_guard())
                    if audio_config.mic_type(audio_config.read_settings(paths["env_path"])) == "i2s":
                        self.assertIn("\n[all]\ndtparam=i2s=on\ndtoverlay=googlevoicehat-soundcard\n",
                                      paths["boot_config"].read_text())
                        self.assertEqual(paths["alsa_config"].read_text(), audio_config.render_i2s_alsa_config())
                        self.assertEqual(paths["guard_path"].read_text(), audio_config.render_i2s_guard())
                    audio_config.set_mic_type(kind, **paths)
                    self.assertEqual(audio_config.mic_type(audio_config.read_settings(paths["env_path"])), kind)
                    self.assertEqual(paths["alsa_config"].exists(), kind == "i2s")
                    return events
                finally:
                    self.root = previous

        for kind in ("i2s", "usb"):
            events = trial(kind)
            for cut, event in enumerate(events):
                with self.subTest(kind=kind, boundary=cut, event=event):
                    trial(kind, cut)

    def test_unsynced_manual_boot_and_env_edits_are_durable_even_when_helper_changes_no_lines(self):
        from test_controller_slots import DurableView

        paths = self.make_i2s_box()
        disk = DurableView(self.root)
        manual = "[all]\ndtparam=i2s=on\ndtoverlay=googlevoicehat-soundcard\n"
        paths["boot_config"].write_text(manual)
        paths["env_path"].write_text(paths["env_path"].read_text().replace("=usb", "=i2s"))
        real_fsync = os.fsync
        def sync(fd):
            real_fsync(fd)
            disk.synced(fd)
        with mock.patch.object(audio_config.os, "fsync", side_effect=sync):
            self.assertFalse(audio_config.set_mic_type("i2s", **paths))
        disk.power_loss()
        self.assertEqual(paths["boot_config"].read_text(), manual)
        self.assertEqual(audio_config.mic_type(audio_config.read_settings(paths["env_path"])), "i2s")
        self.assertTrue(paths["alsa_config"].is_file())
        self.assertTrue(paths["guard_path"].is_file())

    def test_poller_loads_optional_detected_audio_after_canonical_environment(self):
        unit = (ROOT / "systemd" / "messagebox-poller.service").read_text(encoding="utf-8")
        lines = unit.splitlines()
        environments = [line for line in lines if line.startswith("EnvironmentFile=")]
        self.assertEqual(environments, [
            "EnvironmentFile=/etc/messagebox/env",
            "EnvironmentFile=-/run/messagebox-audio/audio.env",
        ])
        after = next(line.removeprefix("After=").split()
                     for line in lines if line.startswith("After="))
        self.assertIn("messagebox-audio-detect.service", after)
        requires = {dependency for line in lines if line.startswith("Requires=")
                    for dependency in line.removeprefix("Requires=").split()}
        self.assertNotIn("messagebox-audio-detect.service", requires)

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
