"""Container-side release setup. Never performs paid model requests."""
import argparse
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tarfile
import uuid

ROOT=Path('/app')
STATE=Path(os.getenv('CRACKRAG_SETUP_ROOT','/release'))
BLOBS=Path('/data/blobs')
sys.path.insert(0,str(ROOT/'ai-runtime/src'))
from crackrag_m1.release import digest, inventory, verify

def write_new(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        stream.write(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n')
    path.chmod(0o640)

def atomic(path,value):
    path=Path(path);temp=path.with_name(path.name+'.new-'+secrets.token_hex(4))
    write_new(temp,value);os.replace(temp,path)

def stamp(value=None):
    value=value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z').replace('.000000Z','Z')

def env_update(**changes):
    path=STATE/'compose.env';rows={}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line:
                key,value=line.split('=',1);rows[key]=value
    rows.update(changes)
    atomic(path,''.join(f'{key}={value}\n' for key,value in sorted(rows.items())))

def init():
    if (STATE/'compose.env').exists():raise ValueError('ALREADY_INITIALIZED')
    for name in ('secrets','state','models','backups'): (STATE/name).mkdir(parents=True,exist_ok=True)
    password=secrets.token_hex(24);token=secrets.token_urlsafe(32)
    for name,value in {'postgres_password':password,'database_url':f'postgres://crackrag:{password}@postgres:5432/crackrag?sslmode=disable',
                       'internal_token':secrets.token_urlsafe(32),'api_tokens':token+'=owner','access_token':token,'deepseek_api_key':''}.items():
        write_new(STATE/'secrets'/name,value)
    atomic(STATE/'state/live-control.json',{'enabled':False,'session_sha256':''})
    env_update(CRACKRAG_PROVIDER='mock',CRACKRAG_EMBEDDING='fixture',CRACKRAG_SESSION='',CRACKRAG_PRICE='/app/config/m1-pricing.json')
    print('Initialized. Local access token: .release/secrets/access_token. No paid calls enabled.')

def build_manifest():
    files={name:digest(ROOT/name) for name in inventory(ROOT)}
    files.update({name:digest(ROOT/name) for name in ('ai-runtime/requirements-m1.lock','api/go.mod','api/go.sum','web/package-lock.json')})
    obj={'version':'release-manifest-v1','config_version':'m3-runtime-v1','model':'deepseek-flash','files':files}
    write_new(ROOT/'release-manifest.json',obj)
    verify(ROOT,ROOT/'release-manifest.json')
    (ROOT/'release-manifest.json').chmod(0o444)
    print(json.dumps({'release_manifest_sha256':digest(ROOT/'release-manifest.json'),'files':len(files)}))

def opening_new(project_id):
    if not project_id or len(project_id)>100:raise ValueError('NEW_PROJECT_ID_REQUIRED')
    if (STATE/'state/opening-balance.json').exists() or (STATE/'opening.json').exists():raise ValueError('OPENING_ALREADY_EXISTS')
    declaration={'version':'release-new-project-declaration-v1','project_id':project_id,'as_of':stamp(),
                 'statement':'Operator declares this is a new independent project with no historical provider expenditure. This must not be used to reset or migrate an existing project.'}
    source=STATE/'state/new-project-declaration.json';write_new(source,declaration)
    write_new(STATE/'opening.json',{'version':'release-opening-balance-v1','project_id':project_id,
      'known_cny':'0','retained_cny':'0','source_sha256':digest(source),'retained_authorization_ref':'',
      'as_of':declaration['as_of'],'original_instances_stopped':True})
    print('Created a zero opening for the declared NEW independent project. Existing projects must import their actual balances; live mode remains paused.')

def models(offline=False):
    from huggingface_hub import snapshot_download
    expected=json.loads((ROOT/'config/m1-embedding.json').read_text())
    directory=STATE/'models/bge-m3'
    if not offline:
        os.environ['HF_HUB_OFFLINE']='0';os.environ['HF_HUB_DISABLE_XET']='1'
        snapshot_download(expected['model'],revision=expected['revision'],local_dir=directory,
                          token=False,allow_patterns=list(expected['files']),local_files_only=False)
    for name,value in expected['files'].items():
        p=directory/name
        if not p.is_file() or p.stat().st_size!=value['bytes'] or digest(p)!=value['sha256']:
            raise ValueError('MODEL_ASSET_MISMATCH: '+name)
    print(json.dumps({'status':'VERIFIED','revision':expected['revision'],'paid_calls':0}))

def model_smoke():
    import resource
    from types import SimpleNamespace
    from crackrag_m1.embedding import DenseEncoder
    encoder=DenseEncoder(SimpleNamespace(embedding_mode='bge-m3',embedding_directory=STATE/'models/bge-m3'))
    vectors,metrics=encoder.encode(['Revenue: 100.00 CNY. 收入：100.00元。'])
    print(json.dumps({'status':'BGE_CPU_SMOKE_PASSED','dimensions':len(vectors[0]),
      'l2_norm_squared':sum(v*v for v in vectors[0]),'metrics':metrics,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'paid_calls':0}))

def price():
    import httpx
    from tariff import verify as verify_tariff
    url='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
    response=httpx.get(url,timeout=30,follow_redirects=True);response.raise_for_status()
    if str(response.url)!=url:raise ValueError('PRICE_SOURCE_REDIRECT_CHANGED')
    raw=response.content;verification=verify_tariff(raw);now=datetime.now(timezone.utc)
    folder=STATE/'state/prices'/now.strftime('%Y%m%dT%H%M%S%fZ');folder.mkdir(parents=True)
    (folder/'pricing.html').write_bytes(raw)
    snapshot={'verified':True,'verified_at':stamp(now),'source_sha256':sha256(raw).hexdigest(),'verification':verification,
      'pricing':{'currency':'CNY','version':'release-deepseek-flash-'+folder.name,'source':url,'model':'deepseek-flash',
                 'schedule':'deepseek-cn-peak-v1','input_miss_per_million':'1','input_hit_per_million':'0.02','output_per_million':'4'},'billing_confirmed':False}
    write_new(folder/'pricing.json',snapshot)
    atomic(STATE/'state/latest-price.json',{'path':str(folder.relative_to(STATE/'state')/'pricing.json')})
    print(json.dumps({'status':'VERIFIED','price':str(folder.relative_to(STATE)),'model_calls':0}))

def opening_value(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    required={'version','project_id','known_cny','retained_cny','source_sha256','retained_authorization_ref','as_of','original_instances_stopped'}
    if set(value)!=required or value['version']!='release-opening-balance-v1' or value['original_instances_stopped'] is not True:
        raise ValueError('OPENING_BALANCE_SCHEMA_INVALID')
    if any(not isinstance(value[k],str) for k in required-{'original_instances_stopped'}):raise ValueError('OPENING_BALANCE_SCHEMA_INVALID')
    if len(value['source_sha256'])!=64 or any(c not in '0123456789abcdef' for c in value['source_sha256']):raise ValueError('OPENING_SOURCE_HASH_INVALID')
    if not value['project_id'] or any(not Decimal(value[k]).is_finite() or Decimal(value[k])<0 for k in ('known_cny','retained_cny')):
        raise ValueError('OPENING_AMOUNT_INVALID')
    if Decimal(value['known_cny'])+Decimal(value['retained_cny'])>100:raise ValueError('OPENING_EXCEEDS_CAP')
    if Decimal(value['retained_cny']) and not value['retained_authorization_ref']:raise ValueError('UNKNOWN_CONTINUATION_EVIDENCE_REQUIRED')
    when=datetime.fromisoformat(value['as_of'])
    if when.tzinfo is None or when>datetime.now(timezone.utc):raise ValueError('OPENING_TIME_INVALID')
    value['as_of']=stamp(when)
    # Match encoding/json's RFC3339Nano formatting and sorted map keys.
    if '.' in value['as_of']:value['as_of']=value['as_of'].rstrip('Z').rstrip('0').rstrip('.')+'Z'
    return value

def opening_bytes(opening):
    # Match Go encoding/json canonical map serialization, including its HTML
    # and JavaScript separator escaping, while retaining UTF-8 for other text.
    raw=json.dumps(opening,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    for char,escaped in (('&',r'\u0026'),('<',r'\u003c'),('>',r'\u003e'),('\u2028',r'\u2028'),('\u2029',r'\u2029')):raw=raw.replace(char,escaped)
    return raw.encode('utf-8')

def live_prepare(opening_path,exclusive=False,cap='5',requests=80):
    if not exclusive:raise ValueError('CONFIRM_EXCLUSIVE_PAID_ENVIRONMENT_REQUIRED')
    control=json.loads((STATE/'state/live-control.json').read_text())
    if control['enabled']:raise ValueError('PAUSE_BEFORE_PREPARING_NEW_SESSION')
    if not Decimal('0')<Decimal(cap)<=Decimal('100') or not 1<=requests<=2000:raise ValueError('LOCAL_LIMIT_INVALID')
    if not (STATE/'secrets/deepseek_api_key').read_text().strip():raise ValueError('SET_SERVER_SIDE_DEEPSEEK_KEY_FILE_FIRST')
    models(True)
    opening=opening_value(opening_path)
    saved=STATE/'state/opening-balance.json'
    if saved.exists() and json.loads(saved.read_text())!=opening:raise ValueError('OPENING_ALREADY_BOUND_DIFFERENTLY')
    price_rel=json.loads((STATE/'state/latest-price.json').read_text())['path']
    price_path=STATE/'state'/price_rel
    if not price_path.resolve().is_relative_to((STATE/'state').resolve()):raise ValueError('PRICE_PATH_INVALID')
    snapshot=json.loads(price_path.read_text());checked=datetime.fromisoformat(snapshot['verified_at'])
    if not timedelta(0)<=datetime.now(timezone.utc)-checked<timedelta(hours=24):raise ValueError('PRICE_EXPIRED')
    from tariff import verify as verify_tariff
    verify_tariff(price_path.with_suffix('.html').read_bytes())
    if digest(price_path.with_suffix('.html'))!=snapshot['source_sha256']:raise ValueError('PRICE_HASH_INVALID')
    identity=verify(ROOT,ROOT/'release-manifest.json');session_id=str(uuid.uuid4())
    if not saved.exists():write_new(saved,opening)
    folder=STATE/'state/sessions'/session_id;folder.mkdir(parents=True)
    for suffix in ('.json','.html'):shutil.copyfile(price_path.with_suffix(suffix),folder/('pricing'+suffix))
    canonical=opening_bytes(opening)
    value={'version':'live-session-manifest-v1','session_id':session_id,'release_manifest_sha256':identity,
           'opening_balance':opening,'opening_sha256':sha256(canonical).hexdigest(),'price_sha256':digest(folder/'pricing.json'),
           'price_html_sha256':digest(folder/'pricing.html'),'cap_cny':cap,'max_requests':requests,'max_output_tokens':2048,
           'concurrency':2,'automatic_retries':0,'expires_at':stamp(checked+timedelta(hours=24))}
    write_new(folder/'session.json',value)
    relative=folder.relative_to(STATE/'state')
    env_update(CRACKRAG_PROVIDER='deepseek',CRACKRAG_EMBEDDING='bge-m3',CRACKRAG_SESSION='/state/'+str(relative/'session.json'),CRACKRAG_PRICE='/state/'+str(relative/'pricing.json'))
    atomic(STATE/'state/live-control.json',{'enabled':False,'session_sha256':digest(folder/'session.json')})
    print(json.dumps({'status':'PREPARED_PAUSED','session_id':session_id,'cap_cny':cap,'max_requests':requests,'paid_calls':0}))

def control(enabled):
    path=STATE/'state/live-control.json';value=json.loads(path.read_text())
    if enabled:
        if not value['session_sha256']:raise ValueError('LIVE_SESSION_NOT_PREPARED')
        sessions=[p for p in (STATE/'state/sessions').glob('*/session.json') if digest(p)==value['session_sha256']]
        if len(sessions)!=1:raise ValueError('SESSION_NOT_FOUND')
        session=json.loads(sessions[0].read_text())
        if verify(ROOT,ROOT/'release-manifest.json')!=session['release_manifest_sha256'] or datetime.now(timezone.utc)>=datetime.fromisoformat(session['expires_at']):raise ValueError('SESSION_EXPIRED_OR_RELEASE_CHANGED')
    value['enabled']=enabled;atomic(path,value)
    print(json.dumps({'enabled':enabled,'session_sha256':value['session_sha256']}))

def volume_init():
    BLOBS.mkdir(parents=True,exist_ok=True);os.chown(BLOBS,10001,10001);BLOBS.chmod(0o750)

def activation_sql():
    identity=verify(ROOT,ROOT/'release-manifest.json')
    catalog=digest(ROOT/'api/internal/app/m2_catalog.json')
    # Both substitutions are verified local SHA-256 values, never caller SQL.
    print(f"""BEGIN;
SELECT pg_advisory_xact_lock(73003001);
LOCK TABLE query_runs,llm_calls,m3_jobs,m2_active_configuration IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM m2_configurations WHERE digest='{catalog}') THEN
  RAISE EXCEPTION 'CONFIGURATION_NOT_REGISTERED_START_NEW_IMAGE_ONCE'; END IF;
 IF EXISTS(SELECT 1 FROM query_runs WHERE state IN ('QUEUED','RUNNING'))
  OR EXISTS(SELECT 1 FROM m3_jobs WHERE state NOT IN ('COMMITTED','SKIPPED','FAILED','OUTCOME_UNKNOWN'))
  OR EXISTS(SELECT 1 FROM llm_calls WHERE state='RESERVED' OR (provider='deepseek' AND state='UNKNOWN')) THEN
  RAISE EXCEPTION 'ACTIVATION_REQUIRES_IDLE_SETTLED_STATE'; END IF;
END $$;
WITH previous AS (SELECT digest FROM m2_active_configuration WHERE singleton),
changed AS (UPDATE m2_active_configuration SET digest='{catalog}' WHERE singleton RETURNING digest)
SELECT jsonb_build_object('version','release-activation-v1','release_manifest_sha256','{identity}',
 'previous_digest',(SELECT digest FROM previous),'active_digest',(SELECT digest FROM changed),
 'historical_facts_retained',(SELECT count(*) FROM facts),'activated_at',clock_timestamp());
COMMIT;""")

def backup_files(name):
    if not name or Path(name).name!=name:raise ValueError('BACKUP_NAME_INVALID')
    folder=STATE/'backups'/name
    if not (folder/'database.dump').is_file():raise ValueError('DATABASE_DUMP_REQUIRED')
    if (folder/'manifest.json').exists():raise ValueError('BACKUP_ALREADY_COMPLETE')
    for source,label in ((BLOBS,'blobs'),(STATE/'state','state')):
        with tarfile.open(folder/(label+'.tar'),'x') as archive:
            for path in sorted(source.rglob('*')):
                if path.is_symlink():raise ValueError('BACKUP_SYMLINK_REJECTED')
                if path.is_file():archive.add(path,arcname=path.relative_to(source).as_posix(),recursive=False)
    write_new(folder/'manifest.json',{'version':'release-backup-v1','created_at':stamp(),
        'release_manifest_sha256':verify(ROOT,ROOT/'release-manifest.json'),
        'files':{n:digest(folder/n) for n in ('database.dump','blobs.tar','state.tar')},
        'secrets_included':False,'restore_policy':'Restore only into empty database/blob volumes. Remains mock/paused until explicit new live preparation.'})
    print('Private backup complete: '+name)

def restore_files(name):
    if not name or Path(name).name!=name:raise ValueError('BACKUP_NAME_INVALID')
    folder=STATE/'backups'/name;manifest=json.loads((folder/'manifest.json').read_text())
    if manifest.get('version')!='release-backup-v1' or manifest.get('release_manifest_sha256')!=verify(ROOT,ROOT/'release-manifest.json'):
        raise ValueError('RESTORE_REQUIRES_MATCHING_RELEASE')
    if set(manifest['files'])!={'database.dump','blobs.tar','state.tar'}:raise ValueError('BACKUP_MANIFEST_INVALID')
    for name,want in manifest['files'].items():
        if digest(folder/name)!=want:raise ValueError('BACKUP_HASH_MISMATCH')
    blobs=BLOBS
    if any(blobs.iterdir()) or (STATE/'state/opening-balance.json').exists():raise ValueError('RESTORE_TARGET_NOT_EMPTY')
    # Validate every member and collision before creating any restored file.
    for label,target in (('blobs',blobs),('state',STATE/'state')):
        with tarfile.open(folder/(label+'.tar')) as archive:
            seen=set()
            for item in archive.getmembers():
                path=(target/item.name).resolve()
                if not item.isfile() or Path(item.name).is_absolute() or '\\' in item.name or path in seen or not path.is_relative_to(target.resolve()):raise ValueError('BACKUP_MEMBER_REJECTED')
                seen.add(path)
                if path.exists() and not(label=='state' and item.name=='live-control.json'):raise ValueError('RESTORE_WOULD_OVERWRITE')
    for label,target in (('blobs',blobs),('state',STATE/'state')):
        with tarfile.open(folder/(label+'.tar')) as archive:
            for item in archive.getmembers():
                path=target/item.name;path.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(item) as source,path.open('wb') as out:shutil.copyfileobj(source,out)
                if label=='blobs':os.chown(path,10001,10001);path.chmod(0o600)
    atomic(STATE/'state/live-control.json',{'enabled':False,'session_sha256':''})
    env_update(CRACKRAG_PROVIDER='mock',CRACKRAG_EMBEDDING='fixture',CRACKRAG_SESSION='',CRACKRAG_PRICE='/app/config/m1-pricing.json')
    print('Files restored. Model calls remain disabled. Restore the verified database before starting services.')

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['manifest','init','opening-new','models','price','prepare','enable','pause','mock','verify','volume-init','activation-sql','demo-start','demo-finish','backup-files','restore-files'])
    p.add_argument('--project-id')
    p.add_argument('--scenario',choices=['happy','pause_after_candidates'],default='happy')
    p.add_argument('--name')
    p.add_argument('--offline',action='store_true');p.add_argument('--smoke',action='store_true');p.add_argument('--opening',default='/release/opening.json');p.add_argument('--confirm-exclusive',action='store_true');p.add_argument('--cap',default='5');p.add_argument('--requests',type=int,default=80);a=p.parse_args()
    if a.action=='manifest':build_manifest()
    elif a.action=='init':init()
    elif a.action=='opening-new':opening_new(a.project_id)
    elif a.action=='models':
        models(a.offline)
        if a.smoke:model_smoke()
    elif a.action=='price':price()
    elif a.action=='prepare':live_prepare(a.opening,a.confirm_exclusive,a.cap,a.requests)
    elif a.action in ('enable','pause'):control(a.action=='enable')
    elif a.action=='mock':control(False);env_update(CRACKRAG_PROVIDER='mock',CRACKRAG_EMBEDDING='fixture',CRACKRAG_SESSION='',CRACKRAG_PRICE='/app/config/m1-pricing.json',CRACKRAG_MOCK_SCENARIO=a.scenario)
    elif a.action=='verify':print(json.dumps({'status':'VERIFIED','release_manifest_sha256':verify(ROOT,ROOT/'release-manifest.json')}))
    elif a.action=='volume-init':volume_init()
    elif a.action=='activation-sql':activation_sql()
    elif a.action in ('demo-start','demo-finish'):
        from demo import execute
        execute(ROOT,STATE,a.action.split('-')[1])
    elif a.action=='backup-files':backup_files(a.name)
    elif a.action=='restore-files':restore_files(a.name)

if __name__=='__main__':
    try:main()
    except Exception as exc:print('Release operation refused: '+str(exc),file=sys.stderr);raise SystemExit(1)
    finally:
        if STATE.is_dir():
            # Operator retains ownership; runtime's explicit group can read
            # secret bind mounts without granting world-readable access.
            for p in [STATE,*STATE.rglob('*')]:
                if p.is_symlink():continue
                try:
                    os.chown(p,int(os.getenv('CRACKRAG_HOST_UID','10001')),10001)
                    p.chmod(0o750 if p.is_dir() else 0o640)
                except PermissionError:pass
