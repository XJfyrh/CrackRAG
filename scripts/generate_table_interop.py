"""Generate a synthetic Python-parser/Go-validator fixture without model assets.

Two physical tables share their header and first two rows, but have different
scope, currency, and final values. The Go interoperability test must accept only
the first table for the consolidated CNY claim. No issuer documents or gold are
read, and neither the dummy geometry encoder nor parsing can call a provider.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ai-runtime/src'))
from crackrag.v1 import runtime_pb2 as pb
from crackrag_m1.parser import parse_pdf


class GeometryEncoder:
    version = 'synthetic-table-interop-no-model-v1'

    def chunks(self, text):
        return [(start, text[start:start + 512]) for start in range(0, len(text), 448)]

    def encode(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts], {
            'duration_ms': 0, 'paid_api_calls': 0, 'simulated': True}


def draw_table(page, top, profit):
    rows = [['Metric', 'FY2024', 'FY2023'], ['Revenue', '120', '110'],
            ['Cost', '70', '60'], ['Net profit', str(profit), '25']]
    xs = [60, 308, 414, 520]
    for row in range(len(rows) + 1):
        page.draw_line((xs[0], top + row * 18), (xs[-1], top + row * 18), width=.5)
    for x in xs:
        page.draw_line((x, top), (x, top + len(rows) * 18), width=.5)
    for index, row in enumerate(rows):
        for col, text in enumerate(row):
            page.insert_text((xs[col] + 4, top + index * 18 + 12), text, fontsize=8)


def generate():
    import pymupdf
    with pymupdf.open() as document:
        page = document.new_page()
        for y, text in [(85, 'Entity: Sample Holdings'), (105, 'Scope: consolidated'),
                        (125, 'Unit: CNY'), (300, 'Entity: Sample Holdings'),
                        (320, 'Scope: parent'), (340, 'Unit: USD')]:
            page.insert_text((65, y), text, fontsize=10)
        draw_table(page, 145, 30)
        draw_table(page, 360, 80)
        data = document.tobytes()
    request = pb.ParseRequest(pdf=data, pdf_sha256=sha256(data).hexdigest(),
                              document_version_id=str(uuid5(NAMESPACE_URL, 'urn:crackrag:synthetic-table-interop-v1')),
                              pages=[1], title='Synthetic same-page binding test')
    with patch('socket.create_connection', side_effect=AssertionError('network forbidden in synthetic fixture')):
        result = parse_pdf(request, GeometryEncoder())
    tables = [r for r in result['regions'] if r['kind'] == 'table']
    if len(tables) != 2 or any('table_context' not in json.loads(r['context_json']) for r in tables):
        raise ValueError('synthetic parser did not produce two bound tables')
    return {'schema': 'synthetic-table-interop-v1', 'no_api_calls': True, 'regions': result['regions']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'tmp/samepage-synthetic-interop.json')
    args = parser.parse_args()
    result = generate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(f'Synthetic table fixture: {args.output}; two physical tables; provider calls=0')


if __name__ == '__main__':
    main()
