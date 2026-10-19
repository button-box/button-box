"""update-bootstrap.sh drop-ins must not create a boot ordering cycle.

Path and timer units are ordered before paths.target and timers.target, and
basic.target (so every default service, recovery included) comes after those.
Ordering a .path or .timer after messagebox-update-recovery.service is a cycle;
systemd then deletes a random start job at boot, for example the setup
button's activators.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts/install/update-bootstrap.sh"


class BootstrapDropInTests(unittest.TestCase):
    def write_drop_ins(self, directory):
        text = BOOTSTRAP.read_text()
        units = re.search(r'^GUARDED_UNITS="([^"]+)"$', text, re.M)[0]
        function = re.search(r"^write_drop_ins\(\) \{\n.*?^\}\n", text, re.M | re.S)[0]
        # Run the real function against a temporary unit directory.
        function = function.replace("/etc/systemd/system", str(directory)).replace("-o root -g root ", "")
        subprocess.run(["/bin/sh", "-c", f"set -eu\n{units}\n{function}\nwrite_drop_ins\n"], check=True)
        return units.split('"')[1].split()

    def test_activators_wait_only_on_the_inhibit_condition(self):
        with tempfile.TemporaryDirectory() as directory:
            units = self.write_drop_ins(Path(directory))
            self.assertTrue(any(unit.endswith(".path") for unit in units))
            self.assertTrue(any(unit.endswith(".timer") for unit in units))
            for unit in units:
                text = (Path(directory) / f"{unit}.d/software-update.conf").read_text()
                with self.subTest(unit=unit):
                    self.assertIn("ConditionPathExists=!/var/lib/button-box-update/install-inhibit", text)
                    if unit.endswith((".path", ".timer")):
                        self.assertNotIn("messagebox-update-recovery", text)
                    else:
                        self.assertIn("Requires=messagebox-update-recovery.service", text)
                        self.assertIn("After=messagebox-update-recovery.service", text)

    def test_services_started_by_guarded_activators_still_wait_for_recovery(self):
        guarded = re.search(r'^GUARDED_UNITS="([^"]+)"$', BOOTSTRAP.read_text(), re.M)[1].split()
        for activator in (unit for unit in guarded if unit.endswith((".path", ".timer"))):
            source = next(ROOT.glob(f"systemd/**/{activator}"))
            started = re.search(r"^Unit=(\S+)$", source.read_text(), re.M)
            service = started[1] if started else activator.rsplit(".", 1)[0] + ".service"
            with self.subTest(activator=activator):
                self.assertIn(service, guarded)

    def test_existing_boxes_can_repair_their_drop_ins(self):
        text = BOOTSTRAP.read_text()
        repair = text.index("--repair-drop-ins")
        self.assertLess(repair, text.index("Already bootstrapped"))
        self.assertLess(text.index("[ \"$(id -u)\" -eq 0 ]"), repair)


if __name__ == "__main__":
    unittest.main()
