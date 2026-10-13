"""Cloud voice feature contracts and the unchanged self-hosted boundary."""
import hashlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from messagebox import cloud_runtime
from messagebox.cloud_device import CloudDeviceError
from messagebox.cloud_voice import ReactionQueue, VoiceCache
import test_cloud_runtime as runtime_tests
from test_cloud_runtime import NOW, PERSON, OP, MID
import test_wacli_contract as wacli_tests

SOUNDS = Path(__file__).resolve().parents[1] / "sounds"


def wav():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as out:
        out.setparams((1, 2, 48000, 0, 'NONE', ''))
        out.writeframes(b'\0\0' * 48)
    return stream.getvalue()


class VoiceCacheTests(unittest.TestCase):
    def test_clip_id_cache_hash_format_fallback_and_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = VoiceCache(directory)
            audio = wav()
            client = mock.Mock()
            client.media.return_value = audio
            payload = {'clip_id':'a'*64, 'sha256':hashlib.sha256(audio).hexdigest(),
                       'content_type':'audio/wav', 'media_url':'https://cloud.example/clip'}
            path = cache.download(client, payload)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(cache.download(client, payload), path)
            self.assertEqual(client.media.call_count, 1)
            self.assertEqual(cache.download(client, {'use_fallback':True}), '')
            self.assertEqual(cache.download(client, {**payload,'sha256':'f'*64}), '')
            self.assertEqual(cache.download(client, {**payload,'clip_id':'../private'}), '')
            client.media.side_effect = CloudDeviceError('offline')
            self.assertEqual(cache.download(client, {**payload,'clip_id':'b'*64}), '')

    def test_bundled_english_fallbacks_and_every_summary_have_audio(self):
        root = Path(__file__).resolve().parents[1] / 'sounds/cloud'
        manifest = json.loads((root/'manifest.json').read_text())
        self.assertEqual(len(manifest['lines']), 100)
        for row in manifest['lines']:
            path = root/row['file']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['sha256'])
            with wave.open(str(path)) as audio:
                self.assertEqual((audio.getnchannels(),audio.getsampwidth(),audio.getframerate()),(1,2,48000))
                self.assertGreater(audio.getnframes(),0)


class ReactionTests(unittest.TestCase):
    setUp = runtime_tests.CloudRuntimeTests.setUp

    def reaction(self, n=0):
        return {'operation_id':f'reaction:{n}', 'expires_at':NOW+600,
                'payload':{'event_id':f'event-{n}', 'message_id':MID, 'sender_id':PERSON['id'],
                           'sender_kind':'person', 'use_fallback':True}}

    def test_quiet_mute_queue_then_button_three_lines_and_summary_restart_dedup(self):
        self.runtime.heartbeat()
        self.runtime.silence = mock.Mock()
        self.runtime.silence.load.return_value = {'muted':True,'quiet_until':'manual'}
        for n in range(5):
            self.runtime._reaction(self.reaction(n))
        played=[]
        with mock.patch.object(cloud_runtime,'CloudRuntime',return_value=self.runtime), \
             mock.patch('messagebox.cloud_voice.APP_DIR',Path(__file__).resolve().parents[1]):
            self.assertFalse(cloud_runtime.play_reactions(played.append))
            self.assertEqual(played,[])
            self.assertTrue(cloud_runtime.play_reactions(played.append,deliberate=True))
            self.assertEqual(len(played),5)  # chime, three reactions, one summary
            self.assertEqual(played[-1].name,'more-2.wav')
            self.runtime._reaction(self.reaction(0))
            self.assertFalse(cloud_runtime.play_reactions(played.append,deliberate=True))

    def test_switch_member_removal_expiry_and_scope_changes_drop_reactions(self):
        self.runtime.heartbeat()
        self.runtime._reaction(self.reaction())
        queue=ReactionQueue(self.runtime.state_path.parent/'cloud-reactions.json')
        self.assertEqual(queue.take('a'*64,set(),now=NOW),([],0))
        self.runtime._reaction(self.reaction(1))
        self.assertEqual(queue.take('b'*64,{('person',PERSON['id'])},now=NOW),([],0))
        self.runtime._reaction(self.reaction(2))
        self.assertEqual(queue.take('a'*64,{('person',PERSON['id'])},now=NOW+601),([],0))
        runtime_tests.CloudRuntimeTests.update_settings(self,say_reactions=False)
        self.runtime._reaction(self.reaction(3))
        self.assertEqual(queue.take('a'*64,{('person',PERSON['id'])},now=NOW),([],0))

    def test_name_service_card_cache_and_old_cloud_fallback(self):
        self.runtime.heartbeat()
        audio=wav()
        self.client.voice_line=mock.Mock(return_value={'clip_id':'a'*64,'voice_pack':'jessica',
           'sha256':hashlib.sha256(audio).hexdigest(),'content_type':'audio/wav','media_url':'https://cloud.example/clip'})
        self.client.media=mock.Mock(return_value=audio)
        self.runtime._preload_voice_lines()
        jid=PERSON['wa_id']+'@s.whatsapp.net'
        self.assertTrue(self.runtime.card_prompt_clip(jid,'jessica'))
        self.assertTrue(self.runtime.named_card_clip(jid,'jessica','card_saved'))
        before=self.client.voice_line.call_count
        self.runtime._preload_voice_lines()
        self.assertEqual(self.client.voice_line.call_count,before)
        self.assertEqual(self.client.media.call_count,1)
        self.client.voice_line.side_effect=CloudDeviceError('old cloud')
        self.assertEqual(self.runtime.fetch_voice_line('listened',identity_id=PERSON['id']),'')

    def test_text_fallback_uses_normal_message_queue(self):
        self.runtime.heartbeat()
        item=runtime_tests.CloudRuntimeTests.audio_item(self)
        item['payload'].update(text_read_aloud=True,use_fallback=True,sha256=None)
        with mock.patch('messagebox.cloud_runtime.fallback',return_value=Path(__file__).resolve().parents[1]/'sounds/cloud/text-message.wav'):
            self.runtime._audio(item,NOW)
        metadata=json.loads(next(self.runtime.queue_dir.glob('*.wav.json')).read_text())
        self.assertTrue(metadata['text_read_aloud'])
        self.assertEqual(metadata['cloud_message_id'],MID)
        self.assertEqual(metadata['sender_id'],PERSON['id'])

    def test_heart_pairing_uses_existing_nfc_flow_without_a_send_recipient(self):
        heartbeat=self.client.heartbeat
        self.client.heartbeat=lambda state:{**heartbeat(state),'voice_features':{'reactions':1}}
        self.runtime.heartbeat()
        item={'operation_id':OP,'kind':'nfc_enroll','created_at':NOW,'expires_at':NOW+120,
              'payload':{'recipient_id':'reaction_heart'}}
        self.runtime._nfc(item)
        self.assertEqual(cloud_runtime.EnrollmentStore(cloud_runtime.NFC_ENROLLMENT_FILE).active()['jid'],'box:reaction_heart')
        with self.assertRaises(cloud_runtime.CloudRuntimeError):
            self.runtime.recipient_id('box:reaction_heart')

    def test_reaction_cards_sync_enroll_remove_and_retain_valid_section(self):
        heartbeat = self.client.heartbeat
        cards = [{"id": "reaction_heart", "emoji": "❤️"}, {"id": "reaction_laugh", "emoji": "😂"}]
        self.client.heartbeat = lambda state: {**heartbeat(state), "voice_features": {"reactions": 1}, "reaction_cards": cards}
        self.runtime.heartbeat()
        self.assertIn("box:reaction_laugh", self.runtime.contacts.load()["contacts"])
        self.runtime._nfc({"operation_id": OP, "kind": "nfc_enroll", "created_at": NOW, "expires_at": NOW+120,
                           "payload": {"recipient_id": "reaction_laugh"}})
        self.assertEqual(cloud_runtime.EnrollmentStore(cloud_runtime.NFC_ENROLLMENT_FILE).active()["jid"], "box:reaction_laugh")
        with self.assertRaises(cloud_runtime.CloudRuntimeError):
            self.runtime.recipient_id("box:reaction_laugh")
        cards = [{"id": "reaction_bad"}]
        self.runtime.heartbeat()
        self.assertIn("box:reaction_laugh", self.runtime.contacts.load()["contacts"])
        cards = []
        self.runtime.heartbeat()
        self.assertNotIn("box:reaction_laugh", self.runtime.contacts.load()["contacts"])

    def test_react_with_card_uses_id_and_old_cloud_uses_legacy_request(self):
        snapshot = {"account_scope": "synthetic-scope", "reactions": True,
                    "reaction_cards": [{"id": "reaction_laugh", "emoji": "😂"}]}
        self.runtime._snapshot = lambda: snapshot
        self.runtime.server_now = lambda: NOW
        self.runtime.flush_acks = mock.Mock()
        self.runtime.client.send_reaction = mock.Mock(return_value={"state": "sent"})
        (self.runtime.state_path.parent / "last-played.json").write_text(json.dumps(
            {"scope": "synthetic-scope", "message_id": MID, "played_at": NOW}))
        with mock.patch.object(cloud_runtime, "CloudRuntime", return_value=self.runtime):
            self.assertEqual(cloud_runtime.react_with_card("reaction_laugh"), "sent")
            self.assertEqual(self.runtime.client.send_reaction.call_args.args[2], "reaction_laugh")
            self.assertEqual(cloud_runtime.react_with_card("reaction_removed"), "nothing_to_react_to")
            snapshot.pop("reaction_cards")
            self.assertEqual(cloud_runtime.react_with_heart(), "sent")
            self.assertEqual(len(self.runtime.client.send_reaction.call_args.args), 2)


class WacliVoiceBoundaryTests(unittest.TestCase):
    setUp = wacli_tests.WacliContractTests.setUp
    set_quiet = wacli_tests.WacliContractTests.set_quiet
    send_to = wacli_tests.WacliContractTests.send_to


    def test_cloud_reaction_card_tap_sends_its_id_and_does_not_select_voice_recipient(self):
        from messagebox import button_send
        from messagebox.nfc import SelectionStore
        self.contacts.add_contact("box:reaction_laugh", "Laugh card", receive_after=100)
        self.contacts.assign_card("box:reaction_laugh", wacli_tests.CARD)
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(cloud_runtime, "react_with_card", return_value="sent") as react, \
             mock.patch.object(cloud_runtime, "reaction_card_emoji", return_value="\U0001f602"), \
             mock.patch.object(button_send.sound_pack, "SOUND_DIR", SOUNDS), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as sound:
            self.assertTrue(button_send._play_nfc_prompt(wacli_tests.CARD, "selected", ""))
            react.assert_called_once_with("reaction_laugh")
            self.assertEqual([call.args[0].name for call in sound.call_args_list],
                             ["cue-card.wav", "voice-sent-big-laugh.wav"])
            self.assertIsNone(SelectionStore(button_send.NFC_SELECTION_FILE).load())

    def reaction_tap(self, result, *, beep=True, say=True, emoji="\u2764\ufe0f", pack="jessica"):
        from messagebox import button_send
        if "box:reaction_heart" not in self.contacts.load()["contacts"]:
            self.contacts.add_contact("box:reaction_heart", "Heart card", receive_after=100)
            self.contacts.assign_card("box:reaction_heart", wacli_tests.CARD)
        settings = {**button_send.caregiver_settings(), "nfc_confirmation_beep": beep,
                    "card_name_prompt": say, "voice_pack": pack}
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "caregiver_settings", return_value=settings), \
             mock.patch.object(cloud_runtime, "react_with_heart", return_value=result), \
             mock.patch.object(cloud_runtime, "reaction_card_emoji", return_value=emoji), \
             mock.patch.object(button_send.sound_pack, "SOUND_DIR", SOUNDS), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as sound:
            self.assertTrue(button_send._play_nfc_prompt(wacli_tests.CARD, "selected", ""))
        return [str(call.args[0].relative_to(call.args[0].parents[2])) if "voices" in call.args[0].parts
                else call.args[0].name for call in sound.call_args_list]

    def test_reaction_card_says_what_was_sent_only_after_the_send_is_accepted(self):
        self.assertEqual(self.reaction_tap("sent"), ["cue-card.wav", "voice-sent-heart.wav"])
        self.assertEqual(self.reaction_tap("sent", pack="pirate"), ["cue-card.wav", "voices/pirate/voice-sent-heart.wav"])
        self.assertEqual(self.reaction_tap("sent", emoji="\U0001f680"), ["cue-card.wav", "voice-sent-reaction.wav"])
        # The card cue is the success signal; the family-card beep switch does not apply.
        self.assertEqual(self.reaction_tap("sent", beep=False), ["cue-card.wav", "voice-sent-heart.wav"])
        self.assertEqual(self.reaction_tap("sent", say=False), ["cue-card.wav"])
        self.assertEqual(self.reaction_tap("sent", beep=False, say=False), ["cue-card.wav"])
        # A failed or uncertain send keeps the failure sound and never claims it was sent.
        for result in ("nothing_to_react_to", "uncertain", "failed"):
            with self.subTest(result=result):
                self.assertEqual(self.reaction_tap(result), ["cue-oops.wav"])

    def test_reaction_card_emoji_is_read_from_the_local_snapshot(self):
        runtime = mock.Mock()
        runtime._snapshot.return_value = {}
        runtime._reaction_cards.return_value = [{"id": "reaction_heart", "emoji": "\u2764\ufe0f"},
                                                {"id": "reaction_abc", "emoji": "\U0001f44d"}]
        with mock.patch.object(cloud_runtime, "CloudRuntime", return_value=runtime):
            self.assertEqual(cloud_runtime.reaction_card_emoji("reaction_abc"), "\U0001f44d")
            self.assertEqual(cloud_runtime.reaction_card_emoji("reaction_gone"), "")
        runtime._snapshot.side_effect = cloud_runtime.CloudRuntimeError("no snapshot")
        with mock.patch.object(cloud_runtime, "CloudRuntime", return_value=runtime):
            self.assertEqual(cloud_runtime.reaction_card_emoji("reaction_heart"), "\u2764\ufe0f")
            self.assertEqual(cloud_runtime.reaction_card_emoji("reaction_abc"), "")

    def test_self_hosted_card_and_press_never_call_cloud_voice_service(self):
        with mock.patch.object(cloud_runtime,'play_reactions',side_effect=AssertionError('Cloud in wacli')), \
             mock.patch.object(cloud_runtime,'card_prompt_clip',side_effect=AssertionError('Cloud in wacli')), \
             mock.patch.object(cloud_runtime,'react_with_heart',side_effect=AssertionError('Cloud in wacli')), \
             mock.patch.object(cloud_runtime,'react_with_card',side_effect=AssertionError('Cloud in wacli')):
            wacli_tests.WacliContractTests.test_family_card_selects_person_and_sends_exact_jid(self)
