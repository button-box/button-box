import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


import messagebox.dashboard.app as dashboard


class DashboardQueueHoldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = self.root / "queue"
        self.hold = self.queue / ".hold"
        self.trash = self.queue / ".trash"
        for path in (self.queue, self.hold, self.trash):
            path.mkdir(parents=True, exist_ok=True)

        self.originals = {
            name: getattr(dashboard, name)
            for name in (
                "QUEUE_DIR",
                "HOLD_DIR",
                "TRASH_DIR",
                "EVENTS_FILE",
                "CONTACTS_FILE",
                "LISTENED_DIR",
                "OUTBOX_DIR",
            )
        }
        self.family = "120363000001@g.us"
        dashboard.QUEUE_DIR = str(self.queue)
        dashboard.HOLD_DIR = str(self.hold)
        dashboard.TRASH_DIR = str(self.trash)
        dashboard.EVENTS_FILE = str(self.root / "events.jsonl")
        dashboard.CONTACTS_FILE = self.root / "contacts.json"
        dashboard.LISTENED_DIR = str(self.root / "listened")
        dashboard.OUTBOX_DIR = str(self.root / "outbox")
        dashboard.PUBLIC_MESSAGES.clear()
        dashboard.PUBLIC_MESSAGE_REVERSE.clear()
        dashboard.contacts_store().add_contact(self.family, "Family")

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(dashboard, name, value)
        self.temporary.cleanup()

    def post(self, path):
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        handler.headers = {"Host": "button-box.local"}
        handler.client_address = ("192.168.1.20", 12345)
        handler.local_host = "button-box.local"
        response = {}

        def send(code, body, ctype="application/json"):
            response.update(code=code, body=json.loads(body), ctype=ctype)

        handler._send = send
        handler.do_POST()
        return response

    def make_message(self, name="1000-message.wav"):
        wav = self.queue / name
        wav.write_bytes(b"RIFF-test")
        Path(str(wav) + ".json").write_text(
            json.dumps({"chat": self.family, "msgid": "message"})
        )
        Path(dashboard.EVENTS_FILE).write_text(
            json.dumps(
                {
                    "type": "received",
                    "ts": 1.0,
                    "file": name,
                    "chat": self.family,
                    "sender": "Mommy",
                }
            )
            + "\n"
        )
        return wav

    def token(self, kind="queue", name="1000-message.wav"):
        return dashboard.public_message_token(kind, name)

    def test_hold_removes_message_from_player_queue_and_resume_restores_it(self):
        wav = self.make_message()
        sidecar = Path(str(wav) + ".json")

        self.assertEqual(self.post(f"/api/hold?f={self.token()}")["code"], 200)
        self.assertFalse(wav.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue((self.hold / wav.name).exists())
        self.assertTrue((self.hold / (wav.name + ".json")).exists())

        data = dashboard.build_data()
        self.assertEqual(data["queue"], [])
        self.assertEqual(len(data["hold"]), 1)
        self.assertEqual(data["hold"][0]["sender"], "Mommy")
        self.assertEqual(data["hold"][0]["chat"], "Family")

        hold_token = data["hold"][0]["token"]
        self.assertEqual(self.post(f"/api/resume?f={hold_token}")["code"], 200)
        self.assertTrue(wav.exists())
        self.assertTrue(sidecar.exists())
        self.assertFalse((self.hold / wav.name).exists())
        self.assertEqual(dashboard.list_wavs(str(self.queue))[0]["file"], wav.name)

    def test_destination_conflict_does_not_overwrite_or_lose_message(self):
        wav = self.make_message()
        destination = self.hold / wav.name
        destination.write_bytes(b"existing")

        response = self.post(f"/api/hold?f={self.token()}")

        self.assertEqual(response["code"], 409)
        self.assertTrue(wav.exists())
        self.assertEqual(destination.read_bytes(), b"existing")

    def test_resume_rolls_sidecar_back_if_wav_move_fails(self):
        wav = self.make_message()
        self.assertEqual(self.post(f"/api/hold?f={self.token()}")["code"], 200)
        held_wav = self.hold / wav.name
        held_meta = self.hold / (wav.name + ".json")
        real_replace = dashboard.os.replace

        def replace(source, destination):
            if Path(source) == held_wav and Path(destination) == wav:
                raise OSError("simulated move failure")
            return real_replace(source, destination)

        with mock.patch.object(dashboard.os, "replace", side_effect=replace):
            response = self.post(f"/api/resume?f={self.token('hold')}")

        self.assertEqual(response["code"], 500)
        self.assertTrue(held_wav.exists())
        self.assertTrue(held_meta.exists())
        self.assertFalse(wav.exists())
        self.assertFalse(Path(str(wav) + ".json").exists())

    def test_held_audio_remains_streamable_from_dashboard(self):
        wav = self.make_message()
        expected = wav.read_bytes()
        self.assertEqual(self.post(f"/api/hold?f={self.token()}")["code"], 200)
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = f"/audio/{self.token('hold')}?hold=1"
        handler.headers = {"Host": "button-box.local"}
        handler.client_address = ("192.168.1.20", 12345)
        handler.local_host = "button-box.local"
        response = {}

        def send(code, body, ctype="application/json"):
            response.update(code=code, body=body, ctype=ctype)

        handler._send = send
        handler.do_GET()
        self.assertEqual(response, {"code": 200, "body": expected, "ctype": "audio/wav"})

    def test_dashboard_serves_static_assets(self):
        expected_types = {
            "/": "text/html; charset=utf-8",
            "/static/app.js": "text/javascript; charset=utf-8",
            "/static/styles.css": "text/css; charset=utf-8",
        }

        for path, expected_type in expected_types.items():
            with self.subTest(path=path):
                handler = dashboard.Handler.__new__(dashboard.Handler)
                handler.path = path
                handler.headers = {"Host": "button-box.local"}
                handler.client_address = ("192.168.1.20", 12345)
                handler.local_host = "button-box.local"
                response = {}
                handler._send = lambda code, body, ctype: response.update(
                    code=code, body=body, ctype=ctype
                )

                handler.do_GET()

                self.assertEqual(response["code"], 200)
                self.assertTrue(response["body"])
                self.assertEqual(response["ctype"], expected_type)

if __name__ == "__main__":
    unittest.main()
