import {useEffect} from 'react';
import type {Dispatch,MutableRefObject,SetStateAction} from 'react';
import {APIError,delay} from './requests';
import {apiUrl} from './urls';
import {awaitingSettlement,finished,pendingJobs,reasonText} from './workspace-model';
import type {Run} from './workspace-model';

type Scope={active:boolean;request:(path:string,options?:RequestInit)=>Promise<Response>;json:<T>(path:string,options?:RequestInit)=>Promise<T>};
type Input={scope:Scope;queryID:string;revision:number;generation:MutableRefObject<number>;queryRef:MutableRefObject<string>;readOnly:boolean;setRun:Dispatch<SetStateAction<Run|null>>;setQueryID:Dispatch<SetStateAction<string>>;onError:(message:string)=>void;onUnauthorized:(error:unknown)=>boolean};

// Only the committed query body is displayed. SSE wakes this reader after
// status changes; unverified model deltas are never rendered as an answer.
export function useQueryWatch({scope,queryID,revision,generation,queryRef,readOnly,setRun,setQueryID,onError,onUnauthorized}:Input){
 useEffect(()=>{
  if(!queryID)return;
  if(!readOnly)localStorage.setItem('m1:last-query',queryID);
  const abort=new AbortController();let lastSeq=0;
  const active=()=>scope.active&&!abort.signal.aborted&&queryRef.current===queryID&&generation.current===revision;
  async function readRun(){
   const current=await scope.json<Run>(apiUrl(`/queries/${queryID}`),{signal:abort.signal});
   if(active())setRun(current);return current;
  }
  async function watch(){
   while(active()){
    try{
     const current=await readRun();if(!active())return;
     if(finished(current.state)){
      if(!awaitingSettlement(current)&&!pendingJobs(current))return;
     }else{
      const stream=await scope.request(apiUrl(`/queries/${queryID}/events`),{signal:abort.signal,headers:{'Last-Event-ID':String(lastSeq)}});
      if(!stream.body)throw new Error('事件连接没有返回内容。');
      const reader=stream.body.getReader(),decoder=new TextDecoder();let buffer='',terminal=false,settled=false;
      try{
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
    }catch(error){
     if(!active())return;
     if(onUnauthorized(error))return;
     onError(error instanceof APIError?reasonText(error.reason):'连接暂时中断。请检查服务状态，再从“最近查询”确认是否已提交，避免直接重复计费。');
     if(error instanceof APIError&&[401,403,404].includes(error.status)){
      queryRef.current='';setQueryID('');setRun(null);localStorage.removeItem('m1:last-query');return;
     }
    }
    await delay(1000,abort.signal);
   }
  }
  void watch().catch(()=>{});return()=>abort.abort();
 },[queryID,revision,scope]);
}
