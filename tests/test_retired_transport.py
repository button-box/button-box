import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox.guided_reply import OutboxStore

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


class RetiredBusinessTransportTests(unittest.TestCase):
    """The Business adapter is gone; its leftover recordings stay on the box, unsent."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_business_mode_is_rejected_at_startup(self):
        with mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "business"}):
            with self.assertRaisesRegex(ValueError, "unsupported message transport"):
                button_send.transport_mode()
        with self.assertRaisesRegex(ValueError, "unsupported outbox transport"):
            OutboxStore(str(self.root / "outbox"), transport="business")

    def test_business_outbox_job_is_kept_and_never_listed_for_sending(self):
        job_dir = self.root / "outbox" / "old.job"
        job_dir.mkdir(parents=True)
        (job_dir / "audio.wav").write_bytes(b"retained family recording")
        (job_dir / "job.json").write_text(json.dumps({
            "version": 1, "message_id": "old", "recipient": "15551234567@s.whatsapp.net",
            "flow_kind": "tap_review", "duration": 2.0, "state": "pending",
            "transport": "business",
        }), encoding="utf-8")
        for transport in ("wacli", "cloud"):
            with self.subTest(transport=transport):
                store = OutboxStore(str(self.root / "outbox"), transport=transport)
                self.assertEqual(store.load(job_dir).transport, "business")
                self.assertEqual(store.jobs(), [])
        self.assertEqual((job_dir / "audio.wav").read_bytes(), b"retained family recording")

    def test_business_hold_release_recording_is_kept_and_not_staged(self):
        wav = self.root / "1000-2.0.wav"
        wav.write_bytes(b"retained family recording")
        Path(str(wav) + ".json").write_text(json.dumps({
            "version": 1, "recipient": "15551234567@s.whatsapp.net", "transport": "business",
        }), encoding="utf-8")
        self.assertEqual(button_send.legacy_job_transport(str(wav)), "business")
        with (
            mock.patch.object(button_send, "OUTBOX_DIR", str(self.root)),
            mock.patch.object(button_send, "log_event"),
            mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"}),
        ):
            self.assertFalse(button_send.stage_hold_release_cloud_job(wav.name))
        with (
            mock.patch.object(button_send, "OUTBOX_DIR", str(self.root)),
            mock.patch.object(button_send, "log_event"),
            mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"}),
        ):
            self.assertFalse(button_send.send_legacy_outbox_file(wav.name))
        self.assertEqual(wav.read_bytes(), b"retained family recording")


if __name__ == "__main__":
    unittest.main()
