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


class WacliVoiceBoundaryTests(unittest.TestCase):
    setUp = wacli_tests.WacliContractTests.setUp
    set_quiet = wacli_tests.WacliContractTests.set_quiet
    send_to = wacli_tests.WacliContractTests.send_to

    def test_self_hosted_card_and_press_never_call_cloud_voice_service(self):
        with mock.patch.object(cloud_runtime,'play_reactions',side_effect=AssertionError('Cloud in wacli')), \
             mock.patch.object(cloud_runtime,'card_prompt_clip',side_effect=AssertionError('Cloud in wacli')), \
             mock.patch.object(cloud_runtime,'react_with_heart',side_effect=AssertionError('Cloud in wacli')):
            wacli_tests.WacliContractTests.test_family_card_selects_person_and_sends_exact_jid(self)
