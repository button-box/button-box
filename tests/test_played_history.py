import json
import tempfile
import unittest
import wave
from pathlib import Path

from messagebox.played_history import (
    archive_played_file,
    list_played_history,
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
        self.assertEqual(
            requeue_played_file(self.queue, archived.name, now=2002),
            "already_queued",
        )
        queued_metadata = json.loads(
            Path(f"{self.queue / archived.name}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(queued_metadata["chat"], metadata["chat"])
        self.assertEqual(queued_metadata["msgid"], metadata["msgid"])
        self.assertTrue(list_played_history(self.queue, now=2002)[0]["queued"])

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
        source = self.message("1000-message.wav")
        Path(f"{source}.json").write_text("{}", encoding="utf-8")
        archived = archive_played_file(self.queue, source, played_at=2000)
        with self.assertRaisesRegex(ValueError, "reply route"):
            requeue_played_file(self.queue, archived.name, now=2001)


if __name__ == "__main__":
    unittest.main()
