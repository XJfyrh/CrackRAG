"""Adapt the frozen M0 parser to versioned, bounded regions for M1 ingestion."""
from hashlib import sha256
import json
import math
from pathlib import Path
import tempfile
import time
from uuid import UUID, uuid5

from .m0_snapshot.pdf_sample import parse_sample, PARSER_VERSION
from .embedding import terms

VERSION = PARSER_VERSION+':m1-region-chunks-v4'
SPARSE_TEXT_CHARS = 60
SCAN_IMAGE_COVERAGE = 0.5
COORDINATES = 'PDF points in the displayed page after rotation; origin top-left; x right, y down'
TABLE_CONTEXT_VERSION = 'same-page-table-context-v1'
GEOMETRY_EPSILON = .01


def page_text_lines(page):
    """Retain display-coordinate line geometry, without image data or OCR."""
    import pymupdf
    result=[]
    flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text('dict',sort=True,flags=flags)['blocks']:
        for line in block.get('lines',[]):
            text=' '.join(''.join(span['text'] for span in line['spans']).split())
            if text:
                result.append({'text':text,'bbox':list(pymupdf.Rect(line['bbox'])*page.rotation_matrix)})
    return result


def bound_table_context(page_number, table_index, tables, table_text, lines, page_size):
    """Bind a complete original table to its own same-page geometric preamble.

    Content resemblance is not an association proof. A previous horizontally
    overlapping table establishes a hard lower boundary; no earlier heading or
    unit can be borrowed. Overlapping tables and oversized evidence abstain.
    """
    if not table_text or len(table_text.encode('utf-8'))>24000:
        return None
    box=list(tables[table_index]['bbox'])
    width,height=page_size
    def valid_box(value):
        return (len(value)==4 and all(math.isfinite(v) for v in value)
                and 0<=value[0]<value[2]<=width+GEOMETRY_EPSILON
                and 0<=value[1]<value[3]<=height+GEOMETRY_EPSILON)
    if not valid_box(box):return None
    preceding=0.0
    for index,other in enumerate(tables):
        if index==table_index:continue
        prior=list(other['bbox'])
        if not valid_box(prior):return None
        if min(box[2],prior[2])-max(box[0],prior[0])<=GEOMETRY_EPSILON:continue
        if prior[3]<=box[1]+GEOMETRY_EPSILON:
            preceding=max(preceding,prior[3])
        elif prior[1]<box[3]-GEOMETRY_EPSILON:
            # Nested/overlapping detector output cannot prove which table owns
            # a heading, even when both tables happen to share numeric rows.
            return None
    if preceding>box[1]:return None
    candidates=[]
    for line in lines:
        current=line['bbox']
        if (current[3]>box[1]+GEOMETRY_EPSILON or current[1]<preceding-GEOMETRY_EPSILON
                or min(box[2],current[2])-max(box[0],current[0])<=GEOMETRY_EPSILON):
            continue
        if not valid_box(current):return None
        candidates.append({'text':line['text'],'bbox':list(current)})
    candidates.sort(key=lambda line:((line['bbox'][1]+line['bbox'][3])/2,line['bbox'][0]))
    groups=[]
    for line in candidates:
        if groups:
            first=groups[-1][0]['bbox']; current=line['bbox']
            overlap=min(first[3],current[3])-max(first[1],current[1])
            shortest=min(first[3]-first[1],current[3]-current[1])
            same_row=(overlap>=shortest*.7 and abs((first[1]+first[3]-current[1]-current[3])/2)<=max(1.0,shortest*.15))
        else:same_row=False
        if same_row:groups[-1].append(line)
        else:groups.append([line])
    preamble=[]
    for group in groups:
        group.sort(key=lambda line:line['bbox'][0])
        if any(right['bbox'][0]<left['bbox'][2]-GEOMETRY_EPSILON for left,right in zip(group,group[1:])):
            return None
        preamble.append({'text':' '.join(line['text'] for line in group),
                         'bbox':[min(line['bbox'][0] for line in group),min(line['bbox'][1] for line in group),
                                 max(line['bbox'][2] for line in group),max(line['bbox'][3] for line in group)]})
    if (len(preamble)>64 or any(len(line['text'].encode('utf-8'))>2000 for line in preamble)
            or sum(len(line['text'].encode('utf-8')) for line in preamble)>12000):
        return None
    return {'version':TABLE_CONTEXT_VERSION,'page':page_number,'table_index':table_index,
            'table_bbox':box,'table_text':table_text,'table_text_sha256':sha256(table_text.encode()).hexdigest(),
            'preamble_lines':preamble,'preceding_table_bottom':preceding}


def image_coverage(page):
    """Area covered by image bboxes, clipped to the page and without overlaps."""
    import pymupdf
    boxes=[]
    for item in page.get_image_info():
        box=(pymupdf.Rect(item['bbox'])*page.rotation_matrix) & page.rect
        if not box.is_empty:boxes.append(tuple(box))
    area=0
    edges=sorted({x for box in boxes for x in (box[0],box[2])})
    for left,right in zip(edges,edges[1:]):
        spans=sorted((y0,y1) for x0,y0,x1,y1 in boxes if x0<right and x1>left)
        covered=0;end=None
        for start,stop in spans:
            if end is None or start>end:covered+=stop-start
            else:covered+=max(0,stop-end)
            end=max(stop,end) if end is not None else stop
        area+=(right-left)*covered
    return area/page.rect.get_area()

def parse_pdf(request, encoder):
    import pymupdf
    start=time.perf_counter()
    if not request.pdf or len(request.pdf)>20*1024*1024 or sha256(request.pdf).hexdigest()!=request.pdf_sha256:
        raise ValueError('PDF_HASH_OR_SIZE_INVALID')
    version=UUID(request.document_version_id)
    try:
        with pymupdf.open(stream=request.pdf,filetype='pdf') as doc:
            if doc.needs_pass:
                raise ValueError('ENCRYPTED_PDF_UNSUPPORTED')
            count=doc.page_count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('INVALID_PDF') from exc
    pages=sorted(request.pages) if request.pages else list(range(1,count+1))
    if not pages or len(pages)>32 or len(set(pages))!=len(pages) or min(pages)<1 or max(pages)>count:
        raise ValueError('PDF_PAGE_SCOPE_INVALID_OR_EXCEEDS_32')
    regions=[];excluded_margin_blocks=[];text_layer_checks=[]
    with tempfile.TemporaryDirectory(prefix='crackrag-m1-parse-') as directory:
        temp=Path(directory); source=temp/'source.pdf'; source.write_bytes(request.pdf)
        with pymupdf.open(source) as document:
            image_coverage_by_page={page:image_coverage(document[page-1]) for page in pages}
            geometry={page:{'rotation':document[page-1].rotation,
                'matrix':tuple(document[page-1].rotation_matrix),
                'size':[document[page-1].rect.width,document[page-1].rect.height]} for page in pages}
            line_geometry={page:page_text_lines(document[page-1]) for page in pages}
            # Reject empty/image-only pages before M0's alternate extractor,
            # which cannot handle a page without a PDF Contents stream.
            for page in pages:
                if not document[page-1].get_text('text').strip():
                    raise ValueError('OCR_REQUIRED_NOT_ENABLED' if image_coverage_by_page[page]>0 else 'NO_RETRIEVABLE_TEXT')
        result=parse_sample(source,pages,temp/'parsed',source_url='',document_id=str(version),
                            reasons={str(p):'M1 caller selected scope' for p in pages})
        previous=None
        for page in pages:
            data=json.loads((temp/'parsed'/f'page-{page}.json').read_text(encoding='utf-8'))
            matrix=pymupdf.Matrix(geometry[page]['matrix'])
            data['blocks']=[{**block,'bbox':list(pymupdf.Rect(block['bbox'])*matrix)} for block in data['blocks']]
            # Pinned PyMuPDF 1.26.7 find_tables already returns display-page
            # table/cell coordinates; only text/image bboxes need rotation.
            data['coordinate_system']=COORDINATES
            data['page_size_pt']=geometry[page]['size']
            # M0's short-text heuristic signals review, not proof that OCR is
            # needed. Large raster bodies still require OCR with a short header.
            text_chars=len(data['normalized_text'].strip())
            coverage=image_coverage_by_page[page]
            has_text=any(block['text'].strip() for block in data['blocks'])
            if (not has_text and coverage>0) or (text_chars<SPARSE_TEXT_CHARS and coverage>=SCAN_IMAGE_COVERAGE):
                raise ValueError('OCR_REQUIRED_NOT_ENABLED')
            if not has_text:
                raise ValueError('NO_RETRIEVABLE_TEXT')
            text_layer_checks.append({'page':page,'text_chars':text_chars,
                'image_bbox_coverage':round(coverage,6),'sparse_text':text_chars<SPARSE_TEXT_CHARS,
                'status':'text_available','completeness':'not_inferred_from_text_presence'})
            context={'coordinate_system':data['coordinate_system'],'pdf_page_rotation':geometry[page]['rotation'],
                'page_number_rule':'one-based physical PDF page; printed label not inferred',
                'page_context':data['normalized_text'][:500],
                'context_blocks':data['blocks'][:2], 'footnote_context':data['normalized_text'][-300:],
                'table_context_join':'neighbor text only; no automatic semantic year/column join',
                'neighbor_page_context':{'page':previous['pdf_page'],'text':previous['normalized_text'][:500]}
                    if previous and previous['pdf_page']==page-1 else None}
            sources=[]
            table_contexts={}
            for i,table in enumerate(data['tables']):
                rows=[' | '.join(str(c or '') for c in row) for row in table['rows']]
                if rows:
                    table_text='\n'.join(rows)
                    key=f'table-{i}'
                    sources.append((key,table['bbox'],table_text,'table'))
                    binding=bound_table_context(page,i,data['tables'],table_text,line_geometry[page],data['page_size_pt'])
                    if binding is not None:table_contexts[key]=binding
            for i,block in enumerate(data['blocks']):
                x0,y0,x1,y1=block['bbox']; mx,my=(x0+x1)/2,(y0+y1)/2
                if y1<=64 or y0>=data['page_size_pt'][1]-35:
                    excluded_margin_blocks.append({'page':page,'block_id':block['block_id'],'bbox':block['bbox'],
                        'reason':'page margin excluded from retrieval only; retained in PDF and page context'})
                    continue
                if any(t['bbox'][0]<=mx<=t['bbox'][2] and t['bbox'][1]<=my<=t['bbox'][3] for t in data['tables']):
                    continue
                if block['text'].strip():
                    sources.append((f'block-{i}',block['bbox'],block['text'],'text'))
            for key,bbox,text,kind in sources:
                for token_start,excerpt in encoder.chunks(text):
                    region_context={**context,'region_source':key,'chunk_start_token':token_start,
                                    'bbox_scope':'original source block/table; chunks share its real bounding box'}
                    if key in table_contexts:region_context['table_context']=table_contexts[key]
                    regions.append({'id':str(uuid5(version,f'{page}:{key}:{token_start}')),
                        'document_version_id':str(version),'page':page,'bbox':bbox,
                        'page_width':data['page_size_pt'][0],'page_height':data['page_size_pt'][1],
                        'kind':kind,'text':excerpt,'text_sha256':sha256(excerpt.encode()).hexdigest(),
                        'context_json':json.dumps(region_context,ensure_ascii=False),
                        'parser_version':VERSION,'embedding_version':encoder.version,'fts_terms':' '.join(terms(excerpt)),
                        'document_title':request.title})
            previous=data
        if not regions or len(regions)>512:
            raise ValueError('REGION_COUNT_LIMIT')
        vectors,usage=encoder.encode([r['text'] for r in regions])
        for region,vector in zip(regions,vectors,strict=True):
            region['embedding']=vector
        return {'regions':regions,'indexed_pages':pages,'total_pages':count,'parser_version':VERSION,
            'embedding_version':encoder.version,'build_usage_json':json.dumps({
                'parser':{'version':VERSION,'duration_ms':round((time.perf_counter()-start)*1000-usage['duration_ms'],3),
                          'pages':len(pages),'ocr_calls':0,'source_sha256':request.pdf_sha256,
                          'coordinate_system':COORDINATES,'page_rotations':{str(page):geometry[page]['rotation'] for page in pages},
                          'text_layer_detection':{'version':'sparse-text-image-coverage-v1',
                              'sparse_text_chars':SPARSE_TEXT_CHARS,'scan_image_coverage':SCAN_IMAGE_COVERAGE,
                              'heuristic':True,'pages':text_layer_checks}},
                'embedding':usage,'total_duration_ms':round((time.perf_counter()-start)*1000,3),
                'cost':{'status':'unknown','amount':None,'currency':'CNY','reason':'local CPU/IO and storage not financially metered'},
                'excluded_margin_blocks':excluded_margin_blocks,'scope':'selected pages only; original PDF preserved','m0_parser_version':PARSER_VERSION},ensure_ascii=False)}
