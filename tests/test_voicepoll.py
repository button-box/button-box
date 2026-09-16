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
    load_contact_authorizations,
    message_is_authorized,
    parse_wacli_timestamp,
)


PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363123456789@g.us"


def make_message(msgid, media_type, timestamp, *, chat=GROUP, sender=PERSON):
    return {
        "ChatJID": chat,
        "MsgID": msgid,
        "SenderJID": sender,
        "SenderName": "Synthetic sender",
        "Timestamp": timestamp,
        "FromMe": False,
        "MediaType": media_type,
    }


def successful_download(path):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True, "data": {"path": str(path)}}),
        stderr="",
    )


def create_synthetic_media(path, *, audio=True, duration=0.25):
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=blue:s=32x32:d={duration}",
    ]
    if audio:
        command.extend([
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:a", "aac",
        ])
    command.extend(["-c:v", "mpeg4", str(path)])
    subprocess.run(command, check=True, capture_output=True)


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


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required",
)
class IncomingMediaTests(unittest.TestCase):
    def runtime_patches(self, root, **overrides):
        values = {
            "QUEUE_DIR": str(root / "queue"),
            "STATE_FILE": str(root / "state" / "seen.json"),
            "EVENTS_FILE": str(root / "state" / "events.jsonl"),
            "EQ_FILTER": "",
            "MAX_MEDIA_BYTES": 104_857_600,
            "MAX_MEDIA_SECONDS": 1800,
        }
        values.update(overrides)
        return mock.patch.multiple(voicepoll, **values)

    def read_events(self, root):
        path = root / "state" / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_voice_note_and_ordinary_video_use_the_same_ordered_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "synthetic-video.mp4"
            voice = root / "synthetic-voice.mp4"
            create_synthetic_media(video)
            create_synthetic_media(voice)
            messages = [
                make_message("VOICE-NEWER", "audio", 102),
                make_message("VIDEO-OLDER", "video", 101),
            ]
            downloads = {
                "VIDEO-OLDER": video,
                "VOICE-NEWER": voice,
            }

            def run_wacli(*args):
                msgid = args[args.index("--id") + 1]
                return successful_download(downloads[msgid])

            with (
                self.runtime_patches(root),
                mock.patch.object(voicepoll, "wacli", side_effect=run_wacli) as cli,
            ):
                seen = set()
                voicepoll.process_messages(messages, seen, {GROUP: 100})

            self.assertEqual(seen, {"VIDEO-OLDER", "VOICE-NEWER"})
            self.assertEqual(
                [call.args[call.args.index("--id") + 1] for call in cli.call_args_list],
                ["VIDEO-OLDER", "VOICE-NEWER"],
            )
            queued = list((root / "queue").glob("*.wav"))
            self.assertEqual(len(queued), 2)
            for path in queued:
                with wave.open(str(path)) as queued_wave:
                    self.assertEqual(queued_wave.getframerate(), 48_000)
                    self.assertEqual(queued_wave.getnchannels(), 1)
            metadata = [
                json.loads(Path(str(path) + ".json").read_text())
                for path in queued
            ]
            self.assertEqual({item["chat"] for item in metadata}, {GROUP})
            self.assertEqual({item["sender_jid"] for item in metadata}, {PERSON})
            received = [event["msgid"] for event in self.read_events(root) if event["type"] == "received"]
            self.assertEqual(received, ["VIDEO-OLDER", "VOICE-NEWER"])

    def test_media_failures_do_not_block_a_later_voice_note(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.mp4"
            silent = root / "silent.mp4"
            valid = root / "valid.mp4"
            invalid.write_text("not media")
            create_synthetic_media(silent, audio=False)
            create_synthetic_media(valid)
            messages = [
                make_message("VALID-LAST", "audio", 104),
                make_message("DOWNLOAD-FAILS", "video", 103),
                make_message("SILENT", "video", 102),
                make_message("INVALID", "video", 101),
            ]

            def run_wacli(*args):
                msgid = args[args.index("--id") + 1]
                if msgid == "DOWNLOAD-FAILS":
                    return SimpleNamespace(returncode=1, stdout="", stderr="private detail")
                return successful_download({"INVALID": invalid, "SILENT": silent, "VALID-LAST": valid}[msgid])

            with (
                self.runtime_patches(root),
                mock.patch.object(voicepoll, "wacli", side_effect=run_wacli),
            ):
                seen = set()
                voicepoll.process_messages(messages, seen, {GROUP: 100})

            self.assertEqual(seen, {"INVALID", "SILENT", "VALID-LAST"})
            queued = list((root / "queue").glob("*.wav"))
            self.assertEqual(len(queued), 1)
            self.assertIn("VALID-LAST", queued[0].name)
            events = self.read_events(root)
            self.assertEqual(
                [(event["type"], event.get("reason")) for event in events],
                [
                    ("receive_skipped", "invalid_media"),
                    ("receive_skipped", "no_audio_track"),
                    ("receive_retry", "download_failed"),
                    ("received", None),
                ],
            )

    def test_configured_size_and_duration_limits_skip_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "synthetic.mp4"
            create_synthetic_media(media, duration=0.4)

            with (
                self.runtime_patches(root, MAX_MEDIA_BYTES=1),
                mock.patch.object(voicepoll, "wacli", return_value=successful_download(media)),
            ):
                self.assertEqual(
                    voicepoll.process_message(make_message("TOO-LARGE", "video", 101), set()),
                    "skipped",
                )
            self.assertEqual(self.read_events(root)[-1]["reason"], "media_too_large")

            with (
                self.runtime_patches(root, MAX_MEDIA_SECONDS=0.1),
                mock.patch.object(voicepoll, "wacli", return_value=successful_download(media)),
            ):
                self.assertEqual(
                    voicepoll.process_message(make_message("TOO-LONG", "video", 102), set()),
                    "skipped",
                )
            self.assertEqual(self.read_events(root)[-1]["reason"], "media_too_long")

    def test_deduplication_and_authorization_run_before_download(self):
        messages = [
            make_message("UNAUTHORIZED", "video", 105, chat=PERSON),
            make_message("SEEN", "video", 104),
            make_message("UNSUPPORTED", "image", 103),
        ]
        with mock.patch.object(voicepoll, "wacli") as cli:
            voicepoll.process_messages(messages, {"SEEN"}, {GROUP: 100})
        cli.assert_not_called()

    def test_positive_limits_are_required(self):
        with mock.patch.dict(voicepoll.os.environ, {"LIMIT": "0"}):
            with self.assertRaises(ValueError):
                voicepoll.positive_setting("LIMIT", 1, int)


if __name__ == "__main__":
    unittest.main()
