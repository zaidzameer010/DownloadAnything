"""Shared job-manager plumbing for the yt-dlp and torrent managers.

Both :mod:`engine` and :mod:`torrent` manage a dict of job objects that are
emitted to websocket listeners (throttled), persisted to SQLite in per-job
order, and executed on a thread pool bounded by a shared work-slot limiter.
This module holds that shared machinery so the two managers only implement
their engine-specific behavior.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import structlog

log = structlog.get_logger("da.jobs")


class JobManager:
    """Base for managers that own jobs, listeners, DB writes, and workers."""

    #: Throttle window between non-forced emissions, in seconds.
    emit_interval = 0.1

    def _init_job_state(self) -> None:
        self._jobs: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._listeners: list[Any] = []
        self._listener_tasks: set[asyncio.Task[None]] = set()
        self._worker_futures: dict[str, Any] = {}
        self._db_tasks: dict[str, asyncio.Task[Any]] = {}
        self._last_emit: dict[str, float] = {}

    # ----------------------------------------------------------------- events
    def add_listener(self, callback: Any) -> None:
        """Register an async callable(job_snapshot) invoked on the event loop."""
        self._listeners.append(callback)

    async def _dispatch_listener(self, listener: Any, snapshot: dict[str, Any]) -> None:
        try:
            await listener(snapshot)
        except Exception:  # noqa: BLE001
            log.exception("Job listener failed")

    def _post_emit(self, job: Any, *, force: bool) -> None:
        """Hook run after a successful emit; callers hold :attr:`_lock`."""

    def _emit_core(self, job: Any, *, force: bool = False) -> None:
        """Throttled snapshot emission; caller must hold :attr:`_lock`."""
        if job.data.get("removed"):
            return
        now = time.monotonic()
        if not force and now - self._last_emit.get(job.id, 0) < self.emit_interval:
            return
        self._last_emit[job.id] = now
        job.event_revision += 1
        if force:
            self._persist(job)
        snapshot = job.snapshot()
        # Capture the listener list while we hold the lock so the dispatch
        # closure doesn't read mutable state from another thread.
        listeners = list(self._listeners)

        def _dispatch() -> None:
            for listener in listeners:
                task = asyncio.create_task(self._dispatch_listener(listener, snapshot))
                self._listener_tasks.add(task)
                task.add_done_callback(self._listener_tasks.discard)

        try:
            self._loop.call_soon_threadsafe(_dispatch)
        except RuntimeError:
            # Event loop closed during shutdown; a late emit must not
            # propagate into the worker thread and fail the job.
            log.debug("Could not dispatch job emit: event loop is closed")
        self._post_emit(job, force=force)

    # -------------------------------------------------------------- database
    def _enqueue_db_operation(self, job_id: str, operation: Any) -> None:
        """Run ``operation`` on the event loop after any pending op for the job."""

        def _schedule() -> None:
            previous = self._db_tasks.get(job_id)

            async def run() -> None:
                if previous is not None:
                    try:
                        await previous
                    except Exception:  # noqa: BLE001
                        log.exception("Previous database operation failed", job_id=job_id)
                try:
                    await operation()
                except Exception:  # noqa: BLE001
                    log.exception("Database operation failed", job_id=job_id)

            task = asyncio.ensure_future(run())
            self._db_tasks[job_id] = task

            def _cleanup(done: asyncio.Future[Any]) -> None:
                if self._db_tasks.get(job_id) is done:
                    self._db_tasks.pop(job_id, None)

            task.add_done_callback(_cleanup)

        try:
            self._loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            log.error("Could not schedule database operation: event loop is closed", job_id=job_id)

    def _persist(self, job: Any) -> None:
        """Schedule an ordered job snapshot write on the event loop."""
        if job.data.get("removed"):
            return
        snapshot = job.snapshot()

        async def write() -> None:
            if not job.data.get("removed"):
                await self._db.upsert_job(snapshot)

        self._enqueue_db_operation(job.id, write)

    def _delete_persisted(self, job_id: str) -> None:
        self._enqueue_db_operation(job_id, lambda: self._db.delete_job(job_id))

    # ----------------------------------------------------------------- worker
    def _submit(self, job: Any) -> None:
        def _clear(_future: Any) -> None:
            with self._lock:
                if self._worker_futures.get(job.id) is _future:
                    self._worker_futures.pop(job.id, None)
            if not _future.cancelled():
                exc = _future.exception()
                if exc:
                    log.error("Job worker thread crashed", job_id=job.id, error=str(exc))

        with self._lock:
            # Register before submit: a job that returns immediately
            # (already cancelled/removed) would otherwise run its done
            # callback before the future is tracked, leaking a stale entry.
            future = self._executor.submit(self._run, job)
            self._worker_futures[job.id] = future
        future.add_done_callback(_clear)

    def _cancel_queued_worker(self, job_id: str) -> None:
        with self._lock:
            future = self._worker_futures.get(job_id)
        if future is not None:
            future.cancel()

    def _acquire_work_slot(self, job: Any) -> bool:
        while not job.cancel_event.is_set():
            if self._work_slots.acquire(timeout=0.1):
                return True
        return False

    # ------------------------------------------------------------------ queue
    def get(self, job_id: str) -> Any:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.snapshot() for job in self._jobs.values()]
