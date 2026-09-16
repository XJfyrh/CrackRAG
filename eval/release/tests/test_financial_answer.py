import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.actions import FINANCIAL_ACTION
from crackrag_m1.agent import Agent
from crackrag_m1.config import ROOT, VERSION
from crackrag_m1.m3 import shared_system

REQ={'entity_id':'sample:holdings','concept_id':'fin:revenue','period':'FY2024','unit':'CNY','scope':'consolidated','kind':'scalar'}
COST={**REQ,'concept_id':'fin:cost_of_revenue'}
TEXT='Entity: Sample Holdings\nScope: consolidated\nUnit: CNY\nMetric | FY2024 | FY2023\nRevenue | 100.00 | 90.00\nCost of revenue | 60.00 | 55.00'

def reply(value):return pb.JsonReply(payload_json=json.dumps(value))

class Tools:
    def __init__(self,mode):
        self.mode=mode;self.searches=0
        self.region=pb.Region(id=str(uuid4()),document_version_id=str(uuid4()),document_id=str(uuid4()),
            text=TEXT,text_sha256=sha256(TEXT.encode()).hexdigest(),context_json='{}',page=1)
    async def ResolveConcepts(self,*args,**kw):
        reqs=[] if self.mode=='unsupported' else [REQ,COST] if self.mode=='partial' else [REQ]
        return reply({'requirements':reqs,'reasons':[],
            'answer_requirements':[{'requirement_key':('a' if r==REQ else 'b')*64,'requirement':r} for r in reqs]})
    def coverage(self):
        return {'status':'FULL' if self.mode=='full' else 'PARTIAL' if self.mode=='partial' else 'MISSING',
            'matched':[{'requirement':REQ,'fact_ids':['known-fact']}] if self.mode in ('full','partial') else [],
            'missing':[{'requirement':COST if self.mode=='partial' else REQ,'reason':'missing'}] if self.mode!='full' else [],'truncated':False}
    async def GetCoverage(self,*args,**kw):return reply(self.coverage())
    async def ReadFacts(self,*args,**kw):return reply({**self.coverage(),'facts':[{'fact_id':'known-fact',**REQ,'value':'100','origin':'REPORTED','sources':[]}]})
    async def SearchDocuments(self,*args,**kw):self.searches+=1;return pb.SearchReply(regions=[] if self.mode=='empty' else [self.region])
    async def OpenDocument(self,*args,**kw):return pb.OpenReply(regions=[self.region])

class Encoder:
    version='fixture-v1'
    async def encode_async(self,texts):return [[1]+[0]*1023],{}

def make_agent(mode='cold'):
    config={'tools':'m2-data-tools-v1','answer_policy':'financial-supported-v1',
        'm2_config_digest':sha256((ROOT/'api/internal/app/m2_catalog.json').read_bytes()).hexdigest()}
    contract=pb.ExecutionContract(deadline_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),
        execution_policy='COLD_ALLOWED',max_steps=3,max_tool_calls=8,max_model_calls=3,max_output_tokens=512,
        max_context_chars=12000,configuration_json=json.dumps(config))
    request=pb.QueryRequest(context=pb.RequestContext(config_version=VERSION),question='Sample Holdings FY2024 revenue',contract=contract)
    return Agent(request,SimpleNamespace(provider='mock'),Encoder(),Tools(mode),())

class FinancialAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def run_agent(self,agent,action=None):
        calls=[]
        class Model:
            def __init__(self,*args):pass
            async def ask(self,payload,mock):calls.append(payload);return json.dumps(action or mock),{}
            async def close(self):pass
        with patch('crackrag_m1.agent.Model',Model),patch('httpx.AsyncClient',side_effect=AssertionError('network forbidden')):
            events=[(e.type,json.loads(e.payload_json)) async for e in agent.run()]
        return events,calls

    async def test_mock_uses_observed_cells_and_has_no_unvalidated_prose_or_events(self):
        agent=make_agent();events,calls=await self.run_agent(agent)
        answer=next(value for kind,value in events if kind=='ANSWER')
        self.assertEqual(answer['claims'][0]['value'],'100.00')
        self.assertEqual(answer['claims'][0]['requirement_key'],'a'*64)
        self.assertNotIn('text',answer)
        self.assertFalse({'FACT_RESOLVED','ANSWER_SUPPORTED','DERIVATION_RECORDED'} & {k for k,_ in events})
        sent=json.loads(calls[0]['messages'][-1]['content'])
        self.assertEqual(sent['answer_requirements'][0]['requirement'],REQ)

    async def test_full_reuse_is_zero_model(self):
        agent=make_agent('full');events,calls=await self.run_agent(agent)
        answer=next(value for kind,value in events if kind=='ANSWER')
        self.assertEqual(answer['reused_fact_ids'],['known-fact']);self.assertEqual(answer['claims'],[])
        self.assertEqual(calls,[]);self.assertEqual(agent.tools.searches,0)

    async def test_partial_keeps_fact_ids_and_separate_raw_claims(self):
        agent=make_agent('partial');events,calls=await self.run_agent(agent)
        answer=next(value for kind,value in events if kind=='ANSWER')
        self.assertEqual(answer['reused_fact_ids'],['known-fact']);self.assertTrue(answer['claims'])
        self.assertNotIn('text',answer)

    async def test_unsupported_or_empty_produces_no_model_assertion(self):
        for mode in ('unsupported','empty'):
            events,calls=await self.run_agent(make_agent(mode));answer=next(v for k,v in events if k=='ANSWER')
            self.assertEqual(calls,[]);self.assertEqual(answer['claims'],[])

    async def test_free_prose_cannot_bypass_with_empty_claims(self):
        events,_=await self.run_agent(make_agent(),{'action':'answer','answer':'2024 income 999','facts':[],'claims':[]})
        self.assertEqual(events[-1][0],'ERROR');self.assertEqual(events[-1][1]['reason_code'],'MODEL_ACTION_INVALID')
        self.assertFalse(any(kind=='ANSWER' for kind,_ in events))

    def test_calculation_and_model_validation_fields_are_not_supported(self):
        for action in ({'action':'calculate','operation':'ratio','operands':[]},
                       {'action':'answer','claims':[],'validation':'SUPPORTED'},
                       {'action':'answer','claims':[],'unresolved':['but income is 999']}):
            with self.assertRaises(ValidationError):FINANCIAL_ACTION.validate_python(action,strict=True)

    def test_shared_prefix_selects_strict_answer_and_keeps_cracking(self):
        strict=shared_system('financial-supported-v1');legacy=shared_system()
        self.assertIn('requirement_key',strict);self.assertIn('CRACKING_BRANCH_RULES',strict)
        self.assertNotEqual(strict,legacy);self.assertNotIn('You may leave facts empty',strict)

if __name__=='__main__':unittest.main()
