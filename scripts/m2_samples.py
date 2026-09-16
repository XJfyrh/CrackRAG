"""Deterministic, visibly synthetic development sources. No production claims."""
from pathlib import Path
from hashlib import sha256
import json
import pymupdf
ROOT=Path(__file__).resolve().parents[1]
out=ROOT/'eval/m2/sources'
out.mkdir(parents=True,exist_ok=True)
documents={
 'financial':[
  'Entity: Sample Holdings\nScope: consolidated\nUnit: CNY\nMetric | FY2024 | FY2023\nRevenue | 100.00 | 90.00',
  'Entity: Sample Holdings\nScope: consolidated\nUnit: CNY\nMetric | FY2024 | FY2023\nCost of revenue | 60.00 | 55.00'],
 'ambiguous':[
  'Entity: Sample Holdings\nBasis: consolidated group results\nUnit: CNY\nMetric | FY2024 | FY2023\nNet profit | 30.00 | 28.00\nFootnote: See the separate consolidation basis note, not included here.']}
manifest={}
for name,pages in documents.items():
    doc=pymupdf.open()
    for number,text in enumerate(pages,1):
        page=doc.new_page(width=595,height=842)
        page.insert_text((52,46),'CrackRAG M2 - SYNTHETIC DEVELOPMENT FIXTURE',fontsize=11,color=(.15,.2,.3))
        lines=text.splitlines()
        page.insert_text((52,95),'\n'.join(lines[:3]),fontsize=12,lineheight=1.2)
        xs=[52,225,370,543];ys=[155,187,219]
        for x in xs:page.draw_line((x,ys[0]),(x,ys[-1]),color=(.4,.45,.5),width=.7)
        for y in ys:page.draw_line((xs[0],y),(xs[-1],y),color=(.4,.45,.5),width=.7)
        for row,line in enumerate(lines[3:5]):
            for col,cell in enumerate(line.split('|')):page.insert_text((xs[col]+8,ys[row]+21),cell.strip(),fontsize=11)
        if len(lines)>5:page.insert_text((52,254),'\n'.join(lines[5:]),fontsize=10)
        page.insert_text((52,790),f'Verifiable source fixture - not a real issuer disclosure | Page {number}',fontsize=9)
    # Stable metadata/IDs make the source bytes reproducible.
    data=doc.tobytes(garbage=4,deflate=True,no_new_id=True)
    path=out/(name+'.pdf');path.write_bytes(data)
    for i,page in enumerate(doc):page.get_pixmap(matrix=pymupdf.Matrix(1,1)).save(out/f'{name}-{i+1}.png')
    doc.close()
    manifest[name]={'file':path.name,'sha256':sha256(data).hexdigest(),'pages':pages,'source_type':'synthetic_development','gold_method':'Read each visible Metric/FY column; independently compute gross margin (100-60)/100 = 0.4. Net profit has a separate unavailable basis note and a scope phrase outside the deterministic grammar; expected INCONCLUSIVE, even if a probe considers it plausible.'}
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'documents':len(manifest),'pages':sum(len(p) for p in documents.values())}))
