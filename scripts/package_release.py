"""Package reviewed main source and explicitly bound public acceptance artifacts.

No uploads, provider calls or private session/ledger reads. Generated image files
are identified by the accepted product manifest, not rebuilt by this script.
"""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path, PurePosixPath
import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Source COPY inputs of deploy/Release.Dockerfile. Generated image outputs are
# deliberately separate: they are absent from a Git source archive.
SOURCE_ROOTS = ('api/', 'proto/', 'ai-runtime/src/crackrag/', 'ai-runtime/src/crackrag_m1/',
                'ai-runtime/prompts/', 'config/', 'migrations/', 'web/src/', 'web/scripts/',
                'web/public/', 'scripts/release/', 'deploy/')
SOURCE_FILES = {'ai-runtime/requirements-m1.lock', 'requirements.lock', 'scripts/release.sh',
                'scripts/release.ps1', 'web/package.json', 'web/package-lock.json',
                'web/tsconfig.json', 'web/vite.config.ts', 'web/index.html'}
REQUIRED_SOURCE = SOURCE_FILES | {'api/go.mod', 'api/go.sum', 'api/cmd/server/main.go',
    'api/internal/app/m2_catalog.json', 'ai-runtime/src/crackrag_m1/server.py',
    'ai-runtime/src/crackrag_m1/release.py', 'proto/crackrag/v1/runtime.proto',
    'deploy/Release.Dockerfile', 'deploy/compose.release.yaml', 'web/src/main.tsx'}
QUALITY_COUNTS = {'supported_positive': 6, 'missing_evidence': 2, 'unsupported_basis': 2, 'reuse': 2}


def digest(data):
    return sha256(data).hexdigest()


def hash_text(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def load_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError('non-finite JSON value')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT).decode().strip()


def safe_name(name):
    return (isinstance(name, str) and bool(name) and '\\' not in name and ':' not in name
            and not name.startswith('/') and name == PurePosixPath(name).as_posix()
            and all(x not in ('.', '..', '') for x in name.split('/')))


def source_path(name):
    return name in SOURCE_FILES or name.startswith(SOURCE_ROOTS)


def verify_archive(raw_tar, stem, manifest):
    if (set(manifest) != {'version', 'config_version', 'model', 'files'} or
            (manifest['version'], manifest['config_version'], manifest['model']) !=
            ('release-manifest-v1', 'm3-runtime-v1', 'deepseek-flash')):
        raise ValueError('expected public product manifest, never a live session')
    files = manifest['files']
    if not isinstance(files, dict) or not files or any(not safe_name(k) or not hash_text(v) for k, v in files.items()):
        raise ValueError('invalid product inventory')
    archive_files = {}
    with tarfile.open(raw_tar) as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(stem + '/'):
                raise ValueError('archive contains non-regular or unscoped files')
            name = member.name[len(stem) + 1:]
            if not safe_name(name) or name in archive_files:
                raise ValueError('invalid or duplicate archive path')
            with archive.extractfile(member) as stream:
                archive_files[name] = stream.read()
    source_names = {name for name in archive_files if source_path(name)}
    if not REQUIRED_SOURCE <= source_names:
        raise ValueError('committed product source is incomplete')
    manifest_sources = {name for name in files if source_path(name)}
    # Equality rejects omitted source and stale/uncommitted image files.
    if source_names != manifest_sources:
        raise ValueError('product source inventory differs from committed source')
    for name in source_names:
        if digest(archive_files[name]) != files[name]:
            raise ValueError('accepted image differs from committed source: ' + name)
    generated = set(files) - manifest_sources
    if (not {'api-bin/crackrag-api', 'web/dist/index.html'} <= generated or
            any(name != 'api-bin/crackrag-api' and not name.startswith('web/dist/') for name in generated)):
        raise ValueError('unexpected or missing generated product files')
    return archive_files, len(source_names), sorted(generated)


def decimal_amount(value):
    try:
        amount = Decimal(value) if isinstance(value, str) else Decimal('NaN')
    except InvalidOperation:
        raise ValueError('invalid estimated amount') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('invalid estimated amount')
    return amount


def verify_acceptance(quality, cost, product_hash):
    if quality.get('schema') != 'crackrag-independent-quality-review-v1' or cost.get('schema') != 'crackrag-public-sequence-cost-v1':
        raise ValueError('unrecognized acceptance record schema')
    if any(record.get('release_manifest_sha256') != product_hash for record in (quality, cost)):
        raise ValueError('quality, cost and product must have the same release identity')
    if (not hash_text(quality.get('model_inputs_sha256')) or
            quality['model_inputs_sha256'] != cost.get('model_inputs_sha256')):
        raise ValueError('quality and cost must use the same frozen inputs')
    if (quality.get('first_attempt_quality_gate') != 'PASS' or
            any(quality.get('quality_counts', {}).get(name) != {'passed': total, 'total': total}
                for name, total in QUALITY_COUNTS.items()) or
            quality.get('unsafe_incorrect_definite_answers_observed') != 0):
        raise ValueError('independent quality acceptance is incomplete or failed')
    cases = quality.get('cases', [])
    if len(cases) != 28 or len({c.get('case_id') for c in cases}) != 28 or any(c.get('verdict') != 'PASS' for c in cases):
        raise ValueError('all independent quality and sequence case results are required')
    for kind, total in QUALITY_COUNTS.items():
        if sum(c.get('suite') == 'quality' and c.get('kind') == kind for c in cases) != total:
            raise ValueError('quality case denominators do not match')
    sequence = quality.get('sequence_quality', {})
    if (sequence.get('passed') != 16 or sequence.get('total') != 16 or
            sequence.get('quality_equivalent_for_these_paired_questions') is not True):
        raise ValueError('cost sequence independent quality was not verified')
    if cost.get('accounting_status') != 'COMPLETE_KNOWN_ESTIMATED_MODEL_COST' or cost.get('currency') != 'CNY':
        raise ValueError('sequence accounting is incomplete')
    quality_cases = {c['case_id']: c for c in cases if c.get('suite') == 'sequence'}
    cost_cases = []
    total, attempts = Decimal(0), 0
    if len(cost.get('pairs', [])) != 2:
        raise ValueError('two complete cost pairs are required')
    for pair in cost['pairs']:
        for arm in ('baseline', 'build'):
            group = pair.get(arm, {}).get('cases', [])
            if len(group) != 4 or {c.get('step') for c in group} != {1, 2, 3, 4}:
                raise ValueError('each cost arm requires four distinct steps')
            for case in group:
                expected = quality_cases.get(case.get('case_id'))
                if (not expected or case.get('run_state') != 'COMPLETED' or
                        case.get('answer_validation') != 'SUPPORTED' or
                        expected.get('model_attempts') != case.get('calls')):
                    raise ValueError('cost cases disagree with independent quality evidence')
                cost_cases.append(case['case_id'])
                total += decimal_amount(case.get('amount_cny'))
                attempts += case['calls']
    if len(set(cost_cases)) != 16 or set(cost_cases) != set(quality_cases):
        raise ValueError('quality and cost case identities differ')
    if total != decimal_amount(cost.get('total_amount_cny')) or attempts != cost.get('total_model_attempts'):
        raise ValueError('cost case totals disagree with aggregate')


def verify_video(raw, name, browser, product_hash):
    if (Path(name).suffix.lower() != '.webm' or len(raw) < 16 or raw[:4] != b'\x1aE\xdf\xa3' or
            b'webm' not in raw[:4096]):
        raise ValueError('expected an actual WebM recording')
    if (browser.get('version') != 'release-browser-acceptance-v1' or browser.get('provider') != 'mock' or
            browser.get('release_manifest_sha256') != product_hash or browser.get('video') is not True or
            browser.get('page_errors') != [] or browser.get('refresh_history_created_posts') != 0):
        raise ValueError('browser acceptance did not pass for the accepted product')
    if browser.get('video_file') != {'name': name, 'sha256': digest(raw), 'bytes': len(raw)}:
        raise ValueError('recording bytes differ from browser acceptance')
    results = browser.get('results', [])
    queries = [r for r in results if 'status' in r]
    if ([r.get('status') for r in queries] != ['SUPPORTED', 'SUPPORTED', 'SUPPORTED', 'INCONCLUSIVE', 'SUPPORTED'] or
            queries[2].get('calls') != 0 or queries[2].get('coverage') != 'FULL'):
        raise ValueError('browser acceptance is missing required demonstration results')
    # Public receipt is a whitelist: no Run/recovery UUIDs, raw errors or paths.
    recovery = [r for r in results if 'recovery_run' in r]
    if any(r.get('calls_before') != r.get('calls_after') or r.get('full_reuse_model_calls') != 0 for r in recovery):
        raise ValueError('browser recovery evidence is inconsistent')
    return {'version': 'release-public-browser-acceptance-v1', 'provider': 'mock',
            'release_manifest_sha256': product_hash, 'video_file': browser['video_file'],
            'results': [{k: r[k] for k in ('status', 'calls', 'coverage') if k in r} for r in queries],
            'recovery': [{k: r[k] for k in ('calls_before', 'calls_after', 'full_reuse_model_calls')} for r in recovery],
            'refresh_history_created_posts': 0, 'page_errors': []}


def parser():
    p = argparse.ArgumentParser()
    p.add_argument('--version', default='v0.1.0')
    for name in ('product-manifest', 'video', 'browser-record', 'quality-report', 'quality-record',
                 'cost-report', 'cost-record', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    return p


def package(a):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', a.version):
        raise ValueError('version must be a release tag')
    if git('status', '--porcelain'):
        raise ValueError('review and commit the complete public source first')
    commit = git('rev-parse', 'HEAD')
    if git('branch', '--show-current') != 'main' or git('rev-parse', 'origin/main') != commit:
        raise ValueError('package only the reviewed main commit already pushed to origin')
    tags = git('tag', '--list', a.version)
    if tags and git('rev-list', '-n', '1', a.version) != commit:
        raise ValueError('existing release tag refers to another commit')
    subprocess.run([sys.executable, str(ROOT / 'scripts/check_public_source.py')], cwd=ROOT, check=True)
    # Snapshot each explicit input once, avoiding validate-then-copy races.
    external = {name: getattr(a, name).read_bytes() for name in ('product_manifest', 'video', 'browser_record',
                                                               'quality_report', 'quality_record', 'cost_report', 'cost_record')}
    product_hash = digest(external['product_manifest'])
    manifest = load_json(external['product_manifest'])
    quality, cost = (load_json(external[name]) for name in ('quality_record', 'cost_record'))
    verify_acceptance(quality, cost, product_hash)
    browser = verify_video(external['video'], a.video.name, load_json(external['browser_record']), product_hash)
    destination = a.output.resolve()
    if destination.exists() or destination.is_relative_to(ROOT):
        raise ValueError('use a new output directory outside the public source tree')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stem = 'CrackRAG-' + a.version[1:]
    with tempfile.TemporaryDirectory(prefix='.crackrag-package-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'artifacts'
        stage.mkdir()
        raw_tar = stage / (stem + '.tar')
        subprocess.run(['git', 'archive', '--format=tar', '--prefix=' + stem + '/', '--output=' + str(raw_tar), commit], cwd=ROOT, check=True)
        archived, compared, generated = verify_archive(raw_tar, stem, manifest)
        for name in ('quality_report', 'quality_record', 'cost_report', 'cost_record'):
            path = getattr(a, name).resolve()
            if not path.is_relative_to(ROOT) or archived.get(path.relative_to(ROOT).as_posix()) != external[name]:
                raise ValueError('public acceptance reports must be exact files in the packaged commit')
        with raw_tar.open('rb') as source, (stage / (stem + '.tar.gz')).open('xb') as target:
            with gzip.GzipFile(filename='', mode='wb', fileobj=target, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        raw_tar.unlink()
        subprocess.run(['git', 'archive', '--format=zip', '--prefix=' + stem + '/', '--output=' + str(stage / (stem + '.zip')), commit], cwd=ROOT, check=True)
        copies = {'product-manifest.json': external['product_manifest'], 'demo-' + a.version + '.webm': external['video'],
                  'quality-report.md': external['quality_report'], 'quality-record.json': external['quality_record'],
                  'cost-report.md': external['cost_report'], 'cost-record.json': external['cost_record'],
                  'getting-started.md': archived['README.md'], 'operations.md': archived['docs/operations.md'],
                  'architecture.md': archived['docs/architecture.md'], 'validation.md': archived['docs/release-validation.md']}
        browser['video_file']['name'] = 'demo-' + a.version + '.webm'
        copies['browser-acceptance.json'] = (json.dumps(browser, indent=2) + '\n').encode()
        for name, data in copies.items():
            (stage / name).write_bytes(data)
        def entry(path):
            data = path.read_bytes()
            return {'sha256': digest(data), 'bytes': len(data)}
        record = {'version': 'release-artifacts-v1', 'release_tag': a.version, 'source_commit': commit,
                  'source_ref': 'main', 'product_manifest_sha256': product_hash,
                  'source_files_verified_against_product': compared,
                  'generated_image_files_bound_by_manifest_only': generated,
                  'browser_record_sha256': digest(external['browser_record']),
                  'files': {file.name: entry(file) for file in sorted(stage.iterdir())}}
        (stage / 'release-artifacts.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8', newline='\n')
        lines = [entry(file)['sha256'] + '  ' + file.name for file in sorted(stage.iterdir())]
        (stage / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
        if destination.exists() or git('rev-parse', 'HEAD') != commit or git('status', '--porcelain'):
            raise ValueError('source or output changed during packaging')
        stage.rename(destination)
    return {'source_commit': commit, 'release': a.version, 'artifacts': len(lines), 'output': str(destination)}


if __name__ == '__main__':
    print(json.dumps(package(parser().parse_args())))
