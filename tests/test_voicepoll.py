import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox import voicepoll

from messagebox.contacts import ContactStore
from messagebox.voicepoll import (
    MediaRejected,
    is_playable_media,
    load_contact_authorizations,
    message_is_authorized,
    parse_wacli_timestamp,
)


PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363123456789@g.us"
FIXTURE = Path(__file__).parent / "fixtures" / "wacli_v0_17_1_messages.json"


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


class MediaCompatibilityTests(unittest.TestCase):
    def fixture_messages(self):
        return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]["messages"]

    def test_wacli_0_17_1_fixture_keeps_voice_and_ordinary_video_playable(self):
        messages = {message["MsgID"]: message for message in self.fixture_messages()}

        self.assertTrue(is_playable_media(messages["AUDIO-SYNTHETIC-1"]))
        self.assertTrue(is_playable_media(messages["VIDEO-SYNTHETIC-1"]))

    def test_wacli_0_17_1_fixture_records_ptv_classification_limit(self):
        messages = {message["MsgID"]: message for message in self.fixture_messages()}

        # wacli 0.17.1 handles VideoMessage but not WhatsApp's distinct
        # PtvMessage field, so the circular note has no downloadable media
        # classification and must not be passed to `media download`.
        self.assertEqual(messages["PTV-SYNTHETIC-1"]["MediaType"], "")
        self.assertFalse(is_playable_media(messages["PTV-SYNTHETIC-1"]))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_ordinary_video_audio_is_queued_as_existing_wav_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "synthetic.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=32x32:r=10:d=0.4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
                    "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(video),
                ],
                check=True,
            )
            message = {
                "ChatJID": GROUP,
                "MsgID": "VIDEO-SYNTHETIC-1",
                "SenderJID": PERSON,
                "SenderName": "Example sender",
                "Timestamp": "2026-09-16T12:00:00Z",
                "MediaType": "video",
            }
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "data": {"path": str(video)}}),
                stderr="",
            )
            seen = set()
            with mock.patch.multiple(
                voicepoll,
                QUEUE_DIR=str(root / "queue"),
                STATE_FILE=str(root / "seen.json"),
                EVENTS_FILE=str(root / "events.jsonl"),
                EQ_FILTER="",
            ), mock.patch.object(voicepoll, "wacli", return_value=response) as command:
                self.assertTrue(voicepoll.process_message(message, seen))

            command.assert_called_once_with(
                "media", "download", "--chat", GROUP, "--id", "VIDEO-SYNTHETIC-1",
                "--lock-wait", voicepoll.LOCK_WAIT, "--json",
            )
            wavs = list((root / "queue").glob("*.wav"))
            self.assertEqual(len(wavs), 1)
            with wave.open(str(wavs[0])) as wav:
                self.assertEqual(wav.getframerate(), 48_000)
                self.assertEqual(wav.getnchannels(), 1)
                self.assertGreater(wav.getnframes(), 0)
            self.assertEqual(
                json.loads(Path(str(wavs[0]) + ".json").read_text(encoding="utf-8")),
                {
                    "chat": GROUP,
                    "msgid": "VIDEO-SYNTHETIC-1",
                    "sender_jid": PERSON,
                    "version": 1,
                },
            )
            event = json.loads((root / "events.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(event["source_media"], "video")

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_probe_rejects_video_without_audio_and_invalid_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent_video = root / "silent.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=32x32:r=10:d=0.2",
                    "-c:v", "mpeg4", str(silent_video),
                ],
                check=True,
            )
            with self.assertRaisesRegex(MediaRejected, "no_audio"):
                voicepoll.probe_audio(str(silent_video))

            invalid = root / "invalid.mp4"
            invalid.write_bytes(b"not media")
            with self.assertRaisesRegex(MediaRejected, "invalid_media"):
                voicepoll.probe_audio(str(invalid))

    def test_no_audio_and_invalid_media_do_not_block_later_messages(self):
        messages = [
            {
                "ChatJID": GROUP,
                "MsgID": "VALID-LATER",
                "SenderJID": PERSON,
                "Timestamp": 102,
                "MediaType": "audio",
            },
            {
                "ChatJID": GROUP,
                "MsgID": "NO-AUDIO-FIRST",
                "SenderJID": PERSON,
                "Timestamp": 101,
                "MediaType": "video",
            },
        ]
        listing = SimpleNamespace(
            stdout=json.dumps({"data": {"messages": messages}}), stderr="", returncode=0,
        )
        queued = []

        def queue(message, _path):
            if message["MsgID"] == "NO-AUDIO-FIRST":
                raise MediaRejected("no_audio")
            queued.append(message["MsgID"])
            return f"/queue/{message['MsgID']}.wav"

        seen = set()
        with (
            mock.patch.object(voicepoll, "wacli", return_value=listing),
            mock.patch.object(voicepoll, "download_media", return_value="/media/input"),
            mock.patch.object(voicepoll, "queue_media", side_effect=queue),
            mock.patch.object(voicepoll, "save_seen"),
            mock.patch.object(voicepoll, "log_event"),
        ):
            voicepoll.poll_once(seen, {GROUP: 100})

        self.assertEqual(queued, ["VALID-LATER"])
        self.assertEqual(seen, {"NO-AUDIO-FIRST", "VALID-LATER"})

    def test_download_failure_is_retryable_and_does_not_block_later_messages(self):
        messages = [
            {
                "ChatJID": GROUP,
                "MsgID": "VALID-LATER",
                "SenderJID": PERSON,
                "Timestamp": 102,
                "MediaType": "audio",
            },
            {
                "ChatJID": GROUP,
                "MsgID": "DOWNLOAD-FAILS-FIRST",
                "SenderJID": PERSON,
                "Timestamp": 101,
                "MediaType": "video",
            },
        ]
        listing = SimpleNamespace(
            stdout=json.dumps({"data": {"messages": messages}}), stderr="", returncode=0,
        )
        queued = []

        def download(message):
            if message["MsgID"] == "DOWNLOAD-FAILS-FIRST":
                raise RuntimeError("synthetic failure")
            return "/media/input"

        def queue(message, _path):
            queued.append(message["MsgID"])
            return f"/queue/{message['MsgID']}.wav"

        seen = set()
        with (
            mock.patch.object(voicepoll, "wacli", return_value=listing),
            mock.patch.object(voicepoll, "download_media", side_effect=download),
            mock.patch.object(voicepoll, "queue_media", side_effect=queue),
            mock.patch.object(voicepoll, "save_seen"),
        ):
            voicepoll.poll_once(seen, {GROUP: 100})

        self.assertEqual(queued, ["VALID-LATER"])
        self.assertEqual(seen, {"VALID-LATER"})

    def test_configured_size_and_duration_limits_reject_media(self):
        message = {"ChatJID": GROUP, "MsgID": "LIMITED", "MediaType": "video"}
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "input.mp4"
            media.write_bytes(b"12")
            with mock.patch.object(voicepoll, "MAX_MEDIA_BYTES", 1):
                with self.assertRaisesRegex(MediaRejected, "too_large"):
                    voicepoll.queue_media(message, str(media))

            with (
                mock.patch.object(voicepoll, "MAX_MEDIA_BYTES", 10),
                mock.patch.object(voicepoll, "MAX_MEDIA_SECONDS", 10),
                mock.patch.object(voicepoll, "probe_audio", return_value=10.1),
            ):
                with self.assertRaisesRegex(MediaRejected, "too_long"):
                    voicepoll.queue_media(message, str(media))

    def test_poll_filters_before_download_and_preserves_oldest_first(self):
        messages = [
            {"ChatJID": GROUP, "MsgID": "NEWER", "Timestamp": 104, "MediaType": "video"},
            {"ChatJID": GROUP, "MsgID": "FROM-ME", "Timestamp": 103, "MediaType": "audio", "FromMe": True},
            {"ChatJID": PERSON, "MsgID": "UNAUTHORIZED", "Timestamp": 102, "MediaType": "video"},
            {"ChatJID": GROUP, "MsgID": "OLDER", "Timestamp": 101, "MediaType": "audio"},
        ]
        listing = SimpleNamespace(
            stdout=json.dumps({"data": {"messages": messages}}), stderr="", returncode=0,
        )
        processed = []
        with (
            mock.patch.object(voicepoll, "wacli", return_value=listing),
            mock.patch.object(
                voicepoll,
                "process_message",
                side_effect=lambda message, _seen: processed.append(message["MsgID"]),
            ),
        ):
            voicepoll.poll_once({"ALREADY-SEEN"}, {GROUP: 100})

        self.assertEqual(processed, ["OLDER", "NEWER"])


if __name__ == "__main__":
    unittest.main()
