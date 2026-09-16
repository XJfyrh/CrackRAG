"""Small, deterministic born-digital PDF experiment; no implicit OCR/downloads."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import time

from .artifacts import write_json
from .prompt import digest

PARSER_VERSION = 'pymupdf-page-regions-v1'


def parse_sample(pdf: Path, pages: list[int], output: Path, *, source_url: str,
                 document_id: str, reasons: dict[str, str]) -> dict:
    import pymupdf
    from pypdf import PdfReader

    if output.exists():
        raise ValueError('parser output exists; use a new directory')
    if not pages or len(set(pages)) != len(pages) or any(type(p) is not int or p < 1 for p in pages):
        raise ValueError('pages must be unique one-based positive integers')
    output.mkdir(parents=True)
    pdf_hash = digest(pdf.read_bytes())
    reader = PdfReader(pdf)
    document = pymupdf.open(pdf)
    extracted, comparison, context = [], [], []
    for number in pages:
        page = document[number - 1]
        start = time.perf_counter()
        text = page.get_text('text', sort=True)
        # Whitespace normalization is versioned; raw text and geometric data stay archived.
        normalized = '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())
        blocks = [{'block_id': f'{document_id}:p{number}:b{i}', 'bbox': list(b[:4]), 'text': b[4]}
                  for i, b in enumerate(page.get_text('blocks', sort=True)) if b[6] == 0]
        tables = []
        table_error = None
        try:
            for table in page.find_tables().tables:
                tables.append({'bbox': list(table.bbox), 'cells': [list(c) if c else None for c in table.cells],
                               'rows': table.extract()})
        except Exception as exc:
            table_error = type(exc).__name__
        elapsed = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        alternative = reader.pages[number - 1].extract_text(extraction_mode='layout')
        alternative_elapsed = (time.perf_counter() - start) * 1000
        suspect_scan = len(normalized) < 60
        row = {'document_id': document_id, 'pdf_sha256': pdf_hash, 'pdf_page': number,
               'printed_page': str(number), 'page_size_pt': [page.rect.width, page.rect.height],
               'coordinate_system': 'PDF points; origin top-left; x right, y down',
               'source_url': source_url, 'selection_reason': reasons[str(number)],
               'raw_text': text, 'normalized_text': normalized, 'blocks': blocks,
               'words': [list(w) for w in page.get_text('words', sort=True)],
               'tables': tables, 'table_error': table_error, 'image_count': len(page.get_images()),
               'needs_ocr': suspect_scan, 'ocr_status': 'not_run_requires_review' if suspect_scan else 'not_applicable_text_page'}
        write_json(output / f'page-{number}.json', row)
        (output / f'page-{number}-pymupdf.txt').write_text(text, encoding='utf-8')
        (output / f'page-{number}-pypdf.txt').write_text(alternative, encoding='utf-8')
        page.get_pixmap(matrix=pymupdf.Matrix(1.35, 1.35)).save(output / f'page-{number}.png')
        comparison.append({'page': number, 'pymupdf_ms': round(elapsed, 3), 'pypdf_ms': round(alternative_elapsed, 3),
                           'pymupdf_chars': len(text), 'pypdf_chars': len(alternative), 'tables': len(tables)})
        context.append(f'[SOURCE {document_id}; PDF page {number}; sha256={pdf_hash}]\n{normalized}')
        extracted.append(row)
    document.close()
    (output / 'document.md').write_text('\n\n'.join(context)+'\n', encoding='utf-8')
    summary = {'schema_version': 1, 'parser_version': PARSER_VERSION, 'source_pdf': pdf.name,
               'pdf_sha256': pdf_hash, 'source_url': source_url, 'selected_pages': pages,
               'document_id': document_id, 'versions': {p: importlib.metadata.version(p) for p in ('PyMuPDF', 'pypdf')},
               'configuration': {'sort': True, 'normalize_line_whitespace': True, 'table_strategy': 'lines',
                                 'render_scale': 1.35, 'ocr_enabled': False, 'page_numbers': 'one-based'},
               'ocr_scope': {'selected_scan_pages': [r['pdf_page'] for r in extracted if r['needs_ocr']],
                             'calls': 0, 'status': 'not_applicable' if not any(r['needs_ocr'] for r in extracted) else 'unverified'},
               'comparison': comparison,
               'defects': ['No automatic cross-page table stitching', 'Wrapped headers/labels need region review',
                           'PDF text presence does not prove entity/year/unit alignment'],
               'fallback': 'Inspect saved page image and bounding boxes; reject ambiguous rows; pypdf for independent text check; scanned pages require separately verified OCR',
               'output_sha256': {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir()) if p.is_file()}}
    write_json(output / 'parser-manifest.json', summary)
    return summary


def rebuild_samples(spec: Path, output: Path) -> list[dict]:
    data = json.loads(spec.read_text(encoding='utf-8'))
    result = []
    for entry in data['documents']:
        source = (spec.parent / entry['pdf']).resolve()
        if digest(source.read_bytes()) != entry['sha256']:
            raise ValueError('source PDF hash mismatch')
        result.append(parse_sample(source, entry['pages'], output / entry['id'], source_url=entry['url'],
                                   document_id=entry['id'], reasons=entry['reasons']))
    return result
