import React,{lazy,Suspense} from 'react';
import {answerStatusDescription,answerTextForDisplay,comparablePrior,displayGap,finished,formatCny,jobNames,names,pendingJobs,reasonText,supportNames} from './workspace-model';
import type {HistoryItem,Job,Run,Source} from './workspace-model';

const PDFSource=lazy(()=>import('./pdf-source').then(m=>({default:m.PDFSource})));
export type OpenSource={region:Source;pdfURL:string};

function AnswerEvidence({run}:{run:Run}){
 const evidence=run.answer?.evidence_summary;const reused=evidence?.reused_facts||[];
 const explanation=run.answer?.answer_validation?.status==='INCONCLUSIVE'?'查阅了所选 PDF，但缺少支撑确定数字的证据。':run.answer?.answer_validation?.status==='UNSUPPORTED'?'这个问题不在当前支持范围内。':reused.length?`复用了 ${reused.length} 个此前核对的数字。`:'本次查阅并核对了所选 PDF。';
 return <details className="structure-diagnostics"><summary>这次回答依据了什么</summary><p className="hint">{explanation}{evidence?.sources.length?'下方可以打开引用，回到原页核对。':''}</p>{reused.map((fact,index)=><p className="evidence-item" key={fact.fact_id}>已核对数字 {index+1}：{fact.value} {fact.unit}（{fact.origin==='DERIVED'?'由已核对数字计算':'年报原文披露'}）</p>)}</details>;
}
function UsageDetails({run}:{run:Run}){
 const calls=run.calls||[];const unknown=Number(run.cost?.unknown_calls||0);
 return <details className="usage"><summary>查看逐次调用记录 <span>{calls.length} 次{run.provider==='mock'?'模拟':'模型'}调用</span></summary><p className="hint">{run.provider==='mock'?'模拟运行不调用供应商。':`已知用量的费用估算为 ${formatCny(run.cost?.known_estimated_subtotal)}。`}{unknown>0?'仍有费用待核对，新的真实请求已暂停。':''}本地解析、计算与存储费用未计入。</p>{calls.map((call,index)=><div className="call" key={call.attempt_id}><strong>第 {index+1} 次调用：{call.state==='SETTLED'?'已结算':call.state==='RESERVED'?'费用待确认':call.state==='FAILED'?'未完成':'已记录'}</strong><p className="hint">{call.amount_cny===null?'费用待确认':`已知费用估算 ${formatCny(call.amount_cny)}`}{call.record?.transport_failure?'；请求未正常完成，请先核对费用':''}</p></div>)}</details>;
}

export function AnswerPanel({run,history,readOnly,onShowSource,onResume,resumingJob,onRetryDraft,onContinue}:{run:Run|null;history:HistoryItem[];readOnly:boolean;onShowSource:(source:Source)=>void;onResume:(job:Job)=>void;resumingJob:string;onRetryDraft:()=>void;onContinue:()=>void}){
 const prior=run?comparablePrior(run,history):undefined;
 return <section className="card answer-card" aria-live="polite"><div className="section-line"><h2>回答与证据</h2>{run&&<span className={`status ${run.state.toLowerCase()}`}>{names[run.state]||run.state}</span>}</div>
 {!run?<div className="empty"><h3>从一个具体问题开始</h3><p>回答将在这里出现，来源可逐条打开核对。</p></div>:<><p className="asked">{run.question}</p><p className="hint">{run.provider==='deepseek'?'这次运行使用 DeepSeek':'这次运行使用模拟模型'}</p>{run.error&&<div role="alert" className="inline-error"><strong>这次查询未完成。</strong> {reasonText(run.error.reason_code)}{!readOnly&&finished(run.state)&&<button type="button" className="secondary retry-draft" onClick={onRetryDraft}>修改问题后重试</button>}</div>}
 {!run.answer&&!finished(run.state)&&<div className="answer-progress" role="status"><div className="progress-steps"><span className="done">已收到问题</span><span className="active">查阅并核对来源</span><span>展示通过验证的答案</span></div><div className="skeleton-line"/><div className="skeleton-line short"/><p className="hint">答案会在证据核验完成后出现；刷新本页不会重新调用模型。</p></div>}
 {run.answer&&<><div className="answer-verdict" data-status={run.answer.answer_validation?.status||'LEGACY'}><strong>{supportNames[run.answer.answer_validation?.status||'']||'请核对来源'}</strong><span>{answerStatusDescription(run)}</span></div><div className="answer-text">{answerTextForDisplay(run.answer.text)}</div>
 <div className="metrics" aria-label="本次回答摘要"><div><strong>{(run.answer.evidence_summary.reused_facts?.length||0)>0?'直接复用':run.answer.answer_validation?.status==='INCONCLUSIVE'?'说明证据缺口':'核对原文'}</strong><span>答案路径</span></div><div><strong>{run.calls?.length||0} 次</strong><span>{run.provider==='mock'?'本次模拟调用':'本次模型调用'}</span></div><div><strong>{run.provider==='mock'?'无供应商费用':formatCny(run.cost?.known_estimated_subtotal)}</strong><span>{Number(run.cost?.unknown_calls||0)>0?'仍有费用待核对':'已知用量费用估算'}</span></div><div><strong>{run.answer.evidence_summary.sources.length} 处</strong><span>可打开的原文来源</span></div></div>
 {prior&&<p className="cost-comparison">同文档同题的一次较早模型回答估算费用为 {formatCny(prior.known_estimated_cny)}；本次使用已核对数字，模型调用为 0。此对照只比较这两次回答，不包含此前保存数字的费用。</p>}
 {run.answer.evidence_summary.unresolved.length>0&&<div className="unresolved"><strong>还缺哪些证据</strong>{run.answer.evidence_summary.unresolved.filter(gap=>gap.trim()!==run.answer!.text.trim()).map((gap,index)=><p key={index}>{displayGap(gap)}</p>)}{[...new Set(run.answer.answer_validation?.items?.filter(item=>item.status!=='SUPPORTED').flatMap(item=>item.reasons)||[])].map(reason=><p className="hint" key={reason}>{reasonText(reason)}</p>)}<p><strong>下一步：</strong>{run.answer.evidence_summary.sources.length?'打开下方来源，核对年份、单位、合并口径与相关附注；若原页未披露，请补选相应页面后再问。':'补选包含年份、单位、合并口径及相关附注的页面后再问；也可把问题缩小到单一指标。'}</p></div>}
 <div className="sources">{run.answer.evidence_summary.sources.map((citation,index)=><button className="source-link" key={`${citation.region_id}-${index}`} onClick={()=>onShowSource(citation)}><span>{String(index+1).padStart(2,'0')}</span><div><strong>{citation.title}</strong><small>PDF 第 {citation.page} 页 · 定位证据区域</small></div><b>↗</b></button>)}</div>
 {!readOnly&&finished(run.state)&&<button type="button" className="secondary continue-button" onClick={onContinue}>接着这一问提问</button>}
 </>}
 {(pendingJobs(run)||!!run.diagnostics?.m3?.jobs?.length)&&<section className="structure-diagnostics" aria-label="后续复用准备"><h3>为下次提问准备数字</h3><p className="hint">{pendingJobs(run)?'回答已经显示，后台仍在核对；完成后才能供下次直接复用。':'后台处理已结束。'}</p>{run.diagnostics?.m3?.status==='unavailable'&&<p className="inline-error">暂时无法读取后台进度。请稍后从“最近查询”重新打开。</p>}{run.diagnostics?.m3?.jobs?.map(job=><div className="call" key={job.job_id}><strong>{jobNames[job.state]||'处理中'}</strong>{job.reason&&['FAILED','SKIPPED','OUTCOME_UNKNOWN'].includes(job.state)&&<p className="hint">{reasonText(job.reason)}</p>}{job.state==='RESULT_READY'&&<button type="button" className="secondary" disabled={!!resumingJob||readOnly} onClick={()=>onResume(job)}>{resumingJob===job.job_id?'正在继续核对…':'继续核对已保存内容'}</button>}</div>)}</section>}
 {run.answer&&<AnswerEvidence run={run}/>}<UsageDetails run={run}/><p className="run-id">刷新页面或从“最近查询”打开这次回答，不会自动重复调用模型。</p></>}</section>;
}

export function SourcePanel({opened,selectedID,onSelect,onClose}:{opened:OpenSource[];selectedID:string;onSelect:(id:string)=>void;onClose:(id:string)=>void}){
 if(!opened.length)return null;
 const active=opened.find(item=>item.region.region_id===selectedID)||opened[0];const source=active.region;
 return <section className="card source-card" id="source-panel"><div className="section-line"><h2>回到原页 · 第 {source.page} 页</h2><button className="text-button" type="button" onClick={()=>onClose(source.region_id)}>关闭此来源</button></div>
 {opened.length>1&&<div className="source-tabs" role="tablist" aria-label="已打开的来源">{opened.map(item=><button type="button" role="tab" aria-selected={item.region.region_id===source.region_id} key={item.region.region_id} onClick={()=>onSelect(item.region.region_id)}>{item.region.title} · 第 {item.region.page} 页</button>)}</div>}
 <p>{source.title}</p>{source.quote&&<blockquote>{source.quote}</blockquote>}<p className="hint">先看高亮区域，再核对公司、年份、列标题、单位与业务口径。</p>{active.pdfURL?<Suspense fallback={<p className="hint">正在准备原页预览…</p>}><PDFSource url={active.pdfURL} page={source.replay_page||source.page} displayPage={source.page} bbox={source.bbox} quote={source.quote}/></Suspense>:<p className="hint">正在读取原页…</p>}{source.text&&<details className="source-extraction"><summary>查看提取的文字</summary><pre className="source-text">{source.text}</pre></details>}</section>;
}
