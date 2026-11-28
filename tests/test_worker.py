"""Whole-job ordering, thread ownership, cancellation and worker shutdown."""

import asyncio
import threading

import anyio
import pytest

from rigol_mcp.worker import InstrumentWorker


async def wait_for(event):
    assert await asyncio.to_thread(event.wait, 3), "worker did not reach the expected step"


async def test_jobs_and_cleanup_use_one_thread_and_loop():
    entered, release = threading.Event(), threading.Event()
    events = []
    owners = []

    def record(label):
        events.append(label)
        owners.append((threading.get_ident(), id(asyncio.get_running_loop())))

    async def first():
        record("first start")
        entered.set()
        assert release.wait(3)
        # Even a yield inside a tool must not let the next tool start.
        await asyncio.sleep(0)
        record("first finish")
        return 1

    async def second():
        record("second")
        return 2

    async def cleanup():
        record("close")

    worker = InstrumentWorker(cleanup)
    try:
        a = asyncio.wrap_future(worker.submit(first))
        await wait_for(entered)
        b = asyncio.wrap_future(worker.submit(second))
        assert events == ["first start"]
        release.set()
        assert await asyncio.gather(a, b) == [1, 2]
    finally:
        release.set()
        await worker.aclose()
    assert events == ["first start", "first finish", "second", "close"]
    assert len(set(owners)) == 1
    assert owners[0][0] != threading.get_ident()


async def test_cancelled_queued_job_never_runs():
    entered, release = threading.Event(), threading.Event()
    ran = []

    async def active():
        entered.set()
        assert release.wait(3)

    async def queued():
        ran.append("queued")

    async def cleanup():
        ran.append("close")

    worker = InstrumentWorker(cleanup)
    try:
        a = asyncio.wrap_future(worker.submit(active))
        await wait_for(entered)
        b = asyncio.wrap_future(worker.submit(queued))
        b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await b
        # Allow asyncio's cancellation callback to cancel the concurrent Future.
        await asyncio.sleep(0)
        release.set()
        await a
    finally:
        release.set()
        await worker.aclose()
    assert ran == ["close"]


async def test_cancelled_active_job_finishes_before_next_job():
    entered, release = threading.Event(), threading.Event()
    events = []

    async def active():
        events.append("start")
        entered.set()
        assert release.wait(3)
        await asyncio.sleep(0)
        events.append("cleanup")

    async def next_job():
        events.append("next")

    async def cleanup():
        events.append("close")

    worker = InstrumentWorker(cleanup)
    try:
        a = asyncio.wrap_future(worker.submit(active))
        await wait_for(entered)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        b = asyncio.wrap_future(worker.submit(next_job))
        await asyncio.sleep(0)
        assert events == ["start"]
        release.set()
        await b
    finally:
        release.set()
        await worker.aclose()
    assert events == ["start", "cleanup", "next", "close"]


async def test_failed_job_does_not_stop_worker():
    async def fail():
        raise ValueError("bad setting")

    async def success():
        return "ok"

    worker = InstrumentWorker(success)
    try:
        with pytest.raises(ValueError, match="bad setting"):
            await asyncio.wrap_future(worker.submit(fail))
        assert await asyncio.wrap_future(worker.submit(success)) == "ok"
    finally:
        await worker.aclose()


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_shutdown_waits_for_active_job_and_cancels_queue(cancellation):
    entered, release, skipped = threading.Event(), threading.Event(), threading.Event()
    events = []

    async def active():
        entered.set()
        assert release.wait(3)
        events.append("finished")

    async def queued():
        events.append("must not run")

    async def cleanup():
        events.append("closed")

    worker = InstrumentWorker(cleanup)
    a = asyncio.wrap_future(worker.submit(active))
    try:
        await wait_for(entered)
        pending = worker.submit(queued)
        pending.add_done_callback(lambda _: skipped.set())

        async def close():
            if cancellation == "anyio":
                with anyio.CancelScope() as scope:
                    scope.cancel()
                    await worker.aclose()
            else:
                await worker.aclose()

        closing = asyncio.create_task(close())
        await wait_for(skipped)
        assert pending.cancelled()
        assert not closing.done() and not events
        with pytest.raises(RuntimeError, match="shutting down"):
            worker.submit(queued)
        if cancellation == "asyncio":
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        release.set()
        await a
        if cancellation == "asyncio":
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
    finally:
        release.set()
        await worker.aclose()
    assert events == ["finished", "closed"]
