"""Frozen BGE-M3 dense encoder; a separately named fixture exists only for tests."""
import asyncio
from contextlib import contextmanager
from hashlib import sha256
import math
import json
import re
import threading
import time

from .config import EMBEDDING_VERSION, ROOT, REVISION

def terms(text):
    result = []
    for part in re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]+',text.lower()):
        result.extend([part] if not '\u4e00'<=part[0]<='\u9fff' or len(part)==1
                      else [part[i:i+2] for i in range(len(part)-1)])
    return list(dict.fromkeys(result))

class DenseEncoder:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.model = self.tokenizer = None
        self.version = EMBEDDING_VERSION if settings.embedding_mode=='bge-m3' else 'fixture-hash1024-v1-NOT-BGE'

    def load(self):
        if self.settings.embedding_mode=='fixture' or self.model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer
        directory = self.settings.embedding_directory
        if not (directory/'model.safetensors').exists() and not (directory/'pytorch_model.bin').exists():
            raise ValueError('EMBEDDING_WEIGHTS_MISSING')
        manifest=json.loads((ROOT/'config/m1-embedding.json').read_text(encoding='utf-8'))
        if manifest['revision']!=REVISION or manifest['dimensions']!=1024:
            raise ValueError('EMBEDDING_CONFIGURATION_MISMATCH')
        for name,expected in manifest['files'].items():
            path=directory/name
            if not path.is_file() or path.stat().st_size!=expected['bytes']:
                raise ValueError('EMBEDDING_FILE_MISMATCH')
            digest=sha256()
            with path.open('rb') as handle:
                for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
            if digest.hexdigest()!=expected['sha256']:raise ValueError('EMBEDDING_FILE_MISMATCH')
        torch.set_num_threads(4)
        self.tokenizer = AutoTokenizer.from_pretrained(directory,local_files_only=True,trust_remote_code=False)
        self.model = AutoModel.from_pretrained(directory,local_files_only=True,trust_remote_code=False,use_safetensors=False).to('cpu').eval()

    def chunks(self, text):
        with self.lock:
            self.load()
            if self.tokenizer is None:
                return [(i,text[i:i+512]) for i in range(0,len(text),448)]
            tokens=self.tokenizer(text,add_special_tokens=False,return_offsets_mapping=True)
            offsets=tokens['offset_mapping']
            # 512 tokens per source chunk, with a 64-token overlap.
            return [(start,text[offsets[start][0]:offsets[min(start+512,len(offsets))-1][1]])
                    for start in range(0,len(offsets),448)]

    @staticmethod
    def check_cancelled(cancelled):
        if cancelled is not None and cancelled.is_set():
            raise ValueError('EMBEDDING_CANCELLED')

    @contextmanager
    def encoding_lock(self,cancelled):
        if cancelled is None:
            self.lock.acquire()
        else:
            while not self.lock.acquire(timeout=.05):
                self.check_cancelled(cancelled)
        try:
            self.check_cancelled(cancelled)
            yield
        finally:
            self.lock.release()

    async def encode_async(self,texts):
        cancelled=threading.Event()
        try:
            return await asyncio.to_thread(self.encode,texts,cancelled=cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def encode(self, texts, *, cancelled=None):
        self.check_cancelled(cancelled)
        start=time.perf_counter()
        if not texts or len(texts)>512 or any(not t or len(t)>20000 for t in texts):
            raise ValueError('EMBEDDING_INPUT_LIMIT')
        with self.encoding_lock(cancelled):
            self.load()
            self.check_cancelled(cancelled)
            if self.settings.embedding_mode=='fixture':
                vectors=[]
                for text in texts:
                    self.check_cancelled(cancelled)
                    values=[0.0]*1024
                    for term in terms(text):
                        h=sha256(term.encode()).digest(); values[int.from_bytes(h[:4])%1024]+=1
                    norm=math.sqrt(sum(v*v for v in values)) or 1
                    vectors.append([v/norm for v in values])
            else:
                import torch
                vectors=[]
                with torch.inference_mode():
                    for offset in range(0,len(texts),4):
                        self.check_cancelled(cancelled)
                        inputs=self.tokenizer(texts[offset:offset+4],padding=True,truncation=True,max_length=514,return_tensors='pt')
                        output=self.model(**inputs).last_hidden_state[:,0]
                        output=torch.nn.functional.normalize(output,p=2,dim=1)
                        vectors.extend(output.cpu().float().tolist())
        if any(len(v)!=1024 or not all(math.isfinite(x) for x in v) or abs(sum(x*x for x in v)-1)>1e-3 for v in vectors):
            raise ValueError('INVALID_EMBEDDING_VECTOR')
        return vectors,{'operation':'embedding','version':self.version,'items':len(texts),
            'duration_ms':round((time.perf_counter()-start)*1000,3),'device':'cpu','cpu_threads':4,
            'local_resource_cost':{'status':'unknown','amount':None,'currency':'CNY','reason':'local compute not financially metered'},
            'paid_api_calls':0,'simulated':self.settings.embedding_mode=='fixture'}
