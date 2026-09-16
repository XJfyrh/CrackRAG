"""Mock-only recovery rehearsal using the same authenticated HTTP API as the UI."""
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from urllib.request import Request,urlopen
from uuid import uuid4

QUESTION='样例控股2024年营业收入是多少？'

def execute(root,state,phase):
    token=(state/'secrets/access_token').read_text().strip()
    def request(host,path,body=None,headers=None):
        hdr={'Authorization':'Bearer '+token,**(headers or {})}
        if isinstance(body,dict):body=json.dumps(body,ensure_ascii=False).encode();hdr['Content-Type']='application/json'
        with urlopen(Request(host+path,data=body,headers=hdr),timeout=30) as response:return json.load(response)
    a='http://api:8080';b='http://api2:8080'
    for host in ((a,b) if phase=='start' else (b,)):
        if request(host,'/healthz').get('provider')!='mock':raise ValueError('DEMO_REQUIRES_MOCK_BOTH_PAIRS')
    def until(read,done,seconds=110):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            value=read()
            if done(value):return value
            time.sleep(.3)
        raise ValueError('DEMO_DEADLINE_EXCEEDED')
    def save(path,value):
        with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)
    def submit(host,document,build):
        value=request(host,'/api/v1/queries',{'question':QUESTION,'document_ids':[document],'mode':'m3',
            'build_facts':build,'execution_policy':'COLD_ALLOWED','deadline_ms':180000},
            {'Idempotency-Key':'recovery-demo-'+uuid4().hex})
        return value.get('query_id',value.get('id'))
    if phase=='start':
        folder=state/'state/demos'/str(uuid4());folder.mkdir(parents=True)
        boundary='crackrag-'+uuid4().hex;parts=[]
        for key,value in {'pages':'1','title':'Recovery rehearsal sample','year':'2024'}.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="financial.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode(),
            (root/'web/dist/samples/financial.pdf').read_bytes(),f'\r\n--{boundary}--\r\n'.encode()])
        uploaded=request(a,'/api/v1/documents',b''.join(parts),{'Content-Type':'multipart/form-data; boundary='+boundary})
        document=uploaded['document_id']
        doc=until(lambda:next(d for d in request(a,'/api/v1/documents')['documents'] if d['id']==document),lambda d:d['state'] in ('READY','FAILED','INTERRUPTED'))
        if doc['state']!='READY':raise ValueError('DEMO_PARSE_FAILED')
        run=submit(a,document,True)
        def ready(value):
            jobs=value.get('diagnostics',{}).get('m3',{}).get('jobs',[])
            return value['state']=='COMPLETED' and len(jobs)==1 and jobs[0]['state']=='RESULT_READY' and bool(value['calls']) and all(c['state']=='SETTLED' for c in value['calls'])
        before=until(lambda:request(a,'/api/v1/queries/'+run),ready)
        job=before['diagnostics']['m3']['jobs'][0]
        if not job.get('candidate_digest'):raise ValueError('DEMO_CANDIDATE_DIGEST_REQUIRED')
        save(folder/'before.json',{'document_id':document,'run':before,'captured_at':datetime.now(timezone.utc).isoformat()})
        (state/'state/current-demo.json').write_text(json.dumps({'folder':str(folder.relative_to(state))}),encoding='utf-8')
        print(json.dumps({'status':'RESULT_READY_AND_ALL_CALLS_SETTLED','run_id':run,'job_id':job['job_id'],'calls':len(before['calls']),'paid_calls':0}))
        return
    location=json.loads((state/'state/current-demo.json').read_text())['folder'];folder=(state/location).resolve()
    if not folder.is_relative_to((state/'state/demos').resolve()):raise ValueError('DEMO_PATH_INVALID')
    stored=json.loads((folder/'before.json').read_text());before=stored['run'];old=before['diagnostics']['m3']['jobs'][0]
    def complete(value):
        jobs=value.get('diagnostics',{}).get('m3',{}).get('jobs',[])
        return len(jobs)==1 and jobs[0]['state'] in ('COMMITTED','FAILED','SKIPPED','OUTCOME_UNKNOWN')
    after=until(lambda:request(b,'/api/v1/queries/'+before['id']),complete)
    job=after['diagnostics']['m3']['jobs'][0]
    if job['state']!='COMMITTED':raise ValueError('DEMO_RECOVERY_NOT_COMMITTED')
    for key in ('job_id','batch_id','candidate_digest'):
        if job[key]!=old[key]:raise ValueError('DEMO_SAVED_CANDIDATES_CHANGED')
    if {c['attempt_id'] for c in after['calls']}!={c['attempt_id'] for c in before['calls']} or any(c['state']!='SETTLED' for c in after['calls']):raise ValueError('DEMO_REDISPATCH_OR_UNSETTLED_CALL')
    reused_id=submit(b,stored['document_id'],False)
    reused=until(lambda:request(b,'/api/v1/queries/'+reused_id),lambda v:v['state'] in ('COMPLETED','FAILED','INTERRUPTED','TIMED_OUT'))
    if reused['state']!='COMPLETED' or reused['calls'] or reused['answer']['evidence_summary']['structured_coverage']['status']!='FULL':raise ValueError('DEMO_FULL_REUSE_FAILED')
    save(folder/'after.json',{'run':after,'reused':reused})
    proof={'version':'release-recovery-demo-v1','status':'PASS','simulated':True,'paid_calls':0,
        'release_manifest_sha256':request(b,'/healthz')['release_manifest_sha256'],'job_id':job['job_id'],
        'candidate_digest':job['candidate_digest'],'calls_before':len(before['calls']),'calls_after':len(after['calls']),
        'full_reuse_model_calls':len(reused['calls']),'checks':['normal_upload','normal_build','candidates_persisted_before_fault','primary_pair_sigkill','redis_restart','same_job_same_candidates','no_repeat_model_request','committed','full_reuse_zero_model']}
    save(folder/'proof.json',proof);print(json.dumps(proof));print('Private evidence: '+str(folder.relative_to(state)))
