import asyncio
import pytest
from llm_client.progress import stage_progress, gather, request_progress


def test_progress_counts_failures_requests_and_preserves_results():
    async def exercise():
        logs=[]
        async def good():
            with request_progress():
                await asyncio.sleep(.02)
            return 7
        async def bad():
            with request_progress():
                await asyncio.sleep(.01)
                raise ValueError('test')
        async with stage_progress('comparison', lambda s,m: logs.append(m), interval=.005):
            results=await gather(good(),bad(),label='papers',return_exceptions=True)
        assert results[0]==7 and isinstance(results[1],ValueError)
        assert any('active=2' in s for s in logs)
        assert 'papers[completed=1/2 active=0 queued=0 failed=1 cancelled=0]' in logs[-1]
        assert 'started=2 active=0 returned=1 failed=1' in logs[-1]
        # No reporter remains after the stage exits.
        size=len(logs)
        await asyncio.sleep(.01)
        assert len(logs)==size
    asyncio.run(exercise())


def test_progress_cancellation_and_disabled_mode():
    async def exercise():
        logs=[]
        async def cancel():
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            async with stage_progress('test',lambda s,m:logs.append(m)):
                await gather(cancel(),label='items')
        assert 'cancelled=1' in logs[-1]
        assert await gather(asyncio.sleep(0,result=8),label='ignored')==[8]
    asyncio.run(exercise())
