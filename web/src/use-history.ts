import {useRef,useState} from 'react';
import {apiUrl} from './urls';
import {APIError} from './requests';
import {reasonText} from './workspace-model';
import type {HistoryItem,HistorySummary} from './workspace-model';

type HistoryScope={active:boolean;json:<T>(path:string,options?:RequestInit)=>Promise<T>};
const errorText=(error:unknown)=>error instanceof APIError?reasonText(error.reason):'历史记录暂时无法读取，请检查连接后刷新。';

export function useHistory(scope:HistoryScope,onError:(message:string)=>void,onUnauthorized:(error:unknown)=>boolean,onNotice:(message:string)=>void){
 const [history,setHistory]=useState<HistoryItem[]>([]),[historyCursor,setHistoryCursor]=useState(''),[historyBusy,setHistoryBusy]=useState(false);
 const [historySummary,setHistorySummary]=useState<HistorySummary|null>(null),[historySearch,setHistorySearch]=useState('');
 const [aliases,setAliases]=useState<Record<string,string>>(()=>{try{return JSON.parse(sessionStorage.getItem('crackrag:history-aliases')||'{}')}catch{return {}}});
 const sequence=useRef(0);
 async function refreshHistory(append=false,term=historySearch){
  if(historyBusy&&append)return;const request=++sequence.current;setHistoryBusy(true);
  try{
   const result=await scope.json<{queries:HistoryItem[];next_cursor:string;summary?:HistorySummary}>(`${apiUrl('/queries')}?limit=20${term?`&q=${encodeURIComponent(term)}`:''}${append&&historyCursor?`&cursor=${encodeURIComponent(historyCursor)}`:''}`);
   if(scope.active&&request===sequence.current){setHistory(old=>append?[...old,...result.queries.filter(item=>!old.some(existing=>existing.id===item.id))]:result.queries);setHistoryCursor(result.next_cursor);if(result.summary)setHistorySummary(result.summary)}
  }catch(error){if(scope.active&&request===sequence.current&&!onUnauthorized(error))onError(errorText(error))}
  finally{if(scope.active&&request===sequence.current)setHistoryBusy(false)}
 }
 function searchHistory(term:string){setHistorySearch(term);setHistoryCursor('');void refreshHistory(false,term)}
 function renameHistory(id:string,title:string){const next={...aliases,[id]:title};setAliases(next);sessionStorage.setItem('crackrag:history-aliases',JSON.stringify(next));onNotice('显示名称已更新；原始问题没有改动。')}
 return {history,historyCursor,historyBusy,historySummary,historySearch,aliases,refreshHistory,searchHistory,renameHistory};
}
