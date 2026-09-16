import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ai-runtime/src'))
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.actions import ACTION,Answer,Fact,Calculate,validate_answer,calculate
from crackrag_m1.config import Settings
from crackrag_m1.embedding import DenseEncoder,terms
from crackrag_m1.model import Model
from crackrag_m1.m0_snapshot.config import Pricing
from crackrag_m1.m0_snapshot.providers import ProviderResult
from crackrag_m1.parser import parse_pdf

class FakeTools:
    def __init__(self,snapshot_age_ms=0):
        self.calls=[];self.reservations=[];self.snapshot_age_ms=snapshot_age_ms
    async def ReserveCall(self,request,**kwargs):
        self.reservations.append(request)
        return pb.ReserveReply(snapshot=pb.RuntimeSnapshot(snapshot_id='test',observed_at=(datetime.now(timezone.utc)-timedelta(milliseconds=self.snapshot_age_ms)).isoformat()),reserved_upper_cny='0.05')
    async def SettleCall(self,request,**kwargs):
        self.calls.append(json.loads(request.call_json));return pb.Empty()

class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.opened={'r1':SimpleNamespace(text='营业收入 100.00 元，营业成本 40.00 元。')}
    def fact(self,value):return Fact(region_id='r1',quote=self.opened['r1'].text,value=value,year='2024',unit='元',entity='企业')
    def test_unobserved_citation_rejected(self):
        answer=Answer(action='answer',answer='答案',citations=[{'region_id':'other','quote':'100.00'}])
        with self.assertRaisesRegex(ValueError,'CITATION_NOT_OBSERVED'):validate_answer(answer,self.opened)
    def test_fabricated_quote_rejected(self):
        answer=Answer(action='answer',answer='答案',citations=[{'region_id':'r1','quote':'收入 999 元'}])
        with self.assertRaisesRegex(ValueError,'CITATION_NOT_OBSERVED'):validate_answer(answer,self.opened)
    def test_fact_value_must_occur(self):
        answer=Answer(action='answer',answer='答案',facts=[self.fact('999')],unresolved=['核对中'])
        with self.assertRaisesRegex(ValueError,'FACT_VALUE_NOT_IN_QUOTE'):validate_answer(answer,self.opened)
    def test_abstention_requires_no_fake_citation(self):
        validate_answer(Answer(action='answer',answer='证据不足',unresolved=['缺少相关年份']),{})
        with self.assertRaises(ValueError):validate_answer(Answer(action='answer',answer='肯定正确'),{})
    def test_arbitrary_tools_and_fields_rejected(self):
        for raw in ('{"action":"sql","query":"SELECT 1"}','{"action":"search","query":"收入","tenant_id":"other"}'):
            with self.assertRaises(ValueError):ACTION.validate_json(raw)
    def test_decimal_calculation_and_basis(self):
        result=calculate(Calculate(action='calculate',operation='subtract',operands=[self.fact('100.00'),self.fact('40.00')]),self.opened)
        self.assertEqual(result['result'],'60.00');self.assertEqual(result['unit'],'元')
        wrong=self.fact('40.00').model_copy(update={'unit':'万元'})
        with self.assertRaisesRegex(ValueError,'BASIS_MISMATCH'):calculate(Calculate(action='calculate',operation='subtract',operands=[self.fact('100.00'),wrong]),self.opened)
    def test_extreme_ratio_fails_with_bounded_error(self):
        tiny='0.'+'0'*90+'1'
        self.opened['r1'].text='1 '+tiny
        action=Calculate(action='calculate',operation='ratio',operands=[self.fact('1'),self.fact(tiny)])
        with self.assertRaisesRegex(ValueError,'CALCULATION_RESULT_LIMIT'):
            calculate(action,self.opened)
    def test_magnitude_limit_does_not_round_the_input(self):
        value='1000000000000000000000000000000.1'
        self.opened['r1'].text=value+' 1'
        action=Calculate(action='calculate',operation='add',operands=[self.fact(value),self.fact('1')])
        with self.assertRaisesRegex(ValueError,'CALCULATION_MAGNITUDE_LIMIT'):
            calculate(action,self.opened)
    def test_chinese_terms_and_fixture_label(self):
        self.assertIn('营业',terms('营业收入 2024'))
        encoder=DenseEncoder(replace(Settings.load(),embedding_mode='fixture'))
        with patch('httpx.Client',side_effect=AssertionError('network forbidden')),patch('httpx.AsyncClient',side_effect=AssertionError('network forbidden')):
            vectors,usage=encoder.encode(['营业收入'])
        self.assertEqual(len(vectors[0]),1024);self.assertTrue(usage['simulated']);self.assertIn('NOT-BGE',encoder.version)
    def test_invalid_pdf_and_hash_rejected(self):
        request=pb.ParseRequest(pdf=b'invalid',pdf_sha256='wrong',document_version_id='00000000-0000-0000-0000-000000000001')
        with self.assertRaisesRegex(ValueError,'PDF_HASH_OR_SIZE_INVALID'):parse_pdf(request,None)

class ParserTextLayerTest(unittest.TestCase):
    def parse_page(self,text,image_rect=None):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page()
            if text:page.insert_text((72,100),text)
            if image_rect is not None:
                with pymupdf.open() as scanned:
                    scanned_page=scanned.new_page(width=300,height=400)
                    scanned_page.insert_text((20,80),'Scanned revenue: 999.00 CNY')
                    page.insert_image(pymupdf.Rect(image_rect),pixmap=scanned_page.get_pixmap())
            data=document.tobytes()
        request=pb.ParseRequest(pdf=data,pdf_sha256=sha256(data).hexdigest(),document_version_id=str(uuid4()))
        settings=replace(Settings.load(),provider='mock',embedding_mode='fixture')
        with patch('httpx.Client',side_effect=AssertionError('network')),patch('httpx.AsyncClient',side_effect=AssertionError('network')):
            return parse_pdf(request,DenseEncoder(settings))
    def test_short_native_text_is_indexed_with_recorded_check(self):
        result=self.parse_page('Revenue: 100.00 CNY')
        self.assertIn('100.00',result['regions'][0]['text'])
        detection=json.loads(result['build_usage_json'])['parser']['text_layer_detection']
        self.assertTrue(detection['heuristic'])
        self.assertTrue(detection['pages'][0]['sparse_text'])
        self.assertEqual(detection['pages'][0]['image_bbox_coverage'],0)
    def test_short_native_text_with_small_logo_is_indexed(self):
        result=self.parse_page('Revenue: 100.00 CNY',(450,20,490,60))
        self.assertIn('100.00',result['regions'][0]['text'])
    def test_short_header_does_not_make_scanned_body_searchable(self):
        with self.assertRaisesRegex(ValueError,'OCR_REQUIRED_NOT_ENABLED'):
            self.parse_page('2024 Annual Report',(30,120,565,820))
    def test_blank_page_has_distinct_no_text_error(self):
        with self.assertRaisesRegex(ValueError,'NO_RETRIEVABLE_TEXT'):
            self.parse_page('')


class ParserGeometryTest(unittest.TestCase):
    def parse_document(self,document):
        data=document.tobytes()
        request=pb.ParseRequest(pdf=data,pdf_sha256=sha256(data).hexdigest(),document_version_id=str(uuid4()))
        settings=replace(Settings.load(),provider='mock',embedding_mode='fixture')
        return parse_pdf(request,DenseEncoder(settings))
    def assert_bbox(self,actual,expected,width,height):
        self.assertEqual(len(actual),4)
        for got,want in zip(actual,expected,strict=True):self.assertAlmostEqual(got,want,places=3)
        x0,y0,x1,y1=actual
        self.assertTrue(0<=x0<x1<=width and 0<=y0<y1<=height)
    def test_rotated_body_and_display_header_share_page_coordinates(self):
        import pymupdf
        for rotation in (0,90,180,270):
            with self.subTest(rotation=rotation),pymupdf.open() as document:
                page=document.new_page(width=595,height=842)
                page.set_rotation(rotation)
                page.insert_text((72,700),'Revenue: 100 CNY. Costs: 40 CNY. The company reported these figures for financial year 2024.',fontsize=9)
                body_bbox=list(pymupdf.Rect(page.get_text('blocks')[0][:4])*page.rotation_matrix)
                header_position=pymupdf.Point(72,30)*page.derotation_matrix
                page.insert_text(header_position,'HEADER_MARKER',fontsize=9,rotate=rotation)
                width,height=page.rect.width,page.rect.height
                result=self.parse_document(document)
                body=next(region for region in result['regions'] if 'Revenue' in region['text'])
                self.assertEqual((body['page_width'],body['page_height']),(width,height))
                self.assert_bbox(body['bbox'],body_bbox,width,height)
                self.assertFalse(any('HEADER_MARKER' in region['text'] for region in result['regions']))
                context=json.loads(body['context_json'])
                self.assertIn('displayed page',context['coordinate_system'])
                self.assertEqual(context['pdf_page_rotation'],rotation)
                excluded=json.loads(result['build_usage_json'])['excluded_margin_blocks']
                self.assertTrue(excluded)
                self.assertTrue(all(box['bbox'][3]<=64 or box['bbox'][1]>=height-35 for box in excluded))
    def test_rotated_tables_are_not_double_rotated_or_duplicated(self):
        import pymupdf
        for rotation in (0,90,180,270):
            with self.subTest(rotation=rotation),pymupdf.open() as document:
                page=document.new_page(width=595,height=842)
                for x in (100,200,300):page.draw_line((x,150),(x,250))
                for y in (150,200,250):page.draw_line((100,y),(300,y))
                for x,y,text in ((110,175,'Revenue'),(210,175,'100.00'),(110,225,'Costs'),(210,225,'40.00')):
                    page.insert_text((x,y),text)
                page.set_rotation(rotation)
                expected=list(pymupdf.Rect(100,150,300,250)*page.rotation_matrix)
                width,height=page.rect.width,page.rect.height
                result=self.parse_document(document)
                tables=[region for region in result['regions'] if region['kind']=='table']
                self.assertEqual(len(tables),1)
                self.assert_bbox(tables[0]['bbox'],expected,width,height)
                self.assertIn('100.00',tables[0]['text'])
                self.assertFalse(any(region['kind']=='text' and '100.00' in region['text'] for region in result['regions']))


class ModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_mock_never_reads_key_or_constructs_http_client(self):
        settings=replace(Settings.load(),provider='mock',mock_scenario='happy');tools=FakeTools()
        with patch('crackrag_m1.model.read_api_key',side_effect=AssertionError('key read')),patch('httpx.AsyncClient',side_effect=AssertionError('network')):
            model=Model(settings,tools,pb.RequestContext(run_id='test'),())
            content,record=await model.ask({'model':'deepseek-flash','max_tokens':512,'messages':[{'role':'user','content':'test'}]}, {'action':'answer','answer':'mock'})
            await model.close()
        self.assertEqual(len(tools.calls),1);self.assertTrue(record['simulated']);self.assertEqual(record['automatic_retries'],0)
        self.assertIn('completion identifier fallback',record['request_id_source'])
        self.assertEqual(record['cost']['status'],'simulated')
    async def test_missing_usage_is_recorded_then_stops(self):
        tools=FakeTools();model=Model(replace(Settings.load(),provider='mock',mock_scenario='missing_usage'),tools,pb.RequestContext(),())
        with self.assertRaisesRegex(ValueError,'USAGE_MISSING'):await model.ask({'model':'deepseek-flash','max_tokens':512,'messages':[{}]}, {'action':'answer','answer':'test'})
        self.assertIsNone(tools.calls[0]['raw_usage']);self.assertEqual(len(tools.reservations),1)
    async def test_cancel_retains_attempt_and_unknown_response(self):
        tools=FakeTools();model=Model(replace(Settings.load(),provider='mock',mock_scenario='slow'),tools,pb.RequestContext(),())
        task=asyncio.create_task(model.ask({'model':'deepseek-flash','max_tokens':512,'messages':[{}]},{'action':'answer','answer':'test'}))
        await asyncio.sleep(.02);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(tools.calls[0]['transport_failure'],'CANCELLED_IN_FLIGHT');self.assertIsNone(tools.calls[0]['raw_usage'])

    def offline_live_model(self,tools,provider):
        model=Model(replace(Settings.load(),provider='mock'),tools,pb.RequestContext(),())
        model.settings=replace(model.settings,provider='deepseek')
        model.price=Pricing(currency='CNY',version='synthetic',source='offline test',
            input_miss_per_million='1',input_hit_per_million='0.02',output_per_million='4',model='deepseek-flash')
        model.provider=provider
        return model
    async def test_stale_snapshot_settles_zero_without_dispatch(self):
        class NeverDispatch:
            async def complete(self,*args):raise AssertionError('must not dispatch')
            async def close(self):pass
        tools=FakeTools(snapshot_age_ms=6000)
        with patch('crackrag_m1.model.read_api_key',side_effect=AssertionError('key read')),patch('httpx.AsyncClient',side_effect=AssertionError('network')):
            model=self.offline_live_model(tools,NeverDispatch())
            with self.assertRaisesRegex(ValueError,'SNAPSHOT_STALE_NOT_DISPATCHED'):
                await model.ask({'model':'deepseek-flash','max_tokens':512,'messages':[{}]}, {})
            await model.close()
        self.assertEqual(len(tools.calls),1)
        record=tools.calls[0]
        self.assertFalse(record['http_dispatched'])
        self.assertEqual(record['cost']['status'],'not_dispatched')
        self.assertEqual(record['cost']['amount'],'0')
        self.assertEqual(record['cost']['reason'],'SNAPSHOT_STALE_NOT_DISPATCHED')
        self.assertIsNone(record['raw_usage'])
    async def test_dispatched_call_without_usage_remains_unknown(self):
        class MissingUsage:
            async def complete(self,*args):return ProviderResult(body={},status_code=200)
            async def close(self):pass
        tools=FakeTools()
        with patch('crackrag_m1.model.read_api_key',side_effect=AssertionError('key read')),patch('httpx.AsyncClient',side_effect=AssertionError('network')):
            model=self.offline_live_model(tools,MissingUsage())
            with self.assertRaisesRegex(ValueError,'COST_UNKNOWN'):
                await model.ask({'model':'deepseek-flash','max_tokens':512,'messages':[{}]}, {})
            await model.close()
        self.assertTrue(tools.calls[0]['http_dispatched'])
        self.assertEqual(tools.calls[0]['cost']['status'],'unknown')
        self.assertIsNone(tools.calls[0]['cost']['amount'])


if __name__=='__main__':unittest.main()
