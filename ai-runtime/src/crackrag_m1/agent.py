"""Bounded source Agent. Gold answers and evaluation modules are never imported."""
import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

from pydantic import ValidationError
from crackrag.v1 import runtime_pb2 as pb
from .actions import ACTION,FINANCIAL_ACTION,FinancialAnswer,Answer,Search,Open,Calculate,validate_answer,calculate
from .config import ROOT
from .model import Model, contract_output_tokens

def event(kind,payload):
    return pb.RuntimeEvent(type=kind,payload_json=json.dumps(payload,ensure_ascii=False,allow_nan=False),
                           occurred_at=datetime.now(timezone.utc).isoformat())

def source(region):
    return {'region_id':region.id,'document_id':region.document_id,'document_version_id':region.document_version_id,
        'title':region.document_title,'page':region.page,'bbox':list(region.bbox),'page_width':region.page_width,
        'page_height':region.page_height,'kind':region.kind,'text':region.text,'text_sha256':region.text_sha256,
        'context':json.loads(region.context_json),'parser_version':region.parser_version,
        'source_url':f'/api/v1/documents/{region.document_id}/versions/{region.document_version_id}/source#page={region.page}'}

class Agent:
    def __init__(self,request,settings,encoder,tools,metadata,control=None,probe_tools=None,registry=None,jobs=None):
        self.request=request;self.settings=settings;self.encoder=encoder;self.tools=tools;self.metadata=metadata
        self.context=pb.RequestContext();self.context.CopyFrom(request.context);self.context.service_id='python-runtime'
        self.contract=request.contract;self.opened={};self.seen={};self.history=[];self.calculations=[]
        self.tool_count=0;self.model_count=0
        self.control=control;self.probe_tools=probe_tools
        self.m2_config=json.loads(self.contract.configuration_json or '{}')
        self.reused=[];self.requirements=[];self.coverage={};self.fallback_question=request.question
        self.answer_requirements=[]
        self.registry=registry;self.jobs=jobs;self.m3_coordinator=None

    @property
    def strict_financial(self):
        return self.m2_config.get('answer_policy')=='financial-supported-v1'

    def financial_events(self,answer):
        # No asserted support event is emitted before Go's final transaction.
        return [event('ANSWER',{'answer_policy':'financial-supported-v1',
            'claims':[c.model_dump() for c in answer.claims],
            'abstentions':[a.model_dump() for a in answer.abstentions],
            'reused_fact_ids':[f['fact_id'] for f in self.reused],
            'tool_calls':self.tool_count,'model_calls':self.model_count,'provider':self.settings.provider}),
            event('DONE',{'state':'COMPLETED'})]

    def financial_mock(self):
        from .m2 import mock_candidates
        candidates=mock_candidates(list(self.opened.values()),self.requirements)['candidates']
        claims=[]
        for item in self.answer_requirements:
            req=item['requirement']
            catalog=json.loads((ROOT/'api/internal/app/m2_catalog.json').read_text(encoding='utf-8'))
            concept=next((c for c in catalog['concepts'] if c['id']==req['concept_id']),None)
            candidate=next((c for c in candidates if c['period']==req['period'] and concept and c['property'] in concept['aliases']),None)
            if candidate:claims.append({'requirement_key':item['requirement_key'],
                **{key:candidate[key] for key in ('region_id','quote','raw_value','value')}})
        return {'action':'answer','claims':claims,'abstentions':[]}

    def check(self):
        if datetime.now(timezone.utc)>=datetime.fromisoformat(self.contract.deadline_at):
            raise ValueError('DEADLINE_EXCEEDED')
        policies=('HOT_ONLY','COLD_ALLOWED') if self.m2_config.get('m3_enabled') else ('COLD_ALLOWED',)
        if self.contract.execution_policy not in policies:
            raise ValueError('UNSUPPORTED_EXECUTION_CONTRACT')
        contract_output_tokens(self.contract)

    def tool_admit(self):
        self.check()
        if self.tool_count>=self.contract.max_tool_calls:raise ValueError('TOOL_CALL_LIMIT')
        self.tool_count+=1

    async def search(self,query,year=0,page=0):
        self.tool_admit();tool_id=str(uuid4())
        vectors,usage=await self.encoder.encode_async([query]);self.check()
        result=await self.tools.SearchDocuments(pb.SearchRequest(context=self.context,query=query,vector=vectors[0],
            top_k=3,year=year,page=page,embedding_version=self.encoder.version),metadata=self.metadata,timeout=20)
        for region in result.regions:self.seen[region.id]=region
        self.history.append({'tool':'SearchDocuments','query':query,'year':year,'page':page,
            'region_ids':[r.id for r in result.regions],'previews':[{'region_id':r.id,'page':r.page,'kind':r.kind,'text':r.text[:350]} for r in result.regions],'method':result.method,'truncated':result.truncated})
        return result,event('TOOL',{'tool_call_id':tool_id,**self.history[-1],'embedding_usage':usage})

    async def open(self,ids):
        self.tool_admit();tool_id=str(uuid4())
        if any(i not in self.seen for i in ids):raise ValueError('REGION_NOT_IN_SEARCH_RESULTS')
        result=await self.tools.OpenDocument(pb.OpenRequest(context=self.context,region_ids=ids),metadata=self.metadata,timeout=10)
        tentative={**self.opened,**{r.id:r for r in result.regions}}
        if sum(len(r.text)+len(r.context_json) for r in tentative.values())>self.contract.max_context_chars:
            raise ValueError('CONTEXT_LIMIT')
        self.opened=tentative;self.history.append({'tool':'OpenDocument','region_ids':ids})
        return [event('SOURCE_ACQUIRED',{'tool_call_id':tool_id,'path':'RAW_DOCUMENT','sources':[source(r) for r in result.regions]}),
                event('PARSE_OBSERVED',{'tool_call_id':tool_id,'regions':[{'region_id':r.id,'page':r.page,'parser_version':r.parser_version,'modality':r.kind} for r in result.regions],
                    'interpretation':'previously built parse observed; not re-parsed during the query'})]

    def final_events(self,answer):
        if self.strict_financial:
            if not isinstance(answer,FinancialAnswer):raise ValueError('ANSWER_POLICY_SCHEMA_MISMATCH')
            return self.financial_events(answer)
        validate_answer(answer,self.opened)
        cited=[]
        for citation in answer.citations:
            cited.append({**source(self.opened[citation.region_id]),'quote':citation.quote})
        evidence={'sources':cited,'facts':[f.model_dump() for f in answer.facts],'calculations':self.calculations,
            'unresolved':answer.unresolved,'validation':'citations match opened source text; semantic support evaluated separately',
            'fact_publication':'NOT_APPLICABLE_M1','configuration_version':self.context.config_version}
        text=answer.answer
        if self.m2_config.get('tools')=='m2-data-tools-v1':
            from .m2 import reusable_text,flattened_sources
            evidence.update(reused_facts=self.reused,structured_coverage=self.coverage,
                raw_observation_region_ids=list(self.opened),fact_publication='QUERY_CLAIMS_NOT_PUBLISHED',
                fallback_question=self.fallback_question)
            evidence['sources']=flattened_sources(self.reused)+cited
            if self.reused:text=reusable_text(self.reused)+'\n\n'+text
        return [event('FACT_RESOLVED',{'status':'OBSERVED_QUERY_CLAIMS' if answer.facts else 'NOT_APPLICABLE','items':evidence['facts'],
                    'validation':'quote and numeral presence only; no formal fact publication'}),
            event('DERIVATION_RECORDED',{'status':'RECORDED' if self.calculations else 'NOT_APPLICABLE','results':self.calculations}),
            event('ANSWER_SUPPORTED',{'source_region_ids':[c.region_id for c in answer.citations],
                    'unresolved':answer.unresolved,'semantic_support':'requires independent verification'}),
            event('ANSWER',{'text':text,'evidence_summary':evidence,'provider':self.settings.provider,
                           'tool_calls':self.tool_count,'model_calls':self.model_count}),event('DONE',{'state':'COMPLETED'})]

    async def run(self):
        model=None
        try:
            self.check()
            if self.m2_config.get('revalidate_batch_id') and self.control:
                from .m2 import revalidate
                async for item in revalidate(self,self.m2_config['revalidate_batch_id']):yield item
                return
            if self.m2_config.get('tools')=='m2-data-tools-v1':
                from .m2 import plan,full_events
                try:
                    async for item in plan(self):yield item
                except Exception as exc:
                    if isinstance(exc,ValueError) and str(exc).startswith('M2_'):raise
                    # One fallback only; permission/cancellation errors must still
                    # be enforced again by the source tools, never bypassed.
                    self.coverage={'status':'UNKNOWN','reason':'STRUCTURED_LOOKUP_FAILED'}
                    self.reused=[];self.fallback_question=self.request.question
                    yield event('STRUCTURE_CHECKED',self.coverage)
                if self.coverage.get('status')=='FULL' and self.reused:
                    for item in full_events(self):yield item
                    return
            if self.strict_financial and (not self.requirements or len(self.requirements)>6 or any(r.get('kind')=='set' for r in self.requirements)):
                for item in self.financial_events(FinancialAnswer(action='answer')):yield item
                return
            result,observed=await self.search(self.fallback_question);yield observed
            if not result.regions:
                empty=(FinancialAnswer(action='answer') if self.strict_financial else Answer(action='answer',answer='证据不足：在所选文档范围内未检索到可用原文。',unresolved=['没有匹配的授权原文区域']))
                for item in self.final_events(empty):yield item
                return
            # Bootstrap acquisition is explicit; subsequent steps remain model-directed.
            for item in await self.open([r.id for r in result.regions[:1 if self.reused else 2]]):yield item
            model=Model(self.settings,self.tools,self.context,self.metadata)
            model.contract=self.contract
            if self.m2_config.get('m3_enabled'):
                model.m3_call={'subexperiment':self.m2_config.get('subexperiment','quality')}
                if hasattr(self.request,'runtime_snapshot') and self.request.runtime_snapshot.observed_at:
                    from google.protobuf.json_format import MessageToDict
                    model.m3_call['snapshot_json']=json.dumps(MessageToDict(self.request.runtime_snapshot,
                        preserving_proto_field_name=True),ensure_ascii=False)
            system=(ROOT/'ai-runtime/prompts/m1-v1/system.txt').read_text(encoding='utf-8')
            if self.m2_config.get('tools')=='m2-data-tools-v1':
                system+='\n'+(ROOT/'ai-runtime/prompts/m2-v1/answer-contract.txt').read_text(encoding='utf-8')
                model.answer_prompt_version='m2-source-agent-v1'
            if self.strict_financial:
                system=(ROOT/'ai-runtime/prompts/release-v1/financial-answer.txt').read_text(encoding='utf-8')
                model.answer_prompt_version='financial-supported-v1'
                model.answer_schema_version='financial-answer-v1'
            if self.m2_config.get('m3_mode')=='m3' and self.registry is not None and self.jobs is not None:
                from .m3 import Coordinator
                self.m3_coordinator=Coordinator(self,self.registry,self.jobs)
                prepared=await self.m3_coordinator.prepare([source(r) for r in self.opened.values()])
                model.on_dispatch=self.m3_coordinator.observed_answer_dispatch
                yield event('BACKGROUND_JOB',prepared)
                model.answer_prompt_version='financial-shared-prefix-v1' if self.strict_financial else 'm3-shared-prefix-v1'
                if self.m3_coordinator.durable:
                    model.m3_call['prefix_manifest_id']=self.m3_coordinator.durable.get('prefix_manifest_id','')
            for step in range(self.contract.max_steps):
                self.check()
                if self.model_count>=self.contract.max_model_calls:raise ValueError('MODEL_CALL_LIMIT')
                context={'question':self.fallback_question,'sources':[source(r) for r in self.opened.values()],
                    'tool_history':self.history,'calculations':self.calculations,
                    'limits':{'remaining_model_calls':self.contract.max_model_calls-self.model_count,
                        'remaining_tool_calls':self.contract.max_tool_calls-self.tool_count}}
                if self.strict_financial:context['answer_requirements']=self.answer_requirements
                payload={'model':'deepseek-flash','max_tokens':contract_output_tokens(self.contract),'temperature':0,'thinking':{'type':'disabled'},
                    'response_format':{'type':'json_object'},'messages':[{'role':'system','content':system},
                        {'role':'user','content':json.dumps(context,ensure_ascii=False)}]}
                if self.m3_coordinator:
                    # Dynamic history/limits stay after the frozen source prefix.
                    frozen_ids={s['region_id'] for s in json.loads(self.m3_coordinator.snapshot.documents_json)}
                    suffix={**context,'sources':[s for s in context['sources'] if s['region_id'] not in frozen_ids]}
                    payload=self.m3_coordinator.answer_payload(suffix)
                first=next(iter(self.opened.values()))
                mock={'action':'answer','answer':'MOCK：以下为检索到的原文片段，用于验证链路，不代表真实模型回答。\n'+first.text[:180],
                    'citations':[{'region_id':first.id,'quote':first.text[:180]}],'facts':[],'unresolved':[]}
                if self.strict_financial:mock=self.financial_mock()
                self.model_count+=1;yield event('STATUS',{'state':'WAITING_LLM','model_call':self.model_count})
                content,record=await model.ask(payload,mock)
                if self.m3_coordinator:self.m3_coordinator.observed_answer(record)
                self.check()
                try:action=(FINANCIAL_ACTION if self.strict_financial else ACTION).validate_json(content,strict=True)
                except ValidationError as exc:raise ValueError('MODEL_ACTION_INVALID') from exc
                if isinstance(action,(Answer,FinancialAnswer)):
                    if not self.strict_financial:validate_answer(action,self.opened)
                    if self.m2_config.get('build_facts') and self.control and not self.m3_coordinator:
                        from .m2 import build
                        async for item in build(self,model):yield item
                    for item in self.final_events(action):yield item
                    return
                if isinstance(action,Search):
                    result,observed=await self.search(action.query,action.year,action.page);yield observed
                    if result.regions:
                        for item in await self.open([r.id for r in result.regions[:2]]):yield item
                elif isinstance(action,Open):
                    for item in await self.open(action.region_ids):yield item
                elif isinstance(action,Calculate):
                    self.tool_admit();derived=calculate(action,self.opened);self.calculations.append(derived)
                    yield event('TOOL',{'tool_call_id':str(uuid4()),'tool':'Calculate','result':derived})
            raise ValueError('STEP_LIMIT')
        except ValueError as exc:
            reason=str(exc);reason=reason if reason.replace('_','').isalnum() and len(reason)<100 else 'VALIDATION_FAILED'
            yield event('ERROR',{'code':'QUERY_FAILED','reason_code':reason})
        finally:
            if self.m3_coordinator:self.m3_coordinator.foreground_failed()
            if model:await model.close()
