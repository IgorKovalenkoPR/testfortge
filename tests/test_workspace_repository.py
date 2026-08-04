"""The project workspace repository — engine/workspace.py (E3.2).

Two behaviours are under test, because the module has two modes and the
whole point of the design is that the first is indistinguishable from
today:

* ``WORKSPACE_DB_FIRST`` **off** — session first, database second. Moving a
  module onto this repository must change nothing a page shows, or the
  refactor in E3.3/E3.4 is not reviewable.
* **on** — the database answers and the session is ignored. This is the
  behaviour that lets two people share a project, which is the reason the
  programme exists (ADR 0001).
"""

import secrets

import pytest
from flask import Flask, g, session

from engine import db as _db
from engine import workspace


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)


@pytest.fixture
def db_first(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")


def _project() -> str:
    return _db.upsert_project(name=f"W-{secrets.token_hex(5)}")


def _tc(external_id="TC-001", summary="Sign in with valid credentials"):
    return {"id": external_id, "summary": summary, "section": "Auth",
            "section_num": 1, "preconditions": "", "test_steps": "1. Open",
            "test_data": "", "expected_result": "The page opens",
            "issues": "", "comment": "", "user_story_id": "US-1",
            "category": "Functional", "priority": "High", "status": "",
            "testing_type": "Functional"}


def _cl(objective="The page loads"):
    return {"id": "CL-001", "section": "Auth", "section_num": 1,
            "objective": objective, "priority": "High",
            "category": "Functional", "comment": "", "expected_result": "",
            "user_story_id": "US-1", "testing_type": "Functional"}


def _app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "workspace-tests"
    return app


# ── The contract ──────────────────────────────────────────────────

class TestReadsFromTheDatabase:
    def test_test_cases_round_trip(self):
        pid = _project()
        workspace.save_test_cases(pid, [_tc()])
        rows = workspace.test_cases(pid)
        assert len(rows) == 1
        assert rows[0]["summary"] == "Sign in with valid credentials"

    def test_the_shape_feeds_the_dataclass(self):
        # The repository's job is artefacts in the shape the app consumes,
        # so this is its actual contract, not the dict keys.
        from routes._shared import reconstruct_test_cases, reconstruct_checklist
        pid = _project()
        workspace.save_test_cases(pid, [_tc()])
        workspace.save_checklist(pid, [_cl()])
        assert reconstruct_test_cases(workspace.test_cases(pid))[0].id
        assert reconstruct_checklist(workspace.checklist(pid))[0].objective

    def test_checklist_round_trip(self):
        pid = _project()
        workspace.save_checklist(pid, [_cl()])
        assert len(workspace.checklist(pid)) == 1

    def test_bugs_come_back_in_the_session_flat_shape(self):
        from engine.bug_report import dict_to_bug
        pid = _project()
        workspace.save_bug(pid, {"id": "BUG-001", "title": "Broken",
                                 "severity": "Major", "priority": "High",
                                 "status": "Open"})
        rows = workspace.bugs(pid)
        assert len(rows) == 1
        # db_id and the displayed id are different things, and conflating
        # them is how a bulk action targets the wrong row.
        assert rows[0]["id"] == "BUG-001"
        assert rows[0]["db_id"] > 0
        assert dict_to_bug(rows[0]).title == "Broken"

    def test_estimation_returns_the_result_payload(self):
        pid = _project()
        workspace.save_estimation(pid, {"platforms": 2}, {"total_hours": 42})
        assert workspace.latest_estimation(pid)["total_hours"] == 42

    def test_an_empty_project_reads_as_empty_not_none(self):
        pid = _project()
        assert workspace.test_cases(pid) == []
        assert workspace.checklist(pid) == []
        assert workspace.bugs(pid) == []
        assert workspace.runs(pid) == []
        # …except estimation, which is one thing or nothing.
        assert workspace.latest_estimation(pid) is None

    def test_no_project_reads_as_empty(self):
        assert workspace.test_cases(None) == []
        assert workspace.latest_estimation("") is None

    def test_runs_carry_every_key_the_template_reads(self):
        # A missing key is an UndefinedError mid-render, i.e. a 500 on a
        # page that had been working.
        pid = _project()
        run_id = _db.start_execution_run(pid, {"source": "manual",
                                              "environment": "staging"})
        assert run_id
        rows = workspace.runs(pid)
        assert len(rows) == 1
        for key in ("run_id", "db_run_id", "source", "tester_id",
                    "tester_name", "environment", "env_type",
                    "testing_types", "results", "stats", "bug_count",
                    "site_url", "base_url", "headless", "record_video",
                    "automation_used", "created_at"):
            assert key in rows[0], key

    def test_counts_agree_with_the_lists(self):
        pid = _project()
        workspace.save_test_cases(pid, [_tc("TC-001"), _tc("TC-002")])
        workspace.save_checklist(pid, [_cl()])
        workspace.save_bug(pid, {"id": "BUG-001", "title": "x"})
        counts = workspace.counts(pid)
        assert counts["test_cases"] == 2
        assert counts["checklist"] == 1
        assert counts["bugs"] == 1

    def test_one_project_never_sees_another(self):
        # The property the whole programme is for.
        mine, theirs = _project(), _project()
        workspace.save_test_cases(mine, [_tc("TC-MINE")])
        workspace.save_test_cases(theirs, [_tc("TC-THEIRS")])
        assert [r["id"] for r in workspace.test_cases(mine)] == ["TC-MINE"]
        assert [r["id"] for r in workspace.test_cases(theirs)] == ["TC-THEIRS"]


# ── Transition behaviour ──────────────────────────────────────────

class TestSessionFirstWhileTheFlagIsOff:
    def test_the_session_wins_over_the_database(self):
        # Byte-for-byte today's behaviour, which is what makes moving a
        # module onto the repository a reviewable no-op.
        pid = _project()
        workspace.save_test_cases(pid, [_tc("TC-DB", "from the database")])
        app = _app()
        with app.test_request_context("/"):
            session["test_cases_data"] = [_tc("TC-SESSION", "from the session")]
            rows = workspace.test_cases(pid)
        assert [r["id"] for r in rows] == ["TC-SESSION"]

    def test_an_empty_session_falls_through_to_the_database(self):
        pid = _project()
        workspace.save_test_cases(pid, [_tc("TC-DB")])
        app = _app()
        with app.test_request_context("/"):
            session["test_cases_data"] = []
            assert [r["id"] for r in workspace.test_cases(pid)] == ["TC-DB"]


class TestDatabaseFirstWhenTheFlagIsOn:
    def test_the_session_is_ignored(self, db_first):
        pid = _project()
        workspace.save_test_cases(pid, [_tc("TC-DB", "from the database")])
        app = _app()
        with app.test_request_context("/"):
            session["test_cases_data"] = [_tc("TC-STALE", "stale copy")]
            rows = workspace.test_cases(pid)
        assert [r["id"] for r in rows] == ["TC-DB"]

    def test_two_callers_see_the_same_project(self, db_first):
        """The reason the programme exists.

        Two independent request contexts — two browsers — must resolve the
        same artefacts, which they cannot while a per-browser session is
        the source of truth.
        """
        pid = _project()
        workspace.save_test_cases(pid, [_tc("TC-SHARED")])
        app = _app()
        seen = []
        for stale in ("one tester's copy", "another tester's copy"):
            with app.test_request_context("/"):
                session["test_cases_data"] = [_tc("TC-LOCAL", stale)]
                seen.append([r["id"] for r in workspace.test_cases(pid)])
        assert seen == [["TC-SHARED"], ["TC-SHARED"]]

    def test_a_write_by_one_caller_is_visible_to_the_next(self, db_first):
        pid = _project()
        app = _app()
        with app.test_request_context("/"):
            workspace.save_test_cases(pid, [_tc("TC-NEW")])
        with app.test_request_context("/"):
            assert [r["id"] for r in workspace.test_cases(pid)] == ["TC-NEW"]

    def test_the_session_still_answers_when_there_is_no_project(self, db_first):
        # The anonymous / pre-project flow: nothing in the database can be
        # scoped without a project id, so the session is the only possible
        # answer and must keep working.
        app = _app()
        with app.test_request_context("/"):
            session["test_cases_data"] = [_tc("TC-ANON")]
            assert [r["id"] for r in workspace.test_cases(None)] == ["TC-ANON"]


# ── Caching ───────────────────────────────────────────────────────

class TestPerRequestCache:
    def test_a_second_read_in_one_request_does_not_query_again(self, db_first,
                                                               monkeypatch):
        pid = _project()
        workspace.save_test_cases(pid, [_tc()])
        calls = []
        real = _db.load_test_cases
        monkeypatch.setattr(_db, "load_test_cases",
                            lambda p: (calls.append(p), real(p))[1])
        app = _app()
        with app.test_request_context("/"):
            workspace.test_cases(pid)
            workspace.test_cases(pid)
            workspace.counts(pid)
        assert len(calls) == 1, f"queried {len(calls)} times in one request"

    def test_the_cache_does_not_survive_the_request(self, db_first):
        # A cache that outlives the request is a second source of truth,
        # which is the thing being removed.
        pid = _project()
        app = _app()
        with app.test_request_context("/"):
            assert workspace.test_cases(pid) == []
            workspace.save_test_cases(pid, [_tc("TC-LATER")])
        with app.test_request_context("/"):
            assert [r["id"] for r in workspace.test_cases(pid)] == ["TC-LATER"]

    def test_a_write_invalidates_within_the_same_request(self, db_first):
        pid = _project()
        app = _app()
        with app.test_request_context("/"):
            assert workspace.test_cases(pid) == []
            workspace.save_test_cases(pid, [_tc("TC-IMMEDIATE")])
            # Without invalidation this would still read the empty list it
            # cached a moment ago — the classic stale-read-after-write.
            assert [r["id"] for r in
                    workspace.test_cases(pid)] == ["TC-IMMEDIATE"]

    def test_invalidate_is_scoped(self, db_first):
        a, b = _project(), _project()
        workspace.save_test_cases(a, [_tc("TC-A")])
        workspace.save_test_cases(b, [_tc("TC-B")])
        app = _app()
        with app.test_request_context("/"):
            workspace.test_cases(a)
            workspace.test_cases(b)
            cache = getattr(g, "_workspace_cache")
            assert len(cache) == 2
            workspace.invalidate(a, "test_cases")
            assert (b, "test_cases") in cache
            assert (a, "test_cases") not in cache

    def test_it_works_with_no_request_context_at_all(self, db_first):
        # The detached runner_worker subprocess and the CLI both call in
        # here without one; a process-lifetime cache there would serve one
        # run's test cases to the next.
        pid = _project()
        workspace.save_test_cases(pid, [_tc("TC-WORKER")])
        assert [r["id"] for r in workspace.test_cases(pid)] == ["TC-WORKER"]


# ── Failure behaviour ─────────────────────────────────────────────

class TestDegradation:
    def test_a_database_failure_falls_back_to_the_session(self, db_first,
                                                          monkeypatch):
        # A blip must not blank a page that had data.
        def _boom(_pid):
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(_db, "load_test_cases", _boom)
        pid = _project()
        app = _app()
        with app.test_request_context("/"):
            session["test_cases_data"] = [_tc("TC-SESSION")]
            assert [r["id"] for r in
                    workspace.test_cases(pid)] == ["TC-SESSION"]

    def test_a_database_failure_with_no_session_reads_as_empty(self, db_first,
                                                              monkeypatch):
        monkeypatch.setattr(_db, "load_test_cases",
                            lambda _p: (_ for _ in ()).throw(RuntimeError("x")))
        pid = _project()
        app = _app()
        with app.test_request_context("/"):
            assert workspace.test_cases(pid) == []


# ── The declared boundary ─────────────────────────────────────────

class TestSessionOnlyKeys:
    def test_the_kinds_it_owns_all_have_a_table(self):
        tables = set(_db.Base.metadata.tables)
        expected = {"test_cases": "test_case", "checklist": "checklist_item",
                    "bugs": "bug_report", "estimation": "estimation",
                    "runs": "execution_run"}
        assert set(workspace.KINDS) == set(expected)
        for kind, table in expected.items():
            assert table in tables, f"{kind} claims a table that is absent"

    def test_the_session_only_keys_are_declared_and_disjoint(self):
        # E3.2's honest boundary: these have no table, so the repository
        # refuses to pretend it owns them. Named so the gap is greppable
        # rather than folklore.
        assert workspace.SESSION_ONLY_KEYS
        assert not (set(workspace.SESSION_ONLY_KEYS)
                    & set(workspace.KINDS.values()))

    def test_together_they_account_for_every_generated_key(self):
        # If a new session key appears in GENERATED_KEYS and neither table
        # claims it, this fails — which is the prompt to decide which side
        # it belongs on rather than discovering it during E3.4.
        from routes._shared import GENERATED_KEYS
        owned = set(workspace.KINDS.values()) | set(workspace.SESSION_ONLY_KEYS)
        # Job ids are plumbing, not artefacts.
        plumbing = {"tc_gen_job_id", "cl_gen_job_id"}
        unaccounted = set(GENERATED_KEYS) - owned - plumbing
        assert not unaccounted, (
            f"session keys belonging to neither side: {sorted(unaccounted)}. "
            f"Give each a table and add it to workspace.KINDS, or declare "
            f"it in SESSION_ONLY_KEYS with the reason."
        )
