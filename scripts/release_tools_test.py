"""Offline product packaging tests; no containers or provider requests."""
import importlib.util
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'ai-runtime/src'))
sys.path.insert(0,str(BASE/'scripts/release'))
spec=importlib.util.spec_from_file_location('release_image_tools',BASE/'scripts/release/image.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
from crackrag_m1.release import REQUIRED,ROOTS,verify

class ReleaseToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'app';self.state=Path(self.tmp.name)/'release'
        self.root.mkdir();self.state.mkdir()
        self.patch=patch.multiple(module,ROOT=self.root,STATE=self.state);self.patch.start();self.addCleanup(self.patch.stop)
        for directory in ROOTS:(self.root/directory).mkdir(parents=True,exist_ok=True)
        for name in [*REQUIRED,'ai-runtime/requirements-m1.lock','api/go.mod','api/go.sum','web/package-lock.json']:
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('offline-fixture-'+name)
        module.build_manifest()

    def test_manifest_binds_all_runtime_inputs_and_rejects_added_code(self):
        verify(self.root,self.root/'release-manifest.json')
        (self.root/'ai-runtime/src/unbound.py').write_text('unexpected')
        with self.assertRaisesRegex(ValueError,'UNBOUND'):verify(self.root,self.root/'release-manifest.json')

    def test_manifest_does_not_follow_symlink_or_escape(self):
        path=self.root/'release-manifest.json';value=json.loads(path.read_text())
        value['files']['../outside.txt']='0'*64;path.chmod(0o600);path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'PATH'):verify(self.root,path)

    def test_manifest_rejects_tamper_and_omission(self):
        p=self.root/'ai-runtime/src/crackrag_m1/server.py';p.write_text('tampered')
        with self.assertRaisesRegex(ValueError,'CHANGED'):verify(self.root,self.root/'release-manifest.json')
        manifest=json.loads((self.root/'release-manifest.json').read_text());del manifest['files']['api-bin/crackrag-api']
        (self.root/'release-manifest.json').chmod(0o600);(self.root/'release-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'UNBOUND'):verify(self.root,self.root/'release-manifest.json')

    def test_init_is_private_paused_and_never_overwrites(self):
        module.init();state=json.loads((self.state/'state/live-control.json').read_text())
        self.assertFalse(state['enabled']);self.assertFalse((self.state/'secrets/deepseek_api_key').read_text())
        first=(self.state/'secrets/access_token').read_text();self.assertGreater(len(first),30)
        with self.assertRaisesRegex(ValueError,'ALREADY'):module.init()
        self.assertEqual(first,(self.state/'secrets/access_token').read_text())

    def test_enable_requires_prepared_session(self):
        module.init()
        with self.assertRaisesRegex(ValueError,'NOT_PREPARED'):module.control(True)

    def test_new_opening_is_explicit_zero_and_does_not_reset_existing_project(self):
        module.init();module.opening_new('independent-offline-fixture')
        value=module.opening_value(self.state/'opening.json')
        self.assertEqual(value['known_cny'],'0');self.assertEqual(value['retained_cny'],'0')
        self.assertEqual(value['source_sha256'],module.digest(self.state/'state/new-project-declaration.json'))
        with self.assertRaisesRegex(ValueError,'ALREADY'):module.opening_new('reset-attempt')

    def test_opening_canonical_bytes_match_go_html_and_unicode_rules(self):
        self.assertEqual(module.opening_bytes({'project_id':'A&B<演示>\u2028'}),
            '{"project_id":"A\\u0026B\\u003c演示\\u003e\\u2028"}'.encode())

    def test_backup_restore_preserves_blobs_and_state_without_secrets_and_stays_paused(self):
        module.init();blobs=self.state/'test-blobs';blobs.mkdir();(blobs/'sample.pdf').write_bytes(b'original-pdf')
        (self.state/'state/audit.json').write_text('{"retained_cny":"0.05760400"}')
        backup=self.state/'backups/offline';backup.mkdir();(backup/'database.dump').write_bytes(b'offline-dump')
        with patch.object(module,'BLOBS',blobs):module.backup_files('offline')
        target=self.state/'restore';target.mkdir();target_blobs=target/'test-blobs';target_blobs.mkdir()
        with patch.multiple(module,STATE=target,BLOBS=target_blobs),patch.object(module.os,'chown',create=True):
            module.init();shutil.copytree(backup,target/'backups/offline')
            module.restore_files('offline')
            self.assertEqual((target_blobs/'sample.pdf').read_bytes(),b'original-pdf')
            self.assertEqual(json.loads((target/'state/audit.json').read_text())['retained_cny'],'0.05760400')
            self.assertFalse(json.loads((target/'state/live-control.json').read_text())['enabled'])
            self.assertNotEqual((target/'secrets/access_token').read_text(),(self.state/'secrets/access_token').read_text())
            with self.assertRaisesRegex(ValueError,'NOT_EMPTY'):module.restore_files('offline')

    def test_opening_requires_exclusive_and_retains_unknown(self):
        value={'version':'release-opening-balance-v1','project_id':'test-project','known_cny':'0.63514168','retained_cny':'0.05760400',
            'source_sha256':'a'*64,'retained_authorization_ref':'historical-exception-fixture','as_of':'2026-09-16T00:00:00Z','original_instances_stopped':True}
        p=self.state/'opening.json';p.write_text(json.dumps(value))
        self.assertEqual(module.opening_value(p)['retained_cny'],'0.05760400')
        value['retained_authorization_ref']='';p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'EVIDENCE'):module.opening_value(p)
        value['original_instances_stopped']=False;p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'SCHEMA'):module.opening_value(p)

    def test_live_prepare_cannot_silently_enable_or_exceed_internal_limits(self):
        module.init()
        with self.assertRaisesRegex(ValueError,'EXCLUSIVE'):module.live_prepare(self.state/'none')
        with self.assertRaisesRegex(ValueError,'LIMIT'):module.live_prepare(self.state/'none',True,'100.1',80)
        with self.assertRaisesRegex(ValueError,'LIMIT'):module.live_prepare(self.state/'none',True,'5',2001)
        self.assertFalse(json.loads((self.state/'state/live-control.json').read_text())['enabled'])

if __name__=='__main__':unittest.main()
