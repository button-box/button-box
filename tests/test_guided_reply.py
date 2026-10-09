import json
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from messagebox.guided_reply import (
    EnergyVAD,
    GuidedSession,
    OutboxStore,
    RecordingResult,
    claim_inbox_file,
    discard_held_playback_press,
    env_flag,
    finish_inbox_file,
    invalid_prompt_files,
    raw_pcm_to_trimmed_wav,
    recover_inflight_files,
    release_inbox_file,
    voice_send_command,
)


def pcm_frame(level, samples=320):
    return struct.pack("<" + "h" * samples, *([level] * samples))


def write_wav(path, seconds=0.25, level=1000, rate=16000):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm_frame(level, int(seconds * rate)))


class PressPolicyTests(unittest.TestCase):
    def test_env_flag_defaults_off_and_parses_boolean_values(self):
        self.assertFalse(env_flag("FEATURE", environ={}))
        self.assertTrue(env_flag("FEATURE", environ={"FEATURE": "1"}))
        self.assertFalse(env_flag("FEATURE", environ={"FEATURE": "0"}))

    def test_prompt_gate_rejects_missing_and_non_audio_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.wav"
            invalid = Path(directory) / "invalid.wav"
            missing = Path(directory) / "missing.wav"
            write_wav(valid)
            invalid.write_text("not audio", encoding="utf-8")
            self.assertEqual(
                invalid_prompt_files([valid, invalid, missing]),
                [str(invalid), str(missing)],
            )

    def test_open_button_after_playback_has_no_approval_dead_zone(self):
        waits = []
        discarded = discard_held_playback_press(lambda: False, lambda: waits.append(True))
        self.assertFalse(discarded)
        self.assertEqual(waits, [])

    def test_press_held_at_playback_end_is_discarded(self):
        waits = []
        discarded = discard_held_playback_press(lambda: True, lambda: waits.append(True))
        self.assertTrue(discarded)
        self.assertEqual(waits, [True])


class VADTests(unittest.TestCase):
    def test_twenty_seconds_of_silence_stops_without_meaningful_speech(self):
        vad = EnergyVAD(silence_seconds=20)
        vad.start(0)
        for i in range(25):
            vad.feed(pcm_frame(40), now=i * 0.02)
        self.assertFalse(vad.meaningful)
        self.assertFalse(vad.silence_expired(19.99))
        self.assertTrue(vad.silence_expired(20.0))

    def test_speech_resets_silence_timer_and_has_no_total_cap(self):
        vad = EnergyVAD(silence_seconds=20)
        vad.start(0)
        for i in range(12):
            vad.feed(pcm_frame(3000), now=100 + i * 0.02)
        self.assertTrue(vad.meaningful)
        self.assertFalse(vad.silence_expired(119.99))
        self.assertTrue(vad.silence_expired(120.22))
        vad.feed(pcm_frame(3000), now=121)
        self.assertFalse(vad.silence_expired(140.99))

    def test_click_is_not_meaningful_and_trim_keeps_internal_silence(self):
        vad = EnergyVAD()
        vad.start(0)
        vad.feed(pcm_frame(5000), now=0.02)
        for i in range(8):
            vad.feed(pcm_frame(30), now=0.04 + i * 0.02)
        self.assertFalse(vad.meaningful)

        vad = EnergyVAD()
        vad.start(0)
        raw = bytearray()
        levels = [20] * 15 + [3000] * 12 + [20] * 20 + [3000] * 12 + [20] * 20
        for i, level in enumerate(levels):
            frame = pcm_frame(level)
            raw.extend(frame)
            vad.feed(frame, now=i * 0.02)
        bounds = vad.trim_bounds()
        self.assertIsNotNone(bounds)
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "capture.raw"
            wav_path = Path(directory) / "capture.wav"
            raw_path.write_bytes(raw)
            duration = raw_pcm_to_trimmed_wav(str(raw_path), str(wav_path), bounds)
            # The 400 ms internal pause remains; only the outside silence shrinks.
            self.assertGreater(duration, 0.75)
            self.assertLess(duration, len(levels) * 0.02)


class OutboxTests(unittest.TestCase):
    def test_approval_binds_exact_recipient_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            write_wav(source)
            store = OutboxStore(str(Path(directory) / "outbox"))
            first = store.approve(
                str(source),
                "exact-origin@s.whatsapp.net",
                "reply",
                0.25,
                message_id="stable-id",
            )
            second = store.approve(
                str(source),
                "exact-origin@s.whatsapp.net",
                "reply",
                0.25,
                message_id="stable-id",
            )
            self.assertEqual(first.path, second.path)
            self.assertEqual(len(store.jobs()), 1)
            self.assertEqual(store.jobs()[0].recipient, "exact-origin@s.whatsapp.net")
            with self.assertRaises(ValueError):
                store.approve(
                    str(source),
                    "wrong-recipient@g.us",
                    "reply",
                    0.25,
                    message_id="stable-id",
                )

    def test_cloud_approval_scope_is_immutable_and_missing_scope_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_wav(source)
            store = OutboxStore(str(root / "outbox"), transport="cloud")
            original = source.read_bytes()
            with self.assertRaises(ValueError):
                store.approve(str(source), "family@example.invalid", "reply", 0.25)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(store.root.iterdir()), [])
            job = store.approve(str(source), "family@example.invalid", "reply", 0.25,
                                message_id="same-recording", account_scope="a" * 64)
            before = {p.name: p.read_bytes() for p in job.path.iterdir()}
            with self.assertRaises(ValueError):
                store.approve(str(source), job.recipient, "reply", 0.25,
                              message_id=job.message_id, account_scope="b" * 64)
            self.assertEqual({p.name: p.read_bytes() for p in job.path.iterdir()}, before)

    def test_send_command_uses_bound_recipient_without_fallback(self):
        command = voice_send_command(
            "/usr/local/bin/wacli",
            "/tmp/message.ogg",
            "exact-origin@s.whatsapp.net",
            "60s",
        )
        self.assertEqual(command[command.index("--to") + 1], "exact-origin@s.whatsapp.net")
        self.assertNotIn("configured-family-group@g.us", command)
        with self.assertRaises(ValueError):
            voice_send_command("wacli", "/tmp/message.ogg", "", "60s")

    def test_restart_removes_unapproved_and_quarantines_ambiguous_send(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            write_wav(source)
            store = OutboxStore(str(Path(directory) / "outbox"))
            pending = store.approve(str(source), "family@g.us", "standalone", 0.25)
            sending = store.approve(str(source), "person@s.whatsapp.net", "reply", 0.25)
            store.set_state(sending, "sending")
            staging = Path(store.root) / ".abandoned.tmp"
            staging.mkdir()
            (staging / "audio.wav").write_bytes(b"partial")
            uncertain = store.recover_startup()
            self.assertEqual(uncertain, [sending.message_id])
            self.assertFalse(staging.exists())
            self.assertEqual(store.load(pending.path).state, "pending")
            self.assertEqual(store.load(sending.path).state, "uncertain")

    def test_transport_scopes_pending_jobs_in_both_directions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_wav(source)
            cloud = OutboxStore(str(root / "outbox"), transport="cloud")
            wacli = OutboxStore(str(root / "outbox"), transport="wacli")
            cloud_job = cloud.approve(
                str(source), "12025550101@s.whatsapp.net", "reply", 0.25,
                message_id="cloud-job", account_scope="a" * 64,
            )
            self.assertEqual([job.message_id for job in cloud.jobs()], [cloud_job.message_id])
            self.assertEqual(wacli.jobs(), [])
            with self.assertRaises(ValueError):
                wacli.approve(
                    str(source), cloud_job.recipient, "reply", 0.25,
                    message_id=cloud_job.message_id, account_scope="a" * 64,
                )

            wacli_job = wacli.approve(
                str(source), "family@g.us", "standalone", 0.25,
                message_id="wacli-job",
            )
            self.assertEqual([job.message_id for job in cloud.jobs()], [cloud_job.message_id])
            self.assertEqual([job.message_id for job in wacli.jobs()], [wacli_job.message_id])

    def test_missing_transport_remains_wacli_and_unknown_metadata_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_wav(source)
            wacli = OutboxStore(str(root / "outbox"), transport="wacli")
            job = wacli.approve(str(source), "family@g.us", "standalone", 0.25)
            metadata_path = job.path / "job.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.pop("transport")
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            self.assertEqual(wacli.jobs()[0].transport, "wacli")
            self.assertEqual(OutboxStore(str(root / "outbox"), transport="cloud").jobs(), [])

            metadata["transport"] = []
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            self.assertEqual(wacli.jobs(), [])
            self.assertTrue(job.path.exists())
            self.assertTrue(job.audio_path.exists())

    def test_recovery_only_quarantines_current_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_wav(source)
            cloud = OutboxStore(str(root / "outbox"), transport="cloud")
            wacli = OutboxStore(str(root / "outbox"), transport="wacli")
            cloud_job = cloud.set_state(
                cloud.approve(str(source), "12025550101@s.whatsapp.net", "reply", 0.25, account_scope="a" * 64),
                "sending",
            )
            wacli_job = wacli.set_state(
                wacli.approve(str(source), "family@g.us", "standalone", 0.25),
                "sending",
            )

            self.assertEqual(wacli.recover_startup(), [wacli_job.message_id])
            self.assertEqual(cloud.load(cloud_job.path).state, "sending")
            self.assertEqual(cloud.recover_startup(), [cloud_job.message_id])
            self.assertEqual(wacli.load(wacli_job.path).state, "uncertain")


class InboxRestartTests(unittest.TestCase):
    def test_interrupted_inbound_returns_to_front_with_routing_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            wav = queue / "0001-message.wav"
            meta = Path(str(wav) + ".json")
            write_wav(wav)
            meta.write_text(json.dumps({"chat": "origin@g.us"}), encoding="utf-8")
            claimed = claim_inbox_file(directory, wav.name)
            self.assertFalse(wav.exists())
            self.assertTrue(claimed.exists())
            recovered = recover_inflight_files(directory)
            self.assertEqual(recovered, [wav.name])
            self.assertTrue(wav.exists())
            self.assertEqual(json.loads(meta.read_text())["chat"], "origin@g.us")

    def test_release_and_finish_are_scoped_to_claimed_message(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            first = queue / "0001.wav"
            second = queue / "0002.wav"
            write_wav(first)
            write_wav(second)
            claimed = claim_inbox_file(directory, first.name)
            release_inbox_file(directory, claimed)
            self.assertEqual(
                sorted(path.name for path in queue.glob("*.wav")),
                ["0001.wav", "0002.wav"],
            )
            claimed = claim_inbox_file(directory, first.name)
            finish_inbox_file(claimed)
            self.assertEqual(
                sorted(path.name for path in queue.glob("*.wav")), ["0002.wav"]
            )


class FakeIO:
    def __init__(self, recordings, approve_initial=False, approve_warning=False, approve_review=False):
        self.recordings = list(recordings)
        self.approve_initial = approve_initial
        self.approve_warning = approve_warning
        self.calls = []
        self.approve_review = approve_review

    def play_review_for_approval(self, path):
        self.calls.append(("review", os.path.basename(path)))
        return self.approve_review

    def play_ordinary(self, path):
        self.calls.append(("ordinary", os.path.basename(path)))
        # Any simulated press here has no return channel and therefore cannot
        # leak into the next state.

    def record(self):
        self.calls.append(("record",))
        return self.recordings.pop(0)

    def wait_for_approval(self, timeout):
        self.calls.append(("wait", timeout))
        return self.approve_initial

    def play_warning_for_approval(self, path):
        self.calls.append(("warning", os.path.basename(path)))
        return self.approve_warning

    def delete(self, path):
        self.calls.append(("delete", os.path.basename(path)))


class SessionTests(unittest.TestCase):
    def run_session(self, root, recording, *, transport="wacli", review=True,
                    approval=False, incoming=False):
        io = FakeIO([recording], approve_initial=approval)
        store = OutboxStore(root / "outbox", transport=transport)
        events = []
        result = GuidedSession(io, store, lambda kind, **data: events.append((kind, data))).run(
            recipient="origin@example.invalid", flow_kind="reply" if incoming else "standalone",
            deleted_cue_path="cue-deleted.wav", review_before_send=review,
            incoming_path="incoming.wav" if incoming else None,
            account_scope="a" * 64 if transport == "cloud" else None,
            session_id="stable-session-id")
        self.assertTrue(all(data["session_id"] == "stable-session-id" for _, data in events))
        return result, io, store, events

    def test_every_mode_review_row_and_exact_routing_in_both_transports(self):
        for transport in ("wacli", "cloud"):
            for talk in ("tap", "hold"):
                for review in (False, True):
                    for incoming in (False, True):
                        with self.subTest(transport=transport, talk=talk, review=review, incoming=incoming), tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            source = root / "child.wav"
                            write_wav(source, seconds=2)
                            result, io, store, events = self.run_session(root,
                                RecordingResult(str(source), 2, True), transport=transport,
                                review=review, approval=True, incoming=incoming)
                            self.assertEqual(result, "approved")
                            self.assertEqual(len(store.jobs()), 1)
                            self.assertEqual(store.jobs()[0].recipient, "origin@example.invalid")
                            self.assertEqual(store.jobs()[0].transport, transport)
                            prefix = [("ordinary", "incoming.wav")] if incoming else []
                            expected = prefix + [("record",)]
                            if review:
                                expected += [("ordinary", "child.wav"), ("wait", 10.0)]
                            self.assertEqual(io.calls, expected + [("delete", "child.wav")])
                            self.assertEqual(sum(kind == "guided_approved" for kind, _ in events), 1)

    def test_review_timeout_deletes_in_both_modes_and_transports(self):
        for transport in ("wacli", "cloud"):
            for talk in ("tap", "hold"):
                with self.subTest(transport=transport, talk=talk), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "child.wav"
                    write_wav(source, seconds=2)
                    result, io, store, _ = self.run_session(root, RecordingResult(str(source), 2, True), transport=transport)
                    self.assertEqual(result, "deleted")
                    self.assertEqual(io.calls, [("record",), ("ordinary", "child.wav"), ("wait", 10.0),
                                               ("delete", "child.wav"), ("ordinary", "cue-deleted.wav")])
                    self.assertFalse(store.jobs())

    def test_tap_recording_timeout_never_reviews_or_sends(self):
        for transport in ("wacli", "cloud"):
            for review in (False, True):
                with self.subTest(transport=transport, review=review), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "child.wav"
                    write_wav(source, seconds=2)
                    result, io, store, _ = self.run_session(root,
                        RecordingResult(str(source), 30, True, timed_out=True), transport=transport, review=review, approval=True)
                    self.assertEqual(result, "deleted")
                    self.assertEqual(io.calls, [("record",), ("delete", "child.wav"), ("ordinary", "cue-deleted.wav")])
                    self.assertFalse(store.jobs())

    def test_short_or_silent_deletion_in_every_mode_review_row_and_transport(self):
        for transport in ("wacli", "cloud"):
            for talk in ("tap", "hold"):
                for review in (False, True):
                    for duration, meaningful in ((1.499, True), (2, False)):
                        with self.subTest(transport=transport, talk=talk, review=review, duration=duration, meaningful=meaningful), tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            source = root / "child.wav"
                            write_wav(source, seconds=2)
                            result, io, store, events = self.run_session(root,
                                RecordingResult(str(source), duration, meaningful), transport=transport, review=review, approval=True)
                            self.assertEqual(result, "empty")
                            self.assertEqual(io.calls, [("record",), ("delete", "child.wav"), ("ordinary", "cue-deleted.wav")])
                            self.assertFalse(store.jobs())
                            self.assertIn("guided_recording_empty", [kind for kind, _ in events])

    def test_minimum_is_inclusive_and_measured_before_silence_trimming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "child.wav"
            write_wav(source)
            result, _, store, _ = self.run_session(root,
                RecordingResult(str(source), .5, True, recorded_seconds=1.5), review=False)
            self.assertEqual(result, "approved")
            self.assertEqual(len(store.jobs()), 1)

    def test_inbound_play_only_does_not_record(self):
        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO([])
            store = OutboxStore(directory)
            result = GuidedSession(io, store, lambda *a, **kw: None).run(
                recipient="origin@example.invalid", flow_kind="reply", deleted_cue_path="cue-deleted.wav",
                incoming_path="incoming.wav", auto_record_after_incoming=False)
            self.assertEqual(result, "played")
            self.assertEqual(io.calls, [("ordinary", "incoming.wav")])
            self.assertFalse(store.jobs())


if __name__ == "__main__":
    unittest.main()
