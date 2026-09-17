import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox import voicepoll

from messagebox.contacts import ContactStore
from messagebox.voicepoll import (
    MediaRejected,
    load_contact_authorizations,
    message_is_authorized,
    parse_wacli_timestamp,
)


PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363123456789@g.us"


class PollingStoreTests(unittest.TestCase):
    def test_unlinked_poll_does_not_initialize_a_store_before_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "wacli.db"

            def run(arguments, **kwargs):
                # Models the real CLI boundary, reproduced on the device:
                # ordinary listing creates the database; read-only does not.
                if "--read-only" not in arguments:
                    database.touch()
                return SimpleNamespace(returncode=1, stdout="", stderr="")

            with (
                mock.patch.object(voicepoll, "load_seen", return_value=set()),
                mock.patch.object(voicepoll, "load_contact_authorizations", return_value={}),
                mock.patch.object(voicepoll.subprocess, "run", side_effect=run) as process,
                mock.patch.object(voicepoll.time, "sleep", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                voicepoll.main()
            process.assert_called_once()
            self.assertFalse(database.exists())

    def test_poll_preserves_authorization_deduplication_and_oldest_first_order(self):
        def message(message_id, timestamp, **changes):
            result = {
                "MsgID": message_id,
                "ChatJID": GROUP,
                "Timestamp": timestamp,
                "MediaType": "video",
                "FromMe": False,
            }
            result.update(changes)
            return result

        newest_first = [
            message("new", 6),
            message("mine", 5, FromMe=True),
            message("image", 4, MediaType="image"),
            message("seen", 3),
            message("unauthorized", 2, ChatJID=PERSON),
            message("old", 1, MediaType="audio"),
        ]
        response = SimpleNamespace(
            stdout=json.dumps({"data": {"messages": newest_first}}),
        )
        seen = {"seen"}
        with (
            mock.patch.object(voicepoll, "load_seen", return_value=seen),
            mock.patch.object(voicepoll, "load_contact_authorizations", return_value={GROUP: 0}),
            mock.patch.object(voicepoll, "wacli", return_value=response),
            mock.patch.object(voicepoll, "process_message") as process,
            mock.patch.object(voicepoll.time, "sleep", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            voicepoll.main()

        self.assertEqual(
            [call.args[0]["MsgID"] for call in process.call_args_list],
            ["old", "new"],
        )


class TimestampTests(unittest.TestCase):
    def test_parses_iso_and_unix_wacli_timestamp_forms(self):
        expected = datetime(2026, 7, 31, 12, tzinfo=timezone.utc).timestamp()
        for value in (
            "2026-07-31T12:00:00Z",
            "2026-07-31T14:00:00+02:00",
            "2026-07-31 12:00:00 UTC",
            expected,
            str(int(expected)),
            int(expected * 1000),
            int(expected * 1_000_000),
            int(expected * 1_000_000_000),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_wacli_timestamp(value), expected)

    def test_invalid_timestamps_fail_closed(self):
        for value in (None, True, "", "not-a-time", float("inf"), {}):
            with self.subTest(value=value):
                self.assertIsNone(parse_wacli_timestamp(value))


class ContactAuthorizationTests(unittest.TestCase):
    def test_store_is_reloaded_and_only_contacts_authorize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            store = ContactStore(path)
            store.upsert_listener("15550001@s.whatsapp.net", "Mom")

            self.assertEqual(load_contact_authorizations(path), {})
            store.add_contact(GROUP, "Family", receive_after=100)
            store.assign_card(GROUP, "04:A1:00:FF")
            self.assertEqual(load_contact_authorizations(path), {GROUP: 100})

            store.add_contact(PERSON, "Grandma", receive_after=200.5)
            self.assertEqual(
                load_contact_authorizations(path),
                {GROUP: 100, PERSON: 200.5},
            )

    def test_missing_empty_and_corrupt_store_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            self.assertEqual(load_contact_authorizations(path), {})
            path.write_text("", encoding="utf-8")
            self.assertEqual(load_contact_authorizations(path), {})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_contact_authorizations(path), {})

    def test_receive_after_blocks_historical_and_unparseable_messages(self):
        authorizations = {GROUP: 1_000.5}

        def message(timestamp, chat=GROUP):
            return {"ChatJID": chat, "Timestamp": timestamp}

        self.assertFalse(message_is_authorized(message(1_000.499), authorizations))
        self.assertTrue(message_is_authorized(message(1_000.5), authorizations))
        self.assertTrue(message_is_authorized(message("1970-01-01T00:16:41Z"), authorizations))
        self.assertFalse(message_is_authorized(message(None), authorizations))
        self.assertFalse(message_is_authorized(message(2_000, PERSON), authorizations))


class DownloadContractTests(unittest.TestCase):
    def test_download_uses_exact_chat_and_message_and_returns_path(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True, "data": {"path": "/tmp/synthetic.mp4"}}),
        )
        message = {"ChatJID": GROUP, "MsgID": "synthetic-video"}
        with mock.patch.object(voicepoll, "wacli", return_value=result) as wacli:
            self.assertEqual(voicepoll.download_path(message), "/tmp/synthetic.mp4")
        wacli.assert_called_once_with(
            "media", "download", "--chat", GROUP, "--id", "synthetic-video",
            "--lock-wait", voicepoll.LOCK_WAIT, "--json",
        )

    def test_failed_or_malformed_download_has_no_path(self):
        for result in (
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout="not json"),
            SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {}})),
        ):
            with self.subTest(result=result):
                with mock.patch.object(voicepoll, "wacli", return_value=result):
                    self.assertIsNone(
                        voicepoll.download_path({"ChatJID": GROUP, "MsgID": "synthetic"})
                    )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required",
)
class MediaQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = self.root / "queue"
        self.audio = self.root / "voice.wav"
        self.video = self.root / "video.mp4"
        self.silent_video = self.root / "silent.mp4"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-ar", "48000", "-ac", "1", str(self.audio),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=32x32:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(self.video),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=32x32:d=2",
                "-c:v", "mpeg4", str(self.silent_video),
            ],
            check=True,
        )
        self.patches = (
            mock.patch.object(voicepoll, "QUEUE_DIR", str(self.queue)),
            mock.patch.object(voicepoll, "EQ_FILTER", ""),
            mock.patch.object(voicepoll, "VIDEO_MAX_BYTES", 10 * 1024 * 1024),
            mock.patch.object(voicepoll, "VIDEO_MAX_DURATION_S", 10),
        )
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def message(self, media_type, message_id):
        return {
            "ChatJID": GROUP,
            "MsgID": message_id,
            "SenderJID": PERSON,
            "MediaType": media_type,
        }

    def test_ordinary_video_queues_first_audio_track_with_routing_sidecar(self):
        queued, duration = voicepoll.queue_message(
            self.message("video", "synthetic-video"), str(self.video)
        )
        queued = Path(queued)
        self.assertTrue(queued.is_file())
        self.assertAlmostEqual(duration, 2, delta=0.1)
        self.assertEqual(
            json.loads(Path(f"{queued}.json").read_text(encoding="utf-8")),
            {
                "version": 1,
                "chat": GROUP,
                "msgid": "synthetic-video",
                "sender_jid": PERSON,
                "media_type": "video",
            },
        )
        self.assertEqual(list(self.queue.glob("*.part")), [])

    def test_existing_voice_note_path_still_queues_audio(self):
        queued, duration = voicepoll.queue_message(
            self.message("audio", "synthetic-audio"), str(self.audio)
        )
        self.assertTrue(Path(queued).is_file())
        self.assertAlmostEqual(duration, 2, delta=0.1)

    def test_video_transcode_always_has_a_hard_duration_cap(self):
        def create_output(command, **_kwargs):
            Path(command[-1]).write_bytes(b"synthetic wav")
            return SimpleNamespace(returncode=0)

        with (
            mock.patch.object(voicepoll, "audio_duration", return_value=2),
            mock.patch.object(voicepoll, "wav_duration", return_value=2),
            mock.patch.object(voicepoll.subprocess, "run", side_effect=create_output) as run,
        ):
            voicepoll.queue_message(
                self.message("video", "bounded-video"), str(self.video)
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("-t") + 1],
            str(voicepoll.VIDEO_MAX_DURATION_S),
        )

    def test_probe_and_transcode_timeouts_remain_retryable(self):
        message = self.message("video", "retryable-timeout")
        with mock.patch.object(
            voicepoll.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("ffprobe", 30),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                voicepoll.queue_message(message, str(self.video))

        with (
            mock.patch.object(voicepoll, "audio_duration", return_value=2),
            mock.patch.object(
                voicepoll.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("ffmpeg", 120),
            ),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                voicepoll.queue_message(message, str(self.video))

    def test_video_without_audio_is_rejected_without_queue_artifacts(self):
        with self.assertRaisesRegex(MediaRejected, "no audio track"):
            voicepoll.queue_message(
                self.message("video", "synthetic-silent"), str(self.silent_video)
            )
        self.assertFalse(self.queue.exists())

    def test_invalid_and_configured_limits_are_rejected(self):
        invalid = self.root / "invalid.mp4"
        invalid.write_text("synthetic invalid media", encoding="utf-8")
        cases = (
            (invalid, {}, "invalid media"),
            (self.video, {"VIDEO_MAX_BYTES": 1}, "size limit"),
            (self.video, {"VIDEO_MAX_DURATION_S": 1}, "duration limit"),
        )
        for source, changes, expected in cases:
            with self.subTest(expected=expected):
                patches = [mock.patch.object(voicepoll, name, value) for name, value in changes.items()]
                for patch in patches:
                    patch.start()
                try:
                    with self.assertRaisesRegex(MediaRejected, expected):
                        voicepoll.queue_message(
                            self.message("video", f"synthetic-{expected}"), str(source)
                        )
                finally:
                    for patch in reversed(patches):
                        patch.stop()
        self.assertFalse(self.queue.exists())


class MessageIsolationTests(unittest.TestCase):
    def test_download_failure_retries_but_permanent_rejection_does_not_block_next(self):
        seen = set()
        saved = []
        messages = [
            {"MsgID": "retry", "ChatJID": GROUP, "SenderName": "Synthetic", "Timestamp": 1},
            {"MsgID": "bad", "ChatJID": GROUP, "SenderName": "Synthetic", "Timestamp": 2},
            {"MsgID": "good", "ChatJID": GROUP, "SenderName": "Synthetic", "Timestamp": 3},
        ]
        with (
            mock.patch.object(voicepoll, "save_seen", side_effect=lambda value: saved.append(set(value))),
            mock.patch.object(
                voicepoll,
                "download_path",
                side_effect=[None, "/tmp/bad.mp4", "/tmp/good.mp4"],
            ),
            mock.patch.object(
                voicepoll,
                "queue_message",
                side_effect=[MediaRejected("invalid media"), ("/tmp/queued.wav", 2)],
            ) as queue,
            mock.patch.object(voicepoll, "log_event"),
        ):
            for message in messages:
                voicepoll.process_message(message, seen)

        self.assertNotIn("retry", seen)
        self.assertIn("bad", seen)
        self.assertIn("good", seen)
        self.assertEqual(queue.call_count, 2)
        self.assertEqual(saved[-1], {"bad", "good"})

    def test_download_exception_retries_without_raising(self):
        seen = set()
        message = {
            "MsgID": "timeout",
            "ChatJID": GROUP,
            "SenderName": "Synthetic",
            "Timestamp": 1,
        }
        with (
            mock.patch.object(voicepoll, "save_seen"),
            mock.patch.object(voicepoll, "download_path", side_effect=subprocess.TimeoutExpired("wacli", 1)),
        ):
            voicepoll.process_message(message, seen)
        self.assertNotIn("timeout", seen)

    def test_transient_queue_failures_clear_seen_for_retry(self):
        message = {
            "MsgID": "private-message-id",
            "ChatJID": GROUP,
            "SenderName": "Private sender",
            "Timestamp": 1,
        }
        failures = (
            subprocess.TimeoutExpired("ffprobe", 30),
            subprocess.TimeoutExpired("ffmpeg", 120),
            OSError("temporary queue failure"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                seen = set()
                with (
                    mock.patch.object(voicepoll, "save_seen"),
                    mock.patch.object(voicepoll, "download_path", return_value="/private/input.mp4"),
                    mock.patch.object(voicepoll, "queue_message", side_effect=failure),
                    mock.patch.object(voicepoll, "log_event") as event,
                ):
                    voicepoll.process_message(message, seen)

                self.assertNotIn(message["MsgID"], seen)
                event.assert_called_once_with(
                    type="receive_retry",
                    msgid=message["MsgID"],
                    reason="queue failure",
                )

    def test_service_output_omits_private_message_and_path_fields(self):
        message = {
            "MsgID": "PRIVATE-MESSAGE-ID",
            "ChatJID": GROUP,
            "SenderJID": PERSON,
            "SenderName": "PRIVATE-SENDER-NAME",
            "Timestamp": "PRIVATE-TIMESTAMP",
        }
        output = io.StringIO()
        with (
            mock.patch.object(voicepoll, "save_seen"),
            mock.patch.object(
                voicepoll,
                "download_path",
                return_value="/private/download/PRIVATE-MESSAGE-ID.mp4",
            ),
            mock.patch.object(
                voicepoll,
                "queue_message",
                return_value=("/private/queue/PRIVATE-MESSAGE-ID.wav", 2),
            ),
            mock.patch.object(voicepoll, "log_event") as event,
            redirect_stdout(output),
        ):
            voicepoll.process_message(message, set())

        rendered = output.getvalue()
        for private_value in (
            message["MsgID"],
            message["SenderName"],
            message["Timestamp"],
            "/private/download/PRIVATE-MESSAGE-ID.mp4",
            "/private/queue/PRIVATE-MESSAGE-ID.wav",
        ):
            self.assertNotIn(private_value, rendered)
        event.assert_called_once_with(
            type="received",
            chat=GROUP,
            sender=message["SenderName"],
            sender_jid=PERSON,
            msgid=message["MsgID"],
            file="PRIVATE-MESSAGE-ID.wav",
            dur=2,
        )

    def test_download_failure_does_not_print_raw_wacli_output(self):
        private_output = "PRIVATE WACLI ERROR /private/download/path"
        message = {
            "MsgID": "PRIVATE-MESSAGE-ID",
            "ChatJID": GROUP,
            "SenderName": "PRIVATE-SENDER-NAME",
            "Timestamp": "PRIVATE-TIMESTAMP",
        }
        result = SimpleNamespace(returncode=1, stdout=private_output, stderr=private_output)
        output = io.StringIO()
        with (
            mock.patch.object(voicepoll, "save_seen"),
            mock.patch.object(voicepoll, "wacli", return_value=result),
            redirect_stdout(output),
        ):
            voicepoll.process_message(message, set())

        rendered = output.getvalue()
        self.assertNotIn(private_output, rendered)
        self.assertNotIn(message["MsgID"], rendered)
        self.assertNotIn(message["SenderName"], rendered)
        self.assertNotIn(message["Timestamp"], rendered)


if __name__ == "__main__":
    unittest.main()
