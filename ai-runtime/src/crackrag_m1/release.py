"""Product release verification; historical research freezes remain separate."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json

ROOTS=('api-bin','api','proto','ai-runtime/src','ai-runtime/prompts','config','migrations','web/dist','web/src','web/scripts','web/public','scripts/release','deploy')
REQUIRED=('api-bin/crackrag-api','ai-runtime/src/crackrag_m1/server.py','ai-runtime/src/crackrag_m1/release.py',
 'ai-runtime/prompts/m3-v1/shared.txt','config/m1-embedding.json','config/m3/cache-calibration-v2.json',
 'api/internal/app/m2_catalog.json','api/cmd/server/main.go','proto/crackrag/v1/runtime.proto',
 'web/package.json','web/package-lock.json','web/tsconfig.json','web/vite.config.ts','web/index.html','web/src/main.tsx','web/scripts/copy-pdf-assets.mjs',
 'web/dist/index.html','scripts/release.sh','scripts/release.ps1','deploy/Release.Dockerfile','deploy/compose.release.yaml',
 'migrations/00015_release_admission.sql','requirements.lock')

def digest(path):
    value=sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()

def inventory(root):
    root=Path(root)
    found=set(REQUIRED)
    for name in ROOTS:
        folder=root/name
        if not folder.is_dir():raise ValueError('RELEASE_REQUIRED_DIRECTORY_MISSING')
        for path in folder.rglob('*'):
            if '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'):continue
            if path.is_file() or path.is_symlink():found.add(path.relative_to(root).as_posix())
    return sorted(found)

def verify(root, manifest):
    root=Path(root).resolve();path=Path(manifest)
    obj=json.loads(path.read_text(encoding='utf-8'))
    if set(obj)!={'version','config_version','model','files'} or (obj['version'],obj['config_version'],obj['model'])!=('release-manifest-v1','m3-runtime-v1','deepseek-flash'):
        raise ValueError('RELEASE_MANIFEST_INVALID')
    if not set(inventory(root))<=set(obj['files']):raise ValueError('RELEASE_UNBOUND_FILE')
    for name,want in obj['files'].items():
        source=root/name
        if '\\' in name or ':' in name or source.is_symlink() or not source.resolve().is_relative_to(root) or not source.is_file():
            raise ValueError('RELEASE_PATH_INVALID')
        if digest(source)!=want:raise ValueError('RELEASE_FILE_CHANGED')
    return digest(path)

def check_session(settings, *, require_enabled=True):
    if not getattr(settings,'release_manifest',''):return ''
    identity=verify(settings.release_root,settings.release_manifest)
    if settings.provider!='deepseek':return identity
    path=Path(settings.live_session)
    obj=json.loads(path.read_text(encoding='utf-8'))
    if obj.get('version')!='live-session-manifest-v1' or obj.get('release_manifest_sha256')!=identity:
        raise ValueError('LIVE_SESSION_INVALID')
    if not datetime.now(timezone.utc)<datetime.fromisoformat(obj['expires_at']):raise ValueError('LIVE_SESSION_EXPIRED')
    if (obj['max_output_tokens'],obj['concurrency'],obj['automatic_retries'])!=(2048,2,0):raise ValueError('LIVE_SESSION_LIMIT_INVALID')
    if digest(settings.price_snapshot)!=obj['price_sha256'] or digest(settings.price_snapshot.with_suffix('.html'))!=obj['price_html_sha256']:
        raise ValueError('LIVE_PRICE_BINDING_MISMATCH')
    if require_enabled:
        control=json.loads(Path(settings.live_control).read_text(encoding='utf-8'))
        if control!={'enabled':True,'session_sha256':digest(path)}:raise ValueError('LIVE_PAUSED')
    return identity
