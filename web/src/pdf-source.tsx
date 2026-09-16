import {useEffect,useRef,useState} from 'react';
import {getDocument,GlobalWorkerOptions,RenderTask} from 'pdfjs-dist';
import worker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc=worker;

export function PDFSource({url,page,bbox}:{url:string;page:number;bbox:number[]}){
 const canvas=useRef<HTMLCanvasElement>(null);
 const [status,setStatus]=useState('loading'),[error,setError]=useState('');
 useEffect(()=>{
  let active=true;let render:RenderTask|undefined;
  setStatus('loading');setError('');
  const loading=getDocument({url:url.split('#')[0],cMapUrl:'/pdfjs/cmaps/',cMapPacked:true,standardFontDataUrl:'/pdfjs/standard_fonts/',wasmUrl:'/pdfjs/wasm/',enableXfa:false});
  void(async()=>{
   try{
    const doc=await loading.promise;if(!active)return;
    const source=await doc.getPage(page);if(!active||!canvas.current)return;
    const viewport=source.getViewport({scale:1.5});
    const target=canvas.current;target.width=Math.ceil(viewport.width);target.height=Math.ceil(viewport.height);
    render=source.render({canvas:target,viewport});await render.promise;
    if(!active)return;
    // Parser coordinates are PDF points with a top-left origin on the displayed page.
    if(source.rotate===0&&bbox.length===4&&bbox.every(Number.isFinite)){
     const ctx=target.getContext('2d');if(ctx){ctx.strokeStyle='#be7625';ctx.lineWidth=2;ctx.strokeRect(bbox[0]*1.5,bbox[1]*1.5,(bbox[2]-bbox[0])*1.5,(bbox[3]-bbox[1])*1.5)}
    }
    setStatus('ready');
   }catch{if(active){setStatus('error');setError('此页暂未完成渲染，可下载原 PDF 核对。')}}
  })();
  return()=>{active=false;render?.cancel();void loading.destroy()};
 },[url,page,bbox.join(',')]);
 return <div className="pdf-preview"><div className="section-line"><span className="hint">原 PDF 第 {page} 页 · 金色框为证据区域</span><a href={url.split('#')[0]} download="source.pdf">下载原 PDF</a></div>{status==='loading'&&<p className="hint">正在渲染原页…</p>}{error&&<p role="alert" className="inline-error">{error}</p>}<canvas ref={canvas} aria-label={`PDF 来源页 ${page}`} data-status={status}/></div>;
}
