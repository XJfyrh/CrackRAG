export type Doc={id:string;title:string;current_version_id:string;state:string;indexed_pages:number[];requested_pages:number[];error_json?:{reason_code:string};build_usage:unknown};
export type Source={region_id:string;document_id:string;document_version_id:string;title:string;page:number;replay_page?:number;bbox:number[];text:string;quote?:string;source_url:string;context:unknown};
export type Call={attempt_id:string;state:string;stage?:string;amount_cny:string|null;record?:{request_id:string;request_id_source:string;latency_ms:number;normalized_usage:unknown;cache:unknown;transport_failure:string|null}};
export type Job={job_id:string;state:string;reason:string;batch_id:string;deadline_at:string;completed_at?:string};
export type Run={id:string;question:string;document_version_ids?:string[];created_at?:string;state:string;provider:string;answer:{text:string;answer_validation?:{status:string;policy:string;items?:{status:string;reasons:string[]}[]};evidence_summary:{sources:Source[];unresolved:string[];calculations?:unknown[];structured_coverage?:{status:string};reused_facts?:{fact_id:string;report_id:string;origin:string;value:string;unit:string}[];raw_observation_region_ids?:string[]}}|null;error:{reason_code:string}|null;calls:Call[];cost:Record<string,unknown>;diagnostics?:{m3?:{status?:string;jobs?:Job[];pending_jobs?:number};[key:string]:unknown}};
export type HistoryItem={id:string;question:string;state:string;provider:string;answer_status:string;model_calls:number;known_estimated_cny:string;created_at:string;document_version_ids?:string[]};
export type HistorySummary={total_queries:number;real_queries:number;zero_model_supported_answers:number;known_estimated_cny:string;unknown_calls:number};
export type DemoStep={role:string;question:string;document_ids:string[];build_facts?:boolean;execution_policy?:string};

export const demoStepNames:Record<string,string>={'first-answer':'① 看第一次回答',build:'② 保存可复用数字',repeat:'③ 再问同一问题',paraphrase:'④ 换个问法',abstention:'⑤ 看证据不足时的回答'};
export const names:Record<string,string>={QUEUED:'等待解析',PARSING:'正在解析与索引',READY:'可检索',FAILED:'未完成',INTERRUPTED:'已中断',RUNNING:'正在查阅原文',COMPLETED:'已完成',CANCELLED:'已取消',TIMED_OUT:'已超时'};
export const supportNames:Record<string,string>={SUPPORTED:'已核对来源',PARTIAL:'部分问题有依据',INCONCLUSIVE:'证据不足，暂不下结论',UNSUPPORTED:'目前无法回答这个问题'};
export const jobNames:Record<string,string>={WAITING_PREFIX:'等待合适的处理时机',RUNNING:'正在核对数字',RESULT_READY:'数字已保存，正在完成核验',COMMITTED:'已保存可复用的数字',SKIPPED:'本次未继续处理',FAILED:'未能保存，请查看原因',OUTCOME_UNKNOWN:'结果或费用待核对'};

const reasons:Record<string,string>={
 INVALID_PDF:'无法读取这份 PDF。请确认文件未加密、未损坏，再重新上传。',
 INVALID_PAGE_SCOPE:'页码不在文档范围内，或超过 32 页。请重新选择页码。',
 OCR_REQUIRED_NOT_ENABLED:'所选页是扫描图像。请换成含文字层的页面；此版本暂不支持 OCR。',
 DOCUMENT_NOT_READY:'文档还在整理。请等状态变为“可检索”后再提问。',
 NOT_FOUND:'这份文档或查询已不可访问。请检查当前访问身份并重新选择文档。',
 UNAUTHENTICATED:'访问令牌无效。请重新输入本机初始化时生成的令牌。',
 MODEL_HTTP_ERROR:'模型服务暂时没有完成请求。请先在“最近查询”确认结果，再手动重试，避免重复计费。',
 USAGE_MISSING:'模型没有返回用量，新的真实请求已暂停。请由维护者核对账单与预算后再继续。',
 COST_UNKNOWN:'本次费用尚无法确认，新的真实请求已暂停。请由维护者核对账单与预算后再继续。',
 DEADLINE_EXCEEDED:'这次查询已超时。请缩小问题范围，再手动发起新查询。',
 USER_CANCELLED:'查询已取消。已发生的调用仍会计入费用；可在“最近查询”查看记录。',
 CITATION_NOT_OBSERVED:'引用没有对应到实际读取的原文，因此未展示确定结论。请打开来源或改问更具体的问题。',
 SOURCE_UNIT_UNPROVEN:'来源没有明确单位。请补选包含表头和单位的页面。',
 BUSINESS_SCOPE_UNPROVEN:'来源无法确认是否为合并口径。请补选写明口径的页面。',
 REQUIRED_BASIS_NOTE_MISSING:'缺少判断口径所需的附注。请补选附注页后重试。',
 HOT_WINDOW_EXPIRED:'这次没有保存可复用数字。若希望现在完成，可选择“立即保存”，但可能产生额外费用。',
 NO_CACHE_EVIDENCE:'这次没有保存可复用数字。若希望现在完成，可选择“立即保存”，但可能产生额外费用。',
 M3_OUTCOME_UNKNOWN:'后台调用或费用待核对，新的真实请求已暂停。请由维护者先检查账单。',
 DEMO_STEP_MISMATCH:'所选内容与录制步骤不一致。请点击当前演示步骤后再查看。',
 DEMO_SEQUENCE_COMPLETE:'演示已经结束。点击“重新播放”可以从头观看。',
};
export const reasonText=(reason:string)=>reasons[reason]||'暂时无法完成这一步。请在“最近查询”核对状态，必要时联系维护者。';
export const answerTextForDisplay=(answer:string)=>answer
 .replace(/\bFY(\d{4})\b/g,'$1 年度')
 .replace(/\bRevenue\b/g,'营业收入')
 .replace(/\bNet profit\b/g,'净利润')
 .replace(/\bCNY\b/g,'人民币元')
 .replace(/\bSample Holdings\b/g,'样例控股');
export const answerStatusDescription=(run:Run)=>{
 const status=run.answer?.answer_validation?.status;
 if(status==='INCONCLUSIVE')return '缺少足够证据支持确定数字。请补选相关页面或改问更具体的问题。';
 if(status==='UNSUPPORTED')return '这个问题超出当前支持范围。请改问明确的财务数字。';
 if(status==='PARTIAL')return '只有部分内容通过核对；未确认的部分会在下方说明。';
 return (run.answer?.evidence_summary.reused_facts?.length||0)>0?'这次直接复用了此前核对的数字，仍可回到原页。':'这次查阅了所选原文；回答不等于自动保存数字。';
};
export const displayGap=(gap:string)=>reasons[gap]||(/^[A-Z][A-Z0-9_]+$/.test(gap)?reasonText(gap):answerTextForDisplay(gap));
export const finished=(state:string)=>['COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED'].includes(state);
export const awaitingSettlement=(run:Run)=>run.calls?.some(call=>call.state==='RESERVED');
export const pendingJobs=(run:Run)=>!!run.diagnostics?.m3?.pending_jobs||run.diagnostics?.m3?.status==='unavailable';
export const formatCny=(raw:unknown)=>{
 if(raw===null||raw===undefined||raw==='')return '费用待核对';
 const value=Number(raw);
 if(!Number.isFinite(value))return '费用待核对';
 const [whole,fraction]=value.toFixed(8).split('.');
 return `¥${whole}.${fraction.replace(/0+$/,'').padEnd(2,'0')}`;
};
export function shortTime(raw:string){
 const date=new Date(raw);
 if(Number.isNaN(date.getTime()))return '时间待核对';
 return new Intl.DateTimeFormat('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(date);
}
// Only resolve follow-ups whose changed field is unambiguous. The completed
// explicit question remains visible in the composer before any paid request.
export function expandFollowup(previous:string,followup:string):string|null{
 const input=followup.trim();
 const year=/^(?:那|再看|换成)?\s*(20\d{2})\s*年(?:度)?\s*(?:呢|的数字|是多少)?[？?]?$/u.exec(input);
 if(year){
  if((previous.match(/20\d{2}\s*年(?:度)?/gu)||[]).length!==1)return null;
  return previous.replace(/20\d{2}\s*年(?:度)?/u,`${year[1]}年`);
 }
 const metric=/^(?:那|再看|换成)?\s*(营业收入|营业成本|净利润|毛利率)\s*(?:呢|是多少)?[？?]?$/u.exec(input);
 if(metric){
  if((previous.match(/(营业收入|营业成本|净利润|毛利率)/gu)||[]).length!==1)return null;
  return previous.replace(/(营业收入|营业成本|净利润|毛利率)/u,metric[1]);
 }
 return null;
}
export function comparablePrior(run:Run,history:HistoryItem[]):HistoryItem|undefined{
 if(run.calls?.length!==0||!(run.answer?.evidence_summary.reused_facts?.length))return;
 const versions=run.document_version_ids||[];
 return history.find(item=>item.id!==run.id&&item.provider===run.provider&&item.question===run.question&&item.model_calls>0&&Number(item.known_estimated_cny)>0&&
  !!run.created_at&&Date.parse(item.created_at)<Date.parse(run.created_at)&&
  versions.length>0&&item.document_version_ids?.length===versions.length&&item.document_version_ids.every(id=>versions.includes(id)));
}
