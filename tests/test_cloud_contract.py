"""Keep config/cloud-contract.json equal to the Cloud calls the client makes."""

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from messagebox import cloud_events
from messagebox.cloud_device import CloudDeviceClient

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "scripts" / "dev" / "cloud_contract.py"
_spec = importlib.util.spec_from_file_location("cloud_contract", GENERATOR)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)

API = "https://cloud.invalid/cloud-api/v1"
SYNTHETIC_ID = "synthetic-contract-0001"
SHA = "a" * 64


class _Response:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.payload[:limit]


def _entry_for(entries, method, path):
    for entry in entries:
        pattern = re.sub(r"\\\{(\w+)\\\}", lambda match: "(?P<%s>[^/]+)" % match.group(1), re.escape(entry["path"]))
        found = re.fullmatch(pattern, path)
        if entry["method"] == method and found and all(
                re.fullmatch(entry.get("params", {})[name], value) for name, value in found.groupdict().items()):
            return entry
    return None


class CloudContractManifestTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((REPO / contract.MANIFEST).read_text(encoding="utf-8"))

    def test_committed_manifest_is_current(self):
        self.assertEqual(contract.render(contract.build(REPO)), (REPO / contract.MANIFEST).read_text(encoding="utf-8"),
                         "config/cloud-contract.json is stale. Run: python3 scripts/dev/cloud_contract.py")
        self.assertEqual(contract.main(["--check"]), 0)

    def test_manifest_shape(self):
        entries = self.document["entries"]
        self.assertEqual(self.document["api_base"], "/cloud-api/v1")
        self.assertEqual(len({entry["id"] for entry in entries}), len(entries))
        for entry in entries:
            with self.subTest(entry=entry["id"]):
                self.assertIn(entry["method"], {"GET", "POST"})
                self.assertTrue(entry["path"].startswith("/device/"))
                self.assertIn(entry["device_state"], {"new", "unclaimed", "claimed"})
                self.assertIn(entry["response"]["content"], {"json", "bytes", "websocket"})
                self.assertTrue(entry["response"]["status"])
                for name in re.findall(r"\{(\w+)\}", entry["path"]):
                    self.assertIn(name, entry["params"])
        self.assertEqual(self.document["registration_texts"], [
            "claim {claim_token}", "Hi! I'd like to register my Button Box. Its code is {claim_token}"])
        cancel = next(entry for entry in entries if entry["id"] == "claim_cancel")
        self.assertEqual(sorted(variant["fields"]["cancelled"]["const"] for variant in cancel["response"]["variants"]),
                         [False, True])

    def _copy(self, directory):
        root = Path(directory)
        for relative in [*contract.SCANNED, "messagebox/cloud_claim.py"]:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / relative, root / relative)
        return root

    def test_new_unlisted_cloud_call_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._copy(directory)
            source = root / "messagebox" / "cloud_device.py"
            text = source.read_text(encoding="utf-8")
            marker = "    def heartbeat(self, state: dict) -> dict:\n"
            source.write_text(text.replace(marker, (
                "    def forget_me(self) -> dict:\n"
                "        return self.json(\"/device/forget\", method=\"POST\", body={})\n\n" + marker)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"CloudDeviceClient\.forget_me calls .*/device/forget"):
                contract.build(root)

    def test_changed_method_or_path_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._copy(directory)
            source = root / "messagebox" / "cloud_device.py"
            text = source.read_text(encoding="utf-8")
            source.write_text(text.replace('"/device/claim/cancel", method="POST"', '"/device/claim/cancel", method="PUT"'),
                              encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"cancel_claim: source calls .*PUT"):
                contract.build(root)

    def test_missing_advertised_capability_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._copy(directory)
            source = root / "messagebox" / "cloud_device.py"
            text = source.read_text(encoding="utf-8")
            source.write_text(text.replace('"voice_lines": 1,', '"voice_lines": 2,'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not advertise voice_lines=1"):
                contract.build(root)


class CloudContractClientTests(unittest.TestCase):
    """Drive the real client: every request it sends must match one entry."""

    def test_every_client_request_matches_an_entry_and_every_entry_is_used(self):
        document = json.loads((REPO / contract.MANIFEST).read_text(encoding="utf-8"))
        entries = document["entries"]
        seen = []

        def opener(request, *, timeout):
            seen.append(request)
            path = urlsplit(request.full_url).path
            if path.endswith("/device/reactions"):
                return _Response({"state": "sent"})
            if path.endswith("/device/voice"):
                return _Response({"message_id": "m1", "state": "queued", "expires_at": 1_800_000_000,
                                  "server_time": 1_800_000_000}, status=202)
            return _Response({"message_id": "m1", "state": "queued", "expires_at": 1_800_000_000,
                              "server_time": 1_800_000_000, "deleted": False})

        identity = {"device_id": SYNTHETIC_ID, "credential": "x" * 43}
        client = CloudDeviceClient(API, identity, opener=opener)
        client.register({"audio": True})
        client.claim()
        client.setup_checkin()
        client.confirm_claim(SYNTHETIC_ID)
        client.cancel_claim(SYNTHETIC_ID)
        client.heartbeat({"version": "cloud-mvp-1"})
        client.inbox(0)
        client.ack({"operation_id": SYNTHETIC_ID, "state": "received"})
        client.voice_line("listened", identity_id="person")
        client.send_reaction("m1", SYNTHETIC_ID)
        client.voice_status(SYNTHETIC_ID)
        client.media(API + "/device/media/m1")
        client.media(API + "/device/listened-media/" + SHA)
        client.media(API + "/device/voice-lines/" + SHA)
        with tempfile.TemporaryDirectory() as directory:
            ogg = Path(directory) / "voice.ogg"
            ogg.write_bytes(b"OggS" + b"\0" * 32)
            client.send_voice(ogg, "person", SYNTHETIC_ID, 1.5, account_scope=SHA)

        sockets = []

        class _Socket:
            def getstatus(self):
                return 101

        def create_connection(url, **options):
            sockets.append((url, options))
            return _Socket()

        with mock.patch.dict(sys.modules, {"websocket": types.SimpleNamespace(create_connection=create_connection)}):
            cloud_events.connect_events(client)

        used = set()
        for request in seen:
            url = urlsplit(request.full_url)
            self.assertTrue(url.path.startswith(document["api_base"] + "/"))
            path = url.path.removeprefix(document["api_base"])
            entry = _entry_for(entries, request.get_method(), path)
            self.assertIsNotNone(entry, f"client sent {request.get_method()} {path} but no manifest entry matches")
            used.add(entry["id"])
            query = parse_qs(url.query)
            self.assertEqual(set(query), set(entry.get("query", {})), entry["id"])
            spec = entry.get("request", {})
            if spec.get("content_type") == "application/json":
                body = json.loads(request.data)
                fields = spec["body"]
                self.assertLessEqual(set(body), set(fields), f"{entry['id']} sends undeclared fields")
                required = {name for name, rule in fields.items() if not rule.get("optional")}
                if entry["id"] != "heartbeat":  # The runtime, not the client, builds heartbeat bodies.
                    self.assertLessEqual(required, set(body), f"{entry['id']} omits required fields")
            for header in spec.get("headers", {}):
                self.assertIsNotNone(request.get_header(header.capitalize()), f"{entry['id']} omits {header}")
            if entry["device_state"] != "new" or "Authorization" in request.headers:
                self.assertTrue(request.get_header("Authorization", "").startswith("Bearer "))
        (url, options), = sockets
        self.assertEqual(urlsplit(url).scheme, "wss")
        events = _entry_for(entries, "GET", urlsplit(url).path.removeprefix(document["api_base"]))
        self.assertIsNotNone(events)
        used.add(events["id"])
        self.assertEqual(used, {entry["id"] for entry in entries})


if __name__ == "__main__":
    unittest.main()
