import {useEffect,useRef,useState} from 'react';
import {financialPageScore,recommendedPages} from './pdf-page-suggestions';

// The parser accepts at most 32 pages. Check the file locally before an upload
// so a long report does not fail after the user has already sent it.
export function useDocumentPreflight(){
 const [pdfPages,setPdfPages]=useState<number|null>(null);
 const [checking,setChecking]=useState(false),[fileError,setFileError]=useState('');
 const [suggestedPages,setSuggestedPages]=useState<number[]>([]),[scanProgress,setScanProgress]=useState(0),[scanning,setScanning]=useState(false);
 const [previewURL,setPreviewURL]=useState('');
 const [scanNotice,setScanNotice]=useState('');
 const sequence=useRef(0),preview=useRef('');
 useEffect(()=>()=>{if(preview.current)URL.revokeObjectURL(preview.current)},[]);
 async function inspect(selectedFile:File|null){
  const current=++sequence.current;let pageCount=0;setPdfPages(null);setFileError('');setScanNotice('');setSuggestedPages([]);setScanProgress(0);setScanning(false);setChecking(false);
  if(preview.current){URL.revokeObjectURL(preview.current);preview.current='';setPreviewURL('')}
  if(!selectedFile){setChecking(false);return}
  if(selectedFile.size>20*1024*1024){setFileError('文件超过 20 MB，请换一份较小的 PDF。');return}
  preview.current=URL.createObjectURL(selectedFile);setPreviewURL(preview.current);
  setChecking(true);
  try{
   const [{getDocument,GlobalWorkerOptions},{default:worker}]=await Promise.all([import('pdfjs-dist'),import('pdfjs-dist/build/pdf.worker.min.mjs?url')]);
   GlobalWorkerOptions.workerSrc=worker;
   const task=getDocument({data:await selectedFile.arrayBuffer()});const document=await task.promise;
   if(current!==sequence.current){await task.destroy();return}
   pageCount=document.numPages;setPdfPages(pageCount);setChecking(false);
   if(document.numPages>32){
    setScanning(true);const scores:{page:number;score:number}[]=[];
    const max=Math.min(document.numPages,320);
    if(document.numPages>320)setScanNotice('自动查找只覆盖前 320 页；后续页面请在本地 PDF 中核对后手动选择。');
    for(let pageNumber=1;pageNumber<=max&&current===sequence.current;pageNumber++){
     const page=await document.getPage(pageNumber);
     const content=await page.getTextContent();
     const contentText=content.items.filter(item=>'str' in item).map(item=>item.str).join('');
     const score=financialPageScore(contentText);
     if(score>0)scores.push({page:pageNumber,score});
     if(pageNumber%12===0||pageNumber===max)setScanProgress(Math.round(pageNumber/max*100));
     page.cleanup();
    }
    if(current===sequence.current){const pages=recommendedPages(scores);setSuggestedPages(pages);setScanning(false);if(!pages.length&&document.numPages<=320)setScanNotice('没有找到可建议的文字表格页。请在本地 PDF 中核对后手动选择。')}
   }
   await task.destroy();
   }catch{if(current===sequence.current){if(pageCount>0)setScanNotice('未能自动定位财务表。请在本地 PDF 中搜索表名并手动选择页面。');else setFileError('无法读取页数。请确认 PDF 未加密且文件完整。')}}
  finally{if(current===sequence.current){setChecking(false);setScanning(false)}}
 }
 function reset(){++sequence.current;setPdfPages(null);setFileError('');setScanNotice('');setChecking(false);setSuggestedPages([]);setScanning(false);setScanProgress(0);if(preview.current){URL.revokeObjectURL(preview.current);preview.current='';setPreviewURL('')}}
 return {pdfPages,fileChecking:checking,fileError,scanNotice,suggestedPages,scanProgress,scanning,previewURL,inspectFile:inspect,resetFile:reset};
}
