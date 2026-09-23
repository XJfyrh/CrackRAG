import {useCallback,useEffect,useRef,useState} from 'react';
import {getDocument,GlobalWorkerOptions,RenderTask} from 'pdfjs-dist';
import worker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import {assetUrl} from './urls';

GlobalWorkerOptions.workerSrc=worker;

type Highlight={left:number;top:number;width:number;height:number};
export function PDFSource({url,page,bbox,quote='',displayPage=page}:{url:string;page:number;bbox:number[];quote?:string;displayPage?:number}){
 const canvas=useRef<HTMLCanvasElement>(null),frame=useRef<HTMLDivElement>(null),viewer=useRef<HTMLDivElement>(null);
 const [status,setStatus]=useState('loading'),[error,setError]=useState(''),[zoom,setZoom]=useState(1.5);
 const [frameWidth,setFrameWidth]=useState(680),[highlight,setHighlight]=useState<Highlight|null>(null),[pageWidth,setPageWidth]=useState(0),[precise,setPrecise]=useState(false);
 const focusHighlight=useCallback(()=>{
  const area=viewer.current,box=highlight;if(!area||!box)return;
  area.scrollTo({left:Math.max(0,box.left+box.width/2-area.clientWidth/2),top:Math.max(0,box.top+box.height/2-area.clientHeight/2),behavior:'smooth'});
 },[highlight]);
 useEffect(()=>{
  if(!frame.current)return;
  const observer=new ResizeObserver(entries=>setFrameWidth(Math.max(260,Math.floor(entries[0].contentRect.width))));
  observer.observe(frame.current);return()=>observer.disconnect();
 },[]);
 useEffect(()=>{
  let active=true;let render:RenderTask|undefined;
  setStatus('loading');setError('');setHighlight(null);setPrecise(false);
  const loading=getDocument({url:url.split('#')[0],cMapUrl:assetUrl('pdfjs/cmaps/'),cMapPacked:true,standardFontDataUrl:assetUrl('pdfjs/standard_fonts/'),wasmUrl:assetUrl('pdfjs/wasm/'),enableXfa:false});
  void(async()=>{
   try{
    const doc=await loading.promise;if(!active)return;
    const source=await doc.getPage(page);if(!active||!canvas.current)return;
    const unscaled=source.getViewport({scale:1});
    const fit=Math.max(.45,Math.min(1.8,(frameWidth-24)/unscaled.width));
    const scale=fit*zoom;
    const viewport=source.getViewport({scale});
    const pixelRatio=Math.min(window.devicePixelRatio||1,2);
    const target=canvas.current;target.style.width=`${viewport.width}px`;target.style.height=`${viewport.height}px`;
    target.width=Math.ceil(viewport.width*pixelRatio);target.height=Math.ceil(viewport.height*pixelRatio);
    const context=target.getContext('2d');if(!context)throw new Error('CANVAS_UNAVAILABLE');
    render=source.render({canvas:target,canvasContext:context,viewport,transform:pixelRatio===1?undefined:[pixelRatio,0,0,pixelRatio,0,0]});
    await render.promise;if(!active)return;
    setPageWidth(viewport.width);
    const numeric=quote.match(/\d[\d,]{4,}(?:\.\d+)?/g)?.[0]?.replaceAll(',','');
    if(numeric){
     const content=await source.getTextContent();
     const item=content.items.find(value=>'str' in value&&value.str.replaceAll(',','').replaceAll(' ','').includes(numeric));
     if(item&&'str' in item){
      const first=viewport.convertToViewportPoint(item.transform[4],item.transform[5]);
      const second=viewport.convertToViewportPoint(item.transform[4]+item.width,item.transform[5]+Math.max(item.height,8));
      const left=Math.min(first[0],second[0]),top=Math.min(first[1],second[1]);
      setHighlight({left:left-6,top:top-6,width:Math.max(25,Math.abs(first[0]-second[0])+12),height:Math.max(15,Math.abs(first[1]-second[1])+12)});
      setPrecise(true);setStatus('ready');return;
     }
    }
    if(source.rotate===0&&bbox.length===4&&bbox.every(Number.isFinite)&&bbox[2]>bbox[0]&&bbox[3]>bbox[1]){
     setHighlight({left:bbox[0]*scale,top:bbox[1]*scale,width:(bbox[2]-bbox[0])*scale,height:(bbox[3]-bbox[1])*scale});
    }else setHighlight(null);
    setStatus('ready');
   }catch{if(active){setStatus('error');setError('此页暂未完成渲染，可下载原 PDF 核对。')}}
  })();
  return()=>{active=false;render?.cancel();void loading.destroy()};
 },[url,page,bbox.join(','),quote,frameWidth,zoom]);
 useEffect(()=>{if(status==='ready'&&highlight)requestAnimationFrame(focusHighlight)},[status,highlight,focusHighlight]);
 return <div className="pdf-preview" ref={frame}><div className="pdf-toolbar"><div><strong>原报告第 {displayPage} 页</strong><span className="hint">{precise?'金色框定位到引文中的数字':'金色区域是本次引用'}</span></div><div className="pdf-controls"><button type="button" className="secondary" aria-label="缩小 PDF" disabled={zoom<=.75} onClick={()=>setZoom(value=>Math.max(.75,Math.round((value-.25)*100)/100))}>－</button><span aria-live="polite">{Math.round(zoom*100)}%</span><button type="button" className="secondary" aria-label="放大 PDF" disabled={zoom>=2.5} onClick={()=>setZoom(value=>Math.min(2.5,Math.round((value+.25)*100)/100))}>＋</button><button type="button" className="secondary" disabled={!highlight} onClick={focusHighlight}>定位证据</button></div></div>
  {status==='loading'&&<p className="hint">正在渲染原页…</p>}{error&&<p role="alert" className="inline-error">{error}</p>}
  <div className="pdf-viewport" ref={viewer}><div className="pdf-page" style={{width:pageWidth||undefined}}><canvas ref={canvas} aria-label={`PDF 来源页 ${displayPage}`} data-status={status}/>{highlight&&<div className="pdf-highlight" style={highlight} aria-hidden="true"/>}</div></div>
  <a className="pdf-download" href={url.split('#')[0]} download={displayPage===page?'source.pdf':'source-page.pdf'}>{displayPage===page?'下载原 PDF':'下载引用页'}，在外部阅读器中查看</a>
 </div>;
}
