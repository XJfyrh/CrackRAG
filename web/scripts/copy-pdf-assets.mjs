import {cp,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const root=new URL('../',import.meta.url);
const target=new URL('public/pdfjs/',root);
await mkdir(target,{recursive:true});
for(const entry of ['cmaps','standard_fonts','wasm','LICENSE']){
 await cp(fileURLToPath(new URL('node_modules/pdfjs-dist/'+entry,root)),fileURLToPath(new URL(entry,target)),{recursive:true});
}
