import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {APIError, RequestScope} from './requests';
import {apiUrl, assetUrl, healthUrl} from './urls';
import './style.css';
import {expandFollowup,finished,pendingJobs,reasonText} from './workspace-model';
import type {DemoStep,Doc,Job,Run,Source} from './workspace-model';
import {AnswerPanel,DemoGuide,DocumentLibrary,HistoryPanel,LedgerSummary,QueryComposer,SourcePanel,FollowupPanel,WorkspaceHeader} from './workspace-ui';
import type {OpenSource} from './workspace-ui';
import {useDocumentPreflight} from './use-document-preflight';
import {useHistory} from './use-history';
import {useQueryWatch} from './use-query-watch';
import {pageSelectionError} from './pdf-page-suggestions';

// The replay build must never resume a query id left behind by an earlier visit:
// its frozen sequence is the entry point, not the previous session's last run.
const routeQuery=()=>/^#run=([0-9a-f-]{36})$/i.exec(window.location.hash)?.[1]||'';
const restoredQuery=()=>import.meta.env.VITE_DEMO_REPLAY==='1'?'':routeQuery()||localStorage.getItem('m1:last-query')||'';
function writeRoute(id:string){window.history.pushState(null,'',id?`#run=${id}`:window.location.pathname+window.location.search)}
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

function Root(){
 const [token,setToken]=useState(()=>sessionStorage.getItem('m1:access-token')||'');
 const [draftToken,setDraftToken]=useState('');
 function changeToken(next:string){
  localStorage.removeItem('m1:pending-query');localStorage.removeItem('m1:last-query');
  sessionStorage.removeItem('crackrag:history-aliases');
  if(!next&&!sessionStorage.getItem('m1:auth-expired'))writeRoute('');
  if(next)sessionStorage.removeItem('m1:auth-expired');
  sessionStorage.setItem('m1:access-token',next);setToken(next);
 }
 if(!token)return <main className="login"><a className="brand" href={assetUrl('')}>Crack<span>RAG</span></a><section className="card"><p className="eyebrow">LOCAL DOCUMENT WORKSPACE</p><h1>让答案有据可查。</h1><p>使用本机初始化时生成的访问令牌进入。文档、查询和费用按身份隔离。</p>{sessionStorage.getItem('m1:auth-expired')&&<p role="alert" className="inline-error">访问令牌已失效。请重新输入令牌；原查询不会自动重复提交。</p>}<form onSubmit={e=>{e.preventDefault();changeToken(draftToken.trim())}}><label className="label">本机访问令牌<input type="password" aria-label="本机访问令牌" autoComplete="off" value={draftToken} onChange={e=>setDraftToken(e.target.value)} required minLength={8}/></label><button type="submit">进入工作区</button></form><p className="hint">默认演示使用模拟模型，不需要供应商密钥。真实模式由维护者在服务端启用。</p></section></main>;
 return <CrackRAGWorkspace key={token} scope={new RequestScope(token)} onTokenChange={changeToken}/>;
}

export function CrackRAGWorkspace({scope,onTokenChange,readOnly=false,notice,demoSteps}:{scope:{active:boolean;close:()=>void;request:(path:string,options?:RequestInit)=>Promise<Response>;json:<T>(path:string,options?:RequestInit)=>Promise<T>};onTokenChange:(token:string)=>void;readOnly?:boolean;notice?:React.ReactNode;demoSteps?:DemoStep[]}){
 const [initialPending]=useState(()=>readOnly?null:readPending());
 const draft=initialPending?JSON.parse(initialPending.body):null;
 const [buildFacts,setBuildFacts]=useState(!!draft?.build_facts);
 const [executionPolicy,setExecutionPolicy]=useState(draft?.execution_policy==='COLD_ALLOWED'?'COLD_ALLOWED':'HOT_ONLY');
 const [resumingJob,setResumingJob]=useState('');
 const [docs,setDocs]=useState<Doc[]>([]),[selected,setSelected]=useState<string[]>(draft?.document_ids||[]),[question,setQuestion]=useState(draft?.question||'');
 const [demoPosition,setDemoPosition]=useState(0);
 const [run,setRun]=useState<Run|null>(null),[queryID,setQueryID]=useState(restoredQuery);
 const [error,setError]=useState(''),[uploading,setUploading]=useState(false),[submitting,setSubmitting]=useState(false);
 const [toast,setToast]=useState('');
 const [scopePages,setScopePages]=useState(''),[year,setYear]=useState('');
 const {pdfPages,fileChecking,fileError,scanNotice,suggestedPages,scanProgress,scanning,previewURL,inspectFile,resetFile}=useDocumentPreflight();
 const [openedSources,setOpenedSources]=useState<OpenSource[]>([]),[selectedSourceID,setSelectedSourceID]=useState('');
 const [followupOpen,setFollowupOpen]=useState(false),[followupDraft,setFollowupDraft]=useState('');
 const [theme,setTheme]=useState(()=>localStorage.getItem('crackrag:theme')||'light');
 const [provider,setProvider]=useState(''),[connection,setConnection]=useState('连接中');
 const file=useRef<HTMLInputElement>(null),submitLock=useRef(false);
 const pending=useRef<PendingSubmission|null>(initialPending),queryRef=useRef(queryID),docsSequence=useRef(0);
 const [queryRevision,setQueryRevision]=useState(0);const queryGeneration=useRef(0);
 const sourceRequest=useRef<AbortController|null>(null),sourceURLs=useRef<Map<string,string>>(new Map());
 useEffect(()=>{document.documentElement.dataset.theme=theme;localStorage.setItem('crackrag:theme',theme)},[theme]);
 useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),4500);return()=>clearTimeout(timer)},[toast]);
 useEffect(()=>()=>{sourceRequest.current?.abort();sourceURLs.current.forEach(url=>URL.revokeObjectURL(url.split('#')[0]))},[]);
 function handleAuth(error:unknown){if(!readOnly&&error instanceof APIError&&error.status===401){sessionStorage.setItem('m1:auth-expired','1');onTokenChange('');return true}return false}
 const {history,historyCursor,historyBusy,historySummary,historySearch,aliases,refreshHistory,searchHistory,renameHistory}=useHistory(scope,setError,handleAuth,setToast);
 useEffect(()=>()=>scope.close(),[scope]);
 async function refreshDocs(){
  const sequence=++docsSequence.current;
  try {
   const result=await scope.json<{documents:Doc[]}>(apiUrl('/documents'));
   if(!scope.active||sequence!==docsSequence.current)return;
   setDocs(result.documents);setConnection('已连接');
  }catch(e){if(scope.active&&sequence===docsSequence.current){if(handleAuth(e))return;setConnection('连接异常');setError(errorText(e))}}
 }
 function openHistory(id:string){closeSources();setFollowupOpen(false);queryRef.current=id;setRun(null);setQueryID(id);setQueryRevision(++queryGeneration.current);if(!readOnly)writeRoute(id)}
 function suggest(text:string,build=false,sample='financial.pdf'){
  const chosen=docs.find(d=>d.state==='READY'&&d.title===sample);
  if(!chosen){setSelected([]);setError(`请先导入上方的“${sample==='ambiguous.pdf'?'缺附注':'自制财务'}样例”，等待它变为“可检索”后再选择这一步。`);return}
  setQuestion(text);setBuildFacts(build);if(build)setExecutionPolicy('COLD_ALLOWED');setSelected([chosen.id]);
 }
 function selectDemoStep(step:DemoStep){setQuestion(step.question);setBuildFacts(!!step.build_facts);setExecutionPolicy(step.execution_policy==='COLD_ALLOWED'?'COLD_ALLOWED':'HOT_ONLY');setSelected(step.document_ids)}
 useEffect(()=>{
  refreshDocs();refreshHistory();
  const t=setInterval(refreshDocs,2500);
  scope.json<{provider:string}>(healthUrl()).then(x=>{if(scope.active)setProvider(x.provider||'')}).catch(()=>{});
  return()=>clearInterval(t);
 },[scope,readOnly]);
 useEffect(()=>{
  if(readOnly)return;
  const restore=()=>{const id=routeQuery();if(id===queryRef.current)return;closeSources();queryRef.current=id;setRun(null);setQueryID(id);setQueryRevision(++queryGeneration.current)};
  window.addEventListener('popstate',restore);window.addEventListener('hashchange',restore);
  return()=>{window.removeEventListener('popstate',restore);window.removeEventListener('hashchange',restore)};
 },[readOnly]);
 useEffect(()=>{if(run&&finished(run.state))void refreshHistory()},[run?.id,run?.state]);
 useQueryWatch({scope,queryID,revision:queryRevision,generation:queryGeneration,queryRef,readOnly,setRun,setQueryID,onError:setError,onUnauthorized:handleAuth});
 function closeSources(){sourceRequest.current?.abort();sourceRequest.current=null;sourceURLs.current.forEach(url=>URL.revokeObjectURL(url.split('#')[0]));sourceURLs.current.clear();setOpenedSources([]);setSelectedSourceID('')}
 function closeSource(id:string){sourceRequest.current?.abort();sourceRequest.current=null;sourceURLs.current.get(id)&&URL.revokeObjectURL(sourceURLs.current.get(id)!.split('#')[0]);sourceURLs.current.delete(id);setOpenedSources(old=>old.filter(item=>item.region.region_id!==id&&item.pdfURL));setSelectedSourceID(old=>old===id?(openedSources.find(item=>item.region.region_id!==id&&item.pdfURL)?.region.region_id||''):old)}
 function changeTenant(next:string){scope.close();sourceRequest.current?.abort();onTokenChange(next)}
 async function upload(event:React.FormEvent){
  event.preventDefault();const selectedFile=file.current?.files?.[0];if(!selectedFile||uploading)return;
  const pageProblem=pageSelectionError(scopePages,pdfPages);if(pageProblem){setError(pageProblem);return}
  setUploading(true);
  try {
   const form=new FormData();form.append('file',selectedFile);form.append('pages',scopePages);form.append('year',year);
   const result=await scope.json<{document_id:string}>(apiUrl('/documents'),{method:'POST',body:form});
   if(!scope.active)return;
   setSelected(v=>[...new Set([...v,result.document_id])]);await refreshDocs();
   if(scope.active&&file.current)file.current.value='';resetFile();setScopePages('');
   setToast('文档已上传，解析完成后即可提问。');
  }catch(e){if(scope.active&&!handleAuth(e))setError(errorText(e))}finally{if(scope.active)setUploading(false)}
 }
 async function submit(event:React.FormEvent){
  event.preventDefault();if(submitLock.current)return;
  submitLock.current=true;setSubmitting(true);closeSources();setFollowupOpen(false);
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
   if(!readOnly)writeRoute(response.query_id);
   if(readOnly)setDemoPosition(position=>position+1);
  }catch(e){if(scope.active&&!handleAuth(e))setError(errorText(e))}finally{submitLock.current=false;if(scope.active)setSubmitting(false)}
 }
 async function cancel(){
  if(!run)return;const id=run.id;
  try {
   await scope.request(apiUrl(`/queries/${id}/cancel`),{method:'POST'});
   if(!scope.active||queryRef.current!==id)return;
   // Let the single query watcher own state and fee updates.
   setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active&&queryRef.current===id&&!handleAuth(e))setError(errorText(e))}
 }
 async function resume(job:Job){
  if(!run||resumingJob)return;const id=run.id;setResumingJob(job.job_id);
  try{
   await scope.request(apiUrl(`/queries/${id}/jobs/${job.job_id}/resume`),{method:'POST'});
   if(scope.active&&queryRef.current===id)setQueryRevision(++queryGeneration.current);
  }catch(e){if(scope.active&&queryRef.current===id&&!handleAuth(e))setError(errorText(e))}
  finally{if(scope.active)setResumingJob('')}
 }
 async function revoke(id:string){
  const previousQuery=queryRef.current;
  try {
   await scope.request(apiUrl(`/documents/${id}`),{method:'DELETE'});if(!scope.active)return;
   setSelected(v=>v.filter(x=>x!==id));closeSources();
   if(queryRef.current===previousQuery){queryRef.current='';setRun(null);setQueryID('');localStorage.removeItem('m1:last-query')}
   await refreshDocs();
  }catch(e){if(scope.active&&!handleAuth(e))setError(errorText(e))}
 }
 async function showSource(citation:Source){
  const existing=openedSources.find(item=>item.region.region_id===citation.region_id);
  if(existing){setSelectedSourceID(citation.region_id);document.getElementById('source-panel')?.scrollIntoView({behavior:'smooth',block:'start'});return}
  sourceRequest.current?.abort();setOpenedSources(old=>old.filter(item=>item.pdfURL));const abort=new AbortController();sourceRequest.current=abort;setSelectedSourceID(citation.region_id);
  const active=()=>scope.active&&!abort.signal.aborted&&sourceRequest.current===abort;
  try {
   const region=await scope.json<Source>(apiUrl(`/regions/${citation.region_id}`),{signal:abort.signal});
   if(!active())return;const fullRegion={...region,quote:citation.quote};setOpenedSources(old=>[...old,{region:fullRegion,pdfURL:''}]);requestAnimationFrame(()=>document.getElementById('source-panel')?.scrollIntoView({behavior:'smooth',block:'start'}));
   // The region carries an absolute source_url for the product API. Rebuild it
   // through apiUrl() so a deployment served under a subpath still resolves.
   const suffix=region.source_url.split('/api/v1')[1]?.split('#')[0];
   if(!suffix)throw new Error('来源地址缺少接口路径。');
   const response=await scope.request(apiUrl(suffix),{signal:abort.signal});
   const blob=await response.blob();if(!active())return;
   const url=URL.createObjectURL(blob)+`#page=${region.replay_page||region.page}`;sourceURLs.current.set(citation.region_id,url);
   setOpenedSources(old=>old.map(item=>item.region.region_id===citation.region_id?{...item,pdfURL:url}:item));
  }catch(e){if(active()&&!handleAuth(e)){setOpenedSources(old=>old.filter(item=>item.region.region_id!==citation.region_id));setError(errorText(e))}}
 }
 function prepareFollowup(event:React.FormEvent){
  event.preventDefault();if(!run)return;const expanded=expandFollowup(run.question,followupDraft);
  if(!expanded){setError('这句追问无法安全补全。请写出明确的公司、年份和指标，再提交。');return}
  const matches=docs.filter(doc=>run.document_version_ids?.includes(doc.current_version_id)&&doc.state==='READY');
  if(!run.document_version_ids?.length||matches.length!==run.document_version_ids.length){setError('原查询使用的文档版本已不可用。请重新选择当前可检索的文档后再提问。');return}
  setSelected(matches.map(doc=>doc.id));setQuestion(expanded);setBuildFacts(false);setFollowupOpen(false);
  setToast('追问已补全到提问框；请核对后发送。');
  document.getElementById('question-composer')?.scrollIntoView({behavior:'smooth',block:'start'});
 }
 function retryDraft(){
  if(!run)return;
  const matches=docs.filter(doc=>run.document_version_ids?.includes(doc.current_version_id)&&doc.state==='READY');
  if(!run.document_version_ids?.length||matches.length!==run.document_version_ids.length){setError('原查询的文档版本已不可用。请重新选择文档后再发起新查询。');return}
  setSelected(matches.map(doc=>doc.id));setQuestion(run.question);setBuildFacts(false);
  setToast('已填入原问题；请检查后手动重试。此前可能发生的费用仍保留在原查询记录中。');
  document.getElementById('question-composer')?.scrollIntoView({behavior:'smooth',block:'start'});
 }
 const busy=submitting||!!queryID&&(!run||!finished(run.state));const allReady=selected.length>0&&selected.every(id=>docs.some(d=>d.id===id&&d.state==='READY'));
 return <><WorkspaceHeader readOnly={readOnly} connection={connection} provider={provider} onExit={()=>changeTenant('')} theme={theme} onTheme={()=>setTheme(value=>value==='dark'?'light':'dark')}/>
 <main>{notice}{!readOnly&&<div className="intro"><p className="eyebrow">FINANCIAL DOCUMENTS, CHECKABLE ANSWERS</p><h1>年报里的数字，先核对，再复用。</h1><p>提出一个具体问题，打开原页核对；证据不足时会说明缺什么，不猜数字。</p></div>}
 <LedgerSummary summary={historySummary} readOnly={readOnly}/>
 <div className={`layout${readOnly?' replay-layout':''}`}><aside>
  <DocumentLibrary docs={docs} selected={selected} onSelect={setSelected} readOnly={readOnly} onUpload={upload} onRevoke={revoke} fileRef={file} scopePages={scopePages} onScopePages={setScopePages} year={year} onYear={setYear} uploading={uploading} pdfPages={pdfPages} fileChecking={fileChecking} fileError={fileError} scanNotice={scanNotice} suggestedPages={suggestedPages} scanProgress={scanProgress} scanning={scanning} previewURL={previewURL} onFileChange={next=>{setScopePages('');void inspectFile(next)}}/>
  <HistoryPanel history={history} cursor={historyCursor} busy={historyBusy} currentID={queryID} onRefresh={refreshHistory} onSearch={searchHistory} onOpen={openHistory} onRename={renameHistory} aliases={aliases} search={historySearch}/>
 </aside><div className="workspace">
  <DemoGuide readOnly={readOnly} demoSteps={demoSteps} demoPosition={demoPosition} busy={busy} onStep={selectDemoStep} onSuggest={suggest}/>
  <QueryComposer readOnly={readOnly} selectedCount={selected.length} question={question} onQuestion={setQuestion} buildFacts={buildFacts} onBuildFacts={setBuildFacts} executionPolicy={executionPolicy} onPolicy={setExecutionPolicy} busy={busy} submitting={submitting} allReady={allReady} onSubmit={submit} onCancel={cancel} canCancel={!readOnly&&(busy||!!run&&pendingJobs(run))&&!!run}/>
  {error&&<div role="alert" className="error"><strong>操作未完成</strong><span>{error}</span><a href="#recent-queries">查看查询记录</a><button type="button" onClick={()=>setError('')} aria-label="关闭错误">关闭</button></div>}
  {toast&&<div role="status" className="toast">{toast}<button type="button" onClick={()=>setToast('')} aria-label="关闭通知">关闭</button></div>}
  <AnswerPanel run={run} history={history} readOnly={readOnly} onShowSource={showSource} onResume={resume} resumingJob={resumingJob} onRetryDraft={retryDraft} onContinue={()=>{setFollowupDraft('');setFollowupOpen(true);requestAnimationFrame(()=>document.getElementById('followup-wrap')?.scrollIntoView({behavior:'smooth',block:'start'}))}}/>
  {followupOpen&&run&&<div id="followup-wrap"><FollowupPanel previous={run.question} draft={followupDraft} onDraft={setFollowupDraft} onSubmit={prepareFollowup} onClose={()=>setFollowupOpen(false)}/></div>}
  <SourcePanel opened={openedSources} selectedID={selectedSourceID} onSelect={setSelectedSourceID} onClose={closeSource}/>
 </div></div></main><footer>CrackRAG · v0.1.0 · 可追溯的财务事实复用 <span>{readOnly?'静态回放不含登录凭据或新的模型调用':'文档范围与来源访问均受权限校验'}</span></footer></>;

}
if(import.meta.env.VITE_DEMO_REPLAY!=='1')createRoot(document.getElementById('root')!).render(<Root/>);
