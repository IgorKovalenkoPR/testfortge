"""TestFortge — In-process background job queue.

Why this exists
---------------
Both ``/automation/run`` (Playwright) and ``/estimation/run`` (site crawl)
can take tens of seconds to a few minutes. Running them in the request
thread blocks the user's browser, ties up a Werkzeug worker, and risks
an HTTP timeout when reverse-proxied. Moving them to a thread pool lets
the routes submit the job and return immediately with a ``job_id``; the
UI polls a status endpoint until the result is ready.

Scope & trade-offs
------------------
* **In-process, in-memory.** No Redis, no Celery. Jobs are lost on
  restart — which matches the existing session-wipe-on-restart guarantee
  (``SERVER_START_TIME``) so users never see stale in-flight jobs.
* **ThreadPoolExecutor** with a small default worker count. Playwright
  already spawns a subprocess, so most time is I/O-bound and threads
  are fine. Increase via ``JOB_QUEUE_WORKERS`` env var if needed.
* **Thread-safe result store** keyed by job_id. Results carry a
  monotonically-increasing timestamp so the UI can tell if anything
  changed since the last poll.
* **Automatic cleanup.** Jobs older than ``JOB_RETENTION_SECONDS`` (30
  minutes by default) are pruned lazily on each ``get`` / ``submit`` so
  the dict can't grow unbounded.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

from engine.log import get_logger

log = get_logger(__name__)


# ── Status constants ─────────────────────────────────────────────
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class Job:
    """A single background job's state.

    Only ``status`` + ``updated_at`` change after submission — callers
    can diff by ``updated_at`` to avoid re-rendering unchanged data.
    """

    id: str
    kind: str                 # "automation" | "estimation" | ...
    status: str = PENDING     # one of PENDING / RUNNING / DONE / FAILED
    result: Any = None
    error: str = ""
    progress: float = 0.0     # 0.0 – 1.0, optional
    message: str = ""         # human-readable current step
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)  # free-form user-supplied context

    def to_public_dict(self) -> dict:
        """Dict safe to hand to the browser (omits ``result`` payload —
        callers serialize that separately when ``status == DONE``)."""
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "progress": self.progress, "message": self.message,
            "error": self.error,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "meta": self.meta,
        }


# ── Queue ───────────────────────────────────────────────────────
_MAX_WORKERS = int(os.environ.get("JOB_QUEUE_WORKERS", "2"))
_RETENTION = int(os.environ.get("JOB_RETENTION_SECONDS", "1800"))  # 30 min


class JobQueue:
    """Thread-safe in-process job store backed by a ``ThreadPoolExecutor``.

    A single global instance is exposed via :func:`get_queue` — routes
    don't instantiate their own.
    """

    def __init__(self, max_workers: int = _MAX_WORKERS,
                 retention_seconds: int = _RETENTION) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="tf-job"
        )
        self._retention = retention_seconds

    # ── Public API ──────────────────────────────────────────────

    def submit(self, kind: str, func: Callable[..., Any], *args,
               meta: Optional[dict] = None, **kwargs) -> str:
        """Submit ``func(*args, **kwargs)`` to the pool and return job_id.

        ``kind`` is a category tag (e.g. ``"automation"``) used by the
        UI to route results back to the right page. ``meta`` is stored
        verbatim for the caller's convenience.
        """
        job = Job(id=uuid.uuid4().hex, kind=kind, meta=meta or {})
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job

        def _runner():
            self._mark_running(job.id)
            try:
                result = func(*args, **kwargs)
                self._mark_done(job.id, result)
            except BaseException as exc:  # noqa: BLE001 — preserve all failures
                log.exception("job %s (%s) failed", job.id, kind)
                self._mark_failed(job.id, f"{type(exc).__name__}: {exc}")

        self._executor.submit(_runner)
        return job.id

    def get(self, job_id: str) -> Optional[Job]:
        """Return the job or ``None``. Prunes old jobs as a side effect."""
        with self._lock:
            self._prune_locked()
            return self._jobs.get(job_id)

    def set_progress(self, job_id: str, progress: float,
                     message: str = "") -> None:
        """Worker-side helper — update progress without locking outside."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress = max(0.0, min(1.0, float(progress)))
            if message:
                job.message = message
            job.updated_at = time.time()

    def list_kind(self, kind: str) -> list[Job]:
        """Return all current jobs of a given kind (most recent first)."""
        with self._lock:
            self._prune_locked()
            return sorted(
                (j for j in self._jobs.values() if j.kind == kind),
                key=lambda j: j.created_at,
                reverse=True,
            )

    # ── Private helpers ─────────────────────────────────────────

    def _mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = RUNNING
                job.updated_at = time.time()

    def _mark_done(self, job_id: str, result: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = DONE
                job.result = result
                job.progress = 1.0
                job.updated_at = time.time()

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = FAILED
                job.error = error
                job.updated_at = time.time()

    def _prune_locked(self) -> None:
        """Drop finished jobs older than the retention window.
        Caller must hold ``self._lock``."""
        cutoff = time.time() - self._retention
        stale = [
            jid for jid, j in self._jobs.items()
            if j.status in (DONE, FAILED) and j.updated_at < cutoff
        ]
        for jid in stale:
            self._jobs.pop(jid, None)


# ── Module-level singleton ──────────────────────────────────────
_QUEUE: Optional[JobQueue] = None
_QUEUE_LOCK = threading.Lock()


def get_queue() -> JobQueue:
    """Return the process-wide :class:`JobQueue` (lazy-initialised)."""
    global _QUEUE
    if _QUEUE is None:
        with _QUEUE_LOCK:
            if _QUEUE is None:
                _QUEUE = JobQueue()
    return _QUEUE


__all__ = [
    "PENDING", "RUNNING", "DONE", "FAILED",
    "Job", "JobQueue", "get_queue",
]
