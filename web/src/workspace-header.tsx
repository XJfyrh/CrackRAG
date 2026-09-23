import {assetUrl} from './urls';
import {formatCny} from './workspace-model';
import type {HistorySummary} from './workspace-model';

export function WorkspaceHeader({readOnly,connection,provider,onExit,theme,onTheme}:{readOnly:boolean;connection:string;provider:string;onExit:()=>void;theme:string;onTheme:()=>void}){
 return <header className="site-header"><a className="brand" href={assetUrl('')}>Crack<span>RAG</span></a><div className="header-meta"><span className="dot"/>{readOnly?'回放已就绪':connection}<span className="mode">{readOnly?'历史运行回放':provider==='deepseek'?'当前使用真实模型':'当前为模拟模式'}</span><button className="text-button theme-switch" type="button" onClick={onTheme} aria-label={theme==='dark'?'切换浅色模式':'切换深色模式'}>{theme==='dark'?'浅色':'深色'}</button><button className="text-button" type="button" onClick={onExit}>{readOnly?'从头播放':'退出'}</button></div></header>;
}


export function LedgerSummary({summary,readOnly}:{summary:HistorySummary|null;readOnly:boolean}){
 if(!summary)return null;
 return <section className="ledger-summary" aria-label="使用概况"><div><small>历次真实查询</small><strong>{summary.real_queries} 次</strong></div><div><small>其中零模型的已核对回答</small><strong>{summary.zero_model_supported_answers} 次</strong></div><div><small>累计已知模型费用估算</small><strong>{formatCny(summary.known_estimated_cny)}</strong></div><p>{summary.unknown_calls>0?`${summary.unknown_calls} 次调用费用待核对；累计值不是最终账单。`:'这是当前身份的查询模型调用小计，含后来撤销文档访问的查询；不含文档处理、本地计算，也不推算未发生调用的费用。'}{readOnly?' 演示最后一步是模拟示例。':''}</p></section>;
}
