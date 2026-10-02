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
from messagebox.guided_reply import OutboxStore, RecordingResult

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
        self.store = OutboxStore(str(root / "outbox"), transport="business")
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

    def test_hold_release_sidecar_binds_current_transport(self):
        path = Path(button_send.OUTBOX_DIR) / "1700000000002-1.5.wav"
        path.write_bytes(b"synthetic-held-audio")
        button_send.bind_legacy_job_recipient(
            str(path), "351900000001@s.whatsapp.net"
        )
        metadata = json.loads(Path(str(path) + ".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["transport"], "business")

    def test_pre_transport_hold_release_is_not_migrated_to_business(self):
        path = Path(button_send.OUTBOX_DIR) / "1700000000003-1.5.wav"
        path.write_bytes(b"synthetic-held-audio")
        Path(str(path) + ".json").write_text(
            json.dumps({"version": 1, "recipient": "351900000001@s.whatsapp.net"}),
            encoding="utf-8",
        )
        eligible = Path(button_send.OUTBOX_DIR) / "1700000000004-1.5.wav"
        eligible.write_bytes(b"synthetic-held-audio")
        button_send.bind_legacy_job_recipient(
            str(eligible), "351900000001@s.whatsapp.net"
        )

        self.assertNotIn(path.name, button_send.compatible_legacy_outbox_files())
        self.assertIn(eligible.name, button_send.compatible_legacy_outbox_files())
        self.assertFalse(button_send.stage_hold_release_business_job(path.name))
        self.assertTrue(path.exists())
        self.assertTrue(Path(str(path) + ".json").exists())

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
                                      "standalone", 1.0, message_id="local-job-0000001", account_scope="a" * 64)
        self.client = mock.Mock()
        for patch in (
            mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "cloud"}),
            mock.patch.object(button_send, "outbox_store", self.store),
            mock.patch.object(button_send, "TEMP_DIR", self.directory.name),
            mock.patch.object(button_send, "send_success_notices", button_send.queue.SimpleQueue()),
            mock.patch.object(button_send, "log_event"),
            mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64),
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
        self.assertEqual(metadata["account_scope"], "a" * 64)
        self.assertEqual(self.client.send_voice.call_args.kwargs["account_scope"], "a" * 64)
        self.assertEqual(metadata["cloud_message_id"], "cloud-message")
        self.assertEqual(metadata["expires_at"], 1_800_604_800)
        self.assertFalse(button_send.send_success_notices.empty())

    def test_transfer_during_recording_keeps_original_approval_scope(self):
        source = Path(self.directory.name) / "source.wav"
        current_scope = ["a" * 64]
        io = mock.Mock()

        def record():
            current_scope[0] = "b" * 64
            return RecordingResult(str(source), 1.0, True)

        io.record.side_effect = record
        io.play_review_for_approval.return_value = True
        with mock.patch.object(button_send.cloud_runtime, "account_scope", side_effect=lambda **_: current_scope[0]), \
             mock.patch.object(button_send, "claim_fresh_card_intent", return_value=("none", None)), \
             mock.patch.object(button_send, "claim_oldest", return_value=None), \
             mock.patch.object(button_send, "recording_recipient_context", return_value={
                 "contact": {"jid": self.job.recipient}, "via_card": False}), \
             mock.patch.object(button_send, "PiGuidedIO", return_value=io), \
             mock.patch.object(button_send, "led", mock.Mock(), create=True), \
             mock.patch.object(button_send, "play_pending_listened"), \
             mock.patch.object(button_send, "mark_queue_known"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send, "queued", return_value=[]):
            button_send.run_guided_once({"max_recording_seconds": 60, "after_listening": "play_only"})
            approved = [job for job in self.store.jobs() if job.message_id != self.job.message_id][0]
            self.assertEqual(approved.account_scope, "a" * 64)
            before = {p.name: p.read_bytes() for p in approved.path.iterdir()}
            with mock.patch.object(button_send.subprocess, "run") as convert:
                self.assertFalse(button_send.send_guided_job(approved))
            convert.assert_not_called()
            self.client.send_voice.assert_not_called()
            self.assertEqual({p.name: p.read_bytes() for p in approved.path.iterdir()}, before)

    def test_unbound_or_foreign_account_jobs_never_convert_or_upload(self):
        metadata_path = self.job.path / "job.json"
        for scope in (None, "b" * 64, "invalid"):
            with self.subTest(scope=scope):
                metadata = json.loads(metadata_path.read_text())
                metadata.pop("account_scope", None)
                if scope is not None:
                    metadata["account_scope"] = scope
                metadata_path.write_text(json.dumps(metadata))
                before = {p.name: p.read_bytes() for p in self.job.path.iterdir()}
                with mock.patch.object(button_send.subprocess, "run") as convert:
                    self.assertFalse(button_send.send_guided_job(self.job))
                convert.assert_not_called()
                self.client.send_voice.assert_not_called()
                self.assertEqual({p.name: p.read_bytes() for p in self.job.path.iterdir()}, before)

    def test_foreign_and_unbound_jobs_do_not_starve_current_account(self):
        source = Path(self.directory.name) / "source.wav"
        for name, scope in (("foreign", "b" * 64), ("unbound", "a" * 64)):
            job = self.store.approve(str(source), self.job.recipient, "standalone", 1,
                                     message_id=name, account_scope=scope)
            if name == "unbound":
                meta = job.path / "job.json"
                document = json.loads(meta.read_text())
                document.pop("account_scope")
                meta.write_text(json.dumps(document))
        before = {str(p): p.read_bytes() for p in self.store.root.rglob("*") if p.is_file()}
        self.assertEqual([job.message_id for job in button_send.compatible_guided_jobs()], [self.job.message_id])
        self.assertEqual({str(p): p.read_bytes() for p in self.store.root.rglob("*") if p.is_file()}, before)
        with mock.patch.object(button_send, "OUTBOX_DIR", str(self.store.root)):
            for index, scope in enumerate((None, "b" * 64, "a" * 64)):
                path = self.store.root / f"170000000001{index}-1.5.wav"
                path.write_bytes(b"synthetic-held-audio")
                Path(str(path) + ".json").write_text(json.dumps({
                    "version": 1, "recipient": self.job.recipient, "transport": "cloud", "account_scope": scope}))
            self.assertEqual(button_send.compatible_legacy_outbox_files(), ["1700000000012-1.5.wav"])

    def test_hold_release_carries_original_scope_and_does_not_adopt_unbound_audio(self):
        with mock.patch.object(button_send, "OUTBOX_DIR", str(self.store.root)):
            path = self.store.root / "1700000000010-1.5.wav"
            path.write_bytes(b"synthetic-held-audio")
            button_send.bind_legacy_job_recipient(str(path), self.job.recipient, account_scope="b" * 64)
            self.assertTrue(button_send.stage_hold_release_business_job(path.name))
            staged = [job for job in self.store.jobs() if job.flow_kind == "hold_release"][0]
            self.assertEqual(staged.account_scope, "b" * 64)
            with mock.patch.object(button_send.subprocess, "run") as convert:
                self.assertFalse(button_send.send_guided_job(staged))
            convert.assert_not_called()
            self.assertTrue(staged.audio_path.exists())
            path.write_bytes(b"legacy-unbound-audio")
            sidecar = Path(str(path) + ".json")
            sidecar.write_text(json.dumps({"version": 1, "recipient": self.job.recipient, "transport": "cloud"}))
            before = (path.read_bytes(), sidecar.read_bytes())
            self.assertFalse(button_send.stage_hold_release_business_job(path.name))
            self.assertEqual((path.read_bytes(), sidecar.read_bytes()), before)

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


class GuidedTransportIsolationTests(unittest.TestCase):
    def test_direct_send_rechecks_durable_transport_before_conversion(self):
        for approved, current in (("cloud", "wacli"), ("wacli", "cloud")):
            with self.subTest(approved=approved, current=current), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.wav"
                source.write_bytes(b"synthetic-wav")
                approved_store = OutboxStore(str(root / "outbox"), transport=approved)
                job = approved_store.approve(
                    str(source), "351900000001@s.whatsapp.net", "standalone", 1.0,
                    account_scope="a" * 64 if approved == "cloud" else None
                )
                current_store = OutboxStore(str(root / "outbox"), transport=current)
                with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": current}), \
                     mock.patch.object(button_send, "outbox_store", current_store), \
                     mock.patch.object(button_send, "log_event") as event, \
                     mock.patch.object(button_send.subprocess, "run") as run, \
                     mock.patch.object(button_send.CloudDeviceClient, "from_environment") as cloud:
                    self.assertFalse(button_send.send_guided_job(job))
                run.assert_not_called()
                cloud.assert_not_called()
                event.assert_called_once_with(
                    "send_blocked", flow="standalone", reason="transport"
                )
                self.assertEqual(approved_store.load(job.path).state, "pending")
                self.assertTrue(job.audio_path.exists())
