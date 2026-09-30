import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.business_receive import (
    BusinessInboxClient,
    BusinessReceiveError,
    poll_once,
)
from messagebox.business_send import BusinessSendClient
from messagebox.contacts import ContactStore


PHONE = "351900000001"
JID = PHONE + "@s.whatsapp.net"


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, count):
        return self.payload[:count]


def _entry(cursor=1, wamid="synthetic-inbound"):
    return {
        "cursor": cursor,
        "message": {
            "wamid": wamid,
            "from": PHONE,
            "timestamp": 100,
            "type": "audio",
            "audio": {"uploadKey": "up:synthetic-audio"},
        },
    }


class BusinessInboxClientTests(unittest.TestCase):
    def setUp(self):
        self.config = BusinessSendClient("https://test.invalid", "A", "synthetic-token")

    def test_poll_and_ack_use_authenticated_exact_box_and_cursor(self):
        requests = []

        def open_request(request, *, timeout):
            requests.append((request, timeout))
            if request.full_url.endswith("device-inbox?box=A"):
                return _Response(json.dumps({"box": "A", "cursor": 0, "messages": [_entry()]}).encode())
            return _Response(b'{"ok":true,"box":"A","cursor":1}')

        client = BusinessInboxClient(self.config, opener=open_request)
        self.assertEqual(client.inbox()[1][0]["cursor"], 1)
        client.ack(1, "synthetic-inbound")
        self.assertEqual(requests[0][0].get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(requests[0][0].get_header("User-agent"), "ButtonBox/0.1.0")
        self.assertEqual(requests[1][0].get_method(), "POST")
        self.assertEqual(json.loads(requests[1][0].data)["cursor"], 1)
        self.assertEqual([timeout for _request, timeout in requests], [30, 30])

    def test_malformed_order_or_route_is_rejected_before_queue(self):
        for payload in (
            {"box": "B", "cursor": 0, "messages": [_entry()]},
            {"box": "A", "cursor": 0, "messages": [_entry(2)]},
            {"box": "A", "cursor": 0, "messages": [{**_entry(), "message": {**_entry()["message"], "from": "bad"}}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(BusinessReceiveError):
                client = BusinessInboxClient(
                    self.config, opener=lambda *_args, **_kwargs: _Response(json.dumps(payload).encode())
                )
                client.inbox()


class BusinessQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.queue = root / "queue"
        self.ledger = root / "business-queued.json"
        self.contacts = root / "contacts.json"
        ContactStore(self.contacts).add_contact(JID, "Synthetic", receive_after=0)
        self.entry = _entry()
        self.client = mock.Mock()
        self.client.inbox.return_value = (0, [self.entry])
        self.client.download.return_value = b"OggSsynthetic"
        self.calls = []

    def queue_func(self, message, _source, *, queue_dir):
        self.calls.append(message)
        queue_dir.mkdir(parents=True, exist_ok=True)
        wav = queue_dir / message["QueueFilename"]
        wav.write_bytes(b"synthetic-wav")
        Path(str(wav) + ".json").write_text(
            json.dumps({"msgid": message["MsgID"], "chat": message["ChatJID"]})
        )
        return wav, 1.0

    def poll(self):
        return poll_once(
            self.client, queue_dir=self.queue, ledger_path=self.ledger,
            contacts_path=self.contacts, queue_func=self.queue_func,
        )

    def test_queue_before_ack_and_lost_ack_does_not_duplicate(self):
        def ack(cursor, wamid):
            self.assertEqual((cursor, wamid), (1, "synthetic-inbound"))
            self.assertEqual(len(self.calls), 1)
            self.assertTrue(self.ledger.exists())
            raise BusinessReceiveError("lost acknowledgement")

        self.client.ack.side_effect = ack
        with self.assertRaises(BusinessReceiveError):
            self.poll()
        self.client.ack.side_effect = None
        self.assertEqual(self.poll(), 0)
        self.assertEqual(len(self.calls), 1)
        self.client.download.assert_called_once()
        self.assertEqual(self.client.ack.call_count, 2)

    def test_crash_after_queue_before_ledger_recovers_existing_wav(self):
        media = {
            "MsgID": self.entry["message"]["wamid"], "ChatJID": JID,
            "QueueFilename": "0000000100000-" + __import__("hashlib").sha256(
                b"synthetic-inbound"
            ).hexdigest()[:24] + ".wav",
        }
        self.queue_func(media, None, queue_dir=self.queue)
        self.assertEqual(self.poll(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.ledger.exists())
        self.client.download.assert_not_called()
        self.client.ack.assert_called_once()

    def test_removed_recipient_is_acknowledged_without_download(self):
        ContactStore(self.contacts).remove_contact(JID)
        self.assertEqual(self.poll(), 0)
        self.client.download.assert_not_called()
        self.client.ack.assert_called_once()

    def test_message_before_receive_authorization_is_not_queued(self):
        ContactStore(self.contacts).remove_contact(JID)
        ContactStore(self.contacts).add_contact(JID, "Synthetic", receive_after=200)
        self.assertEqual(self.poll(), 0)
        self.client.download.assert_not_called()
        self.client.ack.assert_called_once()

    def test_corrupt_contact_store_blocks_ack(self):
        self.contacts.write_text("not-json")
        with self.assertRaises(Exception):
            self.poll()
        self.client.ack.assert_not_called()

    def test_media_or_queue_failure_keeps_server_item_pending(self):
        self.client.download.side_effect = BusinessReceiveError("media unavailable")
        with self.assertRaises(BusinessReceiveError):
            self.poll()
        self.client.ack.assert_not_called()
        self.client.download.side_effect = None
        with mock.patch.object(self, "queue_func", side_effect=RuntimeError("decode failed")):
            with self.assertRaises(RuntimeError):
                self.poll()
        self.client.ack.assert_not_called()
