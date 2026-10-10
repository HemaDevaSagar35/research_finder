"""Stage-local progress; counters never affect budgets or scientific results."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time

_current = ContextVar('stage_progress', default=None)


@dataclass
class Counts:
    total: int = 0
    active: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0

    def text(self):
        queued = self.total - self.active - self.done - self.failed - self.cancelled
        return (f'completed={self.done}/{self.total} active={self.active} '
                f'queued={queued} failed={self.failed} cancelled={self.cancelled}')


@contextmanager
def request_progress():
    tracker = _current.get()
    if tracker is None:
        yield
        return
    counts = tracker.calls
    counts.total += 1
    counts.active += 1
    try:
        yield
    except asyncio.CancelledError:
        counts.cancelled += 1
        raise
    except BaseException:
        counts.failed += 1
        raise
    else:
        counts.done += 1
    finally:
        counts.active -= 1


async def gather(*tasks, label, return_exceptions=False):
    tracker = _current.get()
    if tracker is None:
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)
    counts = tracker.groups.setdefault(label, Counts())
    counts.total += len(tasks)
    async def one(task):
        counts.active += 1
        try:
            result = await task
        except asyncio.CancelledError:
            counts.cancelled += 1
            raise
        except BaseException:
            counts.failed += 1
            raise
        else:
            # Stage-specific unresolved results are settled work, not exceptions.
            if isinstance(result, BaseException):
                counts.failed += 1
            else:
                counts.done += 1
            return result
        finally:
            counts.active -= 1
    return await asyncio.gather(*(one(t) for t in tasks), return_exceptions=return_exceptions)


@asynccontextmanager
async def stage_progress(stage, callback, interval=15):
    if callback is None:
        yield
        return
    tracker = Tracker()
    token = _current.set(tracker)
    started = time.monotonic()
    def report():
        groups = '; '.join(f'{name}[{c.text()}]' for name,c in tracker.groups.items())
        calls = tracker.calls
        callback(stage, f'progress elapsed={time.monotonic()-started:.1f}s '
                 f'{groups} llm_requests[started={calls.total} active={calls.active} '
                 f'returned={calls.done} failed={calls.failed} cancelled={calls.cancelled}] '
                 '(work totals may grow; completed does not mean approved)')
    async def heartbeat():
        while True:
            await asyncio.sleep(interval)
            report()
    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        report()
        _current.reset(token)


class Tracker:
    def __init__(self):
        self.groups = {}
        self.calls = Counts()
