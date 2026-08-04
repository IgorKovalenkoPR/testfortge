"""Optimistic concurrency on pack writes — E3.5.

The hazard is concrete, not theoretical. ``save_test_cases`` and
``save_checklist`` are wipe-and-replace, so any route that reads a pack,
changes one item and writes it all back will delete whatever a colleague
added in between — and it looks exactly like a successful save, because
replacing a pack is indistinguishable from replacing a stale one.

ADR 0001 §4.3 calls for a version the caller has to present. These tests
pin that: the write that lost the race is refused, the winner's rows
survive, and the loser is told to reload rather than being congratulated.
"""

import secrets
import threading

import pytest

from engine import db as _db
from engine import workspace


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


def _project() -> str:
    return _db.upsert_project(name=f"Conflict-{secrets.token_hex(5)}")


def _tc(n: int) -> dict:
    return {"id": f"TC-{n:03d}", "section": "Auth", "section_num": 1,
            "summary": f"case {n}", "preconditions": "", "test_steps": "",
            "test_data": "", "expected_result": "", "issues": "",
            "comment": "", "user_story_id": "", "category": "Functional",
            "priority": "High", "status": "", "testing_type": "Functional"}


def _cl(n: int) -> dict:
    return {"id": f"CL-{n:03d}", "section": "Auth", "section_num": 1,
            "objective": f"check {n}", "priority": "High",
            "category": "Functional", "comment": "", "expected_result": "",
            "user_story_id": "", "testing_type": "Functional"}


# ── The counter ───────────────────────────────────────────────────

class TestPackVersions:
    def test_a_fresh_project_starts_at_zero(self):
        assert _db.pack_versions(_project()) == {"test_cases": 0,
                                                "checklist": 0}

    def test_an_unknown_project_reads_as_zero_rather_than_raising(self):
        # Callers ask for a version before they know whether the project
        # survived; a missing one is not their problem to handle.
        assert _db.pack_versions("deadbeef" * 4)["test_cases"] == 0
        assert _db.pack_versions("")["checklist"] == 0

    def test_each_write_bumps_it(self):
        pid = _project()
        for expected in (1, 2, 3):
            _db.save_test_cases(pid, [_tc(1)])
            assert _db.pack_versions(pid)["test_cases"] == expected

    def test_the_two_packs_have_independent_counters(self):
        # A checklist save has no business conflicting with a test-case
        # save, which one shared counter would cause.
        pid = _project()
        _db.save_test_cases(pid, [_tc(1)])
        assert _db.pack_versions(pid) == {"test_cases": 1, "checklist": 0}
        _db.save_checklist(pid, [_cl(1)])
        assert _db.pack_versions(pid) == {"test_cases": 1, "checklist": 1}


# ── The guard ─────────────────────────────────────────────────────

class TestStaleWritesAreRefused:
    def test_a_matching_version_is_accepted(self):
        pid = _project()
        _db.save_test_cases(pid, [_tc(1)])
        version = _db.pack_versions(pid)["test_cases"]
        _db.save_test_cases(pid, [_tc(1), _tc(2)], expected_version=version)
        assert len(_db.load_test_cases(pid)) == 2

    def test_a_stale_version_raises(self):
        pid = _project()
        _db.save_test_cases(pid, [_tc(1)])
        stale = _db.pack_versions(pid)["test_cases"]
        _db.save_test_cases(pid, [_tc(1), _tc(2)])      # somebody else
        with pytest.raises(_db.WriteConflict) as exc:
            _db.save_test_cases(pid, [_tc(9)], expected_version=stale)
        assert exc.value.expected == stale
        assert exc.value.actual > stale
        assert exc.value.kind == "test_cases"

    def test_the_winners_rows_survive_the_refusal(self):
        """The whole point. A refused write must change nothing."""
        pid = _project()
        _db.save_test_cases(pid, [_tc(1)])
        stale = _db.pack_versions(pid)["test_cases"]
        _db.save_test_cases(pid, [_tc(1), _tc(2), _tc(3)])
        with pytest.raises(_db.WriteConflict):
            _db.save_test_cases(pid, [_tc(9)], expected_version=stale)
        # Not partly replaced, not emptied: exactly what the winner wrote.
        assert [r["id"] for r in _db.load_test_cases(pid)] == \
            ["TC-001", "TC-002", "TC-003"]

    def test_a_refused_write_does_not_bump_the_version(self):
        # Otherwise the loser's retry, using the version it just read,
        # would fail again — and the conflict would never clear.
        pid = _project()
        _db.save_test_cases(pid, [_tc(1)])
        _db.save_test_cases(pid, [_tc(2)])
        before = _db.pack_versions(pid)["test_cases"]
        with pytest.raises(_db.WriteConflict):
            _db.save_test_cases(pid, [_tc(9)], expected_version=0)
        assert _db.pack_versions(pid)["test_cases"] == before

    def test_retrying_with_the_current_version_succeeds(self):
        # The resolution path: reload, redo, save.
        pid = _project()
        _db.save_test_cases(pid, [_tc(1)])
        stale = _db.pack_versions(pid)["test_cases"]
        _db.save_test_cases(pid, [_tc(1), _tc(2)])
        with pytest.raises(_db.WriteConflict):
            _db.save_test_cases(pid, [_tc(9)], expected_version=stale)
        fresh = _db.pack_versions(pid)["test_cases"]
        _db.save_test_cases(pid, [_tc(1), _tc(2), _tc(9)],
                            expected_version=fresh)
        assert len(_db.load_test_cases(pid)) == 3

    def test_omitting_the_version_still_replaces(self):
        # Generating a pack is an intentional replacement, and every
        # pre-E3.5 caller passes nothing. That has to keep working.
        pid = _project()
        _db.save_test_cases(pid, [_tc(1), _tc(2)])
        _db.save_test_cases(pid, [_tc(9)])
        assert [r["id"] for r in _db.load_test_cases(pid)] == ["TC-009"]

    def test_the_checklist_is_guarded_the_same_way(self):
        pid = _project()
        _db.save_checklist(pid, [_cl(1)])
        stale = _db.pack_versions(pid)["checklist"]
        _db.save_checklist(pid, [_cl(1), _cl(2)])
        with pytest.raises(_db.WriteConflict):
            _db.save_checklist(pid, [_cl(9)], expected_version=stale)
        assert len(_db.load_checklist(pid)) == 2

    def test_a_missing_project_is_a_value_error_not_a_conflict(self):
        # Different problem, different answer: a reload fixes a conflict
        # and cannot fix a deleted project.
        with pytest.raises(ValueError):
            _db.save_test_cases("deadbeef" * 4, [_tc(1)], expected_version=0)


# ── Genuinely concurrent, not simulated ───────────────────────────

class TestRealConcurrency:
    def test_two_threads_racing_produce_exactly_one_winner(self):
        """The check and the bump are one conditional UPDATE.

        A read-then-compare would let both threads see the same version and
        both write — which is the bug this replaces, so it is worth proving
        against real threads rather than a hand-ordered sequence.
        """
        pid = _project()
        _db.save_test_cases(pid, [_tc(0)])
        version = _db.pack_versions(pid)["test_cases"]

        outcomes: list[str] = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def _writer(marker: int):
            barrier.wait()          # start together
            try:
                _db.save_test_cases(pid, [_tc(marker)],
                                    expected_version=version)
                with lock:
                    outcomes.append(f"won:{marker}")
            except _db.WriteConflict:
                with lock:
                    outcomes.append(f"lost:{marker}")
            except Exception as exc:      # a locked SQLite counts as a loss
                with lock:
                    outcomes.append(f"error:{marker}:{type(exc).__name__}")

        threads = [threading.Thread(target=_writer, args=(i,))
                   for i in (1, 2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

        wins = [o for o in outcomes if o.startswith("won:")]
        assert len(wins) == 1, outcomes
        # And the surviving pack is the winner's, whole.
        survived = [r["id"] for r in _db.load_test_cases(pid)]
        winner = wins[0].split(":")[1]
        assert survived == [f"TC-{int(winner):03d}"], (survived, outcomes)


# ── Through the repository ────────────────────────────────────────

class TestRepositoryLayer:
    def test_pack_versions_is_exposed(self):
        pid = _project()
        workspace.save_test_cases(pid, [_tc(1)])
        assert workspace.pack_versions(pid)["test_cases"] == 1

    def test_no_project_reads_as_zeroes(self):
        assert workspace.pack_versions(None) == {"test_cases": 0,
                                                "checklist": 0}

    def test_a_conflict_propagates_rather_than_being_swallowed(self):
        pid = _project()
        workspace.save_test_cases(pid, [_tc(1)])
        stale = workspace.pack_versions(pid)["test_cases"]
        workspace.save_test_cases(pid, [_tc(1), _tc(2)])
        with pytest.raises(_db.WriteConflict):
            workspace.save_test_cases(pid, [_tc(9)], expected_version=stale)

    def test_a_refused_write_leaves_the_cache_alone(self, client):
        """A conflict must not invalidate the reader's cached pack.

        Dropping it would make the next read look as though the refused
        write had partly landed.
        """
        pid = _project()
        workspace.save_test_cases(pid, [_tc(1), _tc(2)])
        with client.application.test_request_context("/"):
            before = [r["id"] for r in workspace.test_cases(pid)]
            with pytest.raises(_db.WriteConflict):
                workspace.save_test_cases(pid, [_tc(9)], expected_version=0)
            assert [r["id"] for r in workspace.test_cases(pid)] == before


# ── The HTTP contract ─────────────────────────────────────────────

class TestConflictOverHttp:
    def _project_with_pack(self, client):
        pid = _project()
        _db.save_test_cases(pid, [_tc(1), _tc(2)])
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        return pid

    def _stale_upload(self, client, pid):
        """Move the pack on, so the request's version is out of date."""
        _db.save_test_cases(pid, [_tc(1), _tc(2), _tc(3)])

    def test_an_appending_upload_that_lost_the_race_answers_409(self, client,
                                                                monkeypatch):
        import io

        from routes import generation as _gen

        pid = self._project_with_pack(client)

        # Advance the pack after the route has read its version, which is
        # what a colleague saving in the same moment does.
        real = _gen.pack_version

        def _then_move_on(kind, *a, **kw):
            version = real(kind, *a, **kw)
            self._stale_upload(client, pid)
            return version

        monkeypatch.setattr(_gen, "pack_version", _then_move_on)

        csv = io.BytesIO(b"ID,Section,Summary,Priority\n"
                         b"TC-900,Imported,An imported case,High\n")
        resp = client.post("/test-cases/upload", data={
            "upload_file": (csv, "extra.csv"),
            "upload_mode": "append",
        }, content_type="multipart/form-data",
            headers={"Accept": "application/json"})

        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "conflict"
        assert "reload" in body["message"].lower()
        # …and the colleague's rows are untouched.
        assert len(_db.load_test_cases(pid)) == 3

    def test_a_replacing_upload_is_not_version_checked(self, client):
        # Overwriting is what the user asked for; refusing it would be
        # obeying a guard instead of the operator.
        import io
        pid = self._project_with_pack(client)
        csv = io.BytesIO(b"ID,Section,Summary,Priority\n"
                         b"TC-900,Imported,An imported case,High\n")
        resp = client.post("/test-cases/upload", data={
            "upload_file": (csv, "extra.csv"),
            "upload_mode": "replace",
        }, content_type="multipart/form-data")
        assert resp.status_code in (200, 302)
        assert [r["id"] for r in _db.load_test_cases(pid)] == ["TC-900"]

    def test_the_inline_editor_answers_409_in_json(self, client, monkeypatch):
        from routes import generation as _gen

        pid = self._project_with_pack(client)
        real = _gen.pack_version

        def _then_move_on(kind, *a, **kw):
            version = real(kind, *a, **kw)
            self._stale_upload(client, pid)
            return version

        monkeypatch.setattr(_gen, "pack_version", _then_move_on)

        resp = client.post("/test-cases/TC-001/walkthrough-meta",
                           data={"url_pattern": "/checkout*",
                                 "trigger": "walkthrough_url_match"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "conflict"

    def test_an_uncontended_inline_edit_still_saves(self, client):
        pid = self._project_with_pack(client)
        resp = client.post("/test-cases/TC-001/walkthrough-meta",
                           data={"url_pattern": "/checkout*",
                                 "trigger": "walkthrough_url_match"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 200
        rows = {r["id"]: r for r in _db.load_test_cases(pid)}
        assert rows["TC-001"]["url_pattern"] == "/checkout*"

    def test_a_browser_gets_a_flash_and_a_redirect(self, client, monkeypatch):
        # Same 409 status, but a page rather than JSON — a form post has
        # nowhere to put a JSON body.
        from routes import generation as _gen

        pid = self._project_with_pack(client)
        real = _gen.pack_version

        def _then_move_on(kind, *a, **kw):
            version = real(kind, *a, **kw)
            self._stale_upload(client, pid)
            return version

        monkeypatch.setattr(_gen, "pack_version", _then_move_on)

        resp = client.post("/test-cases/TC-001/walkthrough-meta",
                           data={"url_pattern": "/x*", "trigger": "manual"})
        assert resp.status_code == 409
        assert "/test-cases" in resp.headers.get("Location", "")
