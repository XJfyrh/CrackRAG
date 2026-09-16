"""Synthetic public packaging boundaries; no product build, API or uploads."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('package_release_test_target', ROOT / 'scripts/package_release.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def acceptance(identity):
    quality = {'schema': 'crackrag-independent-quality-review-v1', 'release_manifest_sha256': identity,
               'model_inputs_sha256': '1' * 64, 'first_attempt_quality_gate': 'PASS',
               'quality_counts': {k: {'passed': n, 'total': n} for k, n in pack.QUALITY_COUNTS.items()},
               'unsafe_incorrect_definite_answers_observed': 0,
               'sequence_quality': {'passed': 16, 'total': 16, 'quality_equivalent_for_these_paired_questions': True}, 'cases': []}
    for kind, n in pack.QUALITY_COUNTS.items():
        quality['cases'].extend({'case_id': f'q-{kind}-{i}', 'suite': 'quality', 'kind': kind, 'verdict': 'PASS'} for i in range(n))
    cost = {'schema': 'crackrag-public-sequence-cost-v1', 'release_manifest_sha256': identity,
            'model_inputs_sha256': '1' * 64, 'accounting_status': 'COMPLETE_KNOWN_ESTIMATED_MODEL_COST', 'currency': 'CNY',
            'pairs': [], 'total_amount_cny': '0.1600', 'total_model_attempts': 16}
    for pair in range(2):
        arms = {}
        for arm in ('baseline', 'build'):
            cases = []
            for step in range(1, 5):
                ident = f's-{pair}-{arm}-{step}'
                cases.append({'case_id': ident, 'step': step, 'run_state': 'COMPLETED', 'answer_validation': 'SUPPORTED', 'calls': 1, 'amount_cny': '0.0100'})
                quality['cases'].append({'case_id': ident, 'suite': 'sequence', 'kind': 'sequence', 'verdict': 'PASS', 'model_attempts': 1})
            arms[arm] = {'cases': cases}
        cost['pairs'].append(arms)
    return quality, cost


def browser_record(video, identity):
    return {'version': 'release-browser-acceptance-v1', 'provider': 'mock', 'release_manifest_sha256': identity,
            'video': True, 'page_errors': [], 'refresh_history_created_posts': 0,
            'video_file': {'name': 'example.webm', 'sha256': pack.digest(video), 'bytes': len(video)},
            'results': [{'id': 'private-run', 'status': status, 'calls': 0 if i == 2 else 2,
                         'coverage': 'FULL' if i == 2 else 'MISSING'}
                        for i, status in enumerate(['SUPPORTED', 'SUPPORTED', 'SUPPORTED', 'INCONCLUSIVE', 'SUPPORTED'])]
                       + [{'recovery_run': 'private-recovery', 'calls_before': 2, 'calls_after': 2, 'full_reuse_model_calls': 0}]}


class PackageReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.sources = {name: ('synthetic ' + name).encode() for name in pack.REQUIRED_SOURCE}
        self.sources['api/additional.go'] = b'new source must be bound'
        self.manifest = {'version': 'release-manifest-v1', 'config_version': 'm3-runtime-v1', 'model': 'deepseek-flash',
                         'files': {name: pack.digest(raw) for name, raw in self.sources.items()}}
        self.manifest['files'].update({'api-bin/crackrag-api': '2' * 64, 'web/dist/index.html': '3' * 64})
        self.identity = pack.digest(json.dumps(self.manifest).encode())
        self.quality, self.cost = acceptance(self.identity)
        self.video = b'\x1aE\xdf\xa3' + b'synthetic-header-webm-not-a-real-video'
        self.browser = browser_record(self.video, self.identity)

    def archive(self):
        path = self.folder / 'source.tar'
        with tarfile.open(path, 'w') as archive:
            for name, raw in self.sources.items():
                member = tarfile.TarInfo('CrackRAG-0.1.0/' + name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        return path

    def test_exact_source_and_generated_files_are_distinguished(self):
        _, count, generated = pack.verify_archive(self.archive(), 'CrackRAG-0.1.0', self.manifest)
        self.assertEqual(count, len(self.sources))
        self.assertEqual(generated, ['api-bin/crackrag-api', 'web/dist/index.html'])

    def test_manifest_omitted_source_rejected(self):
        del self.manifest['files']['api/additional.go']
        with self.assertRaisesRegex(ValueError, 'inventory'):
            pack.verify_archive(self.archive(), 'CrackRAG-0.1.0', self.manifest)

    def test_manifest_uncommitted_source_rejected(self):
        self.manifest['files']['api/uncommitted.go'] = '4' * 64
        with self.assertRaisesRegex(ValueError, 'inventory'):
            pack.verify_archive(self.archive(), 'CrackRAG-0.1.0', self.manifest)

    def test_modified_source_rejected(self):
        self.sources['api/additional.go'] = b'modified'
        with self.assertRaisesRegex(ValueError, 'differs'):
            pack.verify_archive(self.archive(), 'CrackRAG-0.1.0', self.manifest)

    def test_manifest_bad_path_or_private_extra_rejected(self):
        for name in ('../secret', '/absolute', 'api/../secret', '.release/session.json'):
            with self.subTest(name=name):
                m = copy.deepcopy(self.manifest)
                m['files'][name] = '4' * 64
                with self.assertRaises(ValueError):
                    pack.verify_archive(self.archive(), 'CrackRAG-0.1.0', m)

    def test_acceptance_positive(self):
        pack.verify_acceptance(self.quality, self.cost, self.identity)

    def test_failed_quality_or_missing_case_rejected(self):
        for change in ('gate', 'case', 'sequence'):
            with self.subTest(change=change):
                q = copy.deepcopy(self.quality)
                if change == 'gate': q['first_attempt_quality_gate'] = 'FAIL'
                if change == 'case': q['cases'].pop()
                if change == 'sequence': q['sequence_quality']['quality_equivalent_for_these_paired_questions'] = False
                with self.assertRaises(ValueError): pack.verify_acceptance(q, self.cost, self.identity)

    def test_mixed_release_or_inputs_rejected(self):
        for field in ('release_manifest_sha256', 'model_inputs_sha256', 'currency'):
            c = copy.deepcopy(self.cost)
            c[field] = '9' * 64
            with self.assertRaises(ValueError): pack.verify_acceptance(self.quality, c, self.identity)

    def test_cost_cases_or_missing_amount_rejected(self):
        for change in ('case', 'amount', 'aggregate', 'calls'):
            c = copy.deepcopy(self.cost)
            case = c['pairs'][0]['baseline']['cases'][0]
            if change == 'case': case['case_id'] = 'unreviewed'
            if change == 'amount': case['amount_cny'] = None
            if change == 'aggregate': c['total_amount_cny'] = '0'
            if change == 'calls': case['calls'] = 2
            with self.assertRaises(ValueError): pack.verify_acceptance(self.quality, c, self.identity)

    def test_browser_receipt_sanitizes_private_ids(self):
        result = pack.verify_video(self.video, 'example.webm', self.browser, self.identity)
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['recovery'][0]['calls_before'], 2)

    def test_video_or_receipt_tamper_rejected(self):
        for change in ('video', 'hash', 'product', 'failure'):
            b = copy.deepcopy(self.browser)
            raw = self.video
            if change == 'video': raw += b'changed'
            if change == 'hash': b['video_file']['sha256'] = '0' * 64
            if change == 'product': b['release_manifest_sha256'] = '0' * 64
            if change == 'failure': b['page_errors'] = ['failure']
            with self.assertRaises(ValueError): pack.verify_video(raw, 'example.webm', b, self.identity)

    def test_duplicate_json_or_nonfinite_rejected(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError): pack.load_json(raw)

    @unittest.skipUnless(shutil.which('git'), 'requires Git; the complete packaging suite runs on the CI host')
    def test_real_local_git_packaging_and_checksum_outputs(self):
        root = self.folder / 'repo'
        root.mkdir()
        for name, raw in self.sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        public = {'README.md': b'synthetic readme', 'docs/operations.md': b'operations',
                  'docs/architecture.md': b'architecture', 'docs/release-validation.md': b'validation',
                  'quality.md': b'quality', 'quality.json': json.dumps(self.quality).encode(),
                  'cost.md': b'cost', 'cost.json': json.dumps(self.cost).encode(),
                  'scripts/check_public_source.py': b'print("synthetic publication scan")\n'}
        for name, raw in public.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        def g(*args):
            return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.STDOUT).decode().strip()
        g('init', '-b', 'main'); g('config', 'core.autocrlf', 'false'); g('add', '.')
        g('-c', 'user.name=Synthetic Test', '-c', 'user.email=synthetic@example.invalid', 'commit', '-m', 'synthetic public fixture')
        g('update-ref', 'refs/remotes/origin/main', g('rev-parse', 'HEAD'))
        external = {'manifest.json': json.dumps(self.manifest).encode(), 'example.webm': self.video,
                    'browser.json': json.dumps(self.browser).encode()}
        for name, raw in external.items(): (self.folder / name).write_bytes(raw)
        args = SimpleNamespace(version='v0.1.0', product_manifest=self.folder/'manifest.json', video=self.folder/'example.webm',
                               browser_record=self.folder/'browser.json', quality_report=root/'quality.md',
                               quality_record=root/'quality.json', cost_report=root/'cost.md', cost_record=root/'cost.json',
                               output=self.folder/'release')
        with patch.object(pack, 'ROOT', root):
            result = pack.package(args)
        record = json.loads((args.output/'release-artifacts.json').read_text())
        self.assertEqual(result['source_commit'], g('rev-parse', 'HEAD'))
        for line in (args.output/'SHA256SUMS').read_text().splitlines():
            checksum, name = line.split('  ')
            self.assertEqual(checksum, pack.digest((args.output/name).read_bytes()))
        for name, entry in record['files'].items():
            self.assertEqual(entry['sha256'], pack.digest((args.output/name).read_bytes()))
        with zipfile.ZipFile(args.output/'CrackRAG-0.1.0.zip') as archive:
            self.assertEqual(archive.read('CrackRAG-0.1.0/quality.json'), public['quality.json'])
        with tarfile.open(args.output/'CrackRAG-0.1.0.tar.gz') as archive:
            self.assertEqual(archive.extractfile('CrackRAG-0.1.0/README.md').read(), public['README.md'])
        self.assertNotIn('private-run', (args.output/'browser-acceptance.json').read_text())
        self.assertFalse(list(self.folder.glob('.crackrag-package-*')))
        # Fail after archive creation: no partial final package survives, and
        # an external lookalike Markdown file cannot replace committed review.
        args.output = self.folder / 'must-not-exist'
        args.quality_report = self.folder / 'external-quality.md'
        args.quality_report.write_bytes(public['quality.md'])
        with patch.object(pack, 'ROOT', root):
            with self.assertRaisesRegex(ValueError, 'exact files in the packaged commit'):
                pack.package(args)
        self.assertFalse(args.output.exists())
        self.assertFalse(list(self.folder.glob('.crackrag-package-*')))


if __name__ == '__main__':
    unittest.main()
