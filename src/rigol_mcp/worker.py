"""Run complete instrument tool calls sequentially on one dedicated thread."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import RLock

import anyio


class InstrumentWorker:
    """A single-worker queue is the exclusion boundary for an entire tool call.

    Each job runs to completion, including its async retry/pacing waits, before
    the next job starts. Cancelling a Future skips a queued job but cannot
    interrupt a running job halfway through instrument changes or cleanup.
    """

    def __init__(self, cleanup):
        self._cleanup = cleanup
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rigol-instrument")
        self._runner: asyncio.Runner | None = None
        self._pending: set[Future] = set()
        self._state_lock = RLock()
        self._shutdown: Future | None = None

    def submit(self, function, *args, **kwargs) -> Future:
        """Queue an async tool handler; the returned Future is safe across threads."""
        with self._state_lock:
            if self._shutdown is not None:
                raise RuntimeError("Instrument worker is shutting down")
            future = self._executor.submit(self._run, copy_context(), function, args, kwargs)
            self._pending.add(future)
        future.add_done_callback(self._forget)
        return future

    def _forget(self, future):
        with self._state_lock:
            self._pending.discard(future)

    def _run(self, context, function, args, kwargs):
        # Preserve one event loop on the instrument thread for the existing async
        # handlers and their locks. The network event loop never runs VISA code.
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(function(*args, **kwargs), context=context)

    def _finish(self):
        try:
            self._run(copy_context(), self._cleanup, (), {})
        finally:
            if self._runner is not None:
                self._runner.close()

    async def aclose(self):
        """Skip queued jobs, finish the active job, and close on its owning thread."""
        with self._state_lock:
            if self._shutdown is None:
                for future in tuple(self._pending):
                    future.cancel()
                self._shutdown = self._executor.submit(self._finish)
                self._executor.shutdown(wait=False)
            finished = asyncio.wrap_future(self._shutdown)

        # MCP uses AnyIO cancellation scopes. Shield shutdown from those and from
        # explicit asyncio task cancellation: closing VISA must follow active I/O.
        cancelled = False
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(finished)
                    break
                except asyncio.CancelledError:
                    if finished.done():
                        # A cleanup that itself raises CancelledError must propagate,
                        # rather than being mistaken for cancellation of this waiter.
                        finished.result()
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError
