"""Managed, bounded background lifetime. PostgreSQL remains authoritative."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class ManagedTask:
    job_id: str
    run_id: str
    deadline_at: str
    task: asyncio.Task
    cancel_reason: str = ''


class TaskRegistry:
    def __init__(self, *, terminal_limit=512):
        self.active = {}
        self.finished = OrderedDict()
        self.terminal_limit = terminal_limit
        self.closing = False

    def register(self, durable_job, run_id, factory: Callable):
        # Deliberately accept an already returned durable identity, never a
        # coroutine that may begin side effects before persistence succeeds.
        if self.closing:
            raise ValueError('RUNTIME_SHUTTING_DOWN')
        job_id = durable_job.get('job_id')
        if not job_id or not durable_job.get('deadline_at'):
            raise ValueError('DURABLE_JOB_REQUIRED')
        if job_id in self.active:
            return self.active[job_id]
        deadline = datetime.fromisoformat(durable_job['deadline_at'])
        if deadline.tzinfo is None:
            raise ValueError('TIMESTAMP_REQUIRES_TIMEZONE')
        timeout = max(0, (deadline-datetime.now(timezone.utc)).total_seconds())

        async def bounded():
            if timeout <= 0:
                raise TimeoutError('JOB_DEADLINE_EXCEEDED')
            async with asyncio.timeout(timeout):
                return await factory()

        task = asyncio.create_task(bounded(), name='m3-job-' + job_id)
        managed = ManagedTask(job_id, run_id, durable_job['deadline_at'], task)
        self.active[job_id] = managed

        def done(completed):
            self.active.pop(job_id, None)
            try:
                value = completed.result()
                result = {'state': 'RETURNED', 'result': value}
            except asyncio.CancelledError:
                result = {'state': 'CANCELLED', 'reason': managed.cancel_reason or 'CANCELLED'}
            except BaseException as exc:
                result = {'state': 'FAILED', 'reason': type(exc).__name__}
                log.error('M3 background job failed job_id=%s error_type=%s', job_id, type(exc).__name__)
            self.finished[job_id] = result
            while len(self.finished) > self.terminal_limit:
                self.finished.popitem(last=False)

        task.add_done_callback(done)
        return managed

    def cancel_run(self, run_id, reason='EXPLICIT_CANCEL'):
        count = 0
        for managed in list(self.active.values()):
            if managed.run_id == run_id:
                managed.cancel_reason = reason
                managed.task.cancel()
                count += 1
        return count

    async def shutdown(self, grace_seconds=5):
        self.closing = True
        tasks = list(self.active.values())
        for managed in tasks:
            managed.cancel_reason = 'RUNTIME_SHUTDOWN'
            managed.task.cancel()
        if not tasks:
            return {'completed': 0, 'remaining': 0}
        done, pending = await asyncio.wait([m.task for m in tasks], timeout=grace_seconds)
        return {'completed': len(done), 'remaining': len(pending),
                'unfinished_job_ids': [m.job_id for m in tasks if m.task in pending]}


async def admit_with_one_refresh(snapshot, refresh, check):
    """At most one refresh; authoritative Go reserve still happens afterwards."""
    reason = check(snapshot)
    refreshed = 0
    if reason == 'SNAPSHOT_STALE':
        snapshot = await refresh()
        refreshed = 1
        reason = check(snapshot)
    return snapshot, refreshed, reason
