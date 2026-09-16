import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ai-runtime/src'))
from crackrag_m1.embedding import DenseEncoder,terms


class ObservedEncoder(DenseEncoder):
    def __init__(self):
        super().__init__(SimpleNamespace(embedding_mode='fixture'))
        self.started=threading.Event();self.finished=threading.Event()
    def encode(self,texts,**kwargs):
        self.started.set()
        try:return super().encode(texts,**kwargs)
        finally:self.finished.set()


class EncoderCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def wait_event(self,event):
        self.assertTrue(await asyncio.to_thread(event.wait,2),'worker did not reach expected boundary')
    async def cancel(self,task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
    async def test_cancelled_lock_waiter_exits_without_encoding(self):
        encoder=ObservedEncoder();computed=[]
        def observe(text):computed.append(text);return terms(text)
        encoder.lock.acquire()
        try:
            with patch('crackrag_m1.embedding.terms',observe):
                task=asyncio.create_task(encoder.encode_async(['cancelled']))
                await self.wait_event(encoder.started)
                await self.cancel(task)
                # It exits even while the other operation still owns the lock.
                await self.wait_event(encoder.finished)
                self.assertEqual(computed,[])
        finally:
            encoder.lock.release()
        with patch('crackrag_m1.embedding.terms',observe):
            await encoder.encode_async(['active'])
        self.assertEqual(computed,['active'])
    async def test_cancel_during_load_stops_before_first_batch(self):
        encoder=ObservedEncoder();loaded=threading.Event();release=threading.Event();computed=[]
        def load():
            loaded.set()
            if not release.wait(2):raise AssertionError('load boundary not released')
        def observe(text):computed.append(text);return terms(text)
        with patch.object(encoder,'load',load),patch('crackrag_m1.embedding.terms',observe):
            task=asyncio.create_task(encoder.encode_async(['cancelled']))
            try:
                await self.wait_event(loaded)
                await self.cancel(task)
            finally:release.set()
            await self.wait_event(encoder.finished)
        self.assertEqual(computed,[])
    async def test_cancel_during_batch_stops_remaining_batches(self):
        encoder=ObservedEncoder();entered=threading.Event();release=threading.Event();computed=[]
        def observe(text):
            computed.append(text);entered.set()
            if not release.wait(2):raise AssertionError('batch boundary not released')
            return terms(text)
        with patch('crackrag_m1.embedding.terms',observe):
            task=asyncio.create_task(encoder.encode_async(['first','second','third']))
            try:
                await self.wait_event(entered)
                await self.cancel(task)
            finally:release.set()
            await self.wait_event(encoder.finished)
        self.assertEqual(computed,['first'])


if __name__=='__main__':unittest.main()
