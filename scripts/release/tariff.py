"""Verify model-column association and all rates before enabling paid work."""
from html.parser import HTMLParser
import re

class Table(HTMLParser):
    def __init__(self):super().__init__();self.rows=[];self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        if tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(re.sub(r'\s+','',''.join(self.cell)).replace('\u200b',''));self.cell=None
        if tag=='tr' and self.row is not None:self.rows.append(self.row);self.row=None

def verify(raw):
    parser=Table();parser.feed(raw.decode('utf-8'));rows=parser.rows
    headers=[r for r in rows if r and r[0]=='模型']
    if len(headers)!=1 or len(headers[0])!=3 or not headers[0][1].startswith('deepseek-flash'):raise ValueError('UNRECOGNIZED_MODEL_PRICE_COLUMNS')
    for label,idle,peak in [('缓存命中','0.02元','0.04元'),('缓存未命中','1元','2元'),('百万tokens输出','4元','8元')]:
        matching=[i for i,r in enumerate(rows) if any(label in c for c in r)]
        if len(matching)!=1:raise ValueError('UNRECOGNIZED_PRICE_ROW')
        i=matching[0]
        if i+1>=len(rows) or len(rows[i])<2 or len(rows[i+1])<2 or rows[i][-2]!=idle or rows[i+1][-2]!=peak or not any('空闲时段' in c for c in rows[i]) or not any('高峰时段' in c for c in rows[i+1]):raise ValueError('OFFICIAL_PRICE_CHANGED')
    flat=re.sub(r'\s+','',re.sub('<[^>]+>','',raw.decode('utf-8')))
    if '周一至周五9:00-12:00、14:00-18:00' not in flat:raise ValueError('PRICE_SCHEDULE_CHANGED')
    return {'official_column_verified':True,'flash_idle':['1','0.02','4'],'flash_peak':['2','0.04','8'],'schedule':'deepseek-cn-peak-v1'}
