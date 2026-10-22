"""Signed bridge admission through unmodified field workers and real clients."""
import base64
import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bridge_admission import AdmissionHost
from field_updater import FieldBox, PROVENANCE, mkdir, write
from messagebox import cloud_update, cloud_updater_permit, updater_admission as admission
from messagebox import controller_selector as selector, controller_slots as slots
import test_controller_descriptor as descriptors
from test_controller_slots import DurableView, Host
from test_release_tooling import build

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('minisign'), 'real minisign required')
class AdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        descriptors.ControllerProofTests.setUpClass()
        cls.keys = descriptors.ControllerProofTests.key_pairs

    @classmethod
    def tearDownClass(cls):
        descriptors.ControllerProofTests.tearDownClass()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='permit-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()

    def box(self, name='box', version='v0.3.12', *, decision='allow', mode='cloud'):
        box = FieldBox(self.base / name, version)
        write(box.root / 'etc/messagebox/env', ('MSGBOX_TRANSPORT=' + mode + '\n').encode())
        for filename in ('updater_admission.py','updater_rollout_keys.py','cloud_updater_permit.py',
                         'controller_selector.py','controller_slots.py'):
            write(box.source / 'messagebox' / filename, (ROOT / 'messagebox' / filename).read_bytes())
        write(box.source / 'scripts/dev/release-manifest.py', (ROOT / 'scripts/dev/release-manifest.py').read_bytes())
        for filename in selector.MODULES:
            write(box.source / 'scripts/install' / filename, (ROOT / 'scripts/install' / filename).read_bytes())
        for filename in ('messagebox-update-selector.service', 'messagebox-update-selector.timer',
                'messagebox-update-bootstrap.service', 'messagebox-update-publish.service',
                'messagebox-update-supervisor.service.d/messagebox.conf',
                'messagebox-update-discovery.service.d/messagebox.conf'):
            write(box.source / 'systemd' / filename, (ROOT / 'systemd' / filename).read_bytes())
        raw = build.controller_descriptor(box.source, 7, 'v0.3.13')
        document = write(self.base / 'descriptor', raw)
        signature = self.base / 'descriptor.sig'
        subprocess.run(['minisign','-S','-m',str(document),'-s',str(self.keys[0][1]),'-x',str(signature)],
                       check=True, capture_output=True)
        write(box.source / 'messagebox/controller_descriptor.py', raw)
        write(box.source / 'messagebox/controller_signature.py', (repr(signature.read_text()) + '\n').encode())
        host = AdmissionHost(box, *self.keys[1], decision=decision)
        host.pin(box.source)
        box.refresh()
        # bundle() adds this outer-envelope test pin; seed it before snapshots.
        write(box.root / 'etc/button-box-update/release-keys/synthetic.pub',
              b'untrusted comment: synthetic test key\n' + base64.b64encode(b'Ed' + b'k' * 40) + b'\n')
        return box, host

    def install(self, box, host):
        with box.controller() as controller:
            bundle = box.bundle(controller)
            with host.installed():
                controller.worker()
            return controller.read('result.json'), bundle

    def test_denied_expired_wrong_box_replayed_and_timeout_use_exact_old_retry_without_mutation(self):
        for decision in ('deny','expired','wrong-box','wrong-account','wrong-nonce','wrong-purpose','wrong-target','timeout'):
            for version in PROVENANCE['releases']:
                with self.subTest(decision=decision, version=version):
                    box, host = self.box(decision + version, version, decision=decision)
                    before = box.tree()
                    result, bundle = self.install(box, host)
                    self.assertEqual((result['state'], result['reason']), ('waiting_for_window','not_idle'))
                    self.assertEqual(box.tree(), before)
                    self.assertTrue(bundle.exists())
                    self.assertEqual(box.worker.read_state(box.root,'journal.json'), box.bootstrap_journal)
                    self.assertFalse((box.root / admission.STATE).exists())
                    self.assertIn(['systemctl','stop',admission.UNIT], host.calls)

    def test_allowed_bridge_bootstraps_and_selects_with_a_fresh_separate_promotion_permit(self):
        box, host = self.box()
        fixed = box.fixed_bytes()
        result, _ = self.install(box, host)
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(box.fixed_bytes(), fixed)
        root = box.root
        write(root / 'etc/button-box-update/release-keys/descriptor.pub', self.keys[0][0].read_bytes())
        runtime = Host(root)
        runtime.recovery_run = box.run
        slots.bootstrap(root, run=runtime)
        self.assertTrue(selector.inspect(root, selector.SLOTS[0])['legacy'])
        self.assertEqual(selector.recover(root, runtime, execute=runtime.execute), selector.SLOTS[0])
        with host.installed():
            candidate = slots.publish(root, run=runtime, platform=descriptors.PLATFORM,
                authorize=lambda *a, **k: admission.admit_promotion(*a, run=host.run),
                trial_options={'execute':runtime.execute,'probe':runtime.probe,'duration':10})
        self.assertEqual(candidate['version'], '1.3.0')
        self.assertEqual(selector.selected(root)['slot'], selector.SLOTS[1])
        admission.installed_capability(root)
        self.assertEqual([r['updater_permit_request']['purpose'] for r in host.requests], ['install_bridge','promote_controller'])
        self.assertNotEqual(host.requests[0]['updater_permit_request']['nonce'], host.requests[1]['updater_permit_request']['nonce'])
        report = admission.report(root, active=root / selector.SLOTS[1], run=host.run)
        self.assertEqual((report['active_slot'], report['last_trial']['result']), ('b','promoted'))
        self.assertNotIn('updater', host.requests[0])
        self.assertEqual(host.requests[1]['updater']['active_generation'], 0)
        self.assertTrue(report['capabilities']['controller_self_upgrade_v1'])
        self.assertEqual(box.fixed_bytes(), fixed)

    def test_replay_and_policy_floor_survive_application_rollback(self):
        box, host = self.box()
        with box.controller() as controller:
            box.bundle(controller)
            before = box.tree()
            original = box.worker._journal
            def fail(root, journal, phase):
                original(root, journal, phase)
                if phase == 'switched':
                    raise RuntimeError('forced bridge probation failure')
            with host.installed(), mock.patch.object(box.worker, '_journal', side_effect=fail):
                with self.assertRaises(box.worker.UpdateError):
                    controller.worker()
            self.assertEqual(box.tree(), before)
            receipt = json.loads((box.root / admission.STATE).read_bytes())
            self.assertEqual(receipt['policy_revision'], host.revision)
            self.assertEqual(box.worker.read_state(box.root,'journal.json')['phase'], 'rolled_back')
            # A second attempt has a new root nonce. A cached signed response
            # for the first operation cannot authorize it, even after rollback.
            host.replay = host.responses[0]
            box.bundle(controller, sequence=2)
            with host.installed():
                controller.worker()
            self.assertEqual(controller.read('result.json')['state'], 'waiting_for_window')
            self.assertEqual(box.tree(), before)
            host.replay, host.revision = None, receipt['policy_revision'] - 1
            with host.installed():
                controller.worker()
            self.assertEqual(controller.read('result.json')['state'], 'waiting_for_window')
            self.assertEqual(json.loads((box.root / admission.STATE).read_bytes()), receipt)

    def test_power_loss_during_bridge_install_preserves_old_controller_and_boot_recovery(self):
        for phase in ('file-copy','prepared','quiesced','switched','probation','restoring_activators','committed'):
            with self.subTest(phase=phase):
                box, host = self.box(phase)
                fixed = box.fixed_bytes()
                disk = DurableView(box.root)
                fsync, journal, install = os.fsync, box.worker._journal, box.worker._atomic_install
                def synced(fd):
                    fsync(fd)
                    disk.synced(fd)
                def cut(root, value, observed):
                    journal(root, value, observed)
                    if observed == phase:
                        raise KeyboardInterrupt('bridge power cut')
                def copy_cut(*args, **kwargs):
                    install(*args, **kwargs)
                    if phase == 'file-copy':
                        raise KeyboardInterrupt('bridge copy power cut')
                with box.controller() as controller:
                    box.bundle(controller)
                    # The signed bundle arrived before this simulated outage.
                    disk = DurableView(box.root)
                    with host.installed(), mock.patch.object(os, 'fsync', side_effect=synced), \
                            mock.patch.object(box.worker, '_journal', side_effect=cut), \
                            mock.patch.object(box.worker, '_atomic_install', side_effect=copy_cut):
                        with self.assertRaises(KeyboardInterrupt):
                            controller.worker()
                disk.power_loss()
                mkdir(box.root / 'run/lock')
                for state in box.states.values():
                    state['active'] = 'inactive'
                box.worker.recover_transaction(root=box.root, run=box.run, start=False)
                self.assertEqual(box.fixed_bytes(), fixed)
                selected = box.worker.controlled_release(box.root)
                self.assertTrue((box.root / selected.lstrip('/') / 'release.json').is_file())
                self.assertIn(box.worker.read_state(box.root,'journal.json')['phase'], {'committed','rolled_back'})

    def test_ask_mode_never_queues_bridge_without_button_and_wacli_never_constructs_cloud_client(self):
        box, host = self.box('ask')
        with box.controller() as controller:
            box.bundle(controller)
            policy = controller.policy()
            policy['mode'] = 'ask'
            controller.save('policy.json', policy)
            before = box.tree()
            with host.installed(), mock.patch.object(controller, 'start_install', side_effect=AssertionError('no button approval')):
                controller.Controller().automatic_if_ready(None)
            self.assertEqual(box.tree(), before)
            self.assertEqual(host.requests, [])
        box, host = self.box('wacli', mode='wacli')
        with host.installed(), mock.patch.object(cloud_updater_permit, 'exchange', side_effect=AssertionError('Cloud forbidden')):
            result, _ = self.install(box, host)
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(host.requests, [])
        self.assertEqual(host.calls, [])
        self.assertFalse((box.root / admission.STATE).exists())

    def test_real_minisign_vectors_binding_canonical_bounds_and_legacy_ed_format(self):
        box, host = self.box()
        self.install(box, host)
        envelope = json.loads(host.responses[0])
        payload = json.loads(base64.b64decode(envelope['payload_b64']))
        expected = {n:v for n,v in payload.items() if n not in {'schema','domain','action','key_id','policy_revision','issued_at','expires_at'}}
        keys = admission.rollout_keys(box.source / 'messagebox/updater_rollout_keys.py')
        directory = mkdir(self.base / 'verify')
        def check(raw):
            return admission.verify_permit(raw, expected, keys, directory, now=int(time.time()))
        check(host.responses[0])
        changes = ({'action':'deny'}, {'schema':True}, {'policy_revision':True}, {'expires_at':payload['issued_at']+301},
                   {'issued_at':payload['issued_at']+1000}, {'device_id':'wrong_device_12345678'},
                   {'release_sequence':True}, {'unknown':1})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                check(host.signed({**payload, **change}))
        for raw in (host.signed(payload, prehash=True), host.signed(payload, secret=self.keys[0][1]),
                    host.signed(payload, raw=json.dumps(payload).encode()),
                    host.signed(payload, raw=admission.canonical(payload).replace(b'{', b'{"schema":1,', 1)),
                    b' ' * 8193):
            with self.assertRaises(ValueError):
                check(raw)

    def test_updater_report_uses_actual_slots_and_invalid_section_does_not_change_software_update(self):
        box, host = self.box()
        self.install(box, host)
        value = admission.report(box.root, run=host.run)
        self.assertIsNone(value)  # Receiver requires an installed known selector.
        write(box.root / 'etc/button-box-update/release-keys/descriptor.pub', self.keys[0][0].read_bytes())
        runtime = Host(box.root)
        runtime.recovery_run = box.run
        slots.bootstrap(box.root, run=runtime)
        value = admission.report(box.root, run=host.run)
        self.assertEqual(value['slots']['a'], {'slot_id':'a','generation':0,'state':'GOOD',
            'controller_version':'1.1.0','set_sha256':admission.set_hash(box.root / selector.SLOTS[0])})
        self.assertEqual(value['selector_version'], '1.0.0')
        self.assertEqual(value['active_generation'], 0)
        self.assertIsNone(value['last_trial'])
        status = {'installed':True, 'effective_mode':'auto', 'updater':value}
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT':'cloud'}), mock.patch.object(cloud_update, 'status', return_value=status):
            fields = cloud_update.heartbeat_fields()
            self.assertEqual(fields['updater'], value)
            bad = copy.deepcopy(value)
            bad['slots']['a']['generation'] = True
            status['updater'] = bad
            self.assertNotIn('updater', cloud_update.heartbeat_fields())
            self.assertEqual(cloud_update.heartbeat_fields()['software_update'], fields['software_update'])
            for field, item in (('schema',2), ('reporter','future'), ('controller_version','1'*33),
                    ('active_slot','c'), ('discovery_timeout_seconds',3601), ('active_generation',True),
                    ('selector_version','01.0.0'), ('unknown','x'*4096), ('unknown',{'a':{'b':{'c':{'d':1}}}})):
                status['updater'] = {**value, field:item}
                self.assertNotIn('updater', cloud_update.heartbeat_fields())
            status['updater'] = {**value, 'unknown':1}
            self.assertEqual(cloud_update.heartbeat_fields()['updater'], value)
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT':'wacli'}), mock.patch.object(cloud_update, 'request', side_effect=AssertionError('Cloud IPC forbidden')):
            self.assertEqual(cloud_update.heartbeat_fields(), {})

    def test_receiver_cross_fields_and_extension_budgets(self):
        # Literal schema-1 trial shapes: active fallback may lower generation;
        # TRIED is complete identity, never just the marker or descriptor hash.
        value = {'schema':1,'reporter':'legacy_admission','controller_version':'1.1.0',
            'selector_version':'1.0.0','active_slot':'a','active_generation':0,
            'slots':{'a':{'slot_id':'a','generation':0,'state':'GOOD','controller_version':'1.1.0','set_sha256':'a'*64},
                     'b':{'slot_id':'b','generation':0,'state':'none','controller_version':None,'set_sha256':None}},
            'last_trial':None,'discovery_timeout_seconds':900,
            'application':{'version':'v0.4.0','commit':'abcdef1','state':'committed'},
            'capabilities':{'permit_v1':True,'controller_self_upgrade_v1':True}}
        self.assertEqual(admission.validate_report(value),value)
        trial = copy.deepcopy(value)
        trial.update(active_slot='b',active_generation=1,controller_version='1.3.0')
        trial['slots']['b'].update(state='TRIED',generation=1,controller_version='1.3.0',set_sha256='b'*64)
        self.assertEqual(admission.validate_report(trial),trial)
        trial['last_trial'] = {'slot_id':'b','generation':1,'result':'none','reason_code':None}
        self.assertEqual(admission.validate_report(trial),trial)
        promoted = copy.deepcopy(trial)
        promoted['slots']['b']['state'] = 'GOOD'
        promoted['last_trial']['result'] = 'promoted'
        self.assertEqual(admission.validate_report(promoted),promoted)
        fallback = copy.deepcopy(trial)
        fallback.update(active_slot='a',active_generation=0,controller_version='1.1.0')
        fallback['last_trial'].update(result='rolled_back',reason_code='timeout')
        self.assertEqual(admission.validate_report(fallback),fallback)
        for mutation in (
                lambda x:x['slots']['b'].update(state='GOOD'),
                lambda x:x['slots']['b'].update(slot_id='a'),
                lambda x:x.update(active_generation=1),
                lambda x:x['last_trial'].update(generation=2),
                lambda x:x['last_trial'].update(reason_code=None),
                lambda x:x['capabilities'].update(permit_v1=False)):
            bad = copy.deepcopy(fallback)
            mutation(bad)
            with self.assertRaises(ValueError):
                admission.validate_report(bad)
        reused = copy.deepcopy(fallback)
        reused['slots']['b'].update(generation=2,state='GOOD')
        self.assertEqual(admission.validate_report(reused),reused)  # Historical result.
        bad = copy.deepcopy(trial)
        bad['slots']['a']['generation'] = 1
        with self.assertRaises(ValueError):
            admission.validate_report(bad)
        for extension in ({'x':{'y':{'z':1}}}, ['item']*15, 'é'*500):
            self.assertEqual(admission.validate_report({**value,'future':extension}),value)
        for extension in ({'x':{'y':{'z':{'too_deep':1}}}}, ['item']*20, float('nan')):
            with self.assertRaises(ValueError):
                admission.validate_report({**value,'future':extension})

    def test_voice_gate_requires_a_selected_good_current_controller_and_builder_requires_rollout_pin(self):
        box, host = self.box(mode='wacli')
        box.data['required_updater_capabilities'] = ['controller_self_upgrade_v1']
        box.save_manifest()
        before = box.tree()
        result, _ = self.install(box, host)
        self.assertEqual(result['state'], 'waiting_for_window')
        self.assertEqual(box.tree(), before)
        self.assertEqual(host.requests, [])
        build.require_rollout_keys(box.source)
        write(box.source / 'messagebox/updater_rollout_keys.py', b"{'schema':1,'keys':[]}\n")
        with self.assertRaisesRegex(build.ReleaseError, 'rollout public pins'):
            build.require_rollout_keys(box.source)
        # The builder emits the capability gate for minimum controller 1.3.0.
        with mock.patch.object(build.subprocess, 'check_output', return_value=json.dumps({
                'commit':'a'*40, 'files':[]}).encode()):
            manifest = build.release_manifest(box.source, 'v0.3.14', 'a'*40, 2, '1.3.0', {})
        self.assertEqual(manifest['required_updater_capabilities'], ['controller_self_upgrade_v1'])

    def test_shared_legacy_and_prehashed_signature_vectors(self):
        vector = json.loads((ROOT / 'tests/fixtures/updater-permit-v1.json').read_bytes())
        payload = json.loads(base64.b64decode(vector['payload_b64']))
        expected = {n:v for n,v in payload.items() if n not in {'schema','domain','action','key_id','policy_revision','issued_at','expires_at'}}
        directory = mkdir(self.base / 'vector')
        envelope = {n:vector[n] for n in ('schema','payload_b64','signature_minisign')}
        envelope['key_id'] = payload['key_id']
        admission.verify_permit(admission.canonical(envelope), expected, {payload['key_id']:vector['public_key']}, directory, now=1800000100)
        envelope['signature_minisign'] = vector['signature_prehash']
        with self.assertRaisesRegex(ValueError, 'signature_format'):
            admission.verify_permit(admission.canonical(envelope), expected, {payload['key_id']:vector['public_key']}, directory, now=1800000100)

    def test_postcommit_hook_queues_publication_only_after_bootstrap_succeeds(self):
        box, host = self.box()
        runtime = Host(box.root)
        runtime.recovery_run = box.run
        bootstrap = slots.bootstrap
        with mock.patch.object(slots.sys, 'argv', ['controller_slots.py','bootstrap']), \
                mock.patch.object(slots, 'bootstrap', side_effect=lambda:bootstrap(box.root,run=runtime)), \
                mock.patch.object(slots.subprocess, 'run') as queue:
            with self.assertRaisesRegex(ValueError, 'not committed'):
                slots.main()
            queue.assert_not_called()
        self.install(box, host)
        with mock.patch.object(slots.sys, 'argv', ['controller_slots.py','bootstrap']), \
                mock.patch.object(slots, 'bootstrap', side_effect=lambda:bootstrap(box.root,run=runtime)), \
                mock.patch.object(slots.subprocess, 'run') as queue:
            slots.main()
            self.assertEqual(queue.call_args.args[0], ['systemctl','--no-block','start','messagebox-update-publish.service'])
        self.assertTrue(selector.inspect(box.root, selector.SLOTS[0])['legacy'])

    def test_staged_controller_voice_limits_and_bounded_transfer(self):
        from test_bounded_update import bounded_update as worker
        from test_update_discovery import discovery
        self.assertEqual(worker._expected_target('sounds/voices/buttonbox/example.wav'), '/opt/messagebox/sounds/voices/buttonbox/example.wav')
        self.assertEqual(worker.MAX_SOUND_TOTAL, 160 * 1024 * 1024)
        self.assertEqual(discovery.DOWNLOAD_SECONDS, 1500)
        times = iter((0,1500))
        class Incoming:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self,n): return b'bytes'
        download = discovery.Discovery(self.base, opener=lambda *a,**k:Incoming(), monotonic=lambda:next(times))
        with self.assertRaisesRegex(discovery.DownloadError, 'download_timeout'):
            download.download('https://github.com/button-box/button-box/releases/download/v0.3.13/archive', self.base / 'download', 1024)
        self.assertFalse((self.base / 'download.part').exists())

    def test_transport_parser_handles_whitespace_quotes_and_rejects_ambiguous_modes(self):
        for line in ('MSGBOX_TRANSPORT=cloud', '  MSGBOX_TRANSPORT="cloud"  ', "MSGBOX_TRANSPORT='cloud'"):
            write(self.base / 'etc/messagebox/env', (line+'\n').encode())
            self.assertTrue(admission.cloud(self.base))
        write(self.base / 'etc/messagebox/env', b'  MSGBOX_TRANSPORT="wacli"\n')
        self.assertFalse(admission.cloud(self.base))
        for raw in (b'MSGBOX_TRANSPORT=cloud\nMSGBOX_TRANSPORT=wacli\n', b'MSGBOX_TRANSPORT=other\n'):
            write(self.base / 'etc/messagebox/env', raw)
            with self.assertRaises(ValueError):
                admission.cloud(self.base)

    def test_frozen_broker_fallback_reports_failed_trial_from_root_cache_with_boot_and_age_bounds(self):
        box, host = self.box()
        self.install(box, host)
        runtime = Host(box.root)
        runtime.recovery_run = box.run
        slots.bootstrap(box.root, run=runtime)
        # Model a failed inactive trial while the frozen A broker still runs.
        release = box.root / os.readlink(box.root / 'opt/messagebox').lstrip('/')
        for n in selector.MODULES:
            write(box.root / selector.SLOTS[1] / n, (release / 'updater' / n).read_bytes())
        for n in ('controller_descriptor.py','controller_signature.py'):
            write(box.root / selector.SLOTS[1] / n, (release / 'messagebox' / n).read_bytes())
        desc = admission.digest((release / 'messagebox/controller_descriptor.py').read_bytes())
        write(box.root / selector.SLOTS[1] / 'TRIED', admission.canonical({'descriptor':desc}))
        write(box.root / slots.STATE / 'trial-result.json', admission.canonical({'descriptor':desc,'slot_id':'b',
            'generation':7,'result':'failed','reason_code':'activation_failed'}))
        write(box.root / 'proc/123/cmdline', b'/usr/bin/python3\0/usr/lib/messagebox-update/update_controller.py\0serve\0', 0o444)
        def run(command, **kwargs):
            if command[:3] == ['systemctl','show','messagebox-update-controller.service']:
                return subprocess.CompletedProcess(command,0,'123\n')
            return host.run(command, **kwargs)
        with mock.patch.object(admission.time,'monotonic',return_value=100):
            admission.publish_report(box.root,run=run)
        cached = admission.cached_report
        with mock.patch.object(admission.time,'monotonic',return_value=101), \
                mock.patch.dict(os.environ,{'MSGBOX_TRANSPORT':'cloud'}), \
                mock.patch.object(cloud_update,'status',return_value={'installed':True}), \
                mock.patch.object(admission,'cached_report',side_effect=lambda:cached(box.root)):
            report = cloud_update.heartbeat_fields()['updater']
            self.assertEqual((report['active_slot'],report['last_trial']['result']),('a','failed'))
            self.assertEqual(report['slots']['b']['state'],'TRIED')
        # A stale marker cannot manufacture a failed controller's identity.
        module = box.root / selector.SLOTS[1] / selector.MODULES[0]
        module.write_bytes(b'corrupt')
        value = admission.report(box.root,run=run)
        self.assertEqual(value['slots']['b'],{'slot_id':'b','generation':0,'state':'none',
            'controller_version':None,'set_sha256':None})
        self.assertEqual(value['last_trial']['result'],'failed')
        with mock.patch.object(admission.time,'monotonic',return_value=221), self.assertRaisesRegex(ValueError,'stale'):
            cached(box.root)
        (box.root / 'proc/sys/kernel/random/boot_id').unlink()
        write(box.root / 'proc/sys/kernel/random/boot_id',b'next-boot\n',0o444)
        with mock.patch.object(admission.time,'monotonic',return_value=101), self.assertRaisesRegex(ValueError,'stale'):
            cached(box.root)
        write(box.root / 'etc/messagebox/env',b'MSGBOX_TRANSPORT=wacli\n')
        admission.publish_report(box.root,run=lambda *a,**k: self.fail('wacli report must be local and omitted'))
