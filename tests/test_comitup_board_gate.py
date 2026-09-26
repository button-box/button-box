import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMITUP = ROOT / "scripts" / "install" / "comitup.sh"


def _board_gate():
    text = COMITUP.read_text(encoding="utf-8")
    match = re.search(
        r"^case \$\(tr -d '\\000' </proc/device-tree/model\) in\n.*?^esac$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("board gate not found in comitup.sh")
    return match.group(0).replace(
        "$(tr -d '\\000' </proc/device-tree/model)", '"$MODEL"'
    )


class ComitupBoardGateTests(unittest.TestCase):
    def _run(self, model):
        script = 'die() { echo "$*" >&2; exit 1; }\n' + _board_gate() + "\necho ok\n"
        return subprocess.run(
            ["sh", "-c", script],
            env={"MODEL": model, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_supported_boards_pass(self):
        for model in (
            "Raspberry Pi 4 Model B Rev 1.5",
            "Raspberry Pi Zero 2 W Rev 1.0",
        ):
            with self.subTest(model=model):
                result = self._run(model)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_boards_fail_closed(self):
        for model in (
            "Raspberry Pi 5 Model B Rev 1.0",
            "Raspberry Pi 3 Model B Plus Rev 1.3",
            "Raspberry Pi Zero W Rev 1.1",
            "",
        ):
            with self.subTest(model=model):
                result = self._run(model)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("validated only on", result.stderr)


if __name__ == "__main__":
    unittest.main()
