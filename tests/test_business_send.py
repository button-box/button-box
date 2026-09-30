import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from messagebox.business_send import (
    BusinessSendClient,
    BusinessSendRejected,
    BusinessSendUncertain,
    recipient_number,
)
from messagebox.guided_reply import OutboxStore

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class BusinessClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ogg = Path(self.directory.name) / "voice.ogg"
        self.ogg.write_bytes(b"OggSsynthetic-audio")
        self.client = BusinessSendClient("https://test.invalid", "A", "synthetic-token")

    def test_send_uses_exact_recipient_and_accepts_complete_response(self):
        observed = []

        def open_request(request, *, timeout):
            observed.append((request, timeout))
            return _Response({"ok": True, "box": "A", "message_id": "synthetic-wamid"})

        result = self.client.send_voice(
            self.ogg, "351900000001@s.whatsapp.net", "a" * 32, opener=open_request
        )
        self.assertEqual(result, "synthetic-wamid")
        request, timeout = observed[0]
        self.assertEqual(request.full_url, "https://test.invalid/api/send-voice")
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(request.get_header("User-agent"), "ButtonBox/0.1.0")
        self.assertEqual(request.get_header("Idempotency-key"), "a" * 32)
        self.assertIn(b'name="box"\r\n\r\nA', request.data)
        self.assertIn(b'name="to"\r\n\r\n351900000001', request.data)
        self.assertIn(b"OggSsynthetic-audio", request.data)

    def test_preflight_rejects_unsafe_route_and_media_without_http(self):
        opener = mock.Mock()
        for recipient in ("123-456@g.us", "351900000001@s.whatsapp.net ", ""):
            with self.subTest(recipient=recipient), self.assertRaises(BusinessSendRejected):
                self.client.send_voice(self.ogg, recipient, "a" * 32, opener=opener)
        self.ogg.write_bytes(b"not-ogg")
        with self.assertRaises(BusinessSendRejected):
            self.client.send_voice(
                self.ogg, "351900000001@s.whatsapp.net", "a" * 32, opener=opener
            )
        opener.assert_not_called()
        self.assertEqual(recipient_number("351900000001@s.whatsapp.net"), "351900000001")

    def test_explicit_rejection_and_ambiguous_result_are_distinct(self):
        for code, error_type in ((400, BusinessSendRejected), (401, BusinessSendRejected),
                                 (403, BusinessSendRejected), (502, BusinessSendUncertain)):
            with self.subTest(code=code), self.assertRaises(error_type):
                self.client.send_voice(
                    self.ogg,
                    "351900000001@s.whatsapp.net",
                    "a" * 32,
                    opener=lambda request, timeout: (_ for _ in ()).throw(
                        urllib.error.HTTPError(request.full_url, code, "error", {}, io.BytesIO())
                    ),
                )
        with self.assertRaises(BusinessSendUncertain):
            self.client.send_voice(
                self.ogg,
                "351900000001@s.whatsapp.net",
                "a" * 32,
                opener=lambda _request, timeout: (_ for _ in ()).throw(TimeoutError()),
            )
        with self.assertRaises(BusinessSendUncertain):
            self.client.send_voice(
                self.ogg,
                "351900000001@s.whatsapp.net",
                "a" * 32,
                opener=lambda _request, timeout: _Response({"ok": True, "box": "B"}),
            )

    def test_configuration_requires_https_explicit_box_and_token_file(self):
        token = Path(self.directory.name) / "token"
        token.write_text("synthetic-token\n", encoding="utf-8")
        base = {
            "MSGBOX_BUSINESS_API_URL": "https://test.invalid",
            "MSGBOX_BUSINESS_BOX": "A",
            "MSGBOX_BUSINESS_TOKEN_FILE": str(token),
        }
        self.assertEqual(BusinessSendClient.from_environment(base).token, "synthetic-token")
        self.assertEqual(
            BusinessSendClient.from_environment({**base, "MSGBOX_BUSINESS_BOX": "C"}).box,
            "C",
        )
        for change in ({"MSGBOX_BUSINESS_API_URL": "http://test.invalid"},
                       {"MSGBOX_BUSINESS_BOX": ""},
                       {"MSGBOX_BUSINESS_TOKEN_FILE": ""}):
            with self.subTest(change=change), self.assertRaises(BusinessSendRejected):
                BusinessSendClient.from_environment({**base, **change})


class GuidedBusinessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        source = root / "source.wav"
        source.write_bytes(b"synthetic-wav")
        self.store = OutboxStore(str(root / "outbox"))
        self.job = self.store.approve(
            str(source), "351900000001@s.whatsapp.net", "standalone", 1.0,
            message_id="local-job-0000001",
        )
        self.client = mock.Mock()
        patches = [
            mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "business"}),
            mock.patch.object(button_send, "outbox_store", self.store),
            mock.patch.object(button_send, "OUTBOX_DIR", str(root / "outbox")),
            mock.patch.object(button_send, "TEMP_DIR", self.directory.name),
            mock.patch.object(button_send, "send_success_notices", button_send.queue.SimpleQueue()),
            mock.patch.object(button_send, "log_event"),
            mock.patch.object(button_send.BusinessSendClient, "from_environment", return_value=self.client),
            mock.patch.object(button_send, "ContactStore"),
            mock.patch.object(button_send.subprocess, "run", side_effect=self._convert),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        button_send.ContactStore.return_value.allowed_jids.return_value = {
            "351900000001@s.whatsapp.net"
        }

    @staticmethod
    def _convert(command, **_kwargs):
        Path(command[-1]).write_bytes(b"OggSsynthetic-audio")
        return types.SimpleNamespace(returncode=0)

    def test_success_completes_only_after_server_acceptance(self):
        self.client.send_voice.return_value = "synthetic-wamid"
        self.assertTrue(button_send.send_guided_job(self.job))
        self.assertEqual(self.store.jobs(states=("pending", "sending", "uncertain", "failed")), [])
        self.assertFalse(button_send.send_success_notices.empty())
        self.assertEqual(self.client.send_voice.call_args.args[2], self.job.message_id)

    def test_unknown_result_is_quarantined_without_cue_or_retry(self):
        self.client.send_voice.side_effect = BusinessSendUncertain("unknown")
        self.assertTrue(button_send.send_guided_job(self.job))
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(self.store.jobs(states=("uncertain",))[0].message_id, self.job.message_id)
        self.assertTrue(button_send.send_success_notices.empty())
        self.client.send_voice.assert_called_once()

    def test_removed_recipient_fails_before_http(self):
        button_send.ContactStore.return_value.allowed_jids.return_value = set()
        self.assertTrue(button_send.send_guided_job(self.job))
        self.assertEqual(self.store.jobs(states=("failed",))[0].message_id, self.job.message_id)
        self.client.send_voice.assert_not_called()

    def test_hold_release_is_staged_with_stable_route_and_id(self):
        name = "1700000000000-1.5.wav"
        path = Path(button_send.OUTBOX_DIR) / name
        path.write_bytes(b"synthetic-held-audio")
        button_send.bind_legacy_job_recipient(str(path), "351900000001@s.whatsapp.net")
        self.assertTrue(button_send.stage_hold_release_business_job(name))
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".json").exists())
        [job] = [item for item in self.store.jobs() if item.flow_kind == "hold_release"]
        self.assertEqual(job.recipient, "351900000001@s.whatsapp.net")
        self.assertEqual(job.flow_kind, "hold_release")
        self.assertEqual(job.duration, 1.5)
        self.client.send_voice.return_value = "synthetic-wamid"
        self.assertTrue(button_send.send_guided_job(job))
        self.assertEqual(self.client.send_voice.call_args.args[2], job.message_id)

    def test_hold_release_stage_retry_cannot_change_recipient(self):
        name = "1700000000001-1.5.wav"
        path = Path(button_send.OUTBOX_DIR) / name
        path.write_bytes(b"synthetic-held-audio")
        button_send.bind_legacy_job_recipient(str(path), "351900000001@s.whatsapp.net")
        with mock.patch.object(button_send.os, "remove", side_effect=OSError("interrupted")):
            self.assertFalse(button_send.stage_hold_release_business_job(name))
        [job] = [item for item in self.store.jobs() if item.flow_kind == "hold_release"]
        button_send.bind_legacy_job_recipient(str(path), "351900000002@s.whatsapp.net")
        self.assertFalse(button_send.stage_hold_release_business_job(name))
        self.assertEqual(self.store.load(job.path).recipient, job.recipient)
        self.assertTrue(path.exists())

    def test_business_recording_does_not_call_legacy_presence(self):
        with mock.patch.object(button_send.subprocess, "Popen") as popen:
            button_send.presence("recording", "351900000001@s.whatsapp.net")
            button_send.presence("paused", "351900000001@s.whatsapp.net")
        popen.assert_not_called()


class GuidedCloudBoundaryTests(unittest.TestCase):
    def test_claim_only_main_skips_all_household_startup(self):
        with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "cloud", "MSGBOX_CLAIM_ONLY": "1"}), \
             mock.patch.object(button_send.sys, "argv", ["button_send"]), \
             mock.patch.object(button_send, "claim_only_loop", return_value=0) as claim, \
             mock.patch.object(button_send, "OutboxStore") as outbox, \
             mock.patch.object(button_send, "recover_inflight") as recover, \
             mock.patch.object(button_send, "cleanup_temp_recordings") as cleanup, \
             mock.patch.object(button_send, "make_beeps") as beeps:
            self.assertEqual(button_send.main(), 0)
        claim.assert_called_once_with()
        outbox.assert_not_called()
        recover.assert_not_called()
        cleanup.assert_not_called()
        beeps.assert_not_called()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        source = root / "source.wav"
        source.write_bytes(b"synthetic-wav")
        self.store = OutboxStore(str(root / "outbox"), transport="cloud")
        self.job = self.store.approve(str(source), "351900000001@s.whatsapp.net",
                                      "standalone", 1.0, message_id="local-job-0000001")
        self.client = mock.Mock()
        for patch in (
            mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "cloud"}),
            mock.patch.object(button_send, "outbox_store", self.store),
            mock.patch.object(button_send, "TEMP_DIR", self.directory.name),
            mock.patch.object(button_send, "send_success_notices", button_send.queue.SimpleQueue()),
            mock.patch.object(button_send, "log_event"),
            mock.patch.object(button_send.cloud_runtime, "recipient_id", return_value="person1234567890123456"),
            mock.patch.object(button_send.CloudDeviceClient, "from_environment", return_value=self.client),
            mock.patch.object(button_send.subprocess, "run", side_effect=GuidedBusinessBoundaryTests._convert),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_accepted_upload_retains_source_to_authoritative_expiry(self):
        self.client.send_voice.return_value = {"message_id": "cloud-message", "state": "accepted",
                                               "expires_at": 1_800_604_800, "server_time": 1_800_000_000}
        self.assertTrue(button_send.send_guided_job(self.job))
        self.assertTrue(self.job.audio_path.exists())
        metadata = json.loads((self.job.path / "job.json").read_text())
        self.assertEqual(metadata["state"], "cloud_retained")
        self.assertEqual(metadata["cloud_message_id"], "cloud-message")
        self.assertEqual(metadata["expires_at"], 1_800_604_800)
        self.assertFalse(button_send.send_success_notices.empty())

    def test_delivery_uncertain_or_lost_response_never_cues_or_reuploads(self):
        self.client.send_voice.return_value = {"message_id": "cloud-message", "state": "delivery_uncertain",
                                               "expires_at": 1_800_604_800, "server_time": 1_800_000_000}
        self.assertTrue(button_send.send_guided_job(self.job))
        self.assertTrue(self.job.audio_path.exists())
        self.assertTrue(button_send.send_success_notices.empty())
        self.client.send_voice.assert_called_once()

    def test_transport_uncertainty_keeps_private_source_and_no_retry(self):
        self.client.send_voice.side_effect = button_send.CloudSendUncertain("unknown")
        self.assertTrue(button_send.send_guided_job(self.job))
        self.assertTrue(self.job.audio_path.exists())
        self.assertEqual(self.store.jobs(states=("uncertain",))[0].message_id, self.job.message_id)
        self.assertTrue(button_send.send_success_notices.empty())
        self.client.send_voice.assert_called_once()
