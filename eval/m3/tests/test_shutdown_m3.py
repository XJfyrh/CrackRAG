"""Real owned parser processes and POSIX SIGTERM; never calls a model."""
import asyncio
import builtins
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crackrag_m1 import server as runtime_server
from crackrag_m1.config import Settings

def phase(directory,name,**details):
    value={'phase':name,'monotonic':time.monotonic(),'pid':os.getpid(),**details}
    Path(directory,name+'.json').write_text(json.dumps(value),encoding='utf-8')
    print(json.dumps(value),flush=True)

async def wait_marker(marker,seconds=45):
    until=asyncio.get_running_loop().time()+seconds
    while not marker.exists() and asyncio.get_running_loop().time()<until:
        await asyncio.sleep(.02)
    if not marker.exists():raise AssertionError('STARTUP_OR_HANDSHAKE_TIMEOUT:'+marker.name)


def busy_parser(marker,ignore_term=False):
    if ignore_term and os.name=='posix':signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path(marker).write_text(str(os.getpid()),encoding='utf-8')
    time.sleep(60)


async def pool_child(directory):
    phase(directory,'child-started')
    pool=ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn'))
    marker=Path(directory)/'parser.pid'
    pool.submit(busy_parser,str(marker),True)
    await wait_marker(marker)
    phase(directory,'pool-ready')
    await wait_marker(Path(directory)/'shutdown.request')
    phase(directory,'shutdown-started')
    started=time.monotonic()
    result=await runtime_server.shutdown_parse_pool(pool,grace_seconds=.1,kill_seconds=1)
    assert result['workers']==1 and result['remaining']==[],result
    assert time.monotonic()-started<2
    phase(directory,'pool-stopped',shutdown_seconds=time.monotonic()-started,**result)


def signal_child(directory,scenario='normal'):
    directory=Path(directory)
    phase(directory,'child-started',scenario=scenario)
    settings=replace(Settings.load(),provider='mock',embedding_mode='fixture',address='127.0.0.1:0',
        tools_address='127.0.0.1:1',mock_scenario='happy')
    original=runtime_server.Runtime
    def executor_block():
        phase(directory,'worker-ready',kind='default-executor')
        time.sleep(60)
    class BusyRuntime(original):
        keepalive=[]
        def __init__(self,config):
            super().__init__(config)
            if scenario=='default-executor':
                self.keepalive.append(asyncio.get_running_loop().run_in_executor(None,executor_block))
            elif scenario=='stubborn-task':
                async def stubborn():
                    phase(directory,'worker-ready',kind='cancellation-resistant-task')
                    try:await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        phase(directory,'runner-task-cancellation-seen')
                        await asyncio.Event().wait()
                self.keepalive.append(asyncio.create_task(stubborn()))
            else:
                self.parse_pool.submit(busy_parser,str(directory/'parser.pid'),True)
                async def ready():
                    await wait_marker(directory/'parser.pid');phase(directory,'worker-ready',kind='parser-process')
                self.keepalive.append(asyncio.create_task(ready()))
                async def background():
                    try:await asyncio.Event().wait()
                    finally:phase(directory,'background-cancelled')
                self.registry.register({'job_id':'shutdown-test-only','deadline_at':
                    (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()},'shutdown-run',background)
    def runtime_print(value,**kwargs):
        builtins.print(value,**kwargs)
        if json.loads(value).get('event')=='runtime_ready':phase(directory,'runtime-ready')
    with patch.object(Settings,'load',return_value=settings),patch.object(runtime_server,'Runtime',BusyRuntime),\
         patch.object(runtime_server,'print',runtime_print,create=True):
        runtime_server.run_runtime(shutdown_timeout=15 if scenario=='normal' else 1)
    phase(directory,'runtime-stopped')


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_preserves_channel_until_jobs_cancel_and_closes_parser_after_rpc_failure(self):
        events=[]
        class Server:
            async def stop(self,grace):events.append(('grpc',grace));raise RuntimeError('synthetic stop failure')
        class Registry:
            closing=False
            async def shutdown(self,grace_seconds):
                self.assert_closing=self.closing;events.append(('jobs',grace_seconds));return {'remaining':0}
        class Channel:
            async def close(self):events.append(('channel',))
        registry=Registry();runtime=SimpleNamespace(registry=registry,channel=Channel(),parse_pool=object())
        async def stop_parser(pool):events.append(('parser',));return {'remaining':[]}
        with patch.object(runtime_server,'shutdown_parse_pool',stop_parser),self.assertLogs(level='ERROR'):
            await runtime_server.shutdown_runtime(Server(),runtime)
        self.assertTrue(registry.assert_closing)
        self.assertEqual(events,[('grpc',3),('jobs',5),('channel',),('parser',)])

    async def test_windows_signal_fallback_sets_stop_event_and_restores_handler(self):
        stopped=asyncio.Event();loop=asyncio.get_running_loop();previous=signal.getsignal(signal.SIGTERM)
        with patch.object(loop,'add_signal_handler',side_effect=NotImplementedError):
            restore=runtime_server.install_termination_handler(loop,stopped)
            try:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
                await asyncio.wait_for(stopped.wait(),1)
            finally:restore()
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous)


class ProcessShutdownTests(unittest.TestCase):
    def run_child(self,mode,directory):
        return subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),mode,str(directory)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',start_new_session=os.name=='posix')

    def wait_ready(self,child,directory,names):
        until=time.monotonic()+45
        while time.monotonic()<until:
            if all((Path(directory)/(name+'.json')).exists() for name in names):return
            if child.poll() is not None:
                out,err=child.communicate()
                self.fail('STARTUP_EXITED:'+out+'\n'+err)
            time.sleep(.02)
        self.fail('STARTUP_TIMEOUT; completed phases='+str(sorted(p.name for p in Path(directory).glob('*.json'))))

    def closed(self,child,directory,seconds):
        try:return child.communicate(timeout=seconds)
        except subprocess.TimeoutExpired as exc:
            self.fail('SHUTDOWN_TIMEOUT; completed phases='+str(sorted(p.name for p in Path(directory).glob('*.json')))
                      +'; partial stdout='+repr(exc.stdout)+'; partial stderr='+repr(exc.stderr))

    def cleanup_child(self,child,directory):
        # Successful shutdown has already reaped its worker. Only clean up a
        # still-running test process tree, never a stale PID after normal exit.
        if child.poll() is None:
            if os.name=='posix':
                try:os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,timeout=3)
        child.communicate(timeout=3)

    def test_active_parser_pool_does_not_hold_interpreter_exit(self):
        with tempfile.TemporaryDirectory(prefix='m3-pool-shutdown-') as directory:
            child=self.run_child('--pool-child',directory)
            try:
                self.wait_ready(child,directory,['pool-ready'])
                started=time.monotonic();(Path(directory)/'shutdown.request').touch()
                out,err=self.closed(child,directory,12)
                self.assertLess(time.monotonic()-started,12)
                self.assertEqual(child.returncode,0,err);self.assertIn('pool-stopped',out)
            finally:self.cleanup_child(child,directory)

    @unittest.skipUnless(os.name=='posix','POSIX SIGTERM is exercised inside the Linux runtime image')
    def test_sigterm_real_runtime_exits_zero_with_active_parser_and_managed_job(self):
        with tempfile.TemporaryDirectory(prefix='m3-sigterm-shutdown-') as directory:
            child=self.run_child('--signal-child',directory)
            try:
                self.wait_ready(child,directory,['runtime-ready','worker-ready'])
                started=time.monotonic();child.send_signal(signal.SIGTERM)
                out,err=self.closed(child,directory,13)
                self.assertEqual(child.returncode,0,err)
                self.assertIn('runtime_ready',out);self.assertIn('runtime-stopped',out)
                self.assertTrue((Path(directory)/'background-cancelled.json').exists())
                self.assertLess(time.monotonic()-started,13)
            finally:self.cleanup_child(child,directory)

    @unittest.skipUnless(os.name=='posix','POSIX self-process watchdog is exercised inside the Linux runtime image')
    def test_shutdown_watchdog_bounds_runner_cancellation_and_default_executor(self):
        for scenario in ('stubborn-task','default-executor'):
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory(prefix='m3-watchdog-') as directory:
                child=self.run_child('--'+scenario,directory)
                try:
                    self.wait_ready(child,directory,['runtime-ready','worker-ready'])
                    started=time.monotonic();child.send_signal(signal.SIGTERM)
                    out,err=self.closed(child,directory,4)
                    elapsed=time.monotonic()-started
                    self.assertEqual(child.returncode,70,err)
                    self.assertIn('runtime_shutdown_forced',err)
                    self.assertIn('SHUTDOWN_DEADLINE_EXCEEDED',err)
                    self.assertGreaterEqual(elapsed,.8);self.assertLess(elapsed,4)
                    if scenario=='stubborn-task':
                        self.assertTrue((Path(directory)/'runner-task-cancellation-seen.json').exists(),out+err)
                finally:self.cleanup_child(child,directory)


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--pool-child':asyncio.run(pool_child(sys.argv[2]))
    elif len(sys.argv)>1 and sys.argv[1]=='--signal-child':signal_child(sys.argv[2])
    elif len(sys.argv)>1 and sys.argv[1] in ('--stubborn-task','--default-executor'):
        signal_child(sys.argv[2],sys.argv[1][2:])
    else:unittest.main()
