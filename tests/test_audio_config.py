import contextlib
import errno
import importlib.util
import io
import os
import stat
import subprocess
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
                 mock.patch.object(audio_config, "activate_i2s", return_value=(audio_config.I2S_CARD, audio_config.I2S_DEVICE)), \
                 mock.patch.object(audio_config, "configured_mic_type", return_value=kind), \
                 mock.patch.object(audio_config, "detect_microphone",
                                   return_value=(card, f"plughw:CARD={card},DEV=0")) as detect, \
                 mock.patch.object(audio_config, "detect_speaker",
                                   return_value=("Speaker", "plughw:CARD=Speaker,DEV=0")):
                self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 0)
                if kind == "usb":
                    detect.assert_called_once_with(kind=kind)
                else:
                    detect.assert_not_called()
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
        boot = self.boot_config = self.root / "config.txt"
        boot.write_text("dtparam=audio=on\n[cm4]\notg_mode=1")
        alsa = self.root / "alsa" / "60-button-box-i2s-mic.conf"
        capability = self.root / "mic_capture.py"
        capability.write_bytes((ROOT / "messagebox/mic_capture.py").read_bytes())
        return {"env_path": env, "alsa_config": alsa,
                "guard_path": self.root / "units" / "zz-i2s-microphone.conf",
                "capability_file": capability}

    def test_set_mic_type_i2s_is_idempotent_and_keeps_env_lines_and_mode(self):
        paths = self.make_i2s_box()
        audio_config.set_mic_type("i2s", 33, **paths)
        boot = self.boot_config.read_text()
        self.assertTrue(boot.startswith("dtparam=audio=on\n[cm4]\notg_mode=1"))
        self.assertEqual(boot, "dtparam=audio=on\n[cm4]\notg_mode=1")
        self.assertIn("ttable.0.0 44.6684", paths["alsa_config"].read_text())
        env = paths["env_path"]
        self.assertEqual(env.read_text(), "MSGBOX_MIC_TYPE=i2s\nMSGBOX_BUTTON_PIN=17\n"
                         "MSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D27\n")
        self.assertEqual(env.stat().st_mode & 0o777, 0o640)

        audio_config.set_mic_type("i2s", **paths)
        self.assertEqual(self.boot_config.read_text(), boot)
        self.assertIn("ttable.0.0 31.6228", paths["alsa_config"].read_text())

    def test_set_mic_type_i2s_keeps_an_overlay_enabled_by_hand(self):
        paths = self.make_i2s_box()
        manual = "dtparam=i2s=on\ndtoverlay=googlevoicehat-soundcard\n"
        self.boot_config.write_text(manual)
        audio_config.set_mic_type("i2s", **paths)
        self.assertEqual(self.boot_config.read_text(), manual)

    def test_set_mic_type_i2s_refuses_conflicting_pins_without_changes(self):
        # Missing NFC pins mean the code defaults D20/D16, which I2S uses.
        paths = self.make_i2s_box(pins="")
        env, boot = paths["env_path"].read_text(), self.boot_config.read_text()
        with self.assertRaisesRegex(audio_config.AudioConfigError,
                                    "MSGBOX_NFC_RESET_PIN=D20, MSGBOX_NFC_REQUEST_PIN=D16"):
            audio_config.set_mic_type("i2s", **paths)
        self.assertEqual(paths["env_path"].read_text(), env)
        self.assertEqual(self.boot_config.read_text(), boot)
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
        original_boot = self.boot_config.read_text()
        audio_config.set_mic_type("i2s", **paths)
        audio_config.set_mic_type("usb", **paths)
        self.assertEqual(self.boot_config.read_text(), original_boot)
        self.assertFalse(paths["alsa_config"].exists())
        self.assertIn("MSGBOX_MIC_TYPE=usb\n", paths["env_path"].read_text())
        audio_config.set_mic_type("usb", **paths)

    def test_setup_template_for_i2s_does_not_need_the_card_before_activation(self):
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

    def test_microphone_switches_never_access_firmware_configuration(self):
        # Includes, model sections, invalid bytes and absent/read-only firmware
        # are immaterial: the helper has no reason even to open that volume.
        for content in (b"include custom.txt\n[pi4]\n", b"\xff" * 200000, None):
            with self.subTest(content=None if content is None else len(content)):
                if hasattr(self, "boot_config") and self.boot_config.exists():
                    self.boot_config.chmod(0o644)
                paths = self.make_i2s_box()
                if content is None:
                    self.boot_config.unlink()
                else:
                    self.boot_config.write_bytes(content)
                    self.boot_config.chmod(0o444)
                original_open = Path.open
                def root_only(path, *args, **kwargs):
                    if path == self.boot_config:
                        raise AssertionError("firmware access")
                    return original_open(path, *args, **kwargs)
                with mock.patch.object(Path, "open", root_only):
                    audio_config.set_mic_type("i2s", **paths)
                    audio_config.set_mic_type("usb", **paths)
                self.assertEqual(self.boot_config.read_bytes() if content is not None else None, content)

    def test_i2s_activation_failure_never_falls_back_or_replaces_runtime_output(self):
        output = self.root / "audio.env"
        output.write_text("previous\n")
        with mock.patch.object(audio_config, "configured_mic_type", return_value="i2s"), \
             mock.patch.object(audio_config, "activate_i2s", side_effect=audio_config.AudioConfigError("I2S activation failed")), \
             mock.patch.object(audio_config, "detect_microphone", side_effect=AssertionError("USB fallback")), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 1)
        self.assertIn("I2S activation failed", error.getvalue())
        self.assertEqual(output.read_text(), "previous\n")

    @contextlib.contextmanager
    def activation_box(self):
        paths = self.make_i2s_box(pins="MSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D5\n")
        audio_config.set_env_value(paths["env_path"], "MSGBOX_MIC_TYPE", "i2s")
        with contextlib.ExitStack() as stack:
            for name, path in (("CONFIG_ENV", "env_path"),
                               ("I2S_ALSA_CONFIG", "alsa_config"), ("I2S_GUARD", "guard_path"),
                               ("I2S_CAPABILITY", "capability_file")):
                stack.enter_context(mock.patch.object(audio_config, name, paths[path]))
            detect = audio_config.detect_microphone
            stack.enter_context(mock.patch.object(audio_config, "detect_microphone",
                                side_effect=lambda **kw: detect(self.sound_root, **kw)))
            yield paths

    def test_activation_loads_each_cold_boot_without_firmware_access_and_preserves_gain(self):
        with self.activation_box() as paths:
            commands = []
            def load(command, **options):
                commands.append(command)
                self.assertEqual(options["timeout"], 10)
                self.assertTrue(options["check"])
                # Guard and ALSA must be durable before hardware is changed.
                self.assertTrue(paths["guard_path"].is_file())
                self.assertTrue(paths["alsa_config"].is_file())
                if command[-1] == "-l":
                    return subprocess.CompletedProcess(command, 0, "No overlays loaded\n")
                if command[0].endswith("dtoverlay"):
                    self.add_capture(3, 0, audio_config.I2S_CARD)
            expected = (audio_config.I2S_CARD, audio_config.I2S_DEVICE)
            self.assertEqual(audio_config.activate_i2s(run=load), expected)
            self.assertEqual(commands, [["/usr/bin/dtoverlay", "-l"],
                                        ["/usr/bin/dtparam", "i2s=on"],
                                        ["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]])
            self.assertEqual(self.boot_config.read_bytes(), b"dtparam=audio=on\n[cm4]\notg_mode=1")
            paths["alsa_config"].write_text(audio_config.render_i2s_alsa_config(33))
            self.assertEqual(audio_config.activate_i2s(run=mock.Mock(side_effect=AssertionError("duplicate overlay"))), expected)
            self.assertIn("44.6684", paths["alsa_config"].read_text())
            # A subsequent cold boot has no runtime overlay. It must load again.
            (self.sound_root / "pcmC3D0c").unlink()
            self.assertEqual(audio_config.activate_i2s(run=load), expected)
            self.assertEqual(len(commands), 6)
            self.assertIn("44.6684", paths["alsa_config"].read_text())

    def test_slow_enumeration_and_first_loader_failure_recover_by_card_name(self):
        from test_bounded_update import FakeTime

        for delay, card in ((6, 1), (12.654, 3), (35, 1)):
            with self.subTest(delay=delay, card=card), self.activation_box():
                clock = FakeTime()
                calls = []
                def load(command, **options):
                    calls.append(command)
                    if command[-1] == "-l":
                        return subprocess.CompletedProcess(command, 0, "No overlays loaded\n")
                    if calls.count(["/usr/bin/dtparam", "i2s=on"]) == 1:
                        raise subprocess.CalledProcessError(1, command)
                def sleep(seconds):
                    clock.sleep(seconds)
                    if clock.now >= delay and not (self.sound_root / f"pcmC{card}D0c").exists():
                        self.add_capture(card, 0, audio_config.I2S_CARD)
                self.assertEqual(audio_config.activate_i2s(run=load, sleep=sleep, monotonic=clock.monotonic),
                                 (audio_config.I2S_CARD, audio_config.I2S_DEVICE))
                self.assertGreaterEqual(clock.now, delay)
                self.assertLessEqual(clock.now, delay + 0.25)
                self.assertEqual(calls.count(["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]), 1)
                (self.sound_root / f"pcmC{card}D0c").unlink()

    def test_failed_overlay_is_checked_before_retry_and_timeout_after_apply_is_not_duplicated(self):
        from test_bounded_update import FakeTime

        for applied in (False, True):
            with self.subTest(applied=applied), self.activation_box():
                clock = FakeTime()
                calls = []
                overlays = []
                def load(command, **options):
                    calls.append(command)
                    if command[-1] == "-l":
                        return subprocess.CompletedProcess(command, 0,
                            "0: googlevoicehat-soundcard\n" if applied and overlays else "No overlays loaded\n")
                    if command[0].endswith("dtoverlay"):
                        overlays.append(command)
                        if len(overlays) == 1:
                            if applied:
                                clock.sleep(options["timeout"])
                                raise subprocess.TimeoutExpired(command, options["timeout"])
                            raise subprocess.CalledProcessError(1, command)
                def sleep(seconds):
                    clock.sleep(seconds)
                    if clock.now >= 12.654 and not (self.sound_root / "pcmC3D0c").exists():
                        self.add_capture(3, 0, audio_config.I2S_CARD)
                self.assertEqual(audio_config.activate_i2s(run=load, sleep=sleep, monotonic=clock.monotonic),
                                 (audio_config.I2S_CARD, audio_config.I2S_DEVICE))
                self.assertEqual(len(overlays), 1 if applied else 2)
                self.assertEqual(calls.count(["/usr/bin/dtoverlay", "-l"]), 2)
                (self.sound_root / "pcmC3D0c").unlink()

    def test_unknown_overlay_listing_never_loads_blindly(self):
        from test_bounded_update import FakeTime

        with self.activation_box():
            clock = FakeTime()
            overlays = []
            def load(command, **options):
                if command[-1] == "-l":
                    return subprocess.CompletedProcess(command, 0, "unrecognized listing format\n")
                if command[0].endswith("dtoverlay"):
                    overlays.append(command)
                    raise subprocess.CalledProcessError(1, command)
            with self.assertRaises(audio_config.I2SUnavailable):
                audio_config.activate_i2s(run=load, sleep=clock.sleep, monotonic=clock.monotonic)
            self.assertEqual(clock.now, 45)
            self.assertEqual(len(overlays), 0)

    def test_activation_missing_loader_timeout_and_missing_card_exhaust_one_bounded_budget(self):
        from test_bounded_update import FakeTime

        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("dtoverlay", 10),
                        subprocess.CalledProcessError(1, "dtoverlay"), None):
            with self.subTest(failure=type(failure).__name__), self.activation_box():
                clock = FakeTime()
                def load(command, **options):
                    self.assertLessEqual(options["timeout"], 10)
                    self.assertLessEqual(options["timeout"], 45 - clock.now)
                    if command[-1] == "-l":
                        return subprocess.CompletedProcess(command, 0, "No overlays loaded\n")
                    if isinstance(failure, subprocess.TimeoutExpired):
                        clock.sleep(options["timeout"])
                    if failure is not None:
                        raise failure
                run = mock.Mock(side_effect=load)
                with self.assertRaisesRegex(audio_config.I2SUnavailable, "45 seconds"):
                    audio_config.activate_i2s(run=run, sleep=clock.sleep, monotonic=clock.monotonic)
                self.assertEqual(clock.now, 45)
                self.assertGreater(run.call_count, 2 if failure else 1)

    def test_overlay_loaded_by_exhausted_run_is_not_loaded_again_in_same_boot(self):
        from test_bounded_update import FakeTime

        with self.activation_box():
            clock = FakeTime()
            calls = []
            overlay_loaded = False
            def load(command, **options):
                nonlocal overlay_loaded
                calls.append(command)
                self.assertLessEqual(options["timeout"], min(10, deadline - clock.now))
                if command[-1] == "-l":
                    listing = "0: googlevoicehat-soundcard\n" if overlay_loaded else "No overlays loaded\n"
                    return subprocess.CompletedProcess(command, 0, listing)
                if command == ["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]:
                    self.assertFalse(overlay_loaded, "duplicate overlay load")
                    overlay_loaded = True
            deadline = 45
            with self.assertRaises(audio_config.I2SUnavailable):
                audio_config.activate_i2s(run=load, sleep=clock.sleep, monotonic=clock.monotonic)
            self.assertEqual(clock.now, 45)
            self.assertTrue(overlay_loaded)
            first_run_calls = len(calls)
            def sleep(seconds):
                clock.sleep(seconds)
                if clock.now >= 47 and not (self.sound_root / "pcmC3D0c").exists():
                    self.add_capture(3, 0, audio_config.I2S_CARD)
            deadline = 90
            self.assertEqual(audio_config.activate_i2s(run=load, sleep=sleep, monotonic=clock.monotonic),
                             (audio_config.I2S_CARD, audio_config.I2S_DEVICE))
            self.assertEqual(calls[first_run_calls:], [["/usr/bin/dtoverlay", "-l"]])
            self.assertEqual(calls.count(["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]), 1)

    def test_failed_initial_listing_is_retried_before_exactly_one_overlay_load(self):
        from test_bounded_update import FakeTime

        for failure in (OSError("listing unavailable"), subprocess.CalledProcessError(1, "dtoverlay")):
            with self.subTest(failure=type(failure).__name__), self.activation_box():
                clock = FakeTime()
                calls = []
                def load(command, **options):
                    calls.append(command)
                    self.assertLessEqual(options["timeout"], min(10, 45 - clock.now))
                    if len(calls) == 1:
                        self.assertEqual(command, ["/usr/bin/dtoverlay", "-l"])
                        raise failure
                    if command[-1] == "-l":
                        return subprocess.CompletedProcess(command, 0, "No overlays loaded\n")
                    if command == ["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]:
                        self.add_capture(3, 0, audio_config.I2S_CARD)
                self.assertEqual(audio_config.activate_i2s(run=load, sleep=clock.sleep, monotonic=clock.monotonic),
                                 (audio_config.I2S_CARD, audio_config.I2S_DEVICE))
                self.assertEqual(clock.now, 2)
                self.assertEqual(calls, [["/usr/bin/dtoverlay", "-l"], ["/usr/bin/dtoverlay", "-l"],
                                         ["/usr/bin/dtparam", "i2s=on"],
                                         ["/usr/bin/dtoverlay", "googlevoicehat-soundcard"]])
                (self.sound_root / "pcmC3D0c").unlink()

    def test_exhausted_i2s_runtime_keeps_processed_pcm_and_button_dependency_successful(self):
        output = self.root / "audio.env"
        with mock.patch.object(audio_config, "configured_mic_type", return_value="i2s"), \
             mock.patch.object(audio_config, "activate_i2s", side_effect=audio_config.I2SUnavailable("45 seconds")), \
             mock.patch.object(audio_config, "detect_speaker", return_value=("D188A", "plughw:CARD=D188A,DEV=0")), \
             mock.patch.object(audio_config, "detect_microphone", side_effect=AssertionError("USB fallback")), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 0)
        self.assertIn("button repair available", error.getvalue())
        values = audio_config.read_settings(output)
        self.assertEqual(values["MSGBOX_MIC_DEV"], audio_config.I2S_DEVICE)
        self.assertEqual(values["MSGBOX_MIC_PCM"], audio_config.I2S_PCM)
        self.assertEqual(values["MSGBOX_SPEAKER_CARD"], "D188A")

    def test_activation_rejects_conflicting_pins_before_any_write_or_hardware_command(self):
        with self.activation_box() as paths:
            audio_config.set_env_value(paths["env_path"], "MSGBOX_NFC_REQUEST_PIN", "D16")
            with mock.patch.object(audio_config, "write_atomic", side_effect=AssertionError("write")), \
                 self.assertRaises(audio_config.AudioConfigError):
                audio_config.activate_i2s(run=mock.Mock(side_effect=AssertionError("hardware")))

    def test_usb_runtime_is_identical_to_v0317_without_activation_side_effects(self):
        legacy = subprocess.check_output(["git", "-C", str(ROOT), "show", "v0.3.17:scripts/install/audio_config.py"], text=True)
        namespace = {"__name__": "legacy_audio"}
        exec(compile(legacy, "legacy_audio.py", "exec"), namespace)
        self.add_capture(2, 0, "mic", usb=True)
        self.add_playback(1, 0, "speaker", usb=True)
        detect = audio_config.detect_microphone
        with mock.patch.object(audio_config, "configured_mic_type", return_value="usb"), \
             mock.patch.object(audio_config, "activate_i2s", side_effect=AssertionError("activation")), \
             mock.patch.object(audio_config, "detect_microphone", side_effect=lambda **kw: detect(self.sound_root, **kw)), \
             mock.patch.object(audio_config, "detect_speaker", return_value=("speaker", "plughw:CARD=speaker,DEV=0")):
            output = self.root / "audio.env"
            self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 0)
        mic = namespace["detect_microphone"](self.sound_root)
        speaker = namespace["detect_speaker"](self.sound_root)
        self.assertEqual(output.read_text(), namespace["render_runtime_environment"](*mic, *speaker))

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
                    self.assertEqual(self.boot_config.read_bytes(), b"dtparam=audio=on\n[cm4]\notg_mode=1")
                    if audio_config.mic_type(audio_config.read_settings(paths["env_path"])) == "i2s":
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

    def test_valid_guards_allow_startup_under_guard_creation_enospc(self):
        with self.activation_box() as paths:
            audio_config.set_mic_type("i2s", **paths)
            guards = [paths["guard_path"], paths["guard_path"].parent.with_name(
                "messagebox-onboarding-button.service.d") / paths["guard_path"].name]
            before = [(p.stat(), p.read_bytes()) for p in guards]
            self.add_capture(3, 0, audio_config.I2S_CARD)
            real_mkstemp = tempfile.mkstemp
            attempts = []
            def no_guard_space(*args, **kwargs):
                if Path(kwargs["dir"]) in [p.parent for p in guards]:
                    attempts.append(kwargs)
                    raise OSError(errno.ENOSPC, "guard storage full")
                return real_mkstemp(*args, **kwargs)
            output = self.root / "run/audio.env"
            with mock.patch.object(audio_config.tempfile, "mkstemp", side_effect=no_guard_space), \
                 mock.patch.object(audio_config, "configured_mic_type", return_value="i2s"), \
                 mock.patch.object(audio_config, "detect_speaker", return_value=("speaker", "plughw:CARD=speaker,DEV=0")):
                self.assertEqual(audio_config.main(["--runtime-output", str(output)]), 0)
            self.assertEqual(attempts, [])
            self.assertIn("MSGBOX_MIC_PCM=buttonbox_mic", output.read_text())
            for path, (status, content) in zip(guards, before):
                self.assertEqual(path.stat().st_ino, status.st_ino)
                self.assertEqual(path.stat().st_mtime_ns, status.st_mtime_ns)
                self.assertEqual(path.read_bytes(), content)

    def test_unsafe_or_wrong_guards_are_replaced_and_storage_errors_fail_closed(self):
        paths = self.make_i2s_box()
        audio_config.install_i2s_guards(paths["guard_path"])
        guard = paths["guard_path"]
        for state in ("wrong contents", "wrong mode", "symlink", "hardlink"):
            with self.subTest(state=state):
                guard.unlink()
                guard.write_text(audio_config.render_i2s_guard())
                guard.chmod(0o644)
                if state == "wrong contents":
                    guard.write_text("[Service]\n")
                elif state == "wrong mode":
                    guard.chmod(0o666)
                else:
                    other = self.root / state
                    other.write_text(audio_config.render_i2s_guard())
                    guard.unlink()
                    if state == "symlink":
                        guard.symlink_to(other)
                    else:
                        os.link(other, guard)
                with mock.patch.object(audio_config.tempfile, "mkstemp", side_effect=OSError(errno.ENOSPC, "full")), \
                     self.assertRaises(OSError):
                    audio_config.install_i2s_guards(guard)
                audio_config.install_i2s_guards(guard)
                self.assertTrue(stat.S_ISREG(guard.lstat().st_mode))
                self.assertEqual(guard.stat().st_nlink, 1)
                self.assertEqual(guard.stat().st_mode & 0o777, 0o644)
                self.assertEqual(guard.read_text(), audio_config.render_i2s_guard())

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
