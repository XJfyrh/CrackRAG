"""Generate original, reproducible PDF fixtures (requires reportlab).

These sources contain synthetic values, not third-party issuer disclosures.
They enter the same upload/parser/verification path as user documents.
"""
from pathlib import Path
from hashlib import sha256
import json

from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'web/public/samples'


def document(name, rows, missing_note=False):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / (name + '.pdf')
    c = Canvas(str(path), pagesize=(595, 842), invariant=1, pageCompression=1)
    c.setTitle('CrackRAG original synthetic sample: ' + name)
    c.setAuthor('CrackRAG contributors')
    c.setFillColor(HexColor('#185d51'))
    c.setFont('Helvetica-Bold', 22)
    c.drawString(48, 792, 'CrackRAG / source sample')
    c.setFont('Helvetica', 10)
    c.setFillColor(HexColor('#687c72'))
    c.drawString(48, 768, 'SYNTHETIC DATA - NOT A REAL ISSUER DISCLOSURE')
    c.setFillColor(HexColor('#1d302e'))
    c.setFont('Helvetica', 12)
    for index, line in enumerate(['Entity: Sample Holdings', 'Scope: consolidated', 'Unit: CNY']):
        c.drawString(48, 720 - index * 20, line)
    c.setFont('STSong-Light', 12)
    c.drawString(320, 720, '主体：样例控股（自制样例）')
    xs = [48, 265, 400, 547]
    top, height = 640, 36
    values = [['Metric', 'FY2024', 'FY2023'], *rows]
    c.setFillColor(HexColor('#edf4ed'))
    c.rect(xs[0], top-height, xs[-1]-xs[0], height, fill=1, stroke=0)
    c.setStrokeColor(HexColor('#9bb1a2'))
    c.setLineWidth(.6)
    for x in xs:
        c.line(x, top, x, top-len(values)*height)
    for row in range(len(values)+1):
        y=top-row*height
        c.line(xs[0], y, xs[-1], y)
    c.setFillColor(HexColor('#1d302e'))
    for row, cells in enumerate(values):
        c.setFont('STSong-Light', 11)
        for col, cell in enumerate(cells):
            c.drawString(xs[col]+12, top-row*height-23, cell)
    y=top-len(values)*height-34
    c.setFont('Helvetica', 10)
    if missing_note:
        lines=['Footnote: See the separate consolidation basis note, not included here.',
               'This fixture intentionally omits evidence needed for verification.']
    else:
        lines=['The amounts above are invented solely to demonstrate source verification.',
               'No facts are pre-published. Import this PDF and build them through the app.']
    for line in lines:
        c.drawString(48,y,line);y-=18
    c.setStrokeColor(HexColor('#d2ded3'));c.line(48,73,547,73)
    c.setFont('Helvetica', 9);c.setFillColor(HexColor('#687c72'))
    c.drawString(48,53,'CrackRAG original sample / MIT / physical PDF page 1')
    c.save()
    return {'file':path.name,'sha256':sha256(path.read_bytes()).hexdigest(),
            'source_type':'original_synthetic_fixture','license':'MIT','pages':[1],
            'expected_support':'INCONCLUSIVE' if missing_note else 'SUPPORTED'}


if __name__ == '__main__':
    files=[document('financial',[['营业收入','100.00','90.00'],['营业成本','60.00','55.00'],['净利润','30.00','28.00']]),
           document('ambiguous',[['净利润','30.00','28.00']],True)]
    (OUTPUT/'manifest.json').write_text(json.dumps({'version':'crackrag-samples-v1','files':files},indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(files))
