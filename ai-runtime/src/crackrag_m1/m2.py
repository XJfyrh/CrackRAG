"""M2 serial coordinator. Only observed regions enter extraction; no eval imports.

Publication is a separate trusted Go RPC, never a model-visible tool. ProbeModel
gets a fresh message history and a capability for the read-only ProbeTools service.
"""
from decimal import Decimal, ROUND_HALF_UP
import json
import re
from hashlib import sha256

import grpc
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from typing import Literal
from crackrag.v1 import runtime_pb2 as pb
from .config import ROOT
from .model import Model, contract_output_tokens

class CandidateShape(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    entity:str
    property:str
    period:str=Field(pattern=r'^FY20\d{2}$')
    unit:Literal['CNY','ratio']
    scope:str
    value:str=Field(pattern=r'^-?(?:0|[1-9]\d{0,29})(?:\.\d{1,12})?$')
    raw_value:str
    origin:Literal['REPORTED','DERIVED']
    region_id:str
    quote:str
    input_fact_ids:list[str]=Field(default_factory=list)
    formula_version:str=''
    precision:int=0
    rounding:str=''
    complete:bool=False

class ExtractionShape(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    candidates:list[CandidateShape]=Field(max_length=12)

def validate_model_shape(stage,content):
    if stage=='extraction':ExtractionShape.model_validate_json(content,strict=True)
    elif stage=='probe':
        value=json.loads(content)
        if not isinstance(value,dict) or value.get('action') not in ('open','abstain','conclude'):raise ValueError('INVALID_PROBE_ACTION')
    else:json.loads(content)

def payload(system,context,*,max_output_tokens=512):
    return {'model':'deepseek-flash','max_tokens':max_output_tokens,'temperature':0,'thinking':{'type':'disabled'},
        'response_format':{'type':'json_object'},'messages':[{'role':'system','content':system},
        {'role':'user','content':json.dumps(context,ensure_ascii=False)}]}

def mock_candidates(regions,requirements):
    """Mock extracts from actual observed cells, never from an answer fixture."""
    catalog=json.loads((ROOT/'api/internal/app/m2_catalog.json').read_text(encoding='utf-8'))
    result=[]
    for region in regions:
        text=region.text+'\n'+region.context_json
        aliases=[a for e in catalog['entities'] for a in e['aliases'] if a in text]
        entity=max(aliases,key=len) if aliases else 'Unresolved entity'
        lines=[list(map(str.strip,line.split('|'))) for line in region.text.splitlines()]
        header=next((cells for cells in lines if cells[0] in ('Metric','项目','指标')),[])
        for req in requirements:
            definition=next((c for c in catalog['concepts'] if c['id']==req['concept_id']),None)
            if not definition or req['period'] not in header:continue
            col=header.index(req['period'])
            row=next((cells for cells in lines if cells[0] in definition['aliases'] and len(cells)==len(header)),None)
            if not row:continue
            raw=row[col]
            try:value=Decimal(raw.replace(',','').rstrip('%'))
            except Exception:continue
            if '%' in raw:value/=100
            if 'Unit: CNY million' in text:value*=1000000
            quote=next(line for line in region.text.splitlines() if row[0] in line and raw in line)
            result.append({'entity':entity,'property':row[0],'period':req['period'],'unit':definition['unit'],
                'scope':'consolidated','value':str(value),'raw_value':raw,'origin':'REPORTED','region_id':region.id,'quote':quote})
            if len(result)==2:return {'candidates':result}
    return {'candidates':result}

def decode(reply):return json.loads(reply.payload_json)

def verify_assets(expected_digest):
    raw=(ROOT/'api/internal/app/m2_catalog.json').read_bytes()
    if sha256(raw).hexdigest()!=expected_digest:raise ValueError('M2_CONFIGURATION_MISMATCH')
    for path,digest in json.loads(raw).get('assets',{}).items():
        if sha256((ROOT/path).read_bytes()).hexdigest()!=digest:raise ValueError('M2_ASSET_VERSION_MISMATCH')

async def probe(agent,batch,report):
    """At most one independent round; durable counters are enforced by Go."""
    independent=None
    try:
        agent.check()
        configuration=json.loads(agent.contract.configuration_json or '{}')
        probe_limit=configuration.get('max_probe_model_calls',2)
        if configuration.get('probe_enabled') is False or probe_limit == 0:
            return 'PROBE_DISABLED_BY_CONTRACT'
        if type(probe_limit) is not int or not 0 <= probe_limit <= 2:
            raise ValueError('UNSUPPORTED_PROBE_CONTRACT')
        if agent.model_count>=agent.contract.max_model_calls:return 'PROBE_MODEL_CALL_LIMIT'
        grant=decode(await agent.control.BeginProbe(pb.BatchRequest(context=agent.context,batch_id=batch),metadata=agent.metadata,timeout=10))
        independent=Model(agent.settings,agent.tools,agent.context,agent.metadata)
        independent.contract=agent.contract
        if agent.m2_config.get('m3_enabled'):
            independent.m3_call={**getattr(getattr(agent,'model',None),'m3_call',{}),
                'subexperiment':agent.m2_config.get('subexperiment','quality')}
        system=(ROOT/'ai-runtime/prompts/m2-v1/probe.txt').read_text(encoding='utf-8')
        allowed=grant['region_ids']
        hypotheses=[{'candidate':i['candidate'],'reasons':i['reasons']} for i in grant['doubts']]
        agent.check();agent.model_count+=1
        text,_=await independent.ask(payload(system,{'phase':'select','hypotheses':hypotheses,'allowed_region_ids':allowed},max_output_tokens=contract_output_tokens(agent.contract)),
            {'action':'open','region_id':allowed[0]},stage='probe',batch_id=batch,probe_token=grant['probe_token'])
        action=json.loads(text)
        if set(action)!= {'action','region_id'} or action['action']!='open' or action['region_id'] not in allowed:return 'PROBE_ABSTAINED_OR_INVALID_ACTION'
        probe_context=pb.RequestContext();probe_context.CopyFrom(agent.context);probe_context.service_id='python-probe'
        observed=await agent.probe_tools.OpenDocument(pb.ProbeOpenRequest(context=probe_context,batch_id=batch,
            probe_token=grant['probe_token'],region_ids=[action['region_id']]),metadata=agent.metadata,timeout=10)
        from .agent import source
        agent.check()
        if probe_limit < 2 or agent.model_count>=agent.contract.max_model_calls:return 'PROBE_MODEL_CALL_LIMIT'
        agent.model_count+=1
        await independent.ask(payload(system,{'phase':'inspect','hypotheses':hypotheses,'observations':[source(r) for r in observed.regions]},max_output_tokens=contract_output_tokens(agent.contract)),
            {'action':'conclude','summary':'Fresh source observed; ambiguity remains for deterministic verification.'},
            stage='probe',batch_id=batch,probe_token=grant['probe_token'])
        return 'PROBE_OBSERVED'
    except (grpc.aio.AioRpcError,ValueError,KeyError,TypeError) as exc:
        record=getattr(independent,'last_record',{})
        if getattr(agent.context,'job_id','') and record.get('http_dispatched') and record.get('cost',{}).get('amount') is None:
            raise ValueError('PROBE_COST_UNKNOWN') from exc
        return 'PROBE_UNAVAILABLE_OR_UNRESOLVED'
    finally:
        record=getattr(independent,'last_record',{})
        if getattr(agent.context,'job_id','') and record.get('http_dispatched') and record.get('cost',{}).get('amount') is None:
            # Propagate the independent transport's uncertain outcome even when
            # cancellation bypasses the ordinary exception handler above.
            agent.m3_unknown_call=record
        if independent:await independent.close()

def failed_extraction_result(model,batch_id,reason):
    """Keep malformed or absent content auditable without masking the failure.

    A pre-dispatch failure may leave last_record pointing to the previous Answer
    call. Never persist that unrelated content as this batch's extraction.
    """
    record=getattr(model,'last_record',{})
    if not isinstance(record,dict) or record.get('stage')!='extraction' or record.get('batch_id')!=batch_id:
        return json.dumps({'extraction_failure':str(reason),'raw_response':None})
    response=record.get('raw_response')
    if isinstance(response,dict):
        choices=response.get('choices')
        if isinstance(choices,list) and choices and isinstance(choices[0],dict):
            message=choices[0].get('message')
            if isinstance(message,dict) and isinstance(message.get('content'),str):return message['content']
    return json.dumps({'extraction_failure':str(reason),'raw_response':response},ensure_ascii=False)

async def build(agent,model):
    from .agent import source,event
    if not agent.opened or not agent.requirements:return
    regions=list(agent.opened.values())[:5]
    batch=decode(await agent.control.BeginExtraction(pb.BeginExtractionRequest(context=agent.context,
        region_ids=[r.id for r in regions],logical_key='m2-observed-extraction-v1'),metadata=agent.metadata,timeout=10))
    batch_id=batch['batch_id']
    if not batch['has_candidates']:
        agent.check()
        if agent.model_count>=agent.contract.max_model_calls:raise ValueError('MODEL_CALL_LIMIT')
        system=(ROOT/'ai-runtime/prompts/m2-v1/extraction.txt').read_text(encoding='utf-8')
        agent.model_count+=1
        try:
            raw,_=await model.ask(payload(system,{'requirements':agent.requirements,'sources':[source(r) for r in regions]},max_output_tokens=contract_output_tokens(agent.contract)),
                mock_candidates(regions,agent.requirements),stage='extraction',batch_id=batch_id)
        except ValueError as exc:
            raw=failed_extraction_result(model,batch_id,exc)
            await agent.control.StoreCandidates(pb.CandidateRequest(context=agent.context,batch_id=batch_id,raw_result=raw),metadata=agent.metadata,timeout=10)
            raise
        await agent.control.StoreCandidates(pb.CandidateRequest(context=agent.context,batch_id=batch_id,raw_result=raw),metadata=agent.metadata,timeout=10)
    report=decode(await agent.control.ValidateCandidates(pb.BatchRequest(context=agent.context,batch_id=batch_id),metadata=agent.metadata,timeout=10))
    probe_reason='NOT_NEEDED'
    if any(item['status']=='INCONCLUSIVE' and item.get('source') for item in report['items']):
        probe_reason=await probe(agent,batch_id,report)
        report=decode(await agent.control.ValidateCandidates(pb.BatchRequest(context=agent.context,batch_id=batch_id,stop_reason=probe_reason),metadata=agent.metadata,timeout=10))
    committed=decode(await agent.control.CommitExtraction(pb.CommitRequest(context=agent.context,batch_id=batch_id,report_id=report['report_id']),metadata=agent.metadata,timeout=10))
    yield event('VALIDATION',{'batch_id':batch_id,'report_id':report['report_id'],'statistics':report['statistics'],
        'probe_counts':report['probe_counts'],'probe_outcome':probe_reason,'published_fact_ids':committed['published_fact_ids']})

def reusable_text(facts):
    return '\n'.join(f"{f['entity_id']} · {f['period']} · {f['concept_id']}：{f['value']} {f['unit']}（{f['origin']}）" for f in facts)

def flattened_sources(facts):
    result={}
    def collect(value):
        if isinstance(value,list):
            for item in value:collect(item)
        elif isinstance(value,dict):
            if 'region_id' in value:result[value['region_id']]=value
            if 'input_sources' in value:collect(value['input_sources'])
    for fact in facts:collect(fact['sources'])
    return list(result.values())

async def plan(agent):
    from .agent import event
    verify_assets(agent.m2_config['m2_config_digest'])
    agent.tool_admit()
    resolved=decode(await agent.tools.ResolveConcepts(pb.ResolveRequest(context=agent.context,question=agent.request.question),metadata=agent.metadata,timeout=10))
    agent.requirements=resolved['requirements']
    agent.answer_requirements=resolved.get('answer_requirements',[])
    if agent.requirements:
        agent.tool_admit()
        request=pb.FactsRequest(context=agent.context,requirements_json=json.dumps(agent.requirements),limit=20)
        coverage=decode(await agent.tools.GetCoverage(request,metadata=agent.metadata,timeout=10))
        agent.coverage=coverage
        if coverage['matched']:
            agent.tool_admit();read=decode(await agent.tools.ReadFacts(request,metadata=agent.metadata,timeout=10))
            agent.coverage=coverage=read
            if not read['truncated']:
                # Prefer disclosed facts and reuse one fact per exact requirement.
                facts=read['facts'];chosen=[]
                for match in coverage['matched']:
                    options=[f for f in facts if f['fact_id'] in match['fact_ids']]
                    if options:chosen.append(sorted(options,key=lambda f:(f['origin']!='REPORTED',f['fact_id']))[0])
                agent.reused=chosen
            else:agent.coverage=read;agent.reused=[]
        if agent.m2_config.get('build_facts') and coverage['status']!='FULL' and agent.control:
            for missing in coverage['missing']:
                wanted=missing['requirement']
                if wanted['concept_id']!='fin:gross_margin' or wanted.get('kind')=='set':continue
                input_reqs=[{**wanted,'concept_id':concept,'unit':'CNY'} for concept in ('fin:revenue','fin:cost_of_revenue')]
                agent.tool_admit()
                inputs=decode(await agent.tools.ReadFacts(pb.FactsRequest(context=agent.context,requirements_json=json.dumps(input_reqs),limit=20),metadata=agent.metadata,timeout=10))
                if inputs['status']=='FULL' and not inputs['truncated']:
                    selected=[]
                    for concept in ('fin:revenue','fin:cost_of_revenue'):
                        selected.append(next(f['fact_id'] for f in inputs['facts'] if f['concept_id']==concept and f['origin']=='REPORTED'))
                    derived=decode(await agent.control.DeriveFacts(pb.DeriveRequest(context=agent.context,input_fact_ids=selected),metadata=agent.metadata,timeout=10))
                    yield event('DERIVATION_RECORDED',{'status':'VALIDATED_DERIVED_FACT','result':derived,'input_fact_ids':selected})
                    agent.tool_admit()
                    read=decode(await agent.tools.ReadFacts(request,metadata=agent.metadata,timeout=10))
                    agent.coverage=coverage=read
                    if read['status']=='FULL' and not read['truncated']:agent.reused=read['facts']
                break
        if agent.reused and coverage['missing']:
            catalog=json.loads((ROOT/'api/internal/app/m2_catalog.json').read_text(encoding='utf-8'))
            phrases=[]
            for missing in coverage['missing']:
                req=missing['requirement'];entity=next(e['aliases'][0] for e in catalog['entities'] if e['id']==req['entity_id'])
                concept=next(c['aliases'][0] for c in catalog['concepts'] if c['id']==req['concept_id'])
                phrases.append(f"{entity} {req['period']} {concept}")
            agent.fallback_question='; '.join(phrases)
    else:agent.coverage={'status':'UNKNOWN','matched':[],'missing':[],'reasons':resolved['reasons'],'truncated':False}
    yield event('STRUCTURE_CHECKED',{'coverage':agent.coverage,'reused_fact_ids':[f['fact_id'] for f in agent.reused],
        'fallback_question':agent.fallback_question if agent.coverage['status']!='FULL' else None})

async def revalidate(agent,batch_id):
    from .agent import event
    report=decode(await agent.control.ValidateCandidates(pb.BatchRequest(context=agent.context,batch_id=batch_id),metadata=agent.metadata,timeout=10))
    # Explicit revalidation does not generate or probe again. Existing cumulative
    # observations remain attached; a fresh deadline never extends the batch.
    committed=decode(await agent.control.CommitExtraction(pb.CommitRequest(context=agent.context,batch_id=batch_id,report_id=report['report_id']),metadata=agent.metadata,timeout=10))
    yield event('VALIDATION',{'batch_id':batch_id,'report_id':report['report_id'],'statistics':report['statistics'],'published_fact_ids':committed['published_fact_ids'],'model_calls':0})
    yield event('ANSWER',{'text':'已重新验证持久候选；仅通过项可发布。','evidence_summary':{'sources':[],'unresolved':[],
        'validation_statistics':report['statistics'],'report_id':report['report_id'],'reused_facts':[]},'tool_calls':0,'model_calls':0,'provider':agent.settings.provider})

def full_events(agent):
    from .agent import event
    if agent.m2_config.get('answer_policy')=='financial-supported-v1':
        from .actions import FinancialAnswer
        return agent.financial_events(FinancialAnswer(action='answer'))
    facts=agent.reused;sources=flattened_sources(facts)
    evidence={'sources':sources,'facts':facts,'reused_facts':facts,'calculations':[f for f in facts if f['origin']=='DERIVED'],
        'unresolved':[],'structured_coverage':agent.coverage,'raw_observation_region_ids':[],
        'fact_publication':'VALIDATED_ONLY','validation':'Go publication report and current scope verified',
        'configuration_version':agent.context.config_version}
    return [event('SOURCE_ACQUIRED',{'path':'VALIDATED_FACT','fact_ids':[f['fact_id'] for f in facts],'report_ids':[f['report_id'] for f in facts]}),
        event('FACT_RESOLVED',{'status':'VALIDATED_FACT_REUSE','fact_ids':[f['fact_id'] for f in facts],'report_ids':[f['report_id'] for f in facts]}),
        event('ANSWER',{'text':reusable_text(facts),'evidence_summary':evidence,'provider':agent.settings.provider,
            'tool_calls':agent.tool_count,'model_calls':agent.model_count}),event('DONE',{'state':'COMPLETED'})]
