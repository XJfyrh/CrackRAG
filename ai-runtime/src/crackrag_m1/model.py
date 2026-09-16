"""Reuse M0 transport, usage normalization and decimal pricing for M1 calls."""
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from .m0_snapshot.accounting import normalize_usage, estimate_cost
from .m0_snapshot.artifacts import redact
from .m0_snapshot.config import Pricing, RequestOptions, read_api_key
from .m0_snapshot.prompt import canonical_bytes
from .m0_snapshot.providers import DeepSeekProvider, CallContext, ProviderResult
from crackrag.v1 import runtime_pb2 as pb
from .config import ROOT

RUNTIME_INSTANCE_ID = str(uuid4())


def contract_output_tokens(contract):
    """Use the immutable Run contract, preserving the historical 512 policy."""
    configuration = json.loads(getattr(contract, 'configuration_json', '') or '{}')
    value = getattr(contract, 'max_output_tokens', 512)
    version = configuration.get('policy_version')
    if type(value) is not int or (version == 'm3-budget-policy-v2' and not 1 <= value <= 2048):
        raise ValueError('UNSUPPORTED_EXECUTION_CONTRACT')
    if version != 'm3-budget-policy-v2' and value != 512:
        raise ValueError('UNSUPPORTED_EXECUTION_CONTRACT')
    return value


class CacheWindowExpiredBeforeDispatch(ValueError):
    pass

class ReleaseGateBeforeDispatch(ValueError):
    """The release gate failed before httpx entered the transport."""
    pass

@dataclass(frozen=True)
class HTTPSettings:
    model: str='deepseek-flash'
    base_url: str='https://api.deepseek.com'
    timeout_seconds: float=30
    request: RequestOptions=RequestOptions(max_tokens=512)

def now():
    return datetime.now(timezone.utc).isoformat()

def load_pricing(settings):
    snapshot=json.loads(settings.price_snapshot.read_text(encoding='utf-8'))
    source=settings.price_snapshot.with_suffix('.html')
    if not snapshot.get('verified') or sha256(source.read_bytes()).hexdigest()!=snapshot['source_sha256']:
        raise ValueError('PRICE_NOT_VERIFIED')
    if not 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(snapshot['verified_at'])).total_seconds() <= 86400:
        raise ValueError('PRICE_SNAPSHOT_EXPIRED')
    price=Pricing(**snapshot['pricing']);price.validate()
    if (price.model,price.currency,price.input_miss_per_million,price.input_hit_per_million,price.output_per_million)!=('deepseek-flash','CNY','1','0.02','4'):
        raise ValueError('PRICE_OUTSIDE_FROZEN_ADMISSION_POLICY')
    return price

class Model:
    def __init__(self,settings,tools,context,metadata):
        self.settings=settings;self.tools=tools;self.context=context;self.metadata=metadata
        self.key='';self.price=None;self.provider=None
        self.records=[];self._uses_http_hook=False;self._dispatch_boundary=None
        if settings.provider=='deepseek':
            from .release import check_session
            check_session(settings)
            self.price=load_pricing(settings)
            key_file=os.getenv('DEEPSEEK_API_KEY_FILE')
            if key_file:
                self.key=Path(key_file).read_text(encoding='utf-8').strip()
                if not self.key:raise ValueError('DEEPSEEK_API_KEY_EMPTY')
            else:self.key=read_api_key(ROOT/'.env')
            provider=DeepSeekProvider
            if context.config_version.startswith('m3-'):
                from .m3_provider import NativeDeepSeekProvider
                provider=NativeDeepSeekProvider
            self.provider=provider(HTTPSettings(),self.key,allow_live=True)
            if context.config_version.startswith('m3-'):
                # httpx runs this immediately before client transport dispatch.
                # This is not a provider prefill/cache-write timestamp.
                self.provider.client.event_hooks['request'].append(self._before_http_send)
                self._uses_http_hook=True

    async def _before_http_send(self, request):
        from .release import check_session
        try:
            check_session(self.settings)
        except (ValueError,OSError,KeyError,TypeError) as exc:
            raise ReleaseGateBeforeDispatch('RELEASE_GATE_NOT_DISPATCHED') from exc
        if self._dispatch_boundary:
            self._dispatch_boundary()

    async def close(self):
        if self.provider:
            await self.provider.close()

    async def ask(self,payload,mock_action,*,stage='answer',batch_id='',probe_token=''):
        if hasattr(self, 'contract') and payload.get('max_tokens') != contract_output_tokens(self.contract):
            raise ValueError('OUTPUT_LIMIT_CONTRACT_MISMATCH')
        if payload.get('response_format',{}).get('type')=='json_object' and not any(
            'json' in str(message.get('content','')).lower() for message in payload.get('messages',[])):
            raise ValueError('JSON_MODE_INSTRUCTION_REQUIRED')
        attempt=str(uuid4());serialized=canonical_bytes(payload).decode('utf-8')
        if self.context.config_version.startswith('m3-'):
            from .m3_prefix import native_json
            serialized=native_json(payload)
        # Freeze the dispatched object too: no caller mutation during admission
        # may change the bytes that Go authorized and reserved.
        payload=json.loads(serialized)
        m3_call=getattr(self,'m3_call',{})
        reserve_started=time.perf_counter()
        reservation=await self.tools.ReserveCall(pb.ReserveRequest(context=self.context,
            attempt_id=attempt,payload_json=serialized,provider=self.settings.provider,stage=stage,batch_id=batch_id,probe_token=probe_token,
            prefix_manifest_id=m3_call.get('prefix_manifest_id',''),cache_evidence_id=m3_call.get('cache_evidence_id',''),
            subexperiment=m3_call.get('subexperiment',''),snapshot_json=m3_call.get('snapshot_json',''),
            snapshot_refreshes=m3_call.get('snapshot_refreshes',0)),metadata=self.metadata,timeout=10)
        started=now();clock=time.perf_counter();cancelled=False
        dispatched=False;dispatch_at=None;dispatch_monotonic_ns=None
        decision_text=getattr(reservation,'cache_decision_json','')
        try:
            cache_decision=json.loads(decision_text) if decision_text else None
        except (TypeError,ValueError):
            cache_decision=None
        hot_dispatch=stage=='extraction' and (m3_call.get('execution_policy')=='HOT_ONLY'
            or bool(m3_call.get('cache_evidence_id')))
        remaining_window=getattr(reservation,'cache_remaining_window_ms',0)
        window_valid=type(remaining_window) is int and 0 <= remaining_window <= 5000
        cache_deadline=reserve_started+(remaining_window if window_valid else 0)/1000
        def mark_dispatch():
            nonlocal dispatched,dispatch_at,dispatch_monotonic_ns
            if hot_dispatch and (not window_valid or not isinstance(cache_decision,dict)
                or cache_decision.get('evidence_id')!=m3_call.get('cache_evidence_id')
                or time.perf_counter()>=cache_deadline):
                raise CacheWindowExpiredBeforeDispatch('CACHE_WINDOW_EXPIRED_NOT_DISPATCHED')
            dispatch_at=now();dispatch_monotonic_ns=time.perf_counter_ns()
            dispatched=self.settings.provider=='deepseek'
            callback=getattr(self,'on_dispatch',None)
            if callback:
                callback({'attempt_id':attempt,'stage':stage,'dispatch_at':dispatch_at,
                    'dispatch_monotonic_ns':dispatch_monotonic_ns,'runtime_instance_id':RUNTIME_INSTANCE_ID,
                    'monotonic_clock':'time.perf_counter_ns',
                    'http_dispatched':dispatched,'simulated':self.settings.provider=='mock',
                    'dispatch_boundary':'httpx_request_hook_before_transport' if self._uses_http_hook else 'provider_call_boundary'})
        self._dispatch_boundary=mark_dispatch
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(reservation.snapshot.observed_at)).total_seconds()*1000
            if not 0<=age<=5000:
                result=ProviderResult(transport_failure='SNAPSHOT_STALE_NOT_DISPATCHED')
            elif self.settings.provider=='deepseek':
                if not self._uses_http_hook:mark_dispatch()
                result=await self.provider.complete(payload,CallContext(stage,1,attempt,sha256(serialized.encode()).hexdigest()))
            else:
                mark_dispatch()
                scenario=self.settings.mock_scenario
                if scenario in ('slow','timeout'):
                    await asyncio.sleep(3 if scenario=='slow' else 60)
                content=json.dumps(mock_action,ensure_ascii=False)
                raw={'id':'mock-completion-'+attempt,'model':'deepseek-flash',
                    'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}],
                    'usage':{'prompt_tokens':1000,'completion_tokens':100,'total_tokens':1100,
                             'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':1000}}
                if scenario=='missing_usage':raw.pop('usage')
                if scenario=='invalid_json':raw['choices'][0]['message']['content']='{broken'
                if scenario=='model_error':
                    result=ProviderResult(body={'error':{'message':'synthetic failure'}},status_code=503)
                else:
                    result=ProviderResult(body=raw,status_code=200,raw_text=json.dumps(raw),headers={})
        except CacheWindowExpiredBeforeDispatch:
            result=ProviderResult(transport_failure='CACHE_WINDOW_EXPIRED_NOT_DISPATCHED')
        except ReleaseGateBeforeDispatch:
            result=ProviderResult(transport_failure='RELEASE_GATE_NOT_DISPATCHED')
        except asyncio.CancelledError:
            cancelled=True;result=ProviderResult(transport_failure='CANCELLED_IN_FLIGHT')
        finally:
            self._dispatch_boundary=None
        finished=now();finished_monotonic_ns=time.perf_counter_ns();body=result.body if isinstance(result.body,dict) else {}
        usage,cache=normalize_usage(body.get('usage'))
        if (self.settings.provider=='deepseek' and not dispatched
                and result.transport_failure in ('SNAPSHOT_STALE_NOT_DISPATCHED', 'CACHE_WINDOW_EXPIRED_NOT_DISPATCHED','RELEASE_GATE_NOT_DISPATCHED')):
            cost={'status':'not_dispatched','amount':'0','currency':'CNY','billing_confirmed':False,
                  'reason':result.transport_failure,'simulated':False}
        elif self.settings.provider=='deepseek':
            cost=estimate_cost(usage,self.price,simulated=False,started_at=started,finished_at=finished)
        else:
            cost={'status':'simulated','amount':None,'currency':'CNY','billing_confirmed':False,
                  'reason':'no paid model request; local mock compute cost unmetered'}
        headers=result.headers or {};request_id=None;id_source=None
        for header in ('x-request-id','request-id','x-ds-request-id'):
            if headers.get(header):request_id=headers[header];id_source='response.header.'+header;break
        completion=body.get('id')
        if not request_id and isinstance(completion,str):request_id=completion;id_source='response.body.id (completion identifier fallback, not a transport request id)'
        from .actions import ACTION,FINANCIAL_ACTION
        validation={'action_schema':'not_evaluated','reason':None,'source_support':'evaluated by Agent after settlement'}
        try:
            choice=body['choices'][0]
            if choice['finish_reason']!='stop':raise ValueError('MODEL_OUTPUT_TRUNCATED')
            if stage=='answer':
                schema=FINANCIAL_ACTION if getattr(self,'answer_schema_version','')=='financial-answer-v1' else ACTION
                schema.validate_json(choice['message']['content'],strict=True)
            else:
                from .m2 import validate_model_shape
                validate_model_shape(stage,choice['message']['content'])
            validation['action_schema']='passed'
        except (KeyError,IndexError,TypeError,ValueError):
            validation.update(action_schema='failed',reason='MODEL_RESPONSE_OR_ACTION_INVALID')
        record={'attempt_id':attempt,'provider':self.settings.provider,'model':'deepseek-flash',
            'request_id':request_id,'request_id_source':id_source,'completion_id':completion,
            'service_id':'python-runtime','trace_id':self.context.trace_id,'run_id':self.context.run_id,
            'config_version':self.context.config_version,'prompt_version':getattr(self,'answer_prompt_version','m1-source-agent-v1'),
            'schema_version':getattr(self,'answer_schema_version','m1-action-v1') if stage=='answer' else 'm2-candidate-v1' if stage=='extraction' else 'm2-probe-v1','stage':stage,'batch_id':batch_id or None,'price_configuration':asdict(self.price) if self.price else None,'started_at':started,'finished_at':finished,
            'latency_ms':round((time.perf_counter()-clock)*1000,3),'http_status':result.status_code,
            'raw_usage':body.get('usage'),'normalized_usage':usage,'cache':cache,'cost':cost,
            'raw_response':body,'raw_text':result.raw_text,'response_headers':headers,
            'transport_failure':result.transport_failure,'automatic_retries':0,'http_dispatched':dispatched,
            'dispatch_at':dispatch_at,'dispatch_monotonic_ns':dispatch_monotonic_ns,
            'finished_monotonic_ns':finished_monotonic_ns,'runtime_instance_id':RUNTIME_INSTANCE_ID,
            'monotonic_clock':'time.perf_counter_ns',
            'dispatch_boundary':'httpx_request_hook_before_transport' if self._uses_http_hook else 'provider_call_boundary',
            'validation':validation,'payload_sha256':sha256(serialized.encode()).hexdigest(),
            'budget_reservation':{'upper_cny':reservation.reserved_upper_cny,'snapshot_id':reservation.snapshot.snapshot_id,
                                  'observed_at':reservation.snapshot.observed_at},
            'simulated':self.settings.provider=='mock'}
        if self.context.config_version.startswith('m3-') or self.context.job_id or m3_call:
            record['payload_wire_json']=serialized
            record['payload_wire_sha256']=sha256(serialized.encode('utf-8')).hexdigest()
        if self.context.job_id or m3_call:
            record['m3']={'job_id':self.context.job_id or None,'lease_owner':self.context.lease_owner or None,
                'fencing_token':self.context.fencing_token,'prefix_manifest_id':m3_call.get('prefix_manifest_id'),
                'cache_evidence_id':m3_call.get('cache_evidence_id'),'subexperiment':m3_call.get('subexperiment'),
                'snapshot_refreshes':reservation.snapshot.refresh_count,
                'snapshot_refresh_reason':reservation.snapshot.refresh_reason,
                'cache_decision':cache_decision,'cache_remaining_window_ms':remaining_window,
                'reserve_round_trip_ms':round((clock-reserve_started)*1000,3)}
        record=redact(record,(self.key,self.settings.internal_token))
        if stage!='answer':record['prompt_version']=('m3-shared-prefix-v1' if self.context.job_id and stage=='extraction'
            else 'm2-extraction-v1' if stage=='extraction' else 'm2-probe-v1')
        if stage=='other' and self.context.config_version.startswith('m3-'):
            record['prompt_version']=getattr(self,'answer_prompt_version','m3-frozen-diagnostic-v1')
            record['schema_version']='m3-diagnostic-json-v1'
        self.last_record=record
        self.records.append(record)
        # Settlement uses its own RPC deadline even when the foreground call was cancelled.
        await asyncio.shield(self.tools.SettleCall(pb.SettleRequest(context=self.context,
            attempt_id=attempt,call_json=json.dumps(record,ensure_ascii=False,allow_nan=False)),metadata=self.metadata,timeout=10))
        if cancelled:raise asyncio.CancelledError
        if self.settings.provider=='deepseek' and cost['amount'] is None:
            raise ValueError('COST_UNKNOWN')
        if result.transport_failure:raise ValueError(result.transport_failure)
        if result.status_code!=200:raise ValueError('MODEL_HTTP_ERROR')
        if body.get('usage') is None:raise ValueError('USAGE_MISSING')
        try:
            choice=body['choices'][0]
            if choice['finish_reason']!='stop':raise ValueError('MODEL_OUTPUT_TRUNCATED')
            return choice['message']['content'],record
        except (KeyError,IndexError,TypeError) as exc:
            raise ValueError('MODEL_RESPONSE_INVALID') from exc
