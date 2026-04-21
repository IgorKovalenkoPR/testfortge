"""Unit tests for engine.job_queue (Wave C.4 infrastructure).

Covers:
  * submit → run → done lifecycle with result propagation
  * exception handling surfaces as FAILED with error text
  * progress updates via set_progress()
  * wrong-kind isolation (list_kind) and retention pruning
  * get() returns None for unknown ids
"""

import time

import pytest

from engine.job_queue import (
    DONE, FAILED, PENDING, RUNNING,
    Job, JobQueue, get_queue,
)


def _wait_for(q: JobQueue, job_id: str, terminal=(DONE, FAILED), timeout: float = 3.0):
    """Block until the job reaches a terminal status or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = q.get(job_id)
        if job is not None and job.status in terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not reach terminal status in {timeout}s")


class TestJobQueueLifecycle:
    def test_success_result_propagates(self):
        q = JobQueue(max_workers=2)
        jid = q.submit("unit", lambda a, b: a + b, 2, 3)
        job = _wait_for(q, jid)
        assert job.status == DONE
        assert job.result == 5
        assert job.progress == 1.0
        assert job.error == ""

    def test_kwargs_and_meta_preserved(self):
        q = JobQueue(max_workers=1)
        jid = q.submit("unit", lambda x, mult=1: x * mult, 4, mult=3,
                       meta={"context": "mult-test"})
        job = _wait_for(q, jid)
        assert job.result == 12
        assert job.meta == {"context": "mult-test"}

    def test_failure_reports_error(self):
        q = JobQueue(max_workers=1)
        jid = q.submit("unit", lambda: 1 / 0)
        job = _wait_for(q, jid)
        assert job.status == FAILED
        assert "ZeroDivisionError" in job.error
        assert job.result is None

    def test_progress_updates(self):
        import threading
        q = JobQueue(max_workers=1)
        jid_holder: dict = {}
        ready = threading.Event()

        def _work():
            # The worker thread can start before submit() returns the id to
            # the caller, so wait for the main thread to record it. Timeout
            # guards against a bug that would otherwise hang the test.
            if not ready.wait(timeout=1.0):
                return "timeout-waiting-for-id"
            q.set_progress(jid_holder["id"], 0.4, "halfway")
            return "ok"

        jid = q.submit("unit", _work)
        jid_holder["id"] = jid
        ready.set()
        job = _wait_for(q, jid)
        assert job.status == DONE
        assert job.result == "ok"
        # Terminal _mark_done sets progress back to 1.0, so we only assert
        # that the message was recorded mid-flight.
        assert job.message == "halfway"


class TestJobQueueLookup:
    def test_get_unknown_returns_none(self):
        q = JobQueue()
        assert q.get("no-such-id") is None

    def test_list_kind_filters_correctly(self):
        q = JobQueue(max_workers=2)
        a = q.submit("alpha", lambda: 1)
        b = q.submit("alpha", lambda: 2)
        c = q.submit("beta", lambda: 3)
        for jid in (a, b, c):
            _wait_for(q, jid)
        alphas = q.list_kind("alpha")
        betas = q.list_kind("beta")
        assert {j.id for j in alphas} == {a, b}
        assert {j.id for j in betas} == {c}


class TestJobQueueRetention:
    def test_finished_jobs_pruned_after_retention(self):
        # Short retention window — long enough to confirm DONE status
        # before the job ages out, short enough to not slow the suite.
        q = JobQueue(max_workers=1, retention_seconds=0.2)
        jid = q.submit("unit", lambda: "done")
        job = _wait_for(q, jid)
        assert job.status == DONE
        # Wait past the retention window so lazy pruning on next get()
        # evicts the terminal record.
        time.sleep(0.3)
        assert q.get(jid) is None

    def test_running_jobs_not_pruned(self):
        """Only terminal jobs get pruned — a running job must survive even
        if retention is tiny, so progress polling never loses its target."""
        import threading
        release = threading.Event()
        q = JobQueue(max_workers=1, retention_seconds=0.1)
        jid = q.submit("unit", lambda: release.wait(timeout=1.0))
        # Give the worker a moment to enter RUNNING.
        time.sleep(0.05)
        # Wait past the retention window with the job still running.
        time.sleep(0.2)
        assert q.get(jid) is not None  # still tracked
        release.set()
        _wait_for(q, jid)


class TestSingleton:
    def test_get_queue_returns_same_instance(self):
        q1 = get_queue()
        q2 = get_queue()
        assert q1 is q2


class TestShutdown:
    def test_shutdown_is_idempotent(self):
        q = JobQueue(max_workers=1)
        q.shutdown(wait=False)
        q.shutdown(wait=False)  # must not raise

    def test_submit_after_shutdown_raises(self):
        q = JobQueue(max_workers=1)
        q.shutdown(wait=False)
        with pytest.raises(RuntimeError, match="shutting down"):
            q.submit("unit", lambda: 1)

    def test_shutdown_waits_when_requested(self):
        q = JobQueue(max_workers=1)
        jid = q.submit("unit", lambda: "finished")
        q.shutdown(wait=True)
        # With wait=True, the in-flight job must complete before shutdown
        # returns, so the result is observable immediately without polling.
        job = q.get(jid)
        assert job is not None
        assert job.status == DONE
        assert job.result == "finished"
