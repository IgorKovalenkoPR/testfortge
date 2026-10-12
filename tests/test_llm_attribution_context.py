"""LLM attribution from context — engine/llm_context.py (ADR 0003 §4.7).

The measured gap: ``tc_author``, ``checklist_author``, ``test_strategy``,
``site_recon``, ``session_segmenter`` and ``mockup_vision`` call
``call_messages`` with no ``org_id``. The budget gate, BYOK and the per-org
usage panel all keyed on it, so all three skipped them.

These tests make a call **shaped exactly like tc_author's** — kind, prompt,
no attribution — and check the org arrives anyway: from the request, and
from inside a JobQueue worker thread, which is where generation runs and
where a ``ContextVar`` does not follow on its own.
"""
from __future__ import annotations

import secrets
import threading
import time

import pytest

from engine import db as _db
from engine import llm_client, llm_context, llm_cost, llm_keys
from engine import permissions as _perm
from engine.job_queue import DONE, FAILED, JobQueue


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


class _FakeUsage:
    input_tokens = 1000
    output_tokens = 200
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeResponse:
    usage = _FakeUsage()
    content = []


@pytest.fixture
def sdk(monkeypatch):
    """A recording SDK client on a platform key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform-test")
    monkeypatch.delenv(llm_keys.ENCRYPTION_KEY_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    seen: list[dict] = []

    class _Messages:
        @staticmethod
        def create(**kwargs):
            seen.append(kwargs)
            return _FakeResponse()

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(llm_client, "_build_client",
                        lambda api_key=None: _Client())
    monkeypatch.setattr(llm_client, "_retryable_exception_types", lambda: ())
    return seen


def _org(budget_usd=None) -> str:
    org = _db.create_organization(f"Ctx {secrets.token_hex(4)}")
    if budget_usd is not None:
        _db.update_org_settings(org, {"llm_budget_usd": budget_usd})
    return org


def _tc_author_style_call():
    """The exact shape of engine/tc_author._call_llm: a kind, a prompt, and
    no org_id / project_id / user_id."""
    return llm_client.call_messages(
        timeout=30, kind="authoring", max_tokens=100,
        system=[{"type": "text", "text": "house style"}],
        messages=[{"role": "user", "content": "write cases"}],
    )


def _usage_rows(org):
    with _db.session_scope() as sess:
        return [(r.org_id, r.project_id, r.user_id, r.kind)
                for r in sess.query(_db.LlmUsage).filter(
                    _db.LlmUsage.org_id == org).all()]


class TestCallMessagesReadsTheContext:
    def test_no_context_is_still_unattributed(self, sdk):
        # Scripts, the eval harness and the MCP service call with no
        # request; that must keep working exactly as before.
        assert llm_context.current() is None
        _tc_author_style_call()
        assert len(sdk) == 1

    def test_a_tc_author_style_call_gets_the_org_from_context(self, sdk):
        org = _org()
        with llm_context.bound(org_id=org, project_id="p-1", user_id="u-1"):
            _tc_author_style_call()
        assert _usage_rows(org) == [(org, "p-1", "u-1", "authoring")]

    def test_the_budget_gate_now_applies_to_it(self, sdk):
        org = _org(budget_usd=1)
        _db.record_llm_usage(kind="authoring", model="claude-haiku-5-5",
                             org_id=org,
                             cost_micros=2 * llm_cost.MICROS_PER_USD)
        with llm_context.bound(org_id=org):
            with pytest.raises(llm_client.LLMBudgetExceeded):
                _tc_author_style_call()
        assert sdk == []

    def test_an_explicit_argument_wins(self, sdk):
        ctx_org, explicit_org = _org(), _org()
        with llm_context.bound(org_id=ctx_org, user_id="u-ctx"):
            llm_client.call_messages(kind="consult", org_id=explicit_org,
                                     max_tokens=10, messages=[])
        assert _usage_rows(ctx_org) == []
        assert _usage_rows(explicit_org) == [
            (explicit_org, None, "u-ctx", "consult")]

    def test_bound_restores_the_previous_value(self):
        with llm_context.bound(org_id="outer"):
            with llm_context.bound(org_id="inner"):
                assert llm_context.current().org_id == "inner"
            assert llm_context.current().org_id == "outer"
        assert llm_context.current() is None


class TestTheRequestSetsIt:
    def test_before_request_binds_session_attribution(self, app, sdk):
        org = _org()
        with app.test_request_context("/guide"):
            from flask import session
            session[_perm.SESSION_ORG_KEY] = org
            session[_perm.SESSION_USER_KEY] = "u-req"
            session["project_id"] = "p-req"
            app.preprocess_request()
            ctx = llm_context.current()
            assert (ctx.org_id, ctx.user_id, ctx.project_id) == \
                (org, "u-req", "p-req")
            _tc_author_style_call()
        assert _usage_rows(org) == [(org, "p-req", "u-req", "authoring")]

    def test_it_is_cleared_when_the_request_ends(self, client):
        client.get("/healthz")
        assert llm_context.current() is None


def _drain(queue, *job_ids, timeout=10.0):
    """Wait for every job to finish, then stop the queue.

    ``shutdown(wait=True)`` alone is not a wait: it cancels futures that
    have not started yet, so on a slow runner a queued job never runs and
    the test sees nothing (CI 3.12, 2026-10-12).
    """
    deadline = time.monotonic() + timeout
    for job_id in job_ids:
        while queue.get(job_id).status not in (DONE, FAILED):
            assert time.monotonic() < deadline, f"job {job_id} never finished"
            time.sleep(0.01)
    queue.shutdown(wait=True)


class TestJobQueueCarriesIt:
    def _run(self, queue, fn):
        job_id = queue.submit("tc_gen", fn)
        _drain(queue, job_id)
        return queue.get(job_id)

    def test_a_worker_thread_inherits_the_submitting_context(self, sdk):
        org = _org()
        queue = JobQueue(max_workers=1)
        seen_in_worker = {}

        def _job():
            seen_in_worker["thread"] = threading.current_thread().name
            seen_in_worker["ctx"] = llm_context.current()
            _tc_author_style_call()
            return "ok"

        with llm_context.bound(org_id=org, project_id="p-job",
                               user_id="u-job"):
            job = self._run(queue, _job)

        assert job.status == DONE, job.error
        # Really another thread — otherwise this proves nothing.
        assert seen_in_worker["thread"] != threading.current_thread().name
        assert seen_in_worker["ctx"].org_id == org
        assert _usage_rows(org) == [(org, "p-job", "u-job", "authoring")]

    def test_a_job_submitted_with_no_context_runs_with_none(self, sdk):
        queue = JobQueue(max_workers=1)
        seen = {}

        def _job():
            seen["ctx"] = llm_context.current()

        self._run(queue, _job)
        assert seen["ctx"] is None

    def test_the_worker_does_not_leak_one_job_s_org_into_the_next(self, sdk):
        org = _org()
        queue = JobQueue(max_workers=1)
        seen = []

        def _job():
            seen.append(llm_context.current())

        with llm_context.bound(org_id=org):
            a = queue.submit("a", _job)
        b = queue.submit("b", _job)
        _drain(queue, a, b)
        assert seen[0].org_id == org
        assert seen[1] is None

    def test_attribution_is_not_published_to_the_browser(self):
        queue = JobQueue(max_workers=1)
        with llm_context.bound(org_id="org-secret", user_id="user-secret"):
            job_id = queue.submit("a", lambda: None)
        _drain(queue, job_id)
        public = queue.get(job_id).to_public_dict()
        assert "org-secret" not in repr(public)
        assert "user-secret" not in repr(public)
