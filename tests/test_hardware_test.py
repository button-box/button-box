import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARDWARE_TEST = ROOT / "scripts" / "dev" / "hardware-test.sh"


class HardwareTestConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_file = self.root / "messagebox.env"
        self.runtime_audio_config_file = self.root / "audio.env"

    def resolve_config(self, environment=None):
        script = HARDWARE_TEST.read_text(encoding="utf-8")
        setup, marker, _ = script.partition("RECORDING=$(mktemp")
        self.assertTrue(marker)
        setup = setup.replace(
            "CONFIG_FILE=/etc/messagebox/env",
            f"CONFIG_FILE={shlex.quote(str(self.config_file))}",
        ).replace(
            "RUNTIME_AUDIO_CONFIG_FILE=/run/messagebox-audio/audio.env",
            "RUNTIME_AUDIO_CONFIG_FILE="
            f"{shlex.quote(str(self.runtime_audio_config_file))}",
        )
        setup += "printf '%s\\n%s\\n' \"$MIC_DEV\" \"$SPK_DEV\"\n"
        process_environment = {"PATH": os.environ.get("PATH", "")}
        process_environment.update(environment or {})
        result = subprocess.run(
            ["sh"],
            input=setup,
            text=True,
            capture_output=True,
            check=True,
            env=process_environment,
        )
        return result.stdout.splitlines()

    def test_runtime_audio_devices_override_persistent_config(self):
        self.config_file.write_text(
            "MSGBOX_MIC_DEV=plughw:CARD=persistent_mic,DEV=0\n"
            "MSGBOX_SPK_DEV=plughw:CARD=persistent_speaker,DEV=0\n",
            encoding="utf-8",
        )
        self.runtime_audio_config_file.write_text(
            "MSGBOX_MIC_DEV=plughw:CARD=detected_mic,DEV=1\n"
            "MSGBOX_SPK_DEV=plughw:CARD=detected_speaker,DEV=2\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.resolve_config(),
            [
                "plughw:CARD=detected_mic,DEV=1",
                "plughw:CARD=detected_speaker,DEV=2",
            ],
        )

    def test_process_environment_overrides_runtime_audio_devices(self):
        self.runtime_audio_config_file.write_text(
            "MSGBOX_MIC_DEV=plughw:CARD=detected_mic,DEV=1\n"
            "MSGBOX_SPK_DEV=plughw:CARD=detected_speaker,DEV=2\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.resolve_config(
                {
                    "MSGBOX_MIC_DEV": "plughw:CARD=manual_mic,DEV=3",
                    "MSGBOX_SPK_DEV": "plughw:CARD=manual_speaker,DEV=4",
                }
            ),
            [
                "plughw:CARD=manual_mic,DEV=3",
                "plughw:CARD=manual_speaker,DEV=4",
            ],
        )

    def test_persistent_audio_config_remains_fallback_without_runtime_file(self):
        self.config_file.write_text(
            "MSGBOX_MIC_DEV=plughw:CARD=persistent_mic,DEV=0\n"
            "MSGBOX_SPK_DEV=plughw:CARD=persistent_speaker,DEV=0\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.resolve_config(),
            [
                "plughw:CARD=persistent_mic,DEV=0",
                "plughw:CARD=persistent_speaker,DEV=0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
