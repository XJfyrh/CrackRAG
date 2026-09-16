import asyncio
from concurrent.futures import ProcessPoolExecutor
import hmac
import json
import logging
import multiprocessing
import os
import re
import signal
import threading

import grpc
from crackrag.v1 import runtime_pb2 as pb, runtime_pb2_grpc as rpc
from .agent import Agent
from .config import Settings,VERSION
from .embedding import DenseEncoder
from .parser import parse_pdf
from .m3_coordinator import TaskRegistry

_worker_encoder=None
def parse_worker(raw,settings):
    global _worker_encoder
    if _worker_encoder is None:_worker_encoder=DenseEncoder(settings)
    return parse_pdf(pb.ParseRequest.FromString(raw),_worker_encoder)

class Runtime(rpc.AIRuntimeServicer):
    def __init__(self,settings):
        from .release import check_session
        self.release_digest=check_session(settings,require_enabled=False)
        self.settings=settings;self.encoder=DenseEncoder(settings)
        self.channel=grpc.aio.insecure_channel(settings.tools_address,options=[('grpc.max_receive_message_length',64<<20)])
        self.tools=rpc.DataToolsStub(self.channel)
        self.control=rpc.ExtractionControlStub(self.channel)
        self.probe_tools=rpc.ProbeToolsStub(self.channel)
        self.jobs=rpc.JobControlStub(self.channel);self.registry=TaskRegistry()
        self.metadata=(('authorization','Bearer '+settings.internal_token),)
        self.parse_pool=ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn'))
        self.parse_slots=asyncio.Semaphore(1);self.query_slots=asyncio.Semaphore(1)

    async def authorize(self,context,request_context=None):
        values=[value for key,value in context.invocation_metadata() if key=='authorization']
        if len(values)!=1 or not hmac.compare_digest(values[0],'Bearer '+self.settings.internal_token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED,'PERMISSION_DENIED')
        if request_context is not None and (request_context.service_id!='go-api' or not request_context.tenant_id or request_context.config_version!=VERSION):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION,'CONTRACT_VERSION_MISMATCH')

    async def Health(self,request,context):
        await self.authorize(context)
        from .release import verify
        try: current=verify(self.settings.release_root,self.settings.release_manifest) if getattr(self.settings,'release_manifest','') else ''
        except (ValueError,OSError,KeyError):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION,'RELEASE_INTEGRITY_FAILED')
        return pb.HealthReply(status='ok',protocol_version='crackrag.v1',config_version=VERSION,embedding_version=self.encoder.version,release_manifest_sha256=current)

    async def Parse(self,request,context):
        await self.authorize(context,request.context)
        try:
            async with self.parse_slots:
                result=await asyncio.get_running_loop().run_in_executor(self.parse_pool,parse_worker,request.SerializeToString(),self.settings)
            return pb.ParseReply(**result)
        except ValueError as exc:
            reason=str(exc)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT,reason if re.fullmatch(r'[A-Z0-9_]{1,80}',reason) else 'PARSE_VALIDATION_FAILED')
        except Exception:
            await context.abort(grpc.StatusCode.INTERNAL,'PARSE_RUNTIME_FAILED')

    async def Embed(self,request,context):
        await self.authorize(context,request.context)
        try:
            vectors,usage=await self.encoder.encode_async(list(request.texts))
            return pb.EmbedReply(vectors=[pb.Vector(values=v) for v in vectors],embedding_version=self.encoder.version,usage_json=json.dumps(usage))
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT,'EMBEDDING_INPUT_INVALID')

    async def RunQuery(self,request,context):
        await self.authorize(context,request.context)
        if request.provider!=self.settings.provider or request.contract.version!='m1-execution-v1':
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION,'CONFIGURATION_MISMATCH')
        async with self.query_slots:
            agent=Agent(request,self.settings,self.encoder,self.tools,self.metadata,self.control,self.probe_tools,self.registry,self.jobs)
            try:
                async for item in agent.run():yield item
            except grpc.aio.AioRpcError as exc:
                detail=exc.details()
                await context.abort(exc.code(),detail if re.fullmatch(r'[A-Z0-9_]{1,80}',detail) else 'DATA_TOOLS_UNAVAILABLE')

    async def CancelRun(self,request,context):
        await self.authorize(context,request.context)
        count=self.registry.cancel_run(request.context.run_id)
        return pb.JsonReply(payload_json=json.dumps({'cancelled_local_tasks':count}))

    async def ResumeJob(self,request,context):
        await self.authorize(context,request.context)
        from .m3_resume import resume_job
        try:
            if json.loads(request.payload_json).get('recovery_mode') == 'm4':
                from .m4_resume import resume_job as recover_job
                result=await recover_job(request,self)
            else:
                result=await resume_job(request,self.jobs,self.control,self.registry,self.metadata)
            return pb.JsonReply(payload_json=json.dumps(result))
        except (ValueError,KeyError,TypeError):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION,'RESULT_READY_RESUME_REJECTED')

SHUTDOWN_TIMEOUT_SECONDS=15

class ShutdownWatchdog:
    """Bound this process's entire exit, including asyncio.run finalization."""
    def __init__(self,timeout=SHUTDOWN_TIMEOUT_SECONDS):
        self.timeout=timeout;self.finished=threading.Event();self.thread=None

    def arm(self):
        if self.thread is not None:return
        def guard():
            if self.finished.wait(self.timeout):return
            message=json.dumps({'event':'runtime_shutdown_forced','reason':'SHUTDOWN_DEADLINE_EXCEEDED',
                'timeout_seconds':self.timeout,'exit_code':70,
                'durable_outcomes':'retain Go job/call state; no automatic replay'})+'\n'
            try:
                # Do not wait for a Python logging lock or a full stderr pipe
                # while another thread is preventing interpreter shutdown.
                os.set_blocking(2,False)
                os.write(2,message.encode('utf-8'))
            except OSError:pass
            os._exit(70)
        self.thread=threading.Thread(target=guard,name='runtime-shutdown-watchdog',daemon=True)
        self.thread.start()

    def disarm(self):self.finished.set()

def install_termination_handler(loop, stopped, watchdog=None):
    def request_stop():
        if watchdog:watchdog.arm()
        stopped.set()
    previous=signal.getsignal(signal.SIGTERM)
    try:
        loop.add_signal_handler(signal.SIGTERM,request_stop)
        def restore():
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM,previous)
    except (NotImplementedError,RuntimeError):
        # Windows ProactorEventLoop has no add_signal_handler. Retain the
        # existing asyncio/KeyboardInterrupt handling of Ctrl+C.
        signal.signal(signal.SIGTERM,lambda *_:loop.call_soon_threadsafe(request_stop))
        def restore():signal.signal(signal.SIGTERM,previous)
    return restore

async def shutdown_parse_pool(pool,grace_seconds=1,kill_seconds=1):
    # Python 3.12 has no public terminate_workers(). Capture only this owned
    # pool's children before shutdown clears its private process collection.
    processes=list((getattr(pool,'_processes',None) or {}).values())
    pool.shutdown(wait=False,cancel_futures=True)
    for process in processes:
        if process.is_alive():process.terminate()

    async def wait_children(seconds):
        until=asyncio.get_running_loop().time()+seconds
        while any(p.is_alive() for p in processes) and asyncio.get_running_loop().time()<until:
            await asyncio.sleep(0.02)

    await wait_children(grace_seconds)
    for process in processes:
        if process.is_alive():process.kill()
    await wait_children(kill_seconds)
    for process in processes:
        if not process.is_alive():process.join(timeout=0)
    remaining=[p.pid for p in processes if p.is_alive()]
    if remaining:logging.error('Parser shutdown left owned processes: %s',remaining)
    return {'workers':len(processes),'remaining':remaining}

async def shutdown_runtime(server,runtime):
    # Prevent new local jobs before gRPC's grace period; keep the outbound
    # channel alive while managed jobs record cancellation and settlement.
    runtime.registry.closing=True
    try:
        try:await asyncio.wait_for(server.stop(3),timeout=3.5)
        except Exception:logging.exception('Runtime gRPC shutdown failed')
        try:
            closing=await runtime.registry.shutdown(grace_seconds=5)
            if closing['remaining']:logging.error('M3 shutdown left durable pending jobs: %s',closing)
        finally:
            try:await asyncio.wait_for(runtime.channel.close(),timeout=1)
            except Exception:logging.exception('Runtime channel shutdown failed')
    finally:
        await shutdown_parse_pool(runtime.parse_pool)

async def main(watchdog=None):
    settings=Settings.load();runtime=Runtime(settings)
    if settings.provider=='deepseek' and settings.release_manifest:
        await runtime.encoder.encode_async(['Release embedding readiness check.'])
    server=grpc.aio.server(options=[('grpc.max_receive_message_length',64<<20),('grpc.max_send_message_length',64<<20)])
    rpc.add_AIRuntimeServicer_to_server(runtime,server)
    if not server.add_insecure_port(settings.address):raise ValueError('RUNTIME_LISTEN_FAILED')
    stopped=asyncio.Event();restore=install_termination_handler(asyncio.get_running_loop(),stopped,watchdog)
    waiters=[]
    try:
        await server.start()
        print(json.dumps({'event':'runtime_ready','address':settings.address,'protocol':'crackrag.v1','provider':settings.provider,'embedding':runtime.encoder.version}),flush=True)
        waiters=[asyncio.create_task(server.wait_for_termination()),asyncio.create_task(stopped.wait())]
        await asyncio.wait(waiters,return_when=asyncio.FIRST_COMPLETED)
    finally:
        if watchdog:watchdog.arm()
        try:
            # Cancelling gRPC's termination waiter before stop propagates into
            # its shared shutdown future and can turn normal SIGTERM into exit 1.
            await shutdown_runtime(server,runtime)
        finally:
            for waiter in waiters:
                if not waiter.done():waiter.cancel()
            await asyncio.gather(*waiters,return_exceptions=True)
            restore()

def run_runtime(shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS):
    watchdog=ShutdownWatchdog(shutdown_timeout)
    try:asyncio.run(main(watchdog))
    except KeyboardInterrupt:pass
    finally:watchdog.disarm()

if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING)
    run_runtime()
