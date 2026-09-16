import React, {useEffect, useRef, useState, lazy, Suspense} from 'react';
import {createRoot} from 'react-dom/client';
import {APIError, RequestScope, delay} from './requests';
import './style.css';
const PDFSource=lazy(()=>import('./pdf-source').then(m=>({default:m.PDFSource})));

type Doc={id:string;title:string;current_version_id:string;state:string;indexed_pages:number[];requested_pages:number[];error_json?:{reason_code:string};build_usage:unknown};
type Source={region_id:string;document_id:string;document_version_id:string;title:string;page:number;bbox:number[];text:string;quote?:string;source_url:string;context:unknown};
type Call={attempt_id:string;state:string;stage?:string;amount_cny:string|null;record?:{request_id:string;request_id_source:string;latency_ms:number;normalized_usage:unknown;cache:unknown;transport_failure:string|null}};
type Job={job_id:string;state:string;reason:string;batch_id:string;deadline_at:string;completed_at?:string};
type Run={id:string;question:string;state:string;provider:string;answer:{text:string;answer_validation?:{status:string;policy:string;items?:{status:string;reasons:string[]}[]};evidence_summary:{sources:Source[];unresolved:string[];calculations?:unknown[];structured_coverage?:{status:string};reused_facts?:{fact_id:string;report_id:string;origin:string;value:string;unit:string}[];raw_observation_region_ids?:string[]}}|null;error:{reason_code:string}|null;calls:Call[];cost:Record<string,unknown>;diagnostics?:{m3?:{status?:string;jobs?:Job[];pending_jobs?:number};[key:string]:unknown}};
type HistoryItem={id:string;question:string;state:string;provider:string;answer_status:string;model_calls:number;known_estimated_cny:string;created_at:string};
const names:Record<string,string>={QUEUED:'等待解析',PARSING:'正在解析与索引',READY:'可检索',FAILED:'未完成',INTERRUPTED:'已中断',RUNNING:'正在查阅原文',COMPLETED:'已完成',CANCELLED:'已取消',TIMED_OUT:'已超时'};
const reasons:Record<string,string>={INVALID_PDF:'文件不是有效 PDF。',OCR_REQUIRED_NOT_ENABLED:'所选页需要 OCR，请改选有文本层的页面。',DOCUMENT_NOT_READY:'请等待文档解析与索引完成。',NOT_FOUND:'文档或查询不可访问，可能已撤销权限。',UNAUTHENTICATED:'访问令牌无效。',MODEL_HTTP_ERROR:'模型请求失败，本次未自动重试。',USAGE_MISSING:'模型未返回用量，已停止后续调用。',COST_UNKNOWN:'费用无法确定，已停止后续调用。',DEADLINE_EXCEEDED:'已达到执行时限。',USER_CANCELLED:'已取消，已发生的调用仍会记入账本。'};
Object.assign(reasons,{CITATION_NOT_OBSERVED:'模型引用没有对应到实际读取的原文，已阻止展示。',SOURCE_UNIT_UNPROVEN:'原文缺少明确单位。',BUSINESS_SCOPE_UNPROVEN:'原文没有证明合并口径。',REQUIRED_BASIS_NOTE_MISSING:'所需口径附注未包含在来源中。',HOT_WINDOW_EXPIRED:'经验复用窗口已结束，本次没有继续后台调用。',NO_CACHE_EVIDENCE:'暂无可用的经验缓存依据。',M3_OUTCOME_UNKNOWN:'调用结果或费用待核对；已暂停新的真实请求。'});
const supportNames:Record<string,string>={SUPPORTED:'来源支持已核验',PARTIAL:'部分需求有支持',INCONCLUSIVE:'证据不足，未作确定回答',UNSUPPORTED:'超出首版支持范围'};
const finished=(state:string)=>['COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED'].includes(state);
type PendingSubmission={body:string;key:string};
function readPending():PendingSubmission|null {
 try {
  const saved=JSON.parse(localStorage.getItem('m1:pending-query')||'null');
  if(!saved)return null;
  const body=JSON.parse(saved.body);
  if(typeof saved.key==='string'&&saved.key&&typeof body.question==='string'&&Array.isArray(body.document_ids)&&body.document_ids.every((id:unknown)=>typeof id==='string'))return saved;
 }catch{/* A damaged draft must not prevent history from loading. */}
 localStorage.removeItem('m1:pending-query');return null;
}
const errorText=(error:unknown)=>error instanceof APIError?(reasons[error.reason]||`操作未完成：${error.reason}`):(error as Error).message;
const awaitingSettlement=(run:Run)=>run.calls?.some(call=>call.state==='RESERVED');
const pendingJobs=(run:Run)=>!!run.diagnostics?.m3?.pending_jobs||run.diagnostics?.m3?.status==='unavailable';
const jobNames:Record<string,string>={WAITING_PREFIX:'等待前缀证据',RUNNING:'后台抽取中',RESULT_READY:'候选已保存，待验证',COMMITTED:'已发布通过验证的事实',SKIPPED:'已跳过',FAILED:'后台未完成',OUTCOME_UNKNOWN:'调用结果或费用待核对'};

function M2Diagnostics({run}:{run:Run}){
 const evidence=run.answer?.evidence_summary;
 const reused=evidence?.reused_facts||[];
 return <details className="structure-diagnostics"><summary>结构复用与验证 <span>{evidence?.structured_coverage?.status||'未检查'}</span></summary>
 <p className="hint">本次复用 {reused.length} 条已验证事实；读取 {evidence?.raw_observation_region_ids?.length||0} 个原文区域。</p>
 {reused.map(f=><div className="call" key={f.fact_id}><strong>{f.origin==='DERIVED'?'派生计算 DERIVED':'原文披露 REPORTED'} · {f.value} {f.unit}</strong><small>事实 {f.fact_id}</small><small>验证报告 {f.report_id}</small></div>)}
 <pre>{JSON.stringify({coverage:evidence?.structured_coverage,validation:run.diagnostics},null,2)}</pre></details>;
}

function Root(){
 const [token,setToken]=useState(()=>sessionStorage.getItem('m1:access-token')||'');
 const [draftToken,setDraftToken]=useState('');
 function changeToken(next:string){
  localStorage.removeItem('m1:pending-query');localStorage.removeItem('m1:last-query');
  sessionStorage.setItem('m1:access-token',next);setToken(next);
 }
 if(!token)return <main className="login"><a className="brand" href="/">Crack<span>RAG</span></a><section className="card"><p className="eyebrow">LOCAL DOCUMENT WORKSPACE</p><h1>让答案有据可查。</h1><p>使用本机初始化时生成的访问令牌进入。文档、查询和费用按身份隔离。</p><form onSubmit={e=>{e.preventDefault();changeToken(draftToken.trim())}}><label className="label">本机访问令牌<input type="password" aria-label="本机访问令牌" autoComplete="off" value={draftToken} onChange={e=>setDraftToken(e.target.value)} required minLength={8}/></label><button type="submit">进入工作区</button></form><p className="hint">默认演示使用模拟模型，不需要供应商密钥。真实模式由维护者在服务端启用。</p></section></main>;
 return <App key={token} token={token} onTokenChange={changeToken}/>;
}

function App({token,onTokenChange}:{token:string;onTokenChange:(token:string)=>void}){
 const [scope]=useState(()=>new RequestScope(token));
 const [initialPending]=useState(readPending);
 const draft=initialPending?JSON.parse(initialPending.body):null;
 const [buildFacts,setBuildFacts]=useState(!!draft?.build_facts);
 const [executionPolicy,setExecutionPolicy]=useState(draft?.execution_policy==='COLD_ALLOWED'?'COLD_ALLOWED':'HOT_ONLY');
 const [resumingJob,setResumingJob]=useState('');
 const [docs,setDocs]=useState<Doc[]>([]),[selected,setSelected]=useState<string[]>(draft?.document_ids||[]),[question,setQuestion]=useState(draft?.question||'');
 const [history,setHistory]=useState<HistoryItem[]>([]),[historyCursor,setHistoryCursor]=useState(''),[historyBusy,setHistoryBusy]=useState(false);
 const [run,setRun]=useState<Run|null>(null),[queryID,setQueryID]=useState(()=>localStorage.getItem('m1:last-query')||'');
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
   const result=await scope.json<{documents:Doc[]}>('/api/v1/documents');
   if(!scope.active||sequence!==docsSequence.current)return;
   setDocs(result.documents);setConnection('已连接');
  }catch(e){if(scope.active&&sequence===docsSequence.current){setConnection('连接异常');setError(errorText(e))}}
 }
 async function refreshHistory(append=false){
  if(historyBusy)return;setHistoryBusy(true);
  try{
   const result=await scope.json<{queries:HistoryItem[];next_cursor:string}>(`/api/v1/queries?limit=20${append&&historyCursor?`&cursor=${encodeURIComponent(historyCursor)}`:''}`);
   if(scope.active){setHistory(old=>append?[...old,...result.queries.filter(x=>!old.some(y=>y.id===x.id))]:result.queries);setHistoryCursor(result.next_cursor)}
  }catch(e){if(scope.active)setError(errorText(e))}finally{if(scope.active)setHistoryBusy(false)}
 }
 function openHistory(id:string){closeSource();setError('');queryRef.current=id;setRun(null);setQueryID(id);setQueryRevision(++queryGeneration.current)}
 function suggest(text:string,build=false,sample='financial.pdf'){setQuestion(text);setBuildFacts(build);if(build)setExecutionPolicy('COLD_ALLOWED');const chosen=docs.find(d=>d.state==='READY'&&d.title===sample);if(chosen){setSelected([chosen.id]);setError('')}else{setSelected([]);setError(`请先上传并完成 ${sample} 的解析。`)}}
 useEffect(()=>{
  sessionStorage.setItem('m1:access-token',token);refreshDocs();refreshHistory();
  const t=setInterval(refreshDocs,2500);
  scope.json<{provider:string}>('/healthz').then(x=>{if(scope.active)setProvider(x.provider||'')}).catch(()=>{});
  return()=>clearInterval(t);
 },[scope,token]);
 useEffect(()=>{if(run&&finished(run.state))void refreshHistory()},[run?.id,run?.state]);
 useEffect(()=>{
  if(!queryID)return;
  localStorage.setItem('m1:last-query',queryID);
  const abort=new AbortController();let lastSeq=0;
  const active=()=>scope.active&&!abort.signal.aborted&&queryRef.current===queryID&&queryGeneration.current===queryRevision;
  async function readRun(){
   const current=await scope.json<Run>(`/api/v1/queries/${queryID}`,{signal:abort.signal});
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
      const stream=await scope.request(`/api/v1/queries/${queryID}/events`,{signal:abort.signal,headers:{'Last-Event-ID':String(lastSeq)}});
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
   const result=await scope.json<{document_id:string}>('/api/v1/documents',{method:'POST',body:form});
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
   const response=await scope.json<{query_id:string}>('/api/v1/queries',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':submission.key},body});
   if(!scope.active)return;
   // Keep the retry key until a query ID is confirmed and durably saved.
   localStorage.setItem('m1:last-query',response.query_id);
   pending.current=null;localStorage.removeItem('m1:pending-query');
   queryRef.current=response.query_id;setRun(null);setQueryID(response.query_id);
   setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active)setError(errorText(e))}finally{submitLock.current=false;if(scope.active)setSubmitting(false)}
 }
 async function cancel(){
  if(!run)return;const id=run.id;
  try {
   await scope.request(`/api/v1/queries/${id}/cancel`,{method:'POST'});
   if(!scope.active||queryRef.current!==id)return;
   // Let the single query watcher own state and fee updates.
   setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active&&queryRef.current===id)setError(errorText(e))}
 }
 async function resume(job:Job){
  if(!run||resumingJob)return;const id=run.id;setResumingJob(job.job_id);setError('');
  try{
   await scope.request(`/api/v1/queries/${id}/jobs/${job.job_id}/resume`,{method:'POST'});
   if(scope.active&&queryRef.current===id)setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active&&queryRef.current===id)setError(errorText(e))}
  finally{if(scope.active)setResumingJob('')}
 }
 async function revoke(id:string){
  const previousQuery=queryRef.current;
  try {
   await scope.request(`/api/v1/documents/${id}`,{method:'DELETE'});if(!scope.active)return;
   setSelected(v=>v.filter(x=>x!==id));closeSource();
   if(queryRef.current===previousQuery){queryRef.current='';setRun(null);setQueryID('');localStorage.removeItem('m1:last-query')}
   await refreshDocs();
  }catch(e){if(scope.active)setError(errorText(e))}
 }
 async function showSource(citation:Source){
  closeSource();setError('');const abort=new AbortController();sourceRequest.current=abort;
  const active=()=>scope.active&&!abort.signal.aborted&&sourceRequest.current===abort;
  try {
   const region=await scope.json<Source>(`/api/v1/regions/${citation.region_id}`,{signal:abort.signal});
   if(!active())return;setSource({...region,quote:citation.quote});
   const response=await scope.request(region.source_url.split('#')[0],{signal:abort.signal});
   const blob=await response.blob();if(!active())return;
   setPdfURL(URL.createObjectURL(blob)+`#page=${region.page}`);
  }catch(e){if(active()){setSource(null);setPdfURL('');setError(errorText(e))}}
 }
 const busy=submitting||!!queryID&&(!run||!finished(run.state));const allReady=selected.length>0&&selected.every(id=>docs.some(d=>d.id===id&&d.state==='READY'));
 return <><header><a className="brand" href="/">Crack<span>RAG</span></a><div className="header-meta"><span className="dot"/>{connection}<span className="mode">{provider==='deepseek'?'新查询：真实模型':'新查询：模拟模式'}</span></div></header>
 <main><div className="intro"><p className="eyebrow">DOCUMENT WORKSPACE</p><h1>从有据可查，到有效复用。</h1><p>先核验来源，再发布事实。每次复用都能回到原文、验证报告与调用账本。</p></div>
 <div className="layout"><aside><section className="card"><h2>文档库 <span className="count">{docs.length}</span></h2><button className="text-button" onClick={()=>changeTenant('')} aria-label="切换访问身份">退出 / 切换访问身份</button>
 <form onSubmit={upload} className="upload"><label className="file-label">导入 PDF<input ref={file} type="file" accept="application/pdf,.pdf" aria-label="选择 PDF" required/></label><div className="two"><label className="label">页码范围<input value={scopePages} onChange={e=>setScopePages(e.target.value)} placeholder="全部，或 5,7,63,64" aria-label="页码范围"/></label><label className="label">报告年份<input value={year} onChange={e=>setYear(e.target.value)} placeholder="可选" aria-label="报告年份"/></label></div><p className="hint">最多 20 MB、32 个选页。保留原 PDF 页码；扫描页暂不支持。</p><button className="secondary full" disabled={uploading}>{uploading?'正在上传…':'上传文档'}</button></form>
 <div className="document-list">{docs.length===0?<div className="empty small">还没有可访问的文档。<br/>上传一份 PDF 开始。</div>:docs.map(d=><article className="document" key={d.id}><label><input type="checkbox" checked={selected.includes(d.id)} disabled={d.state!=='READY'} onChange={e=>setSelected(v=>e.target.checked?[...v,d.id]:v.filter(x=>x!==d.id))}/><strong>{d.title}</strong></label><div className="document-meta"><span className={`status ${d.state.toLowerCase()}`}>{names[d.state]||d.state}</span><button className="text-button" onClick={()=>revoke(d.id)} aria-label={`撤销访问 ${d.title}`}>撤销访问</button></div>{d.indexed_pages?.length>0&&<p className="hint">已索引：第 {d.indexed_pages.join('、')} 页</p>}{d.error_json&&<p className="inline-error">{reasons[d.error_json.reason_code]||d.error_json.reason_code}</p>}</article>)}</div><p className="hint">撤销访问后不再用于查询；原文件与审计记录仍保留。</p></section><section className="card history-card"><div className="section-line"><h2>最近查询</h2><button className="text-button" onClick={()=>refreshHistory()} disabled={historyBusy}>刷新</button></div>{!history.length?<p className="hint">完成一次查询后，可从这里找回答案与后台任务。</p>:history.map(item=><button className={`history-item ${queryID===item.id?'selected':''}`} key={item.id} onClick={()=>openHistory(item.id)}><strong>{item.question}</strong><span>{supportNames[item.answer_status]||names[item.state]||item.state} · {item.model_calls} 次{item.provider==='mock'?'模拟':'模型'}调用</span></button>)}{historyCursor&&<button className="secondary full" disabled={historyBusy} onClick={()=>refreshHistory(true)}>查看更多</button>}</section></aside>
 <div className="workspace"><section className="card demo-guide" aria-label="演示指南"><p className="eyebrow">TRY THE COMPLETE LOOP</p><h2>从原文答案到可复用事实</h2><ol><li>导入<a href="/samples/financial.pdf" download>自制财务样例</a>，等待索引完成。</li><li>首问后打开来源核对，再显式构建事实。</li><li>构建发布后再次提问，观察 FULL 与零模型调用。</li><li>导入<a href="/samples/ambiguous.pdf" download>缺附注样例</a>，查看证据不足的处理。</li><li>按发行包中的恢复演示步骤中断并恢复后台工作。</li></ol><div className="suggestions"><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年营业收入是多少？')}>① 首问</button><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年营业收入是多少？',true)}>② 显式构建</button><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年营业收入是多少？')}>③ 复用查询</button><button className="secondary" disabled={busy} onClick={()=>suggest('样例控股2024年净利润是多少？',false,'ambiguous.pdf')}>④ 缺证据示例</button></div><p className="hint">引导构建显式允许冷执行，真实模式下会额外计费。快捷按钮仅选择对应样例；示例数值是合成材料，不是真实公司披露。</p></section><section className="card query-card"><div className="section-line"><h2>向原文提问</h2><span className="hint">已选择 {selected.length} 份文档</span></div><form onSubmit={submit}><textarea aria-label="问题" placeholder="例如：样例控股2024年营业收入是多少？" value={question} onChange={e=>setQuestion(e.target.value)} maxLength={1000} rows={4}/>{buildFacts&&<label className="label">后台构建策略<select aria-label="后台构建策略" value={executionPolicy} disabled={busy} onChange={e=>setExecutionPolicy(e.target.value)}><option value="HOT_ONLY">预计缓存可复用时构建（默认）</option><option value="COLD_ALLOWED">允许冷执行对照（单独计费）</option></select></label>}<div className="query-actions"><label className="build-facts"><input type="checkbox" checked={buildFacts} onChange={e=>setBuildFacts(e.target.checked)} disabled={busy} aria-label="构建可复用事实"/> 构建可复用事实</label><span className="hint">证据不足时会明确说明。</span>{(busy||!!run&&pendingJobs(run))&&<button type="button" className="secondary" onClick={cancel} disabled={!run}>{busy?'取消':'取消后台任务'}</button>}<button type="submit" disabled={busy||!allReady||!question.trim()}>{submitting?'正在提交…':'查阅并回答'}</button></div></form></section>
 {error&&<div role="alert" className="error"><strong>操作未完成</strong><span>{error}</span><button onClick={()=>setError('')} aria-label="关闭错误">×</button></div>}
 <section className="card answer-card" aria-live="polite"><div className="section-line"><h2>回答与证据</h2>{run&&<span className={`status ${run.state.toLowerCase()}`}>{names[run.state]||run.state}</span>}</div>
 {!run?<div className="empty"><span className="document-symbol">↗</span><h3>从一个具体问题开始</h3><p>回答将在这里出现，来源可逐条打开核验。</p></div>:<><p className="asked">{run.question}</p><p className="hint">本次回答：{run.provider==='deepseek'?'DeepSeek 真实模型':'模拟链路验收'}</p>{run.error&&<p role="alert" className="inline-error">{reasons[run.error.reason_code]||run.error.reason_code}</p>}{!run.answer&&!finished(run.state)&&<p className="waiting"><span className="pulse"/>正在检索并读取授权原文…</p>}{run.answer&&<><div className="answer-verdict" data-status={run.answer.answer_validation?.status||'LEGACY'}><strong>{supportNames[run.answer.answer_validation?.status||'']||'历史回答：请核对来源'}</strong><span>{(run.answer.evidence_summary.reused_facts?.length||0)>0?'包含已发布且当前有效的事实复用':'原文回答校验不等于正式事实发布'}</span></div><div className="answer-text">{run.answer.text}</div><div className="metrics" aria-label="本次执行摘要"><div><strong>{run.answer.evidence_summary.structured_coverage?.status||'原文'}</strong><span>结构覆盖</span></div><div><strong>{run.answer.evidence_summary.reused_facts?.length||0}</strong><span>复用事实</span></div><div><strong>{run.calls?.length||0}</strong><span>{run.provider==='mock'?'模拟调用':'模型调用'}</span></div><div><strong>{run.provider==='mock'?'不调用供应商':`¥${Number(run.cost.known_estimated_subtotal||0).toFixed(6)}`}</strong><span>{Number(run.cost.unknown_calls||0)>0?'有未决费用，保留占用':'已知用量费用估算'}</span></div></div>{run.answer.evidence_summary.unresolved.length>0&&<div className="unresolved"><strong>证据缺口</strong>{run.answer.evidence_summary.unresolved.map((x,i)=><p key={i}>{x}</p>)}{[...new Set(run.answer.answer_validation?.items?.filter(x=>x.status!=='SUPPORTED').flatMap(x=>x.reasons)||[])].map(reason=><p className="hint" key={reason}>{reasons[reason]||`核验原因：${reason}`}</p>)}</div>}<div className="sources">{run.answer.evidence_summary.sources.map((s,i)=><button className="source-link" key={`${s.region_id}-${i}`} onClick={()=>showSource(s)}><span>{String(i+1).padStart(2,'0')}</span><div><strong>{s.title}</strong><small>PDF 第 {s.page} 页 · 查看来源区域</small></div><b>↗</b></button>)}</div></>}
 {run.diagnostics?.m3&&<section className="structure-diagnostics" aria-label="后台事实构建"><h3>后台事实构建</h3><p className="hint">{pendingJobs(run)?'回答结束后仍跟踪后台任务与费用。':run.diagnostics.m3.jobs?.length?'后台任务已结束。':'本次未请求后台构建。'} 前台回答和后台发布分别记录。</p>{run.diagnostics.m3.status==='unavailable'&&<p className="inline-error">后台状态暂不可读取，正在重新连接。</p>}{run.diagnostics.m3.jobs?.map(job=><div className="call" key={job.job_id}><strong>{jobNames[job.state]||job.state}</strong><small>任务 {job.job_id}</small>{job.reason&&<p className="hint">{reasons[job.reason]||job.reason}</p>}{job.state==='RESULT_READY'&&<button type="button" className="secondary" disabled={!!resumingJob} onClick={()=>resume(job)}>{resumingJob===job.job_id?'正在续验…':'继续验证已保存候选'}</button>}</div>)}</section>}
 <M2Diagnostics run={run}/><details className="usage"><summary>用量、费用与执行记录 <span>{run.calls?.length||0} 次调用</span></summary><p className="hint">费用为估算；解析和本地 Embedding 建设开销在文档记录中单列，本地资源费用未核算。</p><pre>{JSON.stringify(run.cost,null,2)}</pre>{run.calls?.map(call=><div className="call" key={call.attempt_id}><strong>{call.state}{call.stage?` · ${call.stage}`:''}</strong><small>调用 {call.attempt_id}</small><pre>{JSON.stringify({amount_cny:call.amount_cny,request_id:call.record?.request_id,request_id_source:call.record?.request_id_source,latency_ms:call.record?.latency_ms,usage:call.record?.normalized_usage,cache:call.record?.cache,failure:call.record?.transport_failure},null,2)}</pre></div>)}</details><p className="run-id">查询 {run.id} · 刷新页面可恢复</p></>}
 </section>{source&&<section className="card source-card"><div className="section-line"><h2>来源核验 · 第 {source.page} 页</h2><button className="text-button" onClick={closeSource}>关闭</button></div><p>{source.title}</p>{source.quote&&<blockquote>{source.quote}</blockquote>}<h3>区域原文</h3><pre className="source-text">{source.text}</pre><p className="hint">原文本块或表格范围：{source.bbox.map(v=>v.toFixed(1)).join(', ')}。坐标单位为 PDF 点，原点在左上。</p><details><summary>表头、单位与相邻页上下文</summary><pre>{JSON.stringify(source.context,null,2)}</pre></details>{pdfURL&&<Suspense fallback={<p className="hint">正在准备原页预览…</p>}><PDFSource url={pdfURL} page={source.page} bbox={source.bbox}/></Suspense>}</section>}</div></div>
 </main><footer>CrackRAG · v0.1.0 · 可追溯的财务事实复用 <span>文档范围与来源访问均受权限校验</span></footer></>;
}
createRoot(document.getElementById('root')!).render(<Root/>);
