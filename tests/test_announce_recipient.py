"""Recipient speech: frozen routing, bounded local audio and field installation."""

import hashlib
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox import cloud_runtime, sound_pack
from messagebox.cloud_device import CloudDeviceClient, CloudDeviceError, capabilities
from messagebox.cloud_voice import (
    RECIPIENT_TEMPLATES,
    recipient_voice_cache,
    recipient_voice_key,
    recipient_voice_valid,
)
from messagebox.settings import SettingsError, VOICE_PACKS, defaults, validate
import test_cloud_runtime as runtime_tests
from test_cloud_runtime import PERSON, BOX, button_send
from test_bounded_update import Fixture, FakeTime

ROOT = Path(__file__).resolve().parents[1]
API = "https://button.box/cloud-api/v1"
JID = PERSON["wa_id"] + "@s.whatsapp.net"


def wav(seconds=0.01, rate=48000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", ""))
        output.writeframes(b"\0\0" * round(seconds * rate))
    return stream.getvalue()


def ready(fields, audio):
    return {
        "status": "ready",
        "use_fallback": False,
        "template": fields["template"],
        "recipient_kind": fields["kind"],
        "recipient_id": fields["subject"],
        "account_scope": fields["scope"],
        "name_hash": fields["name_hash"],
        "voice_pack": fields["pack"],
        "clip_id": "c" * 64,
        "text_hash": "d" * 64,
        "sha256": hashlib.sha256(audio).hexdigest(),
        "content_type": "audio/wav",
        "bytes": len(audio),
        "media_url": API + "/device/voice-lines/" + "c" * 64,
    }


class RecipientContractTests(unittest.TestCase):
    def test_strict_setting_default_and_old_documents(self):
        baseline = defaults({})
        self.assertIs(baseline["announce_recipient"], True)
        baseline.pop("announce_recipient")
        self.assertIs(validate(baseline)["announce_recipient"], True)
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                validate({**baseline, "announce_recipient": value})
        self.assertFalse(
            validate({**baseline, "announce_recipient": False})["announce_recipient"]
        )

    def test_both_request_routes_and_templates_reject_unknown_or_extra_fields(self):
        client = CloudDeviceClient.__new__(CloudDeviceClient)
        client.request = mock.Mock(return_value=b'{"status":"fallback"}')
        for template in RECIPIENT_TEMPLATES:
            for kind in ("identity_id", "link_id"):
                client.voice_line(template, **{kind: "subject_1"})
                self.assertEqual(
                    client.request.call_args.kwargs["body"],
                    {"template": template, kind: "subject_1"},
                )
            for fields in (
                {},
                {"identity_id": "a", "link_id": "b"},
                {"identity_id": "../bad"},
                {"identity_id": True},
                {"identity_id": "a" * 129},
                {"identity_id": "a", "extra": True},
            ):
                before = client.request.call_count
                with self.assertRaises(CloudDeviceError):
                    client.voice_line(template, **fields)
                self.assertEqual(client.request.call_count, before)
        with self.assertRaises(CloudDeviceError):
            client.voice_line("unknown", identity_id="a")

    def test_strict_response_fields_unknown_optional_allowed(self):
        fields = dict(
            scope="a" * 64,
            kind="identity",
            subject="subject",
            template="recording_recipient",
            pack="jessica",
            name_hash="b" * 64,
        )
        payload = ready(fields, wav())
        self.assertTrue(
            recipient_voice_valid({**payload, "future": True}, **fields, api_url=API)
        )
        changes = {
            "status": "fallback",
            "use_fallback": 0,
            "template": "sent_recipient",
            "recipient_kind": "box",
            "recipient_id": "other",
            "account_scope": "f" * 64,
            "name_hash": "f" * 64,
            "voice_pack": "pirate",
            "clip_id": "g" * 64,
            "text_hash": None,
            "sha256": "A" * 64,
            "content_type": "audio/ogg",
            "bytes": True,
            "media_url": "https://other.example/cloud-api/v1/device/media/a",
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertFalse(
                    recipient_voice_valid(
                        {**payload, key: value}, **fields, api_url=API
                    )
                )
        for value in (384045, 44, 0, 44.5):
            self.assertFalse(
                recipient_voice_valid(
                    {**payload, "bytes": value}, **fields, api_url=API
                )
            )
        for suffix in ("?x=y", "#fragment", "/../private", "/%2e%2e/private"):
            self.assertFalse(
                recipient_voice_valid(
                    {**payload, "media_url": payload["media_url"] + suffix},
                    **fields,
                    api_url=API,
                )
            )

    def test_fallbacks_are_optional_exact_pack_bounded_and_gate_capability(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(sound_pack, "SOUND_DIR", Path(directory)),
            mock.patch.object(sound_pack, "current_voice_pack", return_value="jessica"),
        ):
            self.assertNotIn("announce_recipient_v1", capabilities())
            for pack in VOICE_PACKS:
                target = Path(directory) / (
                    "voice" if pack == "jessica" else "voices/" + pack
                )
                target.mkdir(parents=True, exist_ok=True)
                (target / "voice-recording.wav").write_bytes(wav())
                self.assertFalse(sound_pack.recipient_fallbacks_ready(pack))
                (target / "voice-sent.wav").write_bytes(wav(4))
                self.assertTrue(sound_pack.recipient_fallbacks_ready(pack))
                (target / "voice-sent.wav").write_bytes(wav(4.001))
                self.assertFalse(sound_pack.recipient_fallbacks_ready(pack))
                (target / "voice-sent.wav").write_bytes(wav())
            self.assertIs(capabilities()["announce_recipient_v1"], True)
            self.assertEqual(capabilities()["voice_lines"], 1)
            (Path(directory) / "voices/pirate/voice-sent.wav").unlink()
            self.assertIsNone(
                sound_pack.recipient_fallback_path("sent_recipient", "pirate")
            )
            (Path(directory) / "voice/voice-sent.wav").write_bytes(
                b"x" * (2 * 1024 * 1024 + 1)
            )
            self.assertNotIn("announce_recipient_v1", capabilities())

    def test_real_fallback_metadata_and_capability_for_every_pack(self):
        texts = {
            "jessica": ("Recording!", "Sent!"),
            "pirate": ("Arr, recording!", "Sent, matey!"),
            "alien": ("Recording transmission!", "Transmission sent!"),
            "dj": ("Recording, yo!", "Sent, yo!"),
            "robot": ("Beep boop. Recording.", "Beep boop. Sent."),
            "french": ("Voilà, recording!", "Voilà, sent!"),
            "charlie": ("Righto, recording!", "Sent, mate!"),
            "tata": ("J'enregistre !", "Envoyé !"),
        }
        with mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"):
            for pack in VOICE_PACKS:
                directory = ROOT / "sounds" / ("voice" if pack == "jessica" else "voices/" + pack)
                manifest = json.loads((directory / "manifest.json").read_text())
                rows = manifest["recipient_lines"]
                self.assertEqual([row["file"] for row in rows], ["voice-recording.wav", "voice-sent.wav"])
                self.assertEqual([row["text"] for row in rows], list(texts[pack]))
                self.assertTrue(set(row["file"] for row in rows).isdisjoint(row["file"] for row in manifest["lines"]))
                for row in rows:
                    path = directory / row["file"]
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])
                    with wave.open(str(path)) as audio:
                        self.assertEqual(row["seconds"], audio.getnframes() / audio.getframerate())
                    self.assertLessEqual(path.stat().st_size, 2 * 1024 * 1024)
                with mock.patch.object(sound_pack, "current_voice_pack", return_value=pack):
                    self.assertIs(capabilities()["announce_recipient_v1"], True)

    def test_separate_cache_limits_audio_validation_and_subject_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = recipient_voice_cache(directory)
            self.assertEqual(
                (cache.max_entries, cache.max_bytes), (300, 128 * 1024 * 1024)
            )
            fields = dict(
                scope="a" * 64,
                kind="identity",
                subject="subject",
                template="recording_recipient",
                pack="jessica",
                name_hash="b" * 64,
            )
            audio = wav()
            client = mock.Mock()
            client.media.return_value = audio
            payload = ready(fields, audio)
            for index in range(301):
                key = recipient_voice_key(**{**fields, "subject": str(index)})
                self.assertTrue(cache.download(client, payload, key=key))
            self.assertEqual(len(list(Path(directory).glob("*.wav"))), 300)
            self.assertEqual(
                client.media.call_count, 301
            )  # Even a shared clip_id has separate scoped files.
            for field, value in [
                ("scope", "e" * 64),
                ("kind", "box"),
                ("subject", "other"),
                ("template", "sent_recipient"),
                ("pack", "tata"),
                ("name_hash", "f" * 64),
            ]:
                self.assertNotEqual(
                    recipient_voice_key(**fields),
                    recipient_voice_key(**{**fields, field: value}),
                )
            for audio in (wav(4.001), wav(rate=16000), b"not audio"):
                client.media.return_value = audio
                self.assertEqual(
                    cache.download(client, ready(fields, audio), key="e" * 64), ""
                )
            client.media.return_value = wav()
            self.assertEqual(
                cache.download(
                    client, {**ready(fields, wav()), "bytes": 999}, key="e" * 64
                ),
                "",
            )
            self.assertEqual(
                cache.download(
                    client, {**ready(fields, wav()), "sha256": "f" * 64}, key="e" * 64
                ),
                "",
            )
            # Exercise the byte ceiling separately; production uses the same eviction path.
            cache.max_bytes = 2 * len(wav())
            cache.download(client, ready(fields, wav()), key="e" * 64)
            self.assertLessEqual(
                sum(path.stat().st_size for path in Path(directory).glob("*.wav")),
                cache.max_bytes,
            )


class RecipientPrefetchTests(unittest.TestCase):
    setUp = runtime_tests.CloudRuntimeTests.setUp

    def enable(self):
        self.client.connected_boxes = [BOX.copy()]
        self.runtime.heartbeat()
        self.client.api_url = API
        self.client.media = mock.Mock(return_value=wav())

        def response(template, *, deadline=None, **reference):
            if template not in RECIPIENT_TEMPLATES:
                return {"use_fallback": True}
            box = "link_id" in reference
            jid = "box:" + reference["link_id"] if box else JID
            fields = self.runtime._recipient_voice_subject(jid, "jessica", template)
            return ready(fields, wav())

        self.client.voice_line = mock.Mock(side_effect=response)
        patch = mock.patch.object(
            sound_pack, "recipient_fallbacks_ready", return_value=True
        )
        patch.start()
        self.addCleanup(patch.stop)
        runtime_tests.finish_voice_preloads(self.runtime, cycles=3)

    def test_both_routes_lines_restart_corruption_missing_and_offline(self):
        self.enable()
        self.assertEqual(sum(call.args[0] in RECIPIENT_TEMPLATES for call in self.client.voice_line.call_args_list), 4)
        for jid in (JID, "box:" + BOX["id"]):
            for template in RECIPIENT_TEMPLATES:
                clip = self.runtime.recipient_voice_clip(
                    jid, "jessica", template, "a" * 64
                )
                self.assertTrue(Path(clip).is_file())
                self.runtime.state = self.runtime._load()
                self.assertEqual(
                    self.runtime.recipient_voice_clip(jid, "jessica", template), clip
                )
        self.client.voice_line.side_effect = CloudDeviceError("offline")
        before = self.client.voice_line.call_count
        self.assertTrue(
            self.runtime.recipient_voice_clip(JID, "jessica", "recording_recipient")
        )
        self.assertEqual(self.client.voice_line.call_count, before)
        clip = Path(self.runtime.recipient_voice_clip(JID, "jessica", "sent_recipient"))
        clip.write_bytes(b"corrupt")
        self.assertEqual(
            self.runtime.recipient_voice_clip(JID, "jessica", "sent_recipient"), ""
        )
        clip.unlink()
        self.assertEqual(
            self.runtime.recipient_voice_clip(JID, "jessica", "sent_recipient"), ""
        )
        self.assertEqual(
            self.runtime.recipient_voice_clip(JID, "pirate", "recording_recipient"), ""
        )
        self.assertEqual(
            self.runtime.recipient_voice_clip(
                JID, "jessica", "recording_recipient", "b" * 64
            ),
            "",
        )
        self.assertEqual(
            self.runtime.recipient_voice_clip(
                "other@s.whatsapp.net", "jessica", "recording_recipient"
            ),
            "",
        )

    def test_failed_refresh_keeps_cached_clips_hashes_and_retry_delay(self):
        self.enable()
        recipient_response = self.client.voice_line.side_effect

        def response(template, *, deadline=None, **reference):
            if template in RECIPIENT_TEMPLATES:
                return recipient_response(template, **reference)
            return {"clip_id": hashlib.sha256((template + next(iter(reference.values()))).encode()).hexdigest(),
                    "voice_pack": "jessica", "sha256": hashlib.sha256(wav()).hexdigest(),
                    "content_type": "audio/wav", "media_url": API + "/device/voice-lines/clip"}

        self.client.voice_line.side_effect = response
        for row in self.runtime.state["voice_line_cache"].values():
            row["retry_at"] = 0
        runtime_tests.finish_voice_preloads(self.runtime, cycles=2)
        caches = ("recipient_voice_cache", "voice_line_cache")
        expected = {name: {key: {field: value for field, value in row.items() if field != "retry_at"}
                          for key, row in self.runtime.state[name].items()} for name in caches}
        self.assertTrue(all(len(rows) == 4 for rows in expected.values()))
        for failure in (CloudDeviceError("offline"), {"use_fallback": True}):
            with self.subTest(failure=type(failure).__name__):
                for name in caches:
                    for row in self.runtime.state[name].values():
                        row["retry_at"] = 0
                if isinstance(failure, Exception):
                    self.client.voice_line.side_effect = failure
                else:
                    self.client.voice_line.side_effect = None
                    self.client.voice_line.return_value = failure
                runtime_tests.finish_voice_preloads(self.runtime, cycles=3)
                # Persistence and the next preload pass must retain the last good WAV.
                self.runtime.state = self.runtime._load()
                for name in caches:
                    for key, row in self.runtime.state[name].items():
                        self.assertEqual({field: value for field, value in row.items() if field != "retry_at"},
                                         expected[name][key])
                        self.assertEqual(row["retry_at"], self.runtime.server_now() + 60)
                        self.assertEqual(Path(row["clip"]).read_bytes(), wav())
                for jid in (JID, "box:" + BOX["id"]):
                    for template in RECIPIENT_TEMPLATES:
                        self.assertTrue(self.runtime.recipient_voice_clip(jid, "jessica", template))
                    for template in ("card_prompt", "card_saved"):
                        self.assertTrue(self.runtime.named_card_clip(jid, "jessica", template))
                calls = self.client.voice_line.call_count
                runtime_tests.finish_voice_preloads(self.runtime)
                self.assertEqual(self.client.voice_line.call_count, calls)

    def test_wrong_subject_mapping_never_downloads_and_bad_voice_never_breaks_heartbeat(
        self,
    ):
        self.enable()
        original = self.client.voice_line.side_effect
        self.runtime.state["recipient_voice_cache"] = {}
        for changes in (
            {"recipient_id": "other"},
            {"recipient_kind": "box"},
            {"account_scope": "b" * 64},
            {"name_hash": "f" * 64},
            {"voice_pack": "pirate"},
            {"template": "sent_recipient"},
            {"bytes": True},
            {"status": "fallback"},
            {"use_fallback": True},
        ):
            self.runtime.state["recipient_voice_cache"] = {}

            def invalid(template, **reference):
                if template != "recording_recipient" or "link_id" in reference:
                    return {"use_fallback": True}
                return {**original(template, **reference), **changes}

            self.client.voice_line.side_effect = invalid
            before = self.client.media.call_count
            self.runtime.heartbeat()
            runtime_tests.finish_voice_preloads(self.runtime, cycles=3)
            self.assertEqual(self.client.media.call_count, before)
            self.assertEqual(
                self.runtime.recipient_voice_clip(
                    JID, "jessica", "recording_recipient"
                ),
                "",
            )
        self.client.voice_line.side_effect = lambda *args, **kwargs: []
        self.runtime.state["recipient_voice_cache"] = {}
        self.runtime.heartbeat()
        runtime_tests.finish_voice_preloads(self.runtime, cycles=3)
        self.assertEqual(self.runtime._snapshot()["account_scope"], "a" * 64)

    def test_prune_rename_removal_scope_pack_and_download_races(self):
        self.enable()
        old = self.runtime.recipient_voice_clip(JID, "jessica", "recording_recipient")
        self.client.people[0]["display_name"] = "Changed Full Name"
        self.runtime.heartbeat()
        self.assertFalse(Path(old).exists())
        runtime_tests.finish_voice_preloads(self.runtime, cycles=3)
        old = self.runtime.recipient_voice_clip(JID, "jessica", "recording_recipient")
        self.assertTrue(old)
        self.client.people = []
        self.runtime.heartbeat()
        self.assertFalse(Path(old).exists())
        self.assertEqual(
            self.runtime.recipient_voice_clip(JID, "jessica", "recording_recipient"), ""
        )
        old_files = list((self.root / "recipient-voice").glob("*.wav"))
        self.client.voice_line.side_effect = CloudDeviceError("offline")
        self.runtime.state["snapshot"]["account_scope"] = "b" * 64
        runtime_tests.finish_voice_preloads(self.runtime, cycles=3)
        self.assertTrue(all(not path.exists() for path in old_files))
        runtime_tests.CloudRuntimeTests.update_settings(self, voice_pack="pirate")
        runtime_tests.finish_voice_preloads(self.runtime, cycles=3)
        self.assertEqual(list((self.root / "recipient-voice").glob("*.wav")), [])

    def test_rename_or_pack_switch_during_download_discards_result(self):
        self.enable()
        for change in ("rename", "pack"):
            started, release = threading.Event(), threading.Event()
            self.runtime.state["recipient_voice_cache"] = {}
            self.runtime.state.pop("voice_preload_cursor", None)
            for path in (self.root / "recipient-voice").glob("*.wav"):
                path.unlink()

            def download(*args, **kwargs):
                started.set()
                release.wait(2)
                return wav()

            self.client.media.side_effect = download
            with mock.patch.object(self.runtime, "_card_voice_wanted", return_value={}):
                self.runtime._preload_voice_lines(new_cycle=True)
                self.assertTrue(started.wait(1))
                try:
                    if change == "rename":
                        self.runtime.state["snapshot"]["people"][0]["display_name"] = "New name"
                    else:
                        runtime_tests.CloudRuntimeTests.update_settings(self, voice_pack="pirate")
                finally:
                    release.set()
                    self.runtime._voice_thread.join(2)
                self.runtime._preload_voice_lines(start=False)
            self.assertEqual(self.runtime.state["recipient_voice_cache"], {})
            self.assertEqual(list((self.root / "recipient-voice").glob("*.wav")), [])

    def test_polling_does_not_remove_the_workers_active_temporary_file(self):
        self.enable()
        self.runtime.state["recipient_voice_cache"] = {}
        self.runtime.state.pop("voice_preload_cursor", None)
        directory = self.root / "recipient-voice"
        for path in directory.glob("*.wav"):
            path.unlink()
        started, release = threading.Event(), threading.Event()
        replace = os.replace

        def finish_file(source, target):
            if Path(target).parent == directory:
                started.set()
                release.wait(2)
            return replace(source, target)

        with (mock.patch.object(self.runtime, "_card_voice_wanted", return_value={}),
              mock.patch("messagebox.cloud_voice.os.replace", side_effect=finish_file)):
            self.runtime._preload_voice_lines(new_cycle=True)
            self.assertTrue(started.wait(1))
            try:
                temporary = next(directory.glob("*.part"))
                self.runtime.heartbeat()
                self.runtime.poll_once()
                self.assertTrue(temporary.exists())
            finally:
                release.set()
                self.runtime._voice_thread.join(2)
            self.runtime._preload_voice_lines(start=False)
        self.assertTrue(self.runtime.recipient_voice_clip(JID, "jessica", "recording_recipient"))
        self.assertFalse(list(directory.glob("*.part")))

    def test_total_budget_covers_metadata_and_media_and_yields_between_jobs(self):
        self.enable()
        self.runtime.state["recipient_voice_cache"] = {}
        self.runtime.state["voice_line_cache"] = {}
        calls = []
        response = self.client.voice_line.side_effect

        def metadata(template, *, deadline, **reference):
            calls.append(("metadata", deadline - self.mono[0]))
            self.mono[0] += 2
            return response(template, **reference)

        def media(*args, deadline, **kwargs):
            calls.append(("media", deadline - self.mono[0]))
            self.mono[0] += 2
            return wav()

        self.client.voice_line.side_effect = metadata
        self.client.media.side_effect = media
        self.runtime._preload_voice_lines(start=False, new_cycle=True)
        # No optional request starts within heartbeat/reconciliation.
        self.assertEqual(calls, [])
        self.runtime.poll_once()
        self.runtime._voice_thread.join(2)
        self.assertEqual(calls, [("metadata", 5), ("media", 3)])
        # Completion does not start a second job until the next inbox poll.
        inbox = mock.Mock(wraps=self.client.inbox)
        self.client.inbox = inbox
        self.runtime.poll_once()
        self.runtime._voice_thread.join(2)
        self.runtime._preload_voice_lines(start=False)
        self.assertEqual(calls[-1], ("metadata", 1))
        self.assertEqual(len(calls), 3)  # No media after metadata uses the last second.
        self.assertEqual(inbox.call_count, 1)
        self.assertEqual(self.runtime._voice_remaining, 0)
        self.runtime.poll_once()
        self.assertEqual(len(calls), 3)

    def test_blocked_optional_request_cannot_block_heartbeat_inbox_or_mute(self):
        self.enable()
        self.runtime.state["recipient_voice_cache"] = {}
        started = threading.Event()
        release = threading.Event()

        def blocked(*args, **kwargs):
            started.set()
            release.wait(2)
            return {"use_fallback": True}

        self.client.voice_line.side_effect = blocked
        self.runtime._preload_voice_lines(start=False, new_cycle=True)
        self.runtime.poll_once()
        self.assertTrue(started.wait(1))
        try:
            self.runtime.heartbeat()
            self.client.items = [{"operation_id": runtime_tests.OP, "sequence": 1,
                "kind": "mute", "created_at": runtime_tests.NOW,
                "expires_at": runtime_tests.NOW + 60, "payload": {"muted": True}}]
            self.runtime.poll_once()
            self.assertTrue(self.runtime.silence.load()["muted"])
            self.assertTrue(self.runtime._voice_thread.is_alive())
            self.assertTrue(self.runtime._snapshot())
            self.assertEqual(self.client.acks[-1]["state"], "applied")
        finally:
            release.set()
            self.runtime._voice_thread.join(2)
            self.runtime._preload_voice_lines(start=False)

    def test_full_150_contacts_both_templates_and_eviction_respects_retry(self):
        self.client.people = [{**PERSON, "id": "person%03d" % index,
            "wa_id": "1202555%04d" % index, "display_name": "Person %d" % index}
            for index in range(150)]
        all_people = self.client.people
        self.client.people = all_people[:100]
        self.client.connected_boxes = [{"id": person["id"], "box_name": person["display_name"]}
                                       for person in all_people[100:]]
        self.runtime.heartbeat()
        self.client.api_url = API
        self.client.media = mock.Mock(return_value=wav())
        requested = []

        fields_by_subject = {}

        def response(template, *, deadline=None, **reference):
            subject = reference.get("identity_id", reference.get("link_id"))
            requested.append((subject, template))
            return ready(fields_by_subject[(subject, template)], wav())

        self.client.voice_line = mock.Mock(side_effect=response)
        with (mock.patch.object(sound_pack, "recipient_fallbacks_ready", return_value=True),
              mock.patch.object(self.runtime, "_card_voice_wanted", return_value={})):
            fields_by_subject.update(((fields["subject"], fields["template"]), fields)
                for fields in self.runtime._recipient_voice_wanted().values())
            runtime_tests.finish_voice_preloads(self.runtime, cycles=100)
            self.assertEqual(len(requested), 300)
            self.assertEqual(len(set(requested)), 300)
            self.assertEqual(requested[:4], [(self.client.people[index]["id"], template)
                for index in range(2) for template in RECIPIENT_TEMPLATES])
            jids = [person["wa_id"] + "@s.whatsapp.net" for person in self.client.people]
            jids += ["box:" + box["id"] for box in self.client.connected_boxes]
            for jid in jids:
                for template in RECIPIENT_TEMPLATES:
                    self.assertTrue(self.runtime.recipient_voice_clip(jid, "jessica", template))
            self.assertEqual(len(list((self.root / "recipient-voice").glob("*.wav"))), 300)
            # A missing early file must not consume the allowance again before retry.
            next((self.root / "recipient-voice").glob("*.wav")).unlink()
            runtime_tests.finish_voice_preloads(self.runtime, cycles=2)
            self.assertEqual(len(requested), 300)
            self.runtime.state["recipient_voice_cache"][next(iter(self.runtime._recipient_voice_wanted()))]["retry_at"] = 0
            runtime_tests.finish_voice_preloads(self.runtime)
            self.assertEqual(len(requested), 301)



class RecipientButtonTests(unittest.TestCase):
    def setUp(self):
        self.real_quiet_hours = button_send.quiet_hours
        self.settings = defaults({})
        self.switch = SimpleNamespace(is_pressed=False)
        self.clock = FakeTime()
        self.clock.time = self.clock.monotonic
        self.paths = []
        self.finish_at = 2

        class Player:
            def poll(inner):
                return 0 if self.clock.monotonic() >= self.finish_at else None

            def terminate(inner):
                self.finish_at = self.clock.monotonic()

            def wait(inner, timeout):
                return 0

        def start(args):
            self.paths.append(Path(args[-1]).name)
            self.finish_at = self.clock.monotonic() + 2
            return Player()

        for name, value in [
            ("button", self.switch),
            ("time", self.clock),
            ("start_player", start),
            ("_recording", False),
            ("_guided_active", False),
            ("quiet_hours", mock.Mock(return_value=False)),
            ("caregiver_settings", lambda: self.settings),
        ]:
            patch = mock.patch.object(button_send, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_frozen_subject_both_lines_fallback_off_and_silence(self):
        with (
            mock.patch.object(
                sound_pack, "recipient_fallbacks_ready", return_value=True
            ),
            mock.patch.object(
                sound_pack, "recipient_fallback_path", return_value=Path("generic.wav")
            ),
            mock.patch.object(
                cloud_runtime, "recipient_voice_clip", return_value="named.wav"
            ) as lookup,
        ):
            for jid in (JID, "box:" + BOX["id"]):
                for template in RECIPIENT_TEMPLATES:
                    self.assertEqual(
                        button_send.recipient_announcement(
                            jid, template, scope="a" * 64
                        ),
                        Path("named.wav"),
                    )
                    lookup.assert_called_with(jid, "jessica", template, "a" * 64)
            lookup.return_value = ""
            self.assertEqual(
                button_send.recipient_announcement(JID, "sent_recipient"),
                Path("generic.wav"),
            )
            for setting, value in [
                ("announce_recipient", False),
                ("master_volume_percent", 0),
            ]:
                with mock.patch.dict(self.settings, {setting: value}):
                    self.assertIsNone(
                        button_send.recipient_announcement(JID, "recording_recipient")
                    )
            with mock.patch.object(button_send, "quiet_hours", return_value=True):
                self.assertIsNone(
                    button_send.recipient_announcement(JID, "recording_recipient")
                )
            with mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"}):
                before = lookup.call_count
                self.assertIsNone(
                    button_send.recipient_announcement(JID, "recording_recipient")
                )
                self.assertEqual(lookup.call_count, before)

    def test_timing_taps_ignored_hold_release_latched_and_four_second_cap(self):
        sleep = self.clock.sleep

        def taps(seconds):
            sleep(seconds)
            self.switch.is_pressed = 0.3 < self.clock.monotonic() < 0.6

        self.clock.sleep = taps
        self.assertTrue(button_send.play_recipient_line("named.wav"))
        self.assertAlmostEqual(self.clock.monotonic(), 2, delta=0.01)
        self.clock = FakeTime()
        button_send.time = self.clock
        self.switch.is_pressed = True
        sleep = self.clock.sleep

        def release(seconds):
            sleep(seconds)
            self.switch.is_pressed = (
                self.clock.monotonic() < 0.3 or self.clock.monotonic() > 0.6
            )

        self.clock.sleep = release
        self.assertFalse(button_send.play_recipient_line("named.wav", talk_mode="hold"))
        self.assertLess(self.clock.monotonic(), 0.4)
        self.clock = FakeTime()
        button_send.time = self.clock
        with mock.patch.object(
            button_send,
            "start_player",
            return_value=mock.Mock(poll=mock.Mock(return_value=None)),
        ) as start:
            with self.assertRaises(subprocess.TimeoutExpired):
                button_send.play_recipient_line("hung.wav")
            self.assertAlmostEqual(self.clock.monotonic(), 4, delta=0.01)
            start.return_value.terminate.assert_called_once()

    def test_player_failure_does_not_report_success(self):
        with mock.patch.object(button_send, "start_player", return_value=mock.Mock(poll=mock.Mock(return_value=1))):
            with self.assertRaises(subprocess.CalledProcessError):
                button_send.play_recipient_line("named.wav")

    def test_announcement_then_cue_before_microphone_and_release_cancels(self):
        CaptureDenied = button_send.CaptureDenied
        events = []
        with (
            mock.patch.object(button_send, "require_permit"),
            mock.patch.object(
                button_send, "recipient_announcement", return_value=Path("named.wav")
            ),
            mock.patch.object(
                button_send,
                "play_recipient_line",
                side_effect=lambda path, **kw: events.append(Path(path).name) or True,
            ),
            mock.patch.object(
                button_send,
                "open_microphone",
                side_effect=lambda *a, **kw: (
                    events.append("mic") or (_ for _ in ()).throw(CaptureDenied())
                ),
            ),
        ):
            with self.assertRaises(CaptureDenied):
                button_send.capture_guided_recording(JID, account_scope="a" * 64)
        self.assertEqual(events, ["named.wav", "cue-rec_go.wav", "mic"])
        self.switch.is_pressed = True
        with (
            mock.patch.object(button_send, "require_permit"),
            mock.patch.object(
                button_send, "recipient_announcement", return_value=Path("named.wav")
            ),
            mock.patch.object(button_send, "play_recipient_line", return_value=False),
            mock.patch.object(button_send, "open_microphone") as microphone,
        ):
            result = button_send.capture_guided_recording(JID, talk_mode="hold")
            self.assertFalse(result.meaningful)
            microphone.assert_not_called()

    def test_tap_held_through_recording_line_opens_mic_and_capture_is_bounded(self):
        process = mock.Mock(poll=mock.Mock(return_value=None))
        process.communicate.return_value = (b"", b"")
        vad = mock.Mock(trim_bounds=mock.Mock(return_value=None))
        self.switch.is_pressed = True

        def microphone(*args, **kwargs):
            self.assertTrue(self.switch.is_pressed)
            self.assertAlmostEqual(self.clock.monotonic(), 4, delta=0.02)
            return process

        def capture_tick(*args):
            self.clock.sleep(0.1)
            return [], [], []

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for name, value in [("TEMP_DIR", directory), ("require_permit", mock.Mock()),
                ("recipient_announcement", mock.Mock(return_value=Path("named.wav"))),
                ("play_sound", mock.Mock()), ("open_microphone", microphone),
                ("EnergyVAD", mock.Mock(return_value=vad)), ("led", mock.Mock()),
                ("presence", mock.Mock()), ("RecordingLimitCue", mock.Mock()),
                ("wait_for_stable_open", mock.Mock(side_effect=AssertionError("no release wait")))]:
                stack.enter_context(mock.patch.object(button_send, name, value, create=True))
            stack.enter_context(mock.patch.object(button_send.select, "select", side_effect=capture_tick))
            result = button_send.capture_guided_recording(JID, max_seconds=0.3)
            self.assertTrue(result.timed_out)
            self.assertLess(self.clock.monotonic(), 5)
            process.send_signal.assert_called_once()

    def test_sent_line_press_interrupts_without_consuming_next_recording_press(self):
        from messagebox.audio_requests import AudioRequests

        sleep = self.clock.sleep

        def press(seconds):
            sleep(seconds)
            self.switch.is_pressed = True

        self.clock.sleep = press
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            requests = AudioRequests(directory)
            for name, value in [("cloud_audio_requests", requests),
                ("play_send_success_cue", mock.Mock(return_value=False)),
                ("recipient_announcement", mock.Mock(return_value=Path("named-sent.wav"))),
                ("discard_held_playback_press", mock.Mock(side_effect=AssertionError("preserve press")))]:
                stack.enter_context(mock.patch.object(button_send, name, value))
            stack.enter_context(mock.patch.object(cloud_runtime, "account_scope", return_value="a" * 64))
            requests.enqueue_for("sent", "success", "a" * 64, 30, recipient=JID)
            button_send.maybe_play_cloud_sound()
            self.assertEqual(self.paths, ["named-sent.wav"])
            self.assertTrue(self.switch.is_pressed)
            self.assertLess(self.clock.monotonic(), 0.1)
            # The main-loop boundary sees the very same press, with no release wait.
            self.assertFalse(button_send.maybe_play_cloud_sound())
            self.assertTrue(self.switch.is_pressed)

    def test_press_during_sent_cue_skips_sent_line(self):
        from messagebox.audio_requests import AudioRequests

        def press():
            self.switch.is_pressed = True
            return False

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            requests = AudioRequests(directory)
            stack.enter_context(mock.patch.object(button_send, "cloud_audio_requests", requests))
            stack.enter_context(mock.patch.object(cloud_runtime, "account_scope", return_value="a" * 64))
            stack.enter_context(mock.patch.object(button_send, "play_send_success_cue", side_effect=press))
            stack.enter_context(mock.patch.object(button_send, "recipient_announcement", return_value=Path("named.wav")))
            requests.enqueue_for("sent", "success", "a" * 64, 30, recipient=JID)
            button_send.maybe_play_cloud_sound()
            self.assertEqual(self.paths, [])
            self.assertTrue(self.switch.is_pressed)

    def test_sent_swoosh_then_named_or_generic_with_swoosh_off_and_mute(self):
        from messagebox.audio_requests import AudioRequests

        with tempfile.TemporaryDirectory() as directory:
            requests = AudioRequests(directory)
            with (
                mock.patch.object(button_send, "cloud_audio_requests", requests),
                mock.patch.object(
                    cloud_runtime, "account_scope", return_value="a" * 64
                ),
                mock.patch.object(
                    sound_pack, "recipient_fallbacks_ready", return_value=True
                ),
                mock.patch.object(
                    sound_pack,
                    "recipient_fallback_path",
                    return_value=Path("generic-sent.wav"),
                ),
                mock.patch.object(
                    cloud_runtime, "recipient_voice_clip", return_value="named-sent.wav"
                ) as lookup,
                mock.patch.object(
                    button_send,
                    "play_idle_sound",
                    side_effect=lambda path, *args: (
                        self.paths.append(Path(path).name) or True
                    ),
                ),
            ):
                for index, swoosh in enumerate((True, False)):
                    self.settings["swoosh_sound_enabled"] = swoosh
                    requests.enqueue_for(
                        str(index), "success", "a" * 64, 30, recipient=JID
                    )
                    self.paths.clear()
                    self.assertTrue(button_send.maybe_play_cloud_sound())
                    self.assertEqual(
                        self.paths,
                        (["cue-sent.wav"] if swoosh else []) + ["named-sent.wav"],
                    )
                    lookup.assert_called_with(
                        JID, "jessica", "sent_recipient", "a" * 64
                    )
                    self.assertFalse(button_send.maybe_play_cloud_sound())
                lookup.return_value = ""
                requests.enqueue_for("generic", "success", "a" * 64, 30, recipient=JID)
                self.paths.clear()
                self.assertTrue(button_send.maybe_play_cloud_sound())
                self.assertEqual(self.paths, ["generic-sent.wav"])
                for key, fields in [
                    ("zero", {"master_volume_percent": 0}),
                    ("off", {"announce_recipient": False}),
                ]:
                    with mock.patch.dict(self.settings, fields):
                        requests.enqueue_for(
                            key, "success", "a" * 64, 30, recipient=JID
                        )
                        self.paths.clear()
                        button_send.maybe_play_cloud_sound()
                        self.assertEqual(self.paths, [])
                with mock.patch.object(button_send, "quiet_hours", return_value=True):
                    requests.enqueue_for(
                        "muted", "success", "a" * 64, 30, recipient=JID
                    )
                    self.paths.clear()
                    button_send.maybe_play_cloud_sound()
                    self.assertEqual(self.paths, [])

    def test_wacli_recording_keeps_original_cue_and_never_looks_up_speech(self):
        CaptureDenied = button_send.CaptureDenied
        events = []
        with (
            mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"}),
            mock.patch.object(button_send, "require_permit"),
            mock.patch.object(
                button_send,
                "play_sound",
                side_effect=lambda path, **kw: events.append(Path(path).name),
            ),
            mock.patch.object(
                button_send, "open_microphone", side_effect=CaptureDenied
            ),
            mock.patch.object(button_send, "prepare_cloud_capture") as speech,
        ):
            with self.assertRaises(CaptureDenied):
                button_send.capture_guided_recording(JID)
            self.assertEqual(events, ["cue-rec_go.wav"])
            speech.assert_not_called()

    def test_real_mute_quiet_now_and_quiet_schedule_suppress_both_lines(self):
        now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        state = {"muted": False, "quiet_until": None}
        self.settings["timezone"] = "UTC"
        self.settings["quiet_hours"]["enabled"] = False
        with (
            mock.patch.object(
                button_send,
                "quiet_hours",
                side_effect=lambda settings=None: self.real_quiet_hours(
                    settings or self.settings, now
                ),
            ),
            mock.patch.object(
                button_send.cloud_quiet.silence_store, "load", return_value=state
            ),
            mock.patch.object(cloud_runtime, "recipient_voice_clip") as lookup,
            mock.patch.object(sound_pack, "recipient_fallbacks_ready", return_value=True),
        ):
            for muted, until, periods in (
                (True, None, []),
                (False, "manual", []),
                (False, None, [{"start": "11:00", "end": "13:00"}]),
            ):
                state.update(muted=muted, quiet_until=until)
                self.settings["quiet_schedules"] = {
                    "school_days": periods,
                    "weekend": periods,
                }
                for template in RECIPIENT_TEMPLATES:
                    self.assertIsNone(button_send.recipient_announcement(JID, template))
            lookup.assert_not_called()

    def test_card_default_reply_and_recent_recipient_match_speech_and_durable_outbox(
        self,
    ):
        from messagebox.guided_reply import OutboxStore, RecordingResult

        self.settings.update(review_before_send=False, after_listening="invite_reply")
        routes = (
            ("card", JID),
            ("default", "box:" + BOX["id"]),
            ("reply", "12025550199@s.whatsapp.net"),
            ("recent", "12025550198@s.whatsapp.net"),
        )
        for route, recipient in routes:
            with (
                self.subTest(route=route),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                root = Path(directory)
                source = root / "recording.wav"
                source.write_bytes(wav(2))
                store = OutboxStore(root / "outbox", transport="cloud")
                context = {"via_card": route == "card", "contact": {"jid": recipient}}
                claim = (
                    {"path": root / "incoming.wav", "meta": {"chat": recipient}}
                    if route == "reply"
                    else None
                )
                for name, value in (
                    ("led", mock.Mock()),
                    ("outbox_store", store),
                    (
                        "claim_fresh_card_intent",
                        mock.Mock(
                            return_value=("claimed", context)
                            if route == "card"
                            else ("none", None)
                        ),
                    ),
                    ("recording_recipient_context", mock.Mock(return_value=context)),
                    ("claim_oldest", mock.Mock(return_value=claim)),
                    ("ensure_nfc_confirmation", mock.Mock(return_value=True)),
                    ("inbound_audio_authorized", mock.Mock(return_value=True)),
                    ("finish_claim", mock.Mock()),
                    ("react_played", mock.Mock()),
                    ("queued", mock.Mock(return_value=[])),
                    ("mark_queue_known", mock.Mock()),
                    ("refresh_led", mock.Mock()),
                    ("log_event", mock.Mock()),
                    ("message_intro_voice", mock.Mock(return_value=None)),
                    ("message_age_voice", mock.Mock(return_value=None)),
                ):
                    stack.enter_context(
                        mock.patch.object(button_send, name, value, create=True)
                    )
                stack.enter_context(
                    mock.patch.object(
                        cloud_runtime, "account_scope", return_value="a" * 64
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        button_send.ContactStore,
                        "allowed_jids",
                        return_value={recipient},
                    )
                )
                stack.enter_context(
                    mock.patch.object(button_send.PiGuidedIO, "play_ordinary")
                )
                stack.enter_context(
                    mock.patch.object(button_send.PiGuidedIO, "play_after_cue")
                )
                stack.enter_context(
                    mock.patch.object(
                        sound_pack, "recipient_fallbacks_ready", return_value=True
                    )
                )
                lookup = stack.enter_context(
                    mock.patch.object(
                        cloud_runtime, "recipient_voice_clip", return_value="named.wav"
                    )
                )

                def capture(frozen, *args, **kwargs):
                    self.assertEqual(frozen, recipient)
                    self.assertEqual(kwargs["account_scope"], "a" * 64)
                    with (
                        mock.patch.object(
                            button_send,
                            "recording_recipient_context",
                            side_effect=AssertionError("speech reselected route"),
                        ),
                        mock.patch.object(
                            button_send,
                            "current_recipient_context",
                            side_effect=AssertionError("speech reselected card"),
                        ),
                    ):
                        self.assertEqual(
                            button_send.recipient_announcement(
                                frozen,
                                "recording_recipient",
                                scope=kwargs["account_scope"],
                            ),
                            Path("named.wav"),
                        )
                    return RecordingResult(str(source), 2, True)

                stack.enter_context(
                    mock.patch.object(
                        button_send, "capture_guided_recording", side_effect=capture
                    )
                )
                button_send.run_guided_once(self.settings)
                jobs = store.jobs()
                self.assertEqual(len(jobs), 1)
                self.assertEqual(
                    (jobs[0].recipient, jobs[0].account_scope), (recipient, "a" * 64)
                )
                lookup.assert_called_once_with(
                    recipient, "jessica", "recording_recipient", "a" * 64
                )


class RecipientFieldUpdaterTests(unittest.TestCase):
    def test_field_versions_accept_existing_modules_and_extra_wavs_and_roll_back(self):
        # Execute each historical updater's parser and apply/rollback path, not the new parser.
        for version in ("v0.3.9", "v0.3.10", "v0.3.12"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                source = subprocess.check_output(
                    ["git", "show", version + ":scripts/install/bounded_update.py"]
                )
                module_path = Path(directory) / "old_updater.py"
                module_path.write_bytes(source)
                spec = importlib.util.spec_from_file_location(
                    "old_updater", module_path
                )
                updater = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(updater)
                base = Path(directory) / "fixture"
                base.mkdir()
                fixture = Fixture(base)
                assets = []
                for pack in VOICE_PACKS:
                    pack_dir = (
                        "sounds/voice" if pack == "jessica" else "sounds/voices/" + pack
                    )
                    for name in ("recording", "sent"):
                        relative = pack_dir + "/voice-" + name + ".wav"
                        entries = [row for row in fixture.manifest_data["files"] if row["source"] == relative]
                        self.assertEqual(len(entries), 1)
                        path = fixture.source / relative
                        self.assertEqual(path.read_bytes(), (ROOT / relative).read_bytes())
                        assets.append((path, entries[0]))
                with mock.patch.object(updater, "time", FakeTime()):
                    updater.apply(
                        fixture.source,
                        fixture.manifest,
                        fixture.backup,
                        root=fixture.root,
                        run=fixture.systemctl,
                    )
                    for path, entry in assets:
                        self.assertEqual(
                            (
                                fixture.root / entry["installed"].lstrip("/")
                            ).read_bytes(),
                            path.read_bytes(),
                        )
                    for relative in (
                        "messagebox/cloud_voice.py",
                        "messagebox/cloud_runtime.py",
                        "messagebox/button_send.py",
                    ):
                        self.assertEqual(
                            (fixture.root / "opt/messagebox" / relative).read_bytes(),
                            (ROOT / relative).read_bytes(),
                        )
                    updater.rollback(
                        fixture.backup, root=fixture.root, run=fixture.systemctl
                    )
                    fixture.assert_original_state(self)
