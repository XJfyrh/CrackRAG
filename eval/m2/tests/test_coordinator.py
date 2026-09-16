import json
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4

from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.agent import Agent
from crackrag_m1.config import ROOT,VERSION
from crackrag_m1.m2 import ExtractionShape,mock_candidates,probe,verify_assets
from crackrag_m1.model import Model

REQ={'entity_id':'sample:holdings','concept_id':'fin:revenue','period':'FY2024','unit':'CNY','scope':'consolidated','kind':'scalar'}
COST={**REQ,'concept_id':'fin:cost_of_revenue'}

def reply(data):return pb.JsonReply(payload_json=json.dumps(data))
def source():
    text='Metric | FY2024 | FY2023\nCost of revenue | 60.00 | 55.00'
    return pb.Region(id=str(uuid4()),document_version_id=str(uuid4()),document_id=str(uuid4()),page=2,
        bbox=[1,70,500,200],page_width=595,page_height=842,text=text,text_sha256=sha256(text.encode()).hexdigest(),
        context_json=json.dumps({'page_context':'Entity: Sample Holdings\nScope: consolidated\nUnit: CNY'}),parser_version='test')

class FakeTools:
    def __init__(self,mode):self.mode=mode;self.resolve_count=0;self.search_count=0;self.open_ids=[];self.region=source()
    async def ResolveConcepts(self,r,**kw):
        self.resolve_count+=1
        if self.mode=='error':raise ValueError('STRUCTURE_UNAVAILABLE')
        return reply({'requirements':[REQ,COST] if self.mode=='partial' else [REQ],'reasons':[]})
    def coverage(self):return {'status':'PARTIAL' if self.mode=='partial' else 'FULL',
        'matched':[{'requirement':REQ,'fact_ids':['fact-1']}],'missing':[{'requirement':COST,'reason':'missing'}] if self.mode=='partial' else [],'conflicts':[],'truncated':False}
    async def GetCoverage(self,r,**kw):return reply(self.coverage())
    async def ReadFacts(self,r,**kw):return reply({**self.coverage(),'facts':[{'fact_id':'fact-1','report_id':'report-1',**REQ,'value':'100','raw_value':'100.00','origin':'REPORTED','document_version_id':'v-1','sources':[],'input_fact_ids':[]}]})
    async def SearchDocuments(self,r,**kw):self.search_count+=1;self.search_query=r.query;return pb.SearchReply(regions=[self.region,source()],truncated=True,method='test')
    async def OpenDocument(self,r,**kw):self.open_ids+=list(r.region_ids);return pb.OpenReply(regions=[self.region])

class FakeEncoder:
    version='fixture-v1'
    async def encode_async(self,texts):return [[1]+[0]*1023],{'simulated':True}

class FakeModel:
    calls=[]
    def __init__(self,*args):pass
    async def close(self):pass
    async def ask(self,payload,mock,**kwargs):
        self.calls.append((payload,kwargs));return json.dumps(mock),{}

def agent(mode):
    tools=FakeTools(mode)
    config={'tools':'m2-data-tools-v1','m2_config_digest':sha256((ROOT/'api/internal/app/m2_catalog.json').read_bytes()).hexdigest()}
    contract=pb.ExecutionContract(version='m1-execution-v1',deadline_at=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat(),max_output_tokens=512,
        execution_policy='COLD_ALLOWED',max_tool_calls=8,max_model_calls=3,max_steps=6,max_context_chars=12000,configuration_json=json.dumps(config))
    request=pb.QueryRequest(context=pb.RequestContext(service_id='go-api',run_id=str(uuid4()),tenant_id='t',config_version=VERSION),
        question='Sample Holdings FY2024 Revenue and Cost of revenue',contract=contract,provider='mock')
    return Agent(request,SimpleNamespace(provider='mock'),FakeEncoder(),tools,()),tools

class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_mode_invalid_prompt_stops_before_reservation(self):
        class Denied:
            async def ReserveCall(self,*args,**kwargs):raise AssertionError('must not reserve or dispatch')
        model=Model(SimpleNamespace(provider='mock'),Denied(),pb.RequestContext(),())
        with self.assertRaisesRegex(ValueError,'JSON_MODE_INSTRUCTION_REQUIRED'):
            await model.ask({'response_format':{'type':'json_object'},'messages':[{'role':'system','content':'Only return an object.'}]},{},stage='probe')
        for filename in ('probe.txt','extraction.txt'):
            self.assertIn('json',(ROOT/'ai-runtime/prompts/m2-v1'/filename).read_text(encoding='utf-8').lower())
    async def test_full_reuses_without_model_or_raw_open(self):
        a,t=agent('full')
        with patch('crackrag_m1.agent.Model',side_effect=AssertionError('model unnecessary')):
            events=[e async for e in a.run()]
        answer=json.loads(next(e.payload_json for e in events if e.type=='ANSWER'))
        self.assertEqual(answer['model_calls'],0);self.assertEqual(t.search_count,0);self.assertEqual(t.open_ids,[])
        self.assertEqual(answer['evidence_summary']['reused_facts'][0]['report_id'],'report-1')

    async def test_partial_answers_only_missing_raw_requirement(self):
        a,t=agent('partial');FakeModel.calls=[]
        with patch('crackrag_m1.agent.Model',FakeModel):events=[e async for e in a.run()]
        answer=json.loads(next(e.payload_json for e in events if e.type=='ANSWER'))
        self.assertEqual(t.search_query,'Sample Holdings FY2024 Cost of revenue');self.assertEqual(len(t.open_ids),1)
        sent=json.loads(FakeModel.calls[0][0]['messages'][1]['content']);self.assertEqual(sent['question'],t.search_query)
        self.assertTrue(any('json' in message['content'].lower() for message in FakeModel.calls[0][0]['messages']))
        self.assertEqual(answer['evidence_summary']['reused_facts'][0]['value'],'100')

    async def test_failed_structure_has_single_fallback(self):
        a,t=agent('error')
        with patch('crackrag_m1.agent.Model',FakeModel):events=[e async for e in a.run()]
        self.assertEqual(t.resolve_count,1);self.assertEqual(t.search_count,1)
        answer=json.loads(next(e.payload_json for e in events if e.type=='ANSWER'));self.assertEqual(answer['evidence_summary']['structured_coverage']['status'],'UNKNOWN')

    async def test_probe_has_independent_history_and_read_only_observation(self):
        a,_=agent('partial');r=source();a.settings=SimpleNamespace(provider='mock')
        class Control:
            async def BeginProbe(self,*args,**kwargs):return reply({'region_ids':[r.id],'probe_token':'private-capability','doubts':[{'candidate':{'property':'Net profit'},'reasons':['BUSINESS_SCOPE_UNPROVEN']}]})
        class ReadOnly:
            async def OpenDocument(self,request,**kwargs):
                self.request=request;return pb.OpenReply(regions=[r])
        a.control=Control();a.probe_tools=ReadOnly();FakeModel.calls=[]
        with patch('crackrag_m1.m2.Model',FakeModel):reason=await probe(a,'batch-1',{})
        self.assertEqual(reason,'PROBE_OBSERVED');self.assertEqual(len(FakeModel.calls),2)
        self.assertEqual(a.probe_tools.request.context.service_id,'python-probe')
        for sent,kwargs in FakeModel.calls:
            self.assertEqual(kwargs['stage'],'probe')
            self.assertNotIn(a.request.question,json.dumps(sent));self.assertNotIn('private-capability',json.dumps(sent))
            self.assertNotIn('gold',json.loads(sent['messages'][1]['content']))

    async def test_unavailable_probe_stays_unresolved(self):
        a,_=agent('partial')
        class Unavailable:
            async def BeginProbe(self,*args,**kwargs):raise ValueError('BUDGET_EXCEEDED')
        a.control=Unavailable()
        with patch('crackrag_m1.m2.Model',side_effect=AssertionError('must not call model')):self.assertEqual(await probe(a,'batch',{}),'PROBE_UNAVAILABLE_OR_UNRESOLVED')

class ShapeTests(unittest.TestCase):
    def test_mock_reads_actual_source_cells(self):
        r=source();result=mock_candidates([r],[COST]);self.assertEqual(result['candidates'][0]['raw_value'],'60.00')
        self.assertEqual(result['candidates'][0]['region_id'],r.id)
        self.assertEqual(mock_candidates([r],[REQ]),{'candidates':[]})
    def test_decimal_strings_and_unknown_fields(self):
        data=mock_candidates([source()],[COST]);ExtractionShape.model_validate(data,strict=True)
        data['candidates'][0]['value']=60.0
        with self.assertRaises(ValueError):ExtractionShape.model_validate(data,strict=True)
        data['candidates'][0]['value']='60';data['candidates'][0]['pass']=True
        with self.assertRaises(ValueError):ExtractionShape.model_validate(data,strict=True)
    def test_asset_binding(self):
        digest=sha256((ROOT/'api/internal/app/m2_catalog.json').read_bytes()).hexdigest();verify_assets(digest)
        with self.assertRaises(ValueError):verify_assets('0'*64)

if __name__=='__main__':unittest.main()
