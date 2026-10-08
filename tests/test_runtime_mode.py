"""Exercise the CI-selected mode in a fresh interpreter, including import-time paths."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


class RuntimeModeTests(unittest.TestCase):
    def test_selected_mode_startup_and_transport_boundaries(self):
        mode = os.environ.get("MSGBOX_TEST_TRANSPORT", "wacli")
        self.assertIn(mode, {"wacli", "cloud"})
        probe = r'''
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock
from messagebox import cloud_runtime, nfc, runtime_paths, voicepoll

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send

mode = os.environ["MSGBOX_TRANSPORT"]
assert button_send.transport_mode() == mode
expected = cloud_runtime.CONTACTS_FILE if mode == "cloud" else runtime_paths.CONTACTS_FILE
assert Path(button_send.CONTACTS_FILE) == Path(expected)
assert nfc.router().contacts.path == Path(expected)

# Assert entry-point dispatch without constructing a client or touching device state.
with mock.patch.object(cloud_runtime, "CloudRuntime") as cloud, \
     mock.patch.object(voicepoll, "load_seen", return_value=set()) as seen, \
     mock.patch.object(voicepoll, "load_contact_authorizations", return_value={}), \
     mock.patch.object(voicepoll, "poll_once") as poll, \
     mock.patch.object(voicepoll.time, "sleep", side_effect=KeyboardInterrupt):
    if mode == "cloud":
        voicepoll.main()
        cloud.return_value.run.assert_called_once_with()
        seen.assert_not_called()
        poll.assert_not_called()
    else:
        try:
            voicepoll.main()
        except KeyboardInterrupt:
            pass
        seen.assert_called_once_with()
        poll.assert_called_once_with(set(), {})
        cloud.assert_not_called()

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "recording.wav"
    button_send.bind_legacy_job_recipient(str(path), "15551234567@s.whatsapp.net",
                                        account_scope="a" * 64 if mode == "cloud" else None)
    assert button_send.legacy_job_transport(str(path)) == mode

with mock.patch.object(button_send.subprocess, "Popen") as spawn, \
     mock.patch.object(cloud_runtime, "playable", return_value=True) as playable:
    button_send.presence("recording", "15551234567@s.whatsapp.net")
    button_send.react_played({"chat": "15551234567@s.whatsapp.net", "msgid": "synthetic"})
    assert spawn.call_count == (0 if mode == "cloud" else 2)
    assert button_send.inbound_audio_authorized({"cloud": True}) == (mode == "cloud")
    assert playable.call_count == (1 if mode == "cloud" else 0)
'''
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "MSGBOX_TRANSPORT": mode, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
