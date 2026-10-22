"""Opt-in isolated Linux bench. Never substitute systemd-run in this test."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from messagebox import controller_selector as selector


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('MSGBOX_NATIVE_SYSTEMD_TESTS') == '1',
                     'requires opt-in root on isolated Linux with systemd and cgroup v2')
class NativeTrialTests(unittest.TestCase):
    def test_output_overflow_stops_transient_unit_and_sigterm_ignoring_descendant(self):
        self.assertEqual(os.geteuid(), 0)
        self.assertTrue(shutil.which('systemd-run'))
        for name in ('/run/messagebox-update', '/var/lib/button-box-update', '/etc/button-box-update'):
            path = Path(name)
            if not path.exists():
                path.mkdir(parents=True)
                self.addCleanup(path.rmdir)
        with tempfile.TemporaryDirectory(prefix='native-trial-', dir='/run/messagebox-update') as directory:
            directory = Path(directory)
            marker = directory / 'child'
            (directory / 'update_controller.py').write_text(
                "import os,signal,time\nfrom pathlib import Path\n"
                "marker=Path(" + repr(str(marker)) + ")\n"
                "if os.fork()==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                " marker.write_text(str(os.getpid()))\n while True: time.sleep(1)\n"
                "while not marker.exists(): time.sleep(.01)\nwhile True: os.write(1,b'x'*8192)\n")
            unit = selector.TRIAL_UNITS['update_controller.py']
            self.addCleanup(selector.stop_trial, Path('/'), unit, selector.subprocess.run)
            with self.assertRaisesRegex(ValueError, 'output limit'):
                selector.trial_command(Path('/'), str(directory).lstrip('/'), 'update_controller.py',
                                       'self-test', selector.subprocess.run)
            self.assertTrue(marker.exists(), 'real transient service must reach the overflow')
            events = Path('/sys/fs/cgroup/system.slice') / unit / 'cgroup.events'
            self.assertTrue(not events.exists() or 'populated 0' in events.read_text().splitlines())
            status = Path('/proc') / marker.read_text() / 'status'
            self.assertTrue(not status.exists() or '\nState:\tZ' in status.read_text())
            # trial_command returns only after this barrier; recover() repeats
            # the same barrier before any controller or application mutation.
