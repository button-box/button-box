"""Every shared WAV playback starts with silence while the USB speaker wakes."""

from speaker_helpers import isolate_audio

import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from messagebox import speaker
from messagebox.guided_reply import GuidedSession, OutboxStore
from test_mic_access import runtime


def write_wav(path, frames, rate=48000, width=2, channels=1):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)


class LeadInTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def test_sync_playback_in_both_modes_has_300_ms_then_exact_pcm(self):
        for mode in ('cloud', 'wacli'):
            for channels, width in ((1, 1), (1, 2), (2, 2), (1, 3), (1, 4)):
                with self.subTest(mode=mode, width=width, channels=channels), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / 'voice.wav'
                    body = bytes(range(1, 201)) * 120
                    write_wav(source, body, rate=16000, width=width, channels=channels)
                    original = source.read_bytes()
                    played = []
                    def run(command, **kwargs):
                        played.append(Path(command[-1]))
                        self.assertTrue(speaker.is_busy())
                        with wave.open(command[-1], 'rb') as audio:
                            self.assertEqual(audio.getparams()[:3], (channels, width, 16000))
                            lead = 4800
                            self.assertEqual(audio.getnframes(), len(body) // (width * channels) + lead)
                            self.assertEqual(audio.readframes(lead),
                                             (b'\x80' if width == 1 else b'\0') * lead * width * channels)
                            self.assertEqual(audio.readframes(len(body) // (width * channels)), body)
                        return subprocess.CompletedProcess(command, 0)
                    with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': mode}):
                        speaker.run(['aplay', '-q', '-D', 'synthetic', str(source)], runner=run, check=True)
                    self.assertNotEqual(played[0], source)
                    self.assertFalse(played[0].exists())
                    self.assertEqual(source.read_bytes(), original)
                    self.assertFalse(speaker.is_busy())

    def test_async_copy_exists_until_reaped_and_keeps_the_speaker_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cue.wav'
            write_wav(source, b'\x01\x02' * 4800)
            process = mock.Mock()
            process.poll.return_value = None
            process.wait.return_value = 0
            launch = mock.Mock(return_value=process)
            player = speaker.start_player(['aplay', '-q', str(source)], popen=launch)
            padded = Path(launch.call_args.args[0][-1])
            self.assertNotEqual(padded, source)
            self.assertTrue(padded.exists())
            with wave.open(str(padded), 'rb') as audio:
                self.assertEqual(audio.readframes(14400), b'\0\0' * 14400)
                self.assertEqual(audio.readframes(4800), b'\x01\x02' * 4800)
            self.assertIsNone(player.poll())
            self.assertTrue(speaker.is_busy())
            player.wait(timeout=2)
            self.assertFalse(padded.exists())
            self.assertFalse(speaker.is_busy())

    def test_unreadable_or_non_wav_input_keeps_the_original_command(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, data in (('text.wav', b'not a wav'), ('missing.wav', None)):
                path = Path(directory) / name
                if data is not None:
                    path.write_bytes(data)
                command = ['aplay', '-q', str(path)]
                run = mock.Mock()
                speaker.run(command, runner=run)
                self.assertEqual(run.call_args.args[0], command)
                self.assertFalse(speaker.is_busy())

    def test_temporary_copy_is_removed_on_launch_and_playback_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'voice.wav'
            write_wav(source, b'\x01\x02' * 4800)
            copies = []
            def fail(command, **kwargs):
                copies.append(Path(command[-1]))
                self.assertNotEqual(copies[-1], source)
                self.assertTrue(copies[-1].exists())
                raise OSError('synthetic speaker failure')
            for play, options in ((speaker.run, {'runner': fail}),
                                  (speaker.start_player, {'popen': fail})):
                with self.assertRaises(OSError):
                    play(['aplay', '-q', str(source)], **options)
                self.assertFalse(copies[-1].exists())
                self.assertFalse(speaker.is_busy())


class IO:
    def __init__(self, lead_in=True):
        self.calls = []
        if lead_in:
            self.play_after_cue = lambda path: self.calls.append(("after_cue", path))

    def play_ordinary(self, path):
        self.calls.append(("ordinary", path))


class SessionLeadInTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def run_session(self, io, **paths):
        with tempfile.TemporaryDirectory() as directory:
            store = OutboxStore(Path(directory) / "outbox", transport="wacli")
            return GuidedSession(io, store, lambda kind, **data: None).run(
                recipient="origin@example.invalid", flow_kind="reply", deleted_cue_path="cue-deleted.wav",
                incoming_path="incoming.wav", auto_record_after_incoming=False, **paths)

    def test_message_after_cue_uses_lead_in_and_end_cue_stays_ordinary(self):
        io = IO()
        self.assertEqual(self.run_session(io, incoming_cue_path="cue-msg_start.wav",
                                          incoming_end_path="cue-msg_end.wav"), "played")
        self.assertEqual(io.calls, [("ordinary", "cue-msg_start.wav"), ("after_cue", "incoming.wav"),
                                    ("ordinary", "cue-msg_end.wav")])

    def test_no_cue_or_io_without_lead_in_plays_ordinary(self):
        io = IO()
        self.run_session(io)
        self.assertEqual(io.calls, [("ordinary", "incoming.wav")])
        io = IO(lead_in=False)
        self.run_session(io, incoming_cue_path="cue-msg_start.wav")
        self.assertEqual(io.calls, [("ordinary", "cue-msg_start.wav"), ("ordinary", "incoming.wav")])

    def test_runtime_io_wires_lead_in(self):
        with mock.patch.object(runtime, "play_audio_after_cue") as after_cue:
            runtime.PiGuidedIO("r", "s", 30).play_after_cue("incoming.wav")
        after_cue.assert_called_once_with("incoming.wav")


if __name__ == "__main__":
    unittest.main()
