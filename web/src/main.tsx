import React, {useEffect, useRef, useState, lazy, Suspense} from 'react';
import {createRoot} from 'react-dom/client';
import {APIError, RequestScope, delay} from './requests';
import {apiUrl, assetUrl, healthUrl} from './urls';
import './style.css';
const PDFSource=lazy(()=>import('./pdf-source').then(m=>({default:m.PDFSource})));

type Doc={id:string;title:string;current_version_id:string;state:string;indexed_pages:number[];requested_pages:number[];error_json?:{reason_code:string};build_usage:unknown};
type Source={region_id:string;document_id:string;document_version_id:string;title:string;page:number;replay_page?:number;bbox:number[];text:string;quote?:string;source_url:string;context:unknown};
type Call={attempt_id:string;state:string;stage?:string;amount_cny:string|null;record?:{request_id:string;request_id_source:string;latency_ms:number;normalized_usage:unknown;cache:unknown;transport_failure:string|null}};
type Job={job_id:string;state:string;reason:string;batch_id:string;deadline_at:string;completed_at?:string};
type Run={id:string;question:string;state:string;provider:string;answer:{text:string;answer_validation?:{status:string;policy:string;items?:{status:string;reasons:string[]}[]};evidence_summary:{sources:Source[];unresolved:string[];calculations?:unknown[];structured_coverage?:{status:string};reused_facts?:{fact_id:string;report_id:string;origin:string;value:string;unit:string}[];raw_observation_region_ids?:string[]}}|null;error:{reason_code:string}|null;calls:Call[];cost:Record<string,unknown>;diagnostics?:{m3?:{status?:string;jobs?:Job[];pending_jobs?:number};[key:string]:unknown}};
type HistoryItem={id:string;question:string;state:string;provider:string;answer_status:string;model_calls:number;known_estimated_cny:string;created_at:string};
export type DemoStep={role:string;question:string;document_ids:string[];build_facts?:boolean;execution_policy?:string};
const demoStepNames:Record<string,string>={'first-answer':'① 看第一次回答',build:'② 保存可复用数字',repeat:'③ 再问同一问题',paraphrase:'④ 换个问法',abstention:'⑤ 看证据不足时的回答'};
const names:Record<string,string>={QUEUED:'等待解析',PARSING:'正在解析与索引',READY:'可检索',FAILED:'未完成',INTERRUPTED:'已中断',RUNNING:'正在查阅原文',COMPLETED:'已完成',CANCELLED:'已取消',TIMED_OUT:'已超时'};
const reasons:Record<string,string>={
 INVALID_PDF:'无法读取这份 PDF。请确认文件未加密、未损坏，再重新上传。',
 OCR_REQUIRED_NOT_ENABLED:'所选页是扫描图像。请换成含文字层的页面；此版本暂不支持 OCR。',
 DOCUMENT_NOT_READY:'文档还在整理。请等状态变为“可检索”后再提问。',
 NOT_FOUND:'这份文档或查询已不可访问。请检查当前访问身份并重新选择文档。',
 UNAUTHENTICATED:'访问令牌无效。请重新输入本机初始化时生成的令牌。',
 MODEL_HTTP_ERROR:'模型服务暂时没有完成请求。请先在“最近查询”确认结果，再手动重试，避免重复计费。',
 USAGE_MISSING:'模型没有返回用量，新的真实请求已暂停。请由维护者核对账单与预算后再继续。',
 COST_UNKNOWN:'本次费用尚无法确认，新的真实请求已暂停。请由维护者核对账单与预算后再继续。',
 DEADLINE_EXCEEDED:'这次查询已超时。请缩小页码或问题范围，再手动发起新查询。',
 USER_CANCELLED:'查询已取消。已发生的调用仍会计入费用；可在“最近查询”查看记录。',
 CITATION_NOT_OBSERVED:'引用没有对应到实际读取的原文，因此未展示确定结论。请打开来源或改问更具体的问题。',
 SOURCE_UNIT_UNPROVEN:'来源没有明确单位。请补选包含表头和单位的页面。',
 BUSINESS_SCOPE_UNPROVEN:'来源无法确认是否为合并口径。请补选写明口径的页面。',
 REQUIRED_BASIS_NOTE_MISSING:'缺少判断口径所需的附注。请补选附注页后重试。',
 HOT_WINDOW_EXPIRED:'本次未继续保存可复用数字。若希望现在完成，可选择“立即处理”，但可能产生额外费用。',
 NO_CACHE_EVIDENCE:'本次未继续保存可复用数字。若希望现在完成，可选择“立即处理”，但可能产生额外费用。',
 M3_OUTCOME_UNKNOWN:'后台调用或费用待核对，新的真实请求已暂停。请由维护者先检查账单。',
 DEMO_STEP_MISMATCH:'所选内容与录制步骤不一致。请点击当前演示步骤后再查看。',
 DEMO_SEQUENCE_COMPLETE:'演示已经结束。点击“重新播放”可以从头观看。',
};
const reasonText=(reason:string)=>reasons[reason]||'暂时无法完成这一步。请在“最近查询”核对状态，必要时联系维护者。';
const answerTextForDisplay=(answer:string)=>answer
 .replace(/\bFY(\d{4})\b/g,'$1 年度')
 .replace(/\bRevenue\b/g,'营业收入')
 .replace(/\bNet profit\b/g,'净利润')
 .replace(/\bCNY\b/g,'人民币元')
 .replace(/\bSample Holdings\b/g,'样例控股');
const supportNames:Record<string,string>={SUPPORTED:'已核对来源',PARTIAL:'部分问题有依据',INCONCLUSIVE:'证据不足，暂不下结论',UNSUPPORTED:'目前无法回答这个问题'};
const answerStatusDescription=(run:Run)=>{
 const status=run.answer?.answer_validation?.status;
 if(status==='INCONCLUSIVE')return '缺少足够证据支持确定数字。请补选相关页面或改问更具体的问题。';
 if(status==='UNSUPPORTED')return '这个问题超出当前支持范围。请改问明确的财务数字。';
 if(status==='PARTIAL')return '只有部分内容通过核对；未确认的部分会在下方说明。';
 return (run.answer?.evidence_summary.reused_facts?.length||0)>0?'这次直接复用了此前核对的数字，仍可回到原页。':'这次查阅了所选原文；回答不等于自动保存数字。';
};
const displayGap=(gap:string)=>reasons[gap]||(/^[A-Z][A-Z0-9_]+$/.test(gap)?reasonText(gap):answerTextForDisplay(gap));
const finished=(state:string)=>['COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED'].includes(state);
// The replay build must never resume a query id left behind by an earlier visit:
// its frozen sequence is the entry point, not the previous session's last run.
const restoredQuery=()=>import.meta.env.VITE_DEMO_REPLAY==='1'?'':localStorage.getItem('m1:last-query')||'';
type PendingSubmission={body:string;key:string};
function readPending():PendingSubmission|null { try {
  const saved=JSON.parse(localStorage.getItem('m1:pending-query')||'null');
  if(!saved)return null;
  const body=JSON.parse(saved.body);
  if(typeof saved.key==='string'&&saved.key&&typeof body.question==='string'&&Array.isArray(body.document_ids)&&body.document_ids.every((id:unknown)=>typeof id==='string'))return saved;
 }catch{/* A damaged draft must not prevent history from loading. */}
 localStorage.removeItem('m1:pending-query');return null;
}
const errorText=(error:unknown)=>error instanceof APIError?reasonText(error.reason):'连接暂时中断。请检查服务状态，再从“最近查询”确认是否已提交，避免直接重复计费。';
const awaitingSettlement=(run:Run)=>run.calls?.some(call=>call.state==='RESERVED');
const pendingJobs=(run:Run)=>!!run.diagnostics?.m3?.pending_jobs||run.diagnostics?.m3?.status==='unavailable';
const jobNames:Record<string,string>={WAITING_PREFIX:'等待合适的处理时机',RUNNING:'正在核对数字',RESULT_READY:'数字已保存，正在完成核验',COMMITTED:'已保存可复用的数字',SKIPPED:'本次未继续处理',FAILED:'未能保存，请查看原因',OUTCOME_UNKNOWN:'结果或费用待核对'};

function AnswerEvidence({run}:{run:Run}){
 const evidence=run.answer?.evidence_summary;
 const reused=evidence?.reused_facts||[];
 const explanation=run.answer?.answer_validation?.status==='INCONCLUSIVE'?'查阅了所选 PDF，但缺少支撑确定数字的证据。'
  :run.answer?.answer_validation?.status==='UNSUPPORTED'?'这个问题不在当前支持范围内。'
  :reused.length?`复用了 ${reused.length} 个此前核对的数字。`:'本次查阅并核对了所选 PDF。';
 return <details className="structure-diagnostics"><summary>这次回答依据了什么</summary>
 <p className="hint">{explanation}{evidence?.sources.length?'下方可以打开引用，回到原页核对。':''}</p>
 {reused.map((fact,index)=><p className="evidence-item" key={fact.fact_id}>已核对数字 {index+1}：{fact.value} {fact.unit}（{fact.origin==='DERIVED'?'由已核对数字计算':'年报原文披露'}）</p>)}
 </details>;
}

function UsageDetails({run}:{run:Run}){
 const calls=run.calls||[];
 const estimated=Number(run.cost?.known_estimated_subtotal||0);
 const unknown=Number(run.cost?.unknown_calls||0);
 return <details className="usage"><summary>本次调用与费用 <span>{calls.length} 次{run.provider==='mock'?'模拟':'模型'}调用</span></summary>
  <p className="hint">{run.provider==='mock'?'模拟运行不调用供应商。':`已知用量的费用估算为 ¥${estimated.toFixed(6)}。`}{unknown>0?'仍有费用待核对，新的真实请求已暂停。':''}本地解析、计算与存储费用未计入。</p>
  {calls.map((call,index)=><div className="call" key={call.attempt_id}><strong>第 {index+1} 次调用：{call.state==='SETTLED'?'已结算':call.state==='RESERVED'?'费用待确认':call.state==='FAILED'?'未完成':'已记录'}</strong><p className="hint">{call.amount_cny===null?'费用待确认':`已知费用估算 ¥${Number(call.amount_cny).toFixed(6)}`}{call.record?.transport_failure?'；请求未正常完成，请先核对费用':''}</p></div>)}
 </details>;
}

function Root(){
 const [token,setToken]=useState(()=>sessionStorage.getItem('m1:access-token')||'');
 const [draftToken,setDraftToken]=useState('');
 function changeToken(next:string){
  localStorage.removeItem('m1:pending-query');localStorage.removeItem('m1:last-query');
  sessionStorage.setItem('m1:access-token',next);setToken(next);
 }
 if(!token)return <main className="login"><a className="brand" href={assetUrl('')}>Crack<span>RAG</span></a><section className="card"><p className="eyebrow">LOCAL DOCUMENT WORKSPACE</p><h1>让答案有据可查。</h1><p>使用本机初始化时生成的访问令牌进入。文档、查询和费用按身份隔离。</p><form onSubmit={e=>{e.preventDefault();changeToken(draftToken.trim())}}><label className="label">本机访问令牌<input type="password" aria-label="本机访问令牌" autoComplete="off" value={draftToken} onChange={e=>setDraftToken(e.target.value)} required minLength={8}/></label><button type="submit">进入工作区</button></form><p className="hint">默认演示使用模拟模型，不需要供应商密钥。真实模式由维护者在服务端启用。</p></section></main>;
 return <CrackRAGWorkspace key={token} scope={new RequestScope(token)} onTokenChange={changeToken}/>;
}

export function CrackRAGWorkspace({scope,onTokenChange,readOnly=false,notice,demoSteps}:{scope:{active:boolean;close:()=>void;request:(path:string,options?:RequestInit)=>Promise<Response>;json:<T>(path:string,options?:RequestInit)=>Promise<T>};onTokenChange:(token:string)=>void;readOnly?:boolean;notice?:React.ReactNode;demoSteps?:DemoStep[]}){
 const [initialPending]=useState(()=>readOnly?null:readPending());
 const draft=initialPending?JSON.parse(initialPending.body):null;
 const [buildFacts,setBuildFacts]=useState(!!draft?.build_facts);
 const [executionPolicy,setExecutionPolicy]=useState(draft?.execution_policy==='COLD_ALLOWED'?'COLD_ALLOWED':'HOT_ONLY');
 const [resumingJob,setResumingJob]=useState('');
 const [docs,setDocs]=useState<Doc[]>([]),[selected,setSelected]=useState<string[]>(draft?.document_ids||[]),[question,setQuestion]=useState(draft?.question||'');
 const [history,setHistory]=useState<HistoryItem[]>([]),[historyCursor,setHistoryCursor]=useState(''),[historyBusy,setHistoryBusy]=useState(false);
 const [demoPosition,setDemoPosition]=useState(0);
 const [run,setRun]=useState<Run|null>(null),[queryID,setQueryID]=useState(restoredQuery);
 const [error,setError]=useState(''),[uploading,setUploading]=useState(false),[submitting,setSubmitting]=useState(false);
 const [scopePages,setScopePages]=useState(''),[year,setYear]=useState(''),[source,setSource]=useState<Source|null>(null),[pdfURL,setPdfURL]=useState('');
 const [provider,setProvider]=useState(''),[connection,setConnection]=useState('连接中');
 const file=useRef<HTMLInputElement>(null),submitLock=useRef(false);
 const pending=useRef<PendingSubmission|null>(initialPending),queryRef=useRef(queryID),docsSequence=useRef(0);
 const [queryRevision,setQueryRevision]=useState(0);const queryGeneration=useRef(0);
 const sourceRequest=useRef<AbortController|null>(null);
 useEffect(()=>()=>scope.close(),[scope]);
 async function refreshDocs(){
  const sequence=++docsSequence.current;
  try {
   const result=await scope.json<{documents:Doc[]}>(apiUrl('/documents'));
   if(!scope.active||sequence!==docsSequence.current)return;
   setDocs(result.documents);setConnection('已连接');
  }catch(e){if(scope.active&&sequence===docsSequence.current){setConnection('连接异常');setError(errorText(e))}}
 }
 async function refreshHistory(append=false){
  if(historyBusy)return;setHistoryBusy(true);
  try{
   const result=await scope.json<{queries:HistoryItem[];next_cursor:string}>(`${apiUrl('/queries')}?limit=20${append&&historyCursor?`&cursor=${encodeURIComponent(historyCursor)}`:''}`);
   if(scope.active){setHistory(old=>append?[...old,...result.queries.filter(x=>!old.some(y=>y.id===x.id))]:result.queries);setHistoryCursor(result.next_cursor)}
  }catch(e){if(scope.active)setError(errorText(e))}finally{if(scope.active)setHistoryBusy(false)}
 }
 function openHistory(id:string){closeSource();setError('');queryRef.current=id;setRun(null);setQueryID(id);setQueryRevision(++queryGeneration.current)}
 function suggest(text:string,build=false,sample='financial.pdf'){setQuestion(text);setBuildFacts(build);if(build)setExecutionPolicy('COLD_ALLOWED');setError('');const chosen=docs.find(d=>d.state==='READY'&&d.title===sample);setSelected(chosen?[chosen.id]:docs.filter(d=>d.state==='READY').map(d=>d.id))}
 function selectDemoStep(step:DemoStep){setQuestion(step.question);setBuildFacts(!!step.build_facts);setExecutionPolicy(step.execution_policy==='COLD_ALLOWED'?'COLD_ALLOWED':'HOT_ONLY');setSelected(step.document_ids);setError('')}
 useEffect(()=>{
  refreshDocs();refreshHistory();
  const t=setInterval(refreshDocs,2500);
  scope.json<{provider:string}>(healthUrl()).then(x=>{if(scope.active)setProvider(x.provider||'')}).catch(()=>{});
  return()=>clearInterval(t);
 },[scope,readOnly]);
 useEffect(()=>{if(run&&finished(run.state))void refreshHistory()},[run?.id,run?.state]);
 useEffect(()=>{
  if(!queryID)return;
  if(!readOnly)localStorage.setItem('m1:last-query',queryID);
  const abort=new AbortController();let lastSeq=0;
  const active=()=>scope.active&&!abort.signal.aborted&&queryRef.current===queryID&&queryGeneration.current===queryRevision;
  async function readRun(){
   const current=await scope.json<Run>(apiUrl(`/queries/${queryID}`),{signal:abort.signal});
   if(active())setRun(current);
   return current;
  }
  async function watch(){
   while(active()){
    try {
     const current=await readRun();if(!active())return;
     if(finished(current.state)){
      // DONE ends the answer stream; an already reserved call may settle later.
      if(!awaitingSettlement(current)&&!pendingJobs(current))return;
     }else{
      const stream=await scope.request(apiUrl(`/queries/${queryID}/events`),{signal:abort.signal,headers:{'Last-Event-ID':String(lastSeq)}});
      if(!stream.body)throw new Error('事件连接没有返回内容。');
      const reader=stream.body.getReader(),decoder=new TextDecoder();let buffer='',terminal=false,settled=false;
      try {
       while(active()&&!terminal){
        const part=await reader.read();if(part.done)break;
        buffer+=decoder.decode(part.value,{stream:true});let boundary;
        while((boundary=/\r?\n\r?\n/.exec(buffer))){
         const frame=buffer.slice(0,boundary.index);buffer=buffer.slice(boundary.index+boundary[0].length);
         const id=frame.match(/^id:\s*(\d+)/m);if(id)lastSeq=Number(id[1]);
         const kind=frame.match(/^event:\s*(.+)/m)?.[1].trim();
         if(['STATUS','ERROR','DONE','ANSWER_DELTA','USAGE'].includes(kind||'')){
          const updated=await readRun();if(!active())return;
          terminal=finished(updated.state);settled=terminal&&!awaitingSettlement(updated)&&!pendingJobs(updated);
          if(terminal)break;
         }
        }
       }
      }finally{await reader.cancel().catch(()=>{})}
      if(settled)return;
     }
    }catch(e){
     if(!active())return;
     setError(errorText(e));
     if(e instanceof APIError&&[401,403,404].includes(e.status)){
      queryRef.current='';setQueryID('');setRun(null);localStorage.removeItem('m1:last-query');return;
     }
    }
    await delay(1000,abort.signal);
   }
  }
  void watch().catch(()=>{});return()=>abort.abort();
 },[queryID,queryRevision,scope]);
 useEffect(()=>()=>{if(pdfURL)URL.revokeObjectURL(pdfURL.split('#')[0])},[pdfURL]);
 function closeSource(){sourceRequest.current?.abort();sourceRequest.current=null;setSource(null);setPdfURL('')}
 function changeTenant(next:string){scope.close();sourceRequest.current?.abort();onTokenChange(next)}
 async function upload(event:React.FormEvent){
  event.preventDefault();const selectedFile=file.current?.files?.[0];if(!selectedFile||uploading)return;
  setUploading(true);setError('');
  try {
   const form=new FormData();form.append('file',selectedFile);form.append('pages',scopePages);form.append('year',year);
   const result=await scope.json<{document_id:string}>(apiUrl('/documents'),{method:'POST',body:form});
   if(!scope.active)return;
   setSelected(v=>[...new Set([...v,result.document_id])]);await refreshDocs();
   if(scope.active&&file.current)file.current.value='';
  }catch(e){if(scope.active)setError(errorText(e))}finally{if(scope.active)setUploading(false)}
 }
 async function submit(event:React.FormEvent){
  event.preventDefault();if(submitLock.current)return;
  submitLock.current=true;setSubmitting(true);setError('');closeSource();
  const body=JSON.stringify({question,document_ids:[...selected].sort(),mode:'m3',execution_policy:executionPolicy,...(buildFacts?{build_facts:true}:{})});
  if(pending.current?.body!==body)pending.current={body,key:crypto.randomUUID()};
  const submission=pending.current;localStorage.setItem('m1:pending-query',JSON.stringify(submission));
  try {
   const response=await scope.json<{query_id:string}>(apiUrl('/queries'),{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':submission.key},body});
   if(!scope.active)return;
   // Keep the retry key until a query ID is confirmed and durably saved.
   localStorage.setItem('m1:last-query',response.query_id);
   pending.current=null;localStorage.removeItem('m1:pending-query');
   queryRef.current=response.query_id;setRun(null);setQueryID(response.query_id);
   setQueryRevision(++queryGeneration.current);
   if(readOnly)setDemoPosition(position=>position+1);
  }catch(e){if(scope.active)setError(errorText(e))}finally{submitLock.current=false;if(scope.active)setSubmitting(false)}
 }
 async function cancel(){
  if(!run)return;const id=run.id;
  try {
   await scope.request(apiUrl(`/queries/${id}/cancel`),{method:'POST'});
   if(!scope.active||queryRef.current!==id)return;
   // Let the single query watcher own state and fee updates.
   setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active&&queryRef.current===id)setError(errorText(e))}
 }
 async function resume(job:Job){
  if(!run||resumingJob)return;const id=run.id;setResumingJob(job.job_id);setError('');
  try{
   await scope.request(apiUrl(`/queries/${id}/jobs/${job.job_id}/resume`),{method:'POST'});
   if(scope.active&&queryRef.current===id)setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active&&queryRef.current===id)setError(errorText(e))}
  finally{if(scope.active)setResumingJob('')}
 }
 async function revoke(id:string){
  const previousQuery=queryRef.current;
  try {
   await scope.request(apiUrl(`/documents/${id}`),{method:'DELETE'});if(!scope.active)return;
   setSelected(v=>v.filter(x=>x!==id));closeSource();
   if(queryRef.current===previousQuery){queryRef.current='';setRun(null);setQueryID('');localStorage.removeItem('m1:last-query')}
   await refreshDocs();
  }catch(e){if(scope.active)setError(errorText(e))}
 }
 async function showSource(citation:Source){
  closeSource();setError('');const abort=new AbortController();sourceRequest.current=abort;
  const active=()=>scope.active&&!abort.signal.aborted&&sourceRequest.current===abort;
  try {
   const region=await scope.json<Source>(apiUrl(`/regions/${citation.region_id}`),{signal:abort.signal});
   if(!active())return;setSource({...region,quote:citation.quote});
   // The region carries an absolute source_url for the product API. Rebuild it
   // through apiUrl() so a deployment served under a subpath still resolves.
   const suffix=region.source_url.split('/api/v1')[1]?.split('#')[0];
   if(!suffix)throw new Error('来源地址缺少接口路径。');
   const response=await scope.request(apiUrl(suffix),{signal:abort.signal});
   const blob=await response.blob();if(!active())return;
   setPdfURL(URL.createObjectURL(blob)+`#page=${region.replay_page||region.page}`);
  }catch(e){if(active()){setSource(null);setPdfURL('');setError(errorText(e))}}
 }
 const busy=submitting||!!queryID&&(!run||!finished(run.state));const allReady=selected.length>0&&selected.every(id=>docs.some(d=>d.id===id&&d.state==='READY'));
 return <><header><a className="brand" href={assetUrl('')}>Crack<span>RAG</span></a><div className="header-meta"><span className="dot"/>{readOnly?'回放已就绪':connection}<span className="mode">{readOnly?'历史运行回放':provider==='deepseek'?'当前使用真实模型':'当前为模拟模式'}</span></div></header>
 <main>{notice}{!readOnly&&<div className="intro"><p className="eyebrow">FINANCIAL DOCUMENTS, CHECKABLE ANSWERS</p><h1>年报里的数字，先核对，再复用。</h1><p>提出一个具体问题，打开原页核对；证据不足时会说明缺什么，不猜数字。</p></div>}
 <div className={`layout${readOnly?' replay-layout':''}`}><aside><section className="card"><h2>文档库 <span className="count">{docs.length}</span></h2><button className="text-button" onClick={()=>changeTenant('')} aria-label={readOnly?'从头播放':'切换访问身份'}>{readOnly?'从头播放':'退出 / 切换访问身份'}</button>
 <form onSubmit={upload} className={`upload${readOnly?' demo-hidden':''}`}><label className="file-label">导入 PDF<input ref={file} type="file" accept="application/pdf,.pdf" aria-label="选择 PDF" required/></label><div className="two"><label className="label">页码范围<input value={scopePages} onChange={e=>setScopePages(e.target.value)} placeholder="全部，或 5,7,63,64" aria-label="页码范围"/></label><label className="label">报告年份<input value={year} onChange={e=>setYear(e.target.value)} placeholder="可选" aria-label="报告年份"/></label></div><p className="hint">最多 20 MB、32 个选页。保留原 PDF 页码；扫描页暂不支持。</p><button className="secondary full" disabled={uploading}>{uploading?'正在上传…':'上传文档'}</button></form>
 <div className="document-list">{docs.length===0?<div className="empty small">还没有可访问的文档。<br/>上传一份 PDF 开始。</div>:docs.map(d=><article className="document" key={d.id} data-document-id={d.id}><label><input type="checkbox" checked={selected.includes(d.id)} disabled={readOnly||d.state!=='READY'} onChange={e=>setSelected(v=>e.target.checked?[...v,d.id]:v.filter(x=>x!==d.id))}/><strong>{d.title}</strong></label><div className="document-meta"><span className={`status ${d.state.toLowerCase()}`}>{names[d.state]||'处理中'}</span>{!readOnly&&<button className="text-button" onClick={()=>revoke(d.id)} aria-label={`撤销访问 ${d.title}`}>撤销访问</button>}</div>{d.indexed_pages?.length>0&&<p className="hint">已索引：第 {d.indexed_pages.join('、')} 页</p>}{d.error_json&&<p className="inline-error">{reasonText(d.error_json.reason_code)}</p>}</article>)}</div>{!readOnly&&<p className="hint">撤销访问后不再用于查询；原文件与审计记录仍保留。</p>}</section><section className="card history-card"><div className="section-line"><h2>最近查询</h2><button className="text-button" onClick={()=>refreshHistory()} disabled={historyBusy}>刷新</button></div>{!history.length?<p className="hint">完成一次查询后，可从这里找回答案。</p>:history.map(item=><button className={`history-item ${queryID===item.id?'selected':''}`} key={item.id} onClick={()=>openHistory(item.id)}><strong>{item.question}</strong><span>{supportNames[item.answer_status]||names[item.state]||'处理中'} · {item.model_calls} 次{item.provider==='mock'?'模拟':'模型'}调用</span></button>)}{historyCursor&&<button className="secondary full" disabled={historyBusy} onClick={()=>refreshHistory(true)}>查看更多</button>}</section></aside>
 <div className="workspace">
  <section className="card demo-guide" aria-label="演示指南">
   <p className="eyebrow">{readOnly?'已记录的真实问答':'从提问到复用'}</p>
   <h2>{readOnly?'点开一次真实年报问答':'从原页核对，到下次直接复用'}</h2>
   {readOnly&&demoSteps?<>
    <p>按顺序选择一步，再点击“查看录制结果”。前四步来自同一轮真实运行；最后一步是单独的模拟缺证据示例。</p>
    <div className="suggestions">{demoSteps.map((step,index)=><button type="button" className="secondary" key={`${step.role}-${index}`} disabled={busy||index!==demoPosition} onClick={()=>selectDemoStep(step)}>{demoStepNames[step.role]||`第 ${index+1} 步`}</button>)}</div>
    <p className="hint">这里不连接模型；点击只回放当时的结果。原始真实运行视频和费用记录可从页面上方查看。</p>
   </>:<>
    <ol><li>导入<a href={assetUrl('samples/financial.pdf')} download>自制财务样例</a>，等待文档就绪。</li><li>首次提问后，打开原页核对数字与口径。</li><li>明确选择保存已核对数字，再问同一问题，看本次是否无需模型调用。</li><li>导入<a href={assetUrl('samples/ambiguous.pdf')} download>缺附注样例</a>，看系统如何说明证据不足。</li></ol>
    <div className="suggestions"><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年营业收入是多少？')}>① 看第一次回答</button><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年营业收入是多少？',true)}>② 保存可复用数字</button><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年营业收入是多少？')}>③ 再问同一问题</button><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年净利润是多少？',false,'ambiguous.pdf')}>④ 看证据不足</button></div>
    <p className="hint">保存数字可能产生额外模型费用；样例数字是自制材料，不是真实公司披露。</p>
   </>}
  </section>
  <section className="card query-card"><div className="section-line"><h2>{readOnly?'这一步问了什么':'向原文提问'}</h2><span className="hint">已选择 {selected.length} 份文档</span></div>
   <form onSubmit={submit}><textarea aria-label="问题" placeholder={readOnly?'先从上方选择演示步骤':'例如：样例控股2024年营业收入是多少？'} value={question} readOnly={readOnly} onChange={e=>setQuestion(e.target.value)} maxLength={1000} rows={4}/>
    {!readOnly&&buildFacts&&<label className="label">何时保存供后续复用<select aria-label="何时保存供后续复用" value={executionPolicy} disabled={busy} onChange={e=>setExecutionPolicy(e.target.value)}><option value="HOT_ONLY">谨慎执行：条件不合适就跳过（默认）</option><option value="COLD_ALLOWED">立即处理：可能增加本次费用</option></select></label>}
    <div className="query-actions">{!readOnly&&<label className="build-facts"><input type="checkbox" checked={buildFacts} onChange={e=>setBuildFacts(e.target.checked)} disabled={busy} aria-label="为以后提问保存数字"/> 为以后提问保存数字</label>}<span className="hint">{readOnly?'这不会产生新的模型请求或费用。':'证据不足会明确说明。'}</span>{!readOnly&&(busy||!!run&&pendingJobs(run))&&<button type="button" className="secondary" onClick={cancel} disabled={!run}>{busy?'取消':'取消后续处理'}</button>}<button type="submit" disabled={busy||!allReady||!question.trim()}>{submitting?'正在提交…':readOnly?'查看录制结果':'查阅并回答'}</button></div>
   </form>
  </section>
 {error&&<div role="alert" className="error"><strong>操作未完成</strong><span>{error}</span><button onClick={()=>setError('')} aria-label="关闭错误">×</button></div>}
 <section className="card answer-card" aria-live="polite"><div className="section-line"><h2>回答与证据</h2>{run&&<span className={`status ${run.state.toLowerCase()}`}>{names[run.state]||run.state}</span>}</div>
  {!run?<div className="empty"><span className="document-symbol">↗</span><h3>从一个具体问题开始</h3><p>回答将在这里出现，来源可逐条打开核对。</p></div>:<>
   <p className="asked">{run.question}</p><p className="hint">{run.provider==='deepseek'?'这次运行使用 DeepSeek':'这次运行使用模拟模型'}</p>
   {run.error&&<p role="alert" className="inline-error">{reasonText(run.error.reason_code)}</p>}
   {!run.answer&&!finished(run.state)&&<p className="waiting"><span className="pulse"/>正在查阅所选原文…</p>}
   {run.answer&&<>
    <div className="answer-verdict" data-status={run.answer.answer_validation?.status||'LEGACY'}><strong>{supportNames[run.answer.answer_validation?.status||'']||'请核对来源'}</strong><span>{answerStatusDescription(run)}</span></div>
     <div className="answer-text">{answerTextForDisplay(run.answer.text)}</div>
    <div className="metrics" aria-label="本次回答摘要"><div><strong>{(run.answer.evidence_summary.reused_facts?.length||0)>0?'直接复用':run.answer.answer_validation?.status==='INCONCLUSIVE'?'说明证据缺口':'核对原文'}</strong><span>答案路径</span></div><div><strong>{run.calls?.length||0} 次</strong><span>{run.provider==='mock'?'本次模拟调用':'本次模型调用'}</span></div><div><strong>{run.provider==='mock'?'无供应商费用':`¥${Number(run.cost?.known_estimated_subtotal||0).toFixed(6)}`}</strong><span>{Number(run.cost?.unknown_calls||0)>0?'仍有费用待核对':'已知用量费用估算'}</span></div><div><strong>{run.answer.evidence_summary.sources.length} 处</strong><span>可打开的原文来源</span></div></div>
    {run.answer.evidence_summary.unresolved.length>0&&<div className="unresolved"><strong>还缺哪些证据</strong>{run.answer.evidence_summary.unresolved.map((gap,index)=><p key={index}>{displayGap(gap)}</p>)}{[...new Set(run.answer.answer_validation?.items?.filter(item=>item.status!=='SUPPORTED').flatMap(item=>item.reasons)||[])].map(reason=><p className="hint" key={reason}>{reasonText(reason)}</p>)}</div>}
    <div className="sources">{run.answer.evidence_summary.sources.map((citation,index)=><button className="source-link" key={`${citation.region_id}-${index}`} onClick={()=>showSource(citation)}><span>{String(index+1).padStart(2,'0')}</span><div><strong>{citation.title}</strong><small>PDF 第 {citation.page} 页 · 打开原页核对</small></div><b>↗</b></button>)}</div>
   </>}
    {(pendingJobs(run)||!!run.diagnostics?.m3?.jobs?.length)&&<section className="structure-diagnostics" aria-label="后续复用准备"><h3>为下次提问准备数字</h3><p className="hint">{pendingJobs(run)?'回答已经显示，后台仍在核对；完成后才能供下次直接复用。':'后台处理已结束。'}</p>{run.diagnostics?.m3?.status==='unavailable'&&<p className="inline-error">暂时无法读取后台进度。请稍后从“最近查询”重新打开。</p>}{run.diagnostics?.m3?.jobs?.map(job=><div className="call" key={job.job_id}><strong>{jobNames[job.state]||'处理中'}</strong>{job.reason&&<p className="hint">{reasonText(job.reason)}</p>}{job.state==='RESULT_READY'&&<button type="button" className="secondary" disabled={!!resumingJob||readOnly} onClick={()=>resume(job)}>{resumingJob===job.job_id?'正在继续核对…':'继续核对已保存内容'}</button>}</div>)}</section>}
   <AnswerEvidence run={run}/><UsageDetails run={run}/><p className="run-id">刷新页面或从“最近查询”打开这次回答，不会自动重复调用模型。</p></>}
 </section>{source&&<section className="card source-card"><div className="section-line"><h2>回到原页 · 第 {source.page} 页</h2><button className="text-button" onClick={closeSource}>关闭</button></div><p>{source.title}</p>{source.quote&&<blockquote>{source.quote}</blockquote>}<p className="hint">请在原页同时核对公司、年份、列标题、单位与业务口径。</p>{pdfURL&&<Suspense fallback={<p className="hint">正在准备原页预览…</p>}><PDFSource url={pdfURL} page={source.replay_page||source.page} displayPage={source.page} bbox={source.bbox}/></Suspense>}{source.text&&<details className="source-extraction"><summary>查看提取的文字</summary><pre className="source-text">{source.text}</pre></details>}</section>}</div></div>
 </main><footer>CrackRAG · v0.1.0 · 可追溯的财务事实复用 <span>{readOnly?'静态回放不含登录凭据或新的模型调用':'文档范围与来源访问均受权限校验'}</span></footer></>;
}
if(import.meta.env.VITE_DEMO_REPLAY!=='1')createRoot(document.getElementById('root')!).render(<Root/>);
