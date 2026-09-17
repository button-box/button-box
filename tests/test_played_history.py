import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

import messagebox.played_history as played_history
from messagebox.guided_reply import (
    claim_inbox_file,
    recover_inflight_files,
    release_inbox_file,
)
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

    def message(self, name, *, size=8000, chat="120363000001@g.us"):
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
                    "chat": chat,
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
        self.assertEqual(list_played_history(self.queue, now=2002), [])

        replayed = archive_played_file(self.queue, queued, played_at=2003)
        self.assertEqual(replayed, archived)
        self.assertEqual(len(list((self.queue / ".played").glob("*.wav.json"))), 1)
        self.assertFalse(list_played_history(self.queue, now=2003)[0]["queued"])
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

    def test_trashed_replay_remains_hidden_and_cannot_be_queued_twice(self):
        archived = archive_played_file(
            self.queue, self.message("1000-message.wav"), played_at=2000
        )
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2001), "queued")
        queued = next(self.queue.glob("*.wav"))
        trash = self.queue / ".trash"
        trash.mkdir()
        queued.replace(trash / queued.name)
        Path(f"{queued}.json").replace(trash / f"{queued.name}.json")

        self.assertEqual(list_played_history(self.queue, now=2002), [])
        self.assertEqual(
            requeue_played_file(self.queue, archived.name, now=2002),
            "already_queued",
        )
        self.assertEqual(list(self.queue.glob("*.wav")), [])
        self.assertEqual(len(list(trash.glob("*.wav"))), 1)

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

        self.assertEqual(
            [record["file"] for record in list_played_history(self.queue, now=4)],
            [archived.name],
        )
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=4), "queued")
        self.assertFalse((self.queue / f"{interrupted}.part").exists())
        self.assertFalse((self.queue / f"{interrupted}.json").exists())
        self.assertEqual(len(list(self.queue.glob("*.wav"))), 1)
        self.assertEqual(list_played_history(self.queue, now=4), [])
        self.assertEqual(
            requeue_played_file(self.queue, archived.name, now=5),
            "already_queued",
        )

    def test_requeue_scan_is_atomic_with_release_and_claim_transitions(self):
        archived = archive_played_file(
            self.queue, self.message("1000-message.wav"), played_at=2000
        )
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2001), "queued")
        replay = next(self.queue.glob("*.wav"))
        replay_metadata = Path(f"{replay}.json")
        hold = self.queue / ".hold"
        hold.mkdir()
        held = hold / replay.name
        held_metadata = Path(f"{held}.json")
        replay.replace(held)
        replay_metadata.replace(held_metadata)

        transition_started = threading.Event()
        transition_finished = threading.Event()
        transition_threads = []
        real_is_file = Path.is_file

        def transition():
            transition_started.set()
            release_inbox_file(self.queue, held)
            claim_inbox_file(self.queue, replay.name)
            transition_finished.set()

        def is_file(path):
            if path == held and not transition_threads:
                thread = threading.Thread(target=transition)
                transition_threads.append(thread)
                thread.start()
                self.assertTrue(transition_started.wait(1))
                self.assertFalse(transition_finished.wait(0.1))
            return real_is_file(path)

        with mock.patch.object(Path, "is_file", new=is_file):
            self.assertEqual(
                requeue_played_file(self.queue, archived.name, now=2002),
                "already_queued",
            )

        self.assertTrue(transition_finished.wait(1))
        transition_threads[0].join()
        self.assertEqual(list(self.queue.glob("*.wav")), [])
        self.assertEqual(
            [path.name for path in (self.queue / ".inflight").glob("*.wav")],
            [replay.name],
        )

    def test_archive_move_failure_keeps_replay_marker_through_recovery(self):
        archived = archive_played_file(
            self.queue, self.message("1000-message.wav"), played_at=2000
        )
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2001), "queued")
        replay = next(self.queue.glob("*.wav"))
        claimed = claim_inbox_file(self.queue, replay.name)
        real_replace = played_history.os.replace

        def replace(source, destination):
            if Path(source) == claimed and Path(destination) == archived:
                raise OSError("simulated archive move failure")
            return real_replace(source, destination)

        with mock.patch.object(played_history.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "archive move failure"):
                archive_played_file(self.queue, claimed, played_at=2002)

        metadata = json.loads(Path(f"{archived}.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["replay_name"], replay.name)
        self.assertEqual(recover_inflight_files(self.queue), [replay.name])
        self.assertEqual(
            requeue_played_file(self.queue, archived.name, now=2003),
            "already_queued",
        )
        self.assertEqual([path.name for path in self.queue.glob("*.wav")], [replay.name])
        self.assertEqual(list((self.queue / ".inflight").glob("*.wav")), [])

    def test_archive_metadata_failure_requeues_once_after_restart_recovery(self):
        archived = archive_played_file(
            self.queue, self.message("1000-message.wav"), played_at=2000
        )
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2001), "queued")
        replay = next(self.queue.glob("*.wav"))
        claimed = claim_inbox_file(self.queue, replay.name)
        archived_metadata = Path(f"{archived}.json")
        real_write_json = played_history._write_json
        destination_writes = 0

        def write_json(path, value):
            nonlocal destination_writes
            if path == archived_metadata:
                destination_writes += 1
            if path == archived_metadata and destination_writes == 2:
                raise OSError("simulated metadata commit failure")
            return real_write_json(path, value)

        with mock.patch.object(played_history, "_write_json", side_effect=write_json):
            with self.assertRaisesRegex(OSError, "metadata commit failure"):
                archive_played_file(self.queue, claimed, played_at=2002)

        metadata = json.loads(archived_metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata["replay_name"], replay.name)
        self.assertFalse(claimed.exists())
        self.assertEqual(recover_inflight_files(self.queue), [])
        self.assertEqual(requeue_played_file(self.queue, archived.name, now=2003), "queued")
        queued = list(self.queue.glob("*.wav"))
        self.assertEqual(len(queued), 1)
        self.assertNotEqual(queued[0].name, replay.name)
        self.assertFalse(Path(f"{self.queue / replay.name}.json").exists())

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
            ("route", "120363000001@g.us"),
        )
        self.assertEqual(
            recent_reply_recipient(self.queue, set(), now=2001),
            ("blocked", None),
        )
        self.assertEqual(
            recent_reply_recipient(self.queue, allowed, now=5601),
            ("fallback", None),
        )
        self.assertEqual(
            recent_reply_recipient(self.queue / "empty", allowed, now=2001),
            ("fallback", None),
        )

    def test_active_newest_replay_remains_the_recent_route_in_every_queue_state(self):
        older_chat = "120363000001@g.us"
        newest_chat = "120363000002@g.us"
        archive_played_file(
            self.queue,
            self.message("1000-older.wav", chat=older_chat),
            played_at=1000,
        )
        newest = archive_played_file(
            self.queue,
            self.message("2000-newest.wav", chat=newest_chat),
            played_at=2000,
        )
        self.assertEqual(requeue_played_file(self.queue, newest.name, now=2001), "queued")
        replay = next(self.queue.glob("*.wav"))
        replay_metadata = Path(f"{replay}.json")
        allowed = {older_chat, newest_chat}

        for state in ("queue", ".inflight", ".hold", ".trash"):
            with self.subTest(state=state):
                self.assertEqual(
                    recent_reply_recipient(self.queue, allowed, now=2001),
                    ("route", newest_chat),
                )
                self.assertEqual(list_played_history(self.queue, now=2001)[0]["file"], "1000-older.wav")
            if state != ".trash":
                next_state = {
                    "queue": ".inflight",
                    ".inflight": ".hold",
                    ".hold": ".trash",
                }[state]
                destination = self.queue / next_state
                destination.mkdir(exist_ok=True)
                moved = destination / replay.name
                moved_metadata = Path(f"{moved}.json")
                replay.replace(moved)
                replay_metadata.replace(moved_metadata)
                replay, replay_metadata = moved, moved_metadata

    def test_invalid_or_removed_newest_route_never_selects_an_older_sender(self):
        allowed_chat = "120363000001@g.us"
        for newest_chat in ("120363000002@g.us", "not-a-chat"):
            with self.subTest(newest_chat=newest_chat), tempfile.TemporaryDirectory() as directory:
                queue = Path(directory) / "queue"
                queue.mkdir()
                archive_played_file(
                    queue,
                    self.message("1000-older.wav", chat=allowed_chat),
                    played_at=1000,
                )
                # The helper creates in self.queue, so move this synthetic newest
                # source into the isolated queue before archiving it.
                source = self.message("2000-newest.wav", chat=newest_chat)
                source_metadata = Path(f"{source}.json")
                isolated_source = queue / source.name
                isolated_metadata = Path(f"{isolated_source}.json")
                source.replace(isolated_source)
                source_metadata.replace(isolated_metadata)
                archive_played_file(queue, isolated_source, played_at=2000)

                self.assertEqual(
                    recent_reply_recipient(queue, {allowed_chat}, now=2001),
                    ("blocked", None),
                )


if __name__ == "__main__":
    unittest.main()
