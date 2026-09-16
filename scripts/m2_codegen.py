"""Generate/compare M2 schema, frozen asset bindings and Go/Python protocol."""
from pathlib import Path
from hashlib import sha256
import argparse
import json
import os
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ai-runtime/src'))
from crackrag_m1.m2 import ExtractionShape

def main():
    p=argparse.ArgumentParser();p.add_argument('--write',action='store_true');p.add_argument('--tools-directory',default=os.getenv('M2_TOOLS_DIRECTORY',str(ROOT/'tmp/m1-tools')));a=p.parse_args()
    schema=ROOT/'ai-runtime/prompts/m2-v1/candidate.schema.json'
    expected=json.dumps(ExtractionShape.model_json_schema(),ensure_ascii=False,indent=2)+'\n'
    if a.write:schema.write_text(expected,encoding='utf-8',newline='\n')
    assert schema.read_text(encoding='utf-8')==expected,'schema drift'
    assets={path:sha256((ROOT/path).read_bytes()).hexdigest() for path in [
        'ai-runtime/prompts/m2-v1/candidate.schema.json','ai-runtime/prompts/m2-v1/extraction.txt','ai-runtime/prompts/m2-v1/probe.txt','ai-runtime/prompts/m2-v1/answer-contract.txt',
        'ai-runtime/prompts/m1-v1/system.txt','ai-runtime/prompts/m1-v1/action.schema.json']}
    catalog=ROOT/'api/internal/app/m2_catalog.json';body=json.loads(catalog.read_text(encoding='utf-8'))
    if a.write:body['assets']=assets;catalog.write_text(json.dumps(body,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    assert body.get('assets')==assets,'catalog asset binding drift'
    (ROOT/'tmp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
        out=Path(directory);py=out/'python';go=out/'go';py.mkdir();go.mkdir();tool=Path(a.tools_directory)
        args=[sys.executable,'-m','grpc_tools.protoc','-I',str(ROOT/'proto'),f'--python_out={py}',f'--grpc_python_out={py}',
            f'--plugin=protoc-gen-go={tool / "protoc-gen-go.exe"}',f'--plugin=protoc-gen-go-grpc={tool / "protoc-gen-go-grpc.exe"}',
            f'--go_out={go}','--go_opt=module=crackrag/api',f'--go-grpc_out={go}','--go-grpc_opt=module=crackrag/api',str(ROOT/'proto/crackrag/v1/runtime.proto')]
        subprocess.run(args,cwd=ROOT,check=True)
        files=[]
        for folder,actual in [(py,ROOT/'ai-runtime/src'),(go,ROOT/'api')]:
            for file in folder.rglob('*'):
                if file.is_file():
                    path=actual/file.relative_to(folder)
                    if a.write:path.write_bytes(file.read_bytes())
                    assert path.read_bytes()==file.read_bytes(),str(path);files.append(path.relative_to(ROOT).as_posix())
    print(json.dumps({'status':'passed','generated_files':files,'schema':'matches','asset_bindings':assets,'catalog_digest':sha256(catalog.read_bytes()).hexdigest()},indent=2))
if __name__=='__main__':main()
