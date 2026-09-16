import asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'ai-runtime/src'))
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.agent import Agent
from crackrag_m1.config import Settings,VERSION
from crackrag_m1.embedding import DenseEncoder

class Tools:
    def __init__(self,empty=False):
        self.id=str(uuid4());self.empty=empty;self.searches=0;self.opens=0
        self.region=pb.Region(id=self.id,document_version_id=str(uuid4()),document_id=str(uuid4()),
            text='2024年收入100.00元，成本40.00元。',context_json='{}',page=1,bbox=[1,1,100,100],page_width=595,page_height=842)
    async def SearchDocuments(self,r,**kwargs):self.searches+=1;return pb.SearchReply(regions=[] if self.empty else [self.region])
    async def OpenDocument(self,r,**kwargs):self.opens+=1;return pb.OpenReply(regions=[self.region])

class BoundedAgentTest(unittest.IsolatedAsyncioTestCase):
    def setup_agent(self,tools,**limits):
        settings=replace(Settings.load(),provider='mock',embedding_mode='fixture',mock_scenario='happy')
        options={'version':'m1-execution-v1','deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat(),
            'execution_policy':'COLD_ALLOWED','max_steps':6,'max_tool_calls':8,'max_model_calls':3,'max_context_chars':12000,'max_output_tokens':512,'max_snapshot_age_ms':5000}
        options.update(limits)
        return Agent(pb.QueryRequest(context=pb.RequestContext(config_version=VERSION),question='2024年的收入与成本差是多少？',contract=pb.ExecutionContract(**options)),settings,DenseEncoder(settings),tools,())
    async def observe(self,agent,actions):
        iterator=iter(actions)
        class Scripted:
            def __init__(self,*args):pass
            async def ask(self,payload,mock):return json.dumps(next(iterator),ensure_ascii=False),{}
            async def close(self):pass
        with patch('crackrag_m1.agent.Model',Scripted),patch('httpx.AsyncClient',side_effect=AssertionError('network forbidden')):
            return [(e.type,json.loads(e.payload_json)) async for e in agent.run()]
    async def test_search_open_calculate_then_answer(self):
        tools=Tools();agent=self.setup_agent(tools)
        fact=lambda v:{'region_id':tools.id,'quote':tools.region.text,'value':v,'year':'2024','unit':'元','entity':'企业'}
        actions=[{'action':'search','query':'收入 成本','year':2024,'page':1},
            {'action':'calculate','operation':'subtract','operands':[fact('100'),fact('40')],'precision':2},
            {'action':'answer','answer':'差额60.00元。','citations':[{'region_id':tools.id,'quote':tools.region.text}]}]
        events=await self.observe(agent,actions);answer=next(v for k,v in events if k=='ANSWER')
        self.assertEqual(tools.searches,2);self.assertEqual(agent.model_count,3)
        self.assertEqual(answer['evidence_summary']['calculations'][0]['result'],'60.00')
        self.assertEqual(events[-1][0],'DONE')
    async def test_extreme_calculation_has_recorded_query_error(self):
        tools=Tools();tiny='0.'+'0'*90+'1'
        tools.region.text='1 '+tiny
        fact=lambda value:{'region_id':tools.id,'quote':tools.region.text,'value':value,
            'year':'2024','unit':'CNY','entity':'Example'}
        action={'action':'calculate','operation':'ratio','operands':[fact('1'),fact(tiny)]}
        events=await self.observe(self.setup_agent(tools),[action])
        self.assertEqual(events[-1][0],'ERROR')
        self.assertEqual(events[-1][1]['reason_code'],'CALCULATION_RESULT_LIMIT')
        self.assertFalse(any(kind=='ANSWER' for kind,_ in events))
    async def test_model_and_tool_caps_stop_loop(self):
        for limits,reason in [({'max_model_calls':1},'MODEL_CALL_LIMIT'),({'max_tool_calls':2},'TOOL_CALL_LIMIT')]:
            tools=Tools();agent=self.setup_agent(tools,**limits)
            events=await self.observe(agent,[{'action':'search','query':'收入'}]*3)
            self.assertEqual(events[-1][1]['reason_code'],reason)
    async def test_empty_search_abstains_without_model(self):
        tools=Tools(empty=True);events=await self.observe(self.setup_agent(tools),[])
        answer=next(v for k,v in events if k=='ANSWER')
        self.assertTrue(answer['evidence_summary']['unresolved']);self.assertEqual(answer['model_calls'],0)
    async def test_context_deadline_and_bad_citation(self):
        tools=Tools()
        events=await self.observe(self.setup_agent(tools,max_context_chars=1),[])
        self.assertEqual(events[-1][1]['reason_code'],'CONTEXT_LIMIT')
        events=await self.observe(self.setup_agent(tools,deadline_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()),[])
        self.assertEqual(events[-1][1]['reason_code'],'DEADLINE_EXCEEDED')
        events=await self.observe(self.setup_agent(tools),[{'action':'answer','answer':'伪造','citations':[{'region_id':tools.id,'quote':'不存在的999元'}]}])
        self.assertEqual(events[-1][1]['reason_code'],'CITATION_NOT_OBSERVED')
    async def test_invalid_action_has_recorded_error(self):
        tools=Tools();events=await self.observe(self.setup_agent(tools),[{'action':'sql','query':'DELETE FROM documents'}])
        self.assertEqual(events[-1][1]['reason_code'],'MODEL_ACTION_INVALID')
