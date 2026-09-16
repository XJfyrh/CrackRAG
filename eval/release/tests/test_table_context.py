"""Synthetic PDF geometry checks; no issuer reports, gold or model calls."""
from hashlib import sha256
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'ai-runtime/src'))
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.parser import parse_pdf, bound_table_context, TABLE_CONTEXT_VERSION, VERSION


class GeometryEncoder:
    version='geometry-test-no-model'
    def chunks(self,text):
        return [(start,text[start:start+512]) for start in range(0,len(text),448)]
    def encode(self,texts):
        return [[1.0]+[0.0]*1023 for _ in texts],{'duration_ms':0,'paid_api_calls':0,'simulated':True}


def draw_table(page, top, profit, *, left=60, right=520, extra_rows=0):
    import pymupdf
    rows=[['Metric','FY2024','FY2023'],['Revenue','120','110'],['Cost','70','60']]
    rows.extend([[f'Expense {index}','1','2'] for index in range(extra_rows)])
    rows.append(['Net profit',str(profit),'25'])
    xs=[left,left+(right-left)*.54,left+(right-left)*.77,right]
    height=18
    for row in range(len(rows)+1):page.draw_line((left,top+row*height),(right,top+row*height),width=.5)
    for x in xs:page.draw_line((x,top),(x,top+len(rows)*height),width=.5)
    for index,row in enumerate(rows):
        for col,text in enumerate(row):page.insert_text((xs[col]+4,top+index*height+12),text,fontsize=8)
    return pymupdf.Rect(left,top,right,top+len(rows)*height)


class TableContextGeometryTests(unittest.TestCase):
    def parse(self,document,pages=None):
        raw=document.tobytes()
        request=pb.ParseRequest(pdf=raw,pdf_sha256=sha256(raw).hexdigest(),document_version_id=str(uuid4()),pages=pages or [])
        with patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            return parse_pdf(request,GeometryEncoder())

    def contexts(self,result,page=1):
        contexts={}
        for region in result['regions']:
            if region['page']!=page or region['kind']!='table':continue
            metadata=json.loads(region['context_json'])
            context=metadata['table_context']
            self.assertEqual(context['page'],page)
            self.assertEqual(context['version'],TABLE_CONTEXT_VERSION)
            self.assertEqual(region['parser_version'],VERSION)
            self.assertEqual(metadata['region_source'],f"table-{context['table_index']}")
            self.assertEqual(list(region['bbox']),context['table_bbox'])
            self.assertIn(region['text'],context['table_text'])
            self.assertEqual(context['table_text_sha256'],sha256(context['table_text'].encode()).hexdigest())
            for line in context['preamble_lines']:
                self.assertLessEqual(line['bbox'][3],context['table_bbox'][1]+.01)
                self.assertGreaterEqual(line['bbox'][1],context['preceding_table_bottom']-.01)
            contexts[context['table_index']]=context
        return contexts

    def test_two_tables_sharing_prefix_get_their_own_units(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page()
            page.insert_text((65,85),'Consolidated income statement',fontsize=10)
            page.insert_text((360,108),'Unit: CNY',fontsize=10)
            first=draw_table(page,125,30)
            page.insert_text((65,300),'Parent company income statement',fontsize=10)
            page.insert_text((360,324),'Unit: USD',fontsize=10)
            draw_table(page,340,80)
            contexts=self.contexts(self.parse(document))
        self.assertEqual(len(contexts),2)
        top='\n'.join(line['text'] for line in contexts[0]['preamble_lines'])
        bottom='\n'.join(line['text'] for line in contexts[1]['preamble_lines'])
        self.assertIn('Consolidated',top);self.assertIn('CNY',top)
        self.assertNotIn('Parent',top);self.assertNotIn('USD',top)
        self.assertIn('Parent',bottom);self.assertIn('USD',bottom)
        self.assertNotIn('Consolidated',bottom);self.assertNotIn('CNY',bottom)
        self.assertAlmostEqual(contexts[1]['preceding_table_bottom'],first.y1,places=3)
        self.assertNotEqual(contexts[0]['table_text_sha256'],contexts[1]['table_text_sha256'])

    def test_second_table_cannot_borrow_missing_unit(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page()
            page.insert_text((65,90),'Consolidated income statement; Unit: CNY',fontsize=10)
            draw_table(page,120,30)
            page.insert_text((65,300),'Parent company income statement',fontsize=10)
            draw_table(page,330,80)
            contexts=self.contexts(self.parse(document))
        second='\n'.join(line['text'] for line in contexts[1]['preamble_lines'])
        self.assertNotIn('CNY',second)
        self.assertNotIn('Consolidated',second)

    def test_preamble_is_current_page_only(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page();page.insert_text((65,90),'Consolidated income statement; Unit: CNY',fontsize=10)
            page=document.new_page();draw_table(page,100,80)
            contexts=self.contexts(self.parse(document),page=2)
        self.assertEqual(contexts[0]['preamble_lines'],[])

    def test_unit_fragments_on_same_visual_line_are_joined(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page()
            page.insert_text((350,95),'Unit:',fontsize=10)
            page.insert_text((420,95),'CNY',fontsize=10)
            draw_table(page,120,30)
            contexts=self.contexts(self.parse(document))
        self.assertEqual([line['text'] for line in contexts[0]['preamble_lines']],['Unit: CNY'])

    def test_side_by_side_tables_do_not_share_units(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page()
            page.insert_text((65,95),'Unit: CNY',fontsize=9)
            page.insert_text((330,95),'Unit: USD',fontsize=9)
            draw_table(page,120,30,left=60,right=265)
            draw_table(page,120,80,left=320,right=525)
            contexts=list(self.contexts(self.parse(document)).values())
        contexts.sort(key=lambda context:context['table_bbox'][0])
        self.assertEqual([line['text'] for line in contexts[0]['preamble_lines']],['Unit: CNY'])
        self.assertEqual([line['text'] for line in contexts[1]['preamble_lines']],['Unit: USD'])

    def test_chunks_keep_one_complete_table_hash_without_header_reconstruction(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page()
            page.insert_text((65,80),'Consolidated income statement; Unit: CNY',fontsize=9)
            draw_table(page,100,30,extra_rows=25)
            result=self.parse(document)
            self.contexts(result)
        regions=[region for region in result['regions'] if region['kind']=='table']
        self.assertGreater(len(regions),1)
        contexts=[json.loads(region['context_json'])['table_context'] for region in regions]
        self.assertEqual(len({context['table_text_sha256'] for context in contexts}),1)
        self.assertNotIn('Metric',regions[-1]['text'])
        self.assertIn('Metric',contexts[-1]['table_text'])

    def test_overlapping_tables_oversized_text_and_intersecting_lines(self):
        tables=[{'bbox':[50,100,500,200]}]
        lines=[{'text':'Outside Unit: USD','bbox':[510,60,590,70]},
               {'text':'Inside Unit: EUR','bbox':[60,101,180,112]},
               {'text':'Top Unit: CNY','bbox':[60,80,180,90]}]
        bound=bound_table_context(1,0,tables,'Metric | FY2024 | FY2023',lines,[595,842])
        self.assertEqual([line['text'] for line in bound['preamble_lines']],['Top Unit: CNY'])
        self.assertIsNone(bound_table_context(1,0,tables,'x'*24001,lines,[595,842]))
        self.assertIsNone(bound_table_context(1,0,tables+[{'bbox':[60,120,490,210]}],'table',lines,[595,842]))
        self.assertIsNone(bound_table_context(1,0,tables,'table',[{'text':'x'*2001,'bbox':[60,80,180,90]}],[595,842]))


if __name__=='__main__':unittest.main()
