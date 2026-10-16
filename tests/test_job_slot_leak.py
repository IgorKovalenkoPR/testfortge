"""A hung generation job must not hold its slot forever.

Found 2026-09-16 on staging: a 503 during generation, and from then on
every retry came back 429 — "You already have 2 active generation jobs" —
until the process restarted. ``count_active_by_meta`` counts PENDING and
RUNNING jobs, and ``_prune_locked`` only ever dropped DONE/FAILED ones, so
a job whose thread never returned (a browser or a model call that hung,
while the proxy answered the poller 502/503) counted as active for the
life of the process. The process dying does not cause it — the queue
dies with the process — a thread that never finishes does.

Fixed 2026-10-16: a PENDING/RUNNING job with no update for
``JOB_STALE_SECONDS`` is marked FAILED and stops counting. The hung jobs
here wait on an Event nobody sets until the end, so the slot is held for
certain rather than by timing luck.
"""
from __future__ import annotations

import threading
import time

import pytest

from engine import job_queue as jq
from engine.job_queue import DONE, FAILED, RUNNING, JobQueue


def _hang(gate):
    def run():
        gate.wait(timeout=10)
        return {"tc_dicts": []}
    return run


def _until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestTheQueue:
    def test_a_hung_job_stops_counting_once_stale(self):
        gate = threading.Event()
        q = JobQueue(max_workers=1, stale_seconds=0.3)
        try:
            jid = q.submit("tc_gen", _hang(gate), meta={"session_id": "s"})
            assert _until(lambda: q.get(jid).status == RUNNING)
            assert q.count_active_by_meta("tc_gen", "session_id", "s") == 1
            time.sleep(0.45)
            assert q.count_active_by_meta("tc_gen", "session_id", "s") == 0
            job = q.get(jid)
            assert job.status == FAILED and job.error.startswith("Abandoned")
        finally:
            gate.set()
            q.shutdown(wait=True)

    def test_a_queued_job_behind_hung_ones_is_freed_too(self):
        """Two hung workers fill the pool; the third job never starts and
        sat PENDING — counted — for as long as they hung."""
        gate = threading.Event()
        q = JobQueue(max_workers=2, stale_seconds=0.3)
        try:
            for _ in range(3):
                q.submit("tc_gen", _hang(gate), meta={"session_id": "s"})
            assert q.count_active_by_meta("tc_gen", "session_id", "s") == 3
            time.sleep(0.45)
            assert q.count_active_by_meta("tc_gen", "session_id", "s") == 0
        finally:
            gate.set()
            q.shutdown(wait=True)

    def test_a_job_within_the_window_still_counts(self):
        gate = threading.Event()
        q = JobQueue(max_workers=1, stale_seconds=60)
        try:
            q.submit("tc_gen", _hang(gate), meta={"session_id": "s"})
            time.sleep(0.2)
            assert q.count_active_by_meta("tc_gen", "session_id", "s") == 1
        finally:
            gate.set()
            q.shutdown(wait=True)

    def test_a_late_finish_still_records_its_result(self):
        gate = threading.Event()
        q = JobQueue(max_workers=1, stale_seconds=0.2)
        try:
            jid = q.submit("tc_gen", lambda: (gate.wait(timeout=10), "late")[1])
            time.sleep(0.35)
            assert q.get(jid).status == FAILED
            gate.set()
            assert _until(lambda: q.get(jid).status == DONE)
            assert q.get(jid).result == "late"
        finally:
            gate.set()
            q.shutdown(wait=True)

    def test_the_default_window_is_thirty_minutes(self):
        assert JobQueue()._stale == jq._STALE == 1800


class TestTheRoute:
    def test_a_session_with_hung_jobs_can_generate_again(self, client, monkeypatch):
        """The user-visible half: 429 while the slots are held, accepted
        once the hung jobs are abandoned."""
        q = JobQueue(max_workers=4, stale_seconds=0.4)
        monkeypatch.setattr(jq, "_QUEUE", q)
        gate = threading.Event()
        try:
            client.get("/test-cases")
            with client.session_transaction() as sess:
                sid = sess.get("_tf_sid")
            assert sid, "the session id is mirrored into _tf_sid"
            from routes.generation import MAX_CONCURRENT_GEN_JOBS
            for _ in range(MAX_CONCURRENT_GEN_JOBS):
                q.submit("tc_gen", _hang(gate), meta={"session_id": sid})

            data = {"input_text": "User must log in with email."}
            refused = client.post("/test-cases/run-async", data=data)
            assert refused.status_code == 429

            time.sleep(0.55)
            accepted = client.post("/test-cases/run-async", data=data)
            assert accepted.status_code == 200, accepted.get_json()
            assert accepted.get_json()["job_id"]
        finally:
            gate.set()
            q.shutdown(wait=True)
