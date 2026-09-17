import json
import tempfile
import unittest
import wave
from pathlib import Path

from messagebox.played_history import (
    RETENTION_SECONDS,
    archive_played_file,
    list_played_history,
    recent_reply_recipient,
    read_played_file,
    requeue_played_file,
)


class PlayedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.queue = Path(self.temporary.name) / "queue"
        self.queue.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def message(self, name, *, size=8000):
        path = self.queue / name
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * size)
        Path(f"{path}.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "chat": "120363000001@g.us",
                    "msgid": name,
                    "sender_jid": "15551234567@s.whatsapp.net",
                    "media_type": "audio",
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_archive_preserves_route_and_survives_requeue_restart(self):
        source = self.message("1000-message.wav")
        archived = archive_played_file(self.queue, source, played_at=2000)

        self.assertFalse(source.exists())
        metadata = json.loads(Path(f"{archived}.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["chat"], "120363000001@g.us")
        self.assertEqual(metadata["msgid"], "1000-message.wav")
        self.assertEqual(metadata["played_at"], 2000)
        self.assertEqual(metadata["duration_s"], 1)

        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2001), "queued")
        queued = next(self.queue.glob("*.wav"))
        self.assertNotEqual(queued.name, archived.name)
        self.assertEqual(
            requeue_played_file(self.queue, archived.name, now=2002),
            "already_queued",
        )
        queued_metadata = json.loads(
            Path(f"{queued}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(queued_metadata["chat"], metadata["chat"])
        self.assertEqual(queued_metadata["msgid"], metadata["msgid"])
        self.assertEqual(queued_metadata["replay_history_file"], archived.name)
        self.assertTrue(list_played_history(self.queue, now=2002)[0]["queued"])

        replayed = archive_played_file(self.queue, queued, played_at=2003)
        self.assertEqual(replayed, archived)
        self.assertEqual(len(list((self.queue / ".played").glob("*.wav.json"))), 1)
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2004), "queued")

    def test_replays_append_after_waiting_items_and_keep_request_order(self):
        first = archive_played_file(
            self.queue, self.message("1000-first.wav"), played_at=2000
        )
        second = archive_played_file(
            self.queue, self.message("1001-second.wav"), played_at=2001
        )
        waiting = self.message("3000-waiting.wav")

        self.assertEqual(requeue_played_file(self.queue, first.name, now=4), "queued")
        self.assertEqual(requeue_played_file(self.queue, second.name, now=4), "queued")

        ordered = sorted(path.name for path in self.queue.glob("*.wav"))
        self.assertEqual(ordered[0], waiting.name)
        self.assertIn("-replay-", ordered[1])
        self.assertIn("-replay-", ordered[2])
        self.assertLess(ordered[1], ordered[2])

    def test_interrupted_publication_recovers_without_phantom_duplicate(self):
        archived = archive_played_file(
            self.queue, self.message("1000-message.wav"), played_at=2000
        )
        metadata_path = Path(f"{archived}.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        interrupted = "3000-replay-interrupted.wav"
        metadata.update(replay_queued_at=3, replay_name=interrupted)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        (self.queue / f"{interrupted}.part").write_bytes(b"partial")
        (self.queue / f"{interrupted}.json").write_text("{}", encoding="utf-8")

        self.assertEqual(requeue_played_file(self.queue, archived.name, now=4), "queued")
        self.assertFalse((self.queue / f"{interrupted}.part").exists())
        self.assertFalse((self.queue / f"{interrupted}.json").exists())
        self.assertEqual(len(list(self.queue.glob("*.wav"))), 1)
        self.assertEqual(
            requeue_played_file(self.queue, archived.name, now=5),
            "already_queued",
        )

    def test_expired_media_cannot_be_read_or_requeued_without_later_archive(self):
        archived = archive_played_file(
            self.queue, self.message("1000-message.wav"), played_at=100
        )
        expired_at = 100 + RETENTION_SECONDS + 1

        with self.assertRaises(FileNotFoundError):
            read_played_file(self.queue, archived.name, now=expired_at)
        with self.assertRaises(FileNotFoundError):
            requeue_played_file(self.queue, archived.name, now=expired_at)
        self.assertFalse(archived.exists())
        self.assertFalse(Path(f"{archived}.json").exists())

    def test_retention_keeps_bounded_metadata_and_marks_pruned_media(self):
        for index in range(4):
            source = self.message(f"{1000 + index}-message.wav", size=100)
            archive_played_file(
                self.queue,
                source,
                played_at=2000 + index,
                metadata_limit=3,
                media_limit=1,
                retention_seconds=100,
                media_bytes_limit=100000,
            )

        metadata = list((self.queue / ".played").glob("*.wav.json"))
        media = list((self.queue / ".played").glob("*.wav"))
        self.assertEqual(len(metadata), 3)
        self.assertEqual(len(media), 1)
        records = list_played_history(self.queue, now=2003)
        self.assertEqual([record["played_at"] for record in records], [2003, 2002, 2001])
        self.assertEqual([record["available"] for record in records], [True, False, False])

    def test_unroutable_history_cannot_be_requeued(self):
        for index, route in enumerate(
            (None, "", "not-a-chat", "12345@example.com", " 12345@g.us ")
        ):
            with self.subTest(route=route):
                source = self.message(f"{1000 + index}-message.wav")
                metadata = {} if route is None else {"chat": route}
                Path(f"{source}.json").write_text(json.dumps(metadata), encoding="utf-8")
                archived = archive_played_file(self.queue, source, played_at=2000)
                with self.assertRaisesRegex(ValueError, "reply route"):
                    requeue_played_file(self.queue, archived.name, now=2001)

    def test_recent_reply_uses_only_fresh_authorized_latest_route(self):
        source = self.message("2000-message.wav")
        archive_played_file(self.queue, source, played_at=2000)
        allowed = {"120363000001@g.us"}

        self.assertEqual(
            recent_reply_recipient(self.queue, allowed, now=2001),
            "120363000001@g.us",
        )
        self.assertIsNone(recent_reply_recipient(self.queue, set(), now=2001))
        self.assertIsNone(recent_reply_recipient(self.queue, allowed, now=5601))


if __name__ == "__main__":
    unittest.main()
