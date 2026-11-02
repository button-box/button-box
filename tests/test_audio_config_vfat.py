"""Opt-in real FAT mount regression; never format or mount from the test.

Prepare a disposable read-only FAT32 mount with a config.txt over 1 MiB, then:
MSGBOX_TEST_VFAT_ROOT=/path/to/mount python3 -m unittest discover -s tests -p test_audio_config_vfat.py -v
This kills helper processes, not the host kernel or Pi power. It proves that
firmware survives every remaining helper boundary without relying on FAT writes.
"""

import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_audio_config import ROOT, audio_config


@unittest.skipUnless(os.environ.get('MSGBOX_TEST_VFAT_ROOT') and hasattr(os, 'fork'),
                     'requires a disposable read-only FAT mount and fork')
class RealFirmwareFilesystemTests(unittest.TestCase):
    def test_killed_configuration_and_activation_preserve_multicluster_firmware(self):
        mount = Path(os.environ['MSGBOX_TEST_VFAT_ROOT']).resolve()
        if sys.platform == 'darwin':
            listing = subprocess.check_output(['/sbin/mount'], text=True)
            self.assertTrue(any(f' on {mount} (msdos,' in line for line in listing.splitlines()),
                            'fixture must be a mounted FAT filesystem')
        elif sys.platform.startswith('linux'):
            # Require the real FAT driver, rather than accepting a directory on
            # the host's journaled filesystem as a substitute for this test.
            mounts = Path('/proc/self/mountinfo').read_text().splitlines()
            self.assertTrue(any(line.split()[4] == str(mount) and
                                line.split(' - ', 1)[1].split()[0] in {'vfat', 'msdos'}
                                for line in mounts))
        else:
            self.fail('FAT mount validation is supported on Linux and macOS')
        firmware = mount / 'config.txt'
        original = firmware.read_bytes()
        self.assertGreater(len(original), 1024 * 1024)
        self.assertTrue(os.statvfs(firmware.parent).f_flag & os.ST_RDONLY)
        digest = hashlib.sha256(original).digest()
        boundaries = []

        def trial(kind, cut=None):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = root / 'env'
                env.write_text('MSGBOX_MIC_TYPE=usb\nMSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D5\n')
                alsa, guard = root / 'alsa/mic.conf', root / 'button.service.d/guard.conf'
                with contextlib.ExitStack() as stack:
                    for name, value in (('CONFIG_ENV', env), ('BOOT_CONFIG', firmware),
                                        ('I2S_ALSA_CONFIG', alsa), ('I2S_GUARD', guard),
                                        ('I2S_CAPABILITY', ROOT / 'messagebox/mic_capture.py')):
                        stack.enter_context(mock.patch.object(audio_config, name, value, create=True))
                    stack.enter_context(mock.patch.object(audio_config, 'configured_mic_type',
                        side_effect=lambda: audio_config.mic_type(audio_config.read_settings(env))))
                    loaded = [False]
                    def detect(**options):
                        if options.get('kind') == 'i2s':
                            if not loaded[0]:
                                raise audio_config.AudioConfigError('card absent')
                            return audio_config.I2S_CARD, audio_config.I2S_DEVICE
                        return 'mic', 'plughw:CARD=mic,DEV=0'
                    def load(command, **options):
                        if command[-1] == '-l':
                            return subprocess.CompletedProcess(command, 0, 'No overlays loaded\n')
                        if command[0].endswith('dtoverlay'):
                            loaded[0] = True
                    activation = audio_config.activate_i2s
                    stack.enter_context(mock.patch.object(audio_config, 'detect_microphone', side_effect=detect))
                    stack.enter_context(mock.patch.object(audio_config, 'detect_speaker',
                        return_value=('speaker', 'plughw:CARD=speaker,DEV=0')))
                    stack.enter_context(mock.patch.object(audio_config, 'activate_i2s',
                        side_effect=lambda: activation(run=load, sleep=lambda seconds: None)))
                    def operation():
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(audio_config.main(['--set-mic-type', kind]), 0)
                        self.assertEqual(audio_config.main(['--runtime-output', str(root / 'run/audio.env')]), 0)
                    if kind == 'usb':
                        audio_config.set_mic_type('i2s', env_path=env, alsa_config=alsa,
                                                  guard_path=guard, capability_file=ROOT / 'messagebox/mic_capture.py')
                    events = []
                    def instrument(function, name):
                        def wrapped(*args, **kwargs):
                            def boundary(label):
                                events.append(label)
                                if len(events) - 1 == cut:
                                    os._exit(77)  # No exception unwinding, cleanup or final flush.
                            boundary('before ' + name)
                            result = function(*args, **kwargs)
                            boundary('after ' + name)
                            return result
                        return wrapped
                    def interrupted():
                        with contextlib.ExitStack() as hooks:
                            for owner, name in ((audio_config.os, 'fsync'), (audio_config.os, 'replace'),
                                                (audio_config.os, 'unlink'), (audio_config, 'write_atomic')):
                                hooks.enter_context(mock.patch.object(owner, name,
                                    side_effect=instrument(getattr(owner, name), name)))
                            operation()
                    if cut is None:
                        interrupted()
                    else:
                        pid = os.fork()
                        if pid == 0:
                            try:
                                interrupted()
                            except BaseException:
                                os._exit(78)
                            os._exit(0)
                        _, status = os.waitpid(pid, 0)
                        self.assertEqual(os.waitstatus_to_exitcode(status), 77)
                    self.assertEqual(hashlib.sha256(firmware.read_bytes()).digest(), digest)
                    # Fresh detection after the interrupted process must recover.
                    loaded[0] = False
                    operation()
                    self.assertEqual(audio_config.read_settings(env)['MSGBOX_MIC_TYPE'], kind)
                    runtime = audio_config.read_settings(root / 'run/audio.env')
                    self.assertEqual(runtime['MSGBOX_MIC_CARD'], audio_config.I2S_CARD if kind == 'i2s' else 'mic')
                    self.assertEqual(runtime.get('MSGBOX_MIC_PCM'), audio_config.I2S_PCM if kind == 'i2s' else None)
                    self.assertEqual(firmware.read_bytes(), original)
                    return events

        for kind in ('i2s', 'usb'):
            events = trial(kind)
            for cut, event in enumerate(events):
                with self.subTest(kind=kind, boundary=cut, event=event):
                    trial(kind, cut)
            boundaries.extend(events)
        print(f'Real read-only FAT firmware preserved across {len(boundaries)} abrupt process exits; recovery passed.')
