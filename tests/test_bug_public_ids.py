"""A bug id identifies one bug — E4.4a, extended to ``bug_report``.

E4.4a gave test cases and checklist items a unique
``(project_id, external_id)`` index and a retry that takes the next number
when the index refuses. Bug reports were left out on the reasonable-sounding
grounds that nobody files two at the same instant. Ten testers in one
organisation is precisely what the org model invites, E9.7 put ten of them
there, and the id a bug carries is the most-cited id the product has:
"reopening BUG-004" names two findings if two rows answer to it, and the
reader cannot tell which.

Three properties, and they are not the same property:

1. **A new id does not collide** — the index refuses, ``save_bug`` re-mints.
2. **A renumbered id still looks like a bug id** — the prefix survives,
   because these strings are read by people and quoted in reports.
3. **Nothing is lost to make the index possible** — not on the way in, and
   not during the migration that puts the index on an existing database.

``tests/test_migration_populated_copy.py`` owns the migration half; this
file owns what happens at write time. ``tests/test_load_smoke.py`` measures
the same property through HTTP with ten real sessions — the difference is
that this one can say *why* it holds.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect

from engine import db as _db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A database of this test's own — these count rows table-wide."""
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bugids.db'}")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)

    prev_engine, prev_session = _db._engine, _db._Session
    _db._engine = None
    _db._Session = None
    try:
        _db.init_db()
        yield
    finally:
        if _db._engine is not None:
            _db._engine.dispose()
        _db._engine, _db._Session = prev_engine, prev_session


@pytest.fixture
def project(fresh_db):
    return _db.upsert_project("Bug Ids")


def _ids(project_id) -> list[str]:
    return [b["external_id"] for b in _db.list_bugs(project_id)]


def _file(project_id, external_id: str | None, title: str) -> int:
    return _db.save_bug(project_id, {"id": external_id, "title": title,
                                     "severity": "Major"})


class TestTheIndexIsThere:
    def test_a_fresh_database_carries_the_unique_index(self, fresh_db):
        names = {ix["name"] for ix in
                 inspect(_db.get_engine()).get_indexes("bug_report")}
        assert "ux_bug_report_project_external_id" in names

    def test_the_index_is_what_refuses_a_duplicate(self, project):
        """Asserted at the database, not through ``save_bug``.

        ``save_bug`` retries past the refusal, so testing only through it
        would pass just as well with no index at all — the retry would
        never fire and the duplicate would simply be written.
        """
        from sqlalchemy.exc import IntegrityError

        _file(project, "BUG-001", "The first")
        with pytest.raises(IntegrityError):
            with _db.session_scope() as sess:
                sess.add(_db.BugReport(project_id=project,
                                       external_id="BUG-001",
                                       title="A hand-written duplicate"))


class TestFilingTwiceUnderTheSameId:
    def test_the_second_filing_is_renumbered_not_refused(self, project):
        """A tester's finding is not the right thing to drop."""
        _file(project, "BUG-001", "The basket count is stale")
        _file(project, "BUG-001", "The total ignores the discount")

        assert sorted(_ids(project)) == ["BUG-001", "BUG-002"]
        assert len(_db.list_bugs(project)) == 2

    def test_the_first_filing_keeps_the_id(self, project):
        """Whoever wrote "BUG-001" down meant the one that was there."""
        _file(project, "BUG-001", "The first")
        _file(project, "BUG-001", "The second")

        by_title = {b["title"]: b["external_id"]
                    for b in _db.list_bugs(project)}
        assert by_title["The first"] == "BUG-001"
        assert by_title["The second"] == "BUG-002"

    def test_the_renumbered_id_keeps_its_prefix(self, project):
        """``BUG-002``, not ``ITEM_002``.

        These ids are read by people and pasted into reports; a row that
        changed prefix when it was renumbered would look like a different
        kind of artefact.
        """
        _file(project, "TFG-7", "The first")
        _file(project, "TFG-7", "The second")

        by_title = {b["title"]: b["external_id"]
                    for b in _db.list_bugs(project)}
        assert by_title["The second"].startswith("TFG-")

    def test_the_next_number_clears_everything_already_taken(self, project):
        """One past the *highest*, not one past the id that collided.

        Filing another ``BUG-001`` into a project that already holds
        ``BUG-001`` and ``BUG-009`` must land on ``BUG-010``: stopping at
        the first free number would put a new finding in a gap somebody
        deleted a bug out of, which reads as the deleted one coming back.
        """
        _file(project, "BUG-001", "The first")
        _file(project, "BUG-009", "The ninth")

        _file(project, "BUG-001", "The newcomer")

        by_title = {b["title"]: b["external_id"]
                    for b in _db.list_bugs(project)}
        assert by_title["The newcomer"] == "BUG-010"

    def test_an_id_with_no_number_gets_one(self, project):
        """``"regression"`` twice would otherwise collide forever."""
        _file(project, "regression", "The first")
        _file(project, "regression", "The second")

        assert sorted(_ids(project)) == ["regression", "regression_001"]


class TestIdsThatAreNotIds:
    def test_an_empty_id_is_stored_as_nothing(self, project):
        """Empty string is not an id.

        Storing it as one would put every id-less bug in a project into a
        single collision the index cannot resolve — and it would make the
        second such filing renumber to ``_001``, inventing an id for a bug
        nobody gave one.
        """
        _file(project, "", "No id at all")
        _file(project, "   ", "Also no id")

        assert _ids(project) == [None, None]
        assert len(_db.list_bugs(project)) == 2

    def test_a_bug_with_no_project_is_not_constrained(self, fresh_db):
        """Tedgie files before a project is chosen, and that is allowed.

        Both engines allow repeated NULLs in a unique index, so these rows
        are outside what ``(project_id, external_id)`` can constrain. The
        assertion is that they are stored as they arrive rather than
        renumbered to satisfy a rule that does not reach them.
        """
        _file(None, "BUG-001", "Filed before a project")
        _file(None, "BUG-001", "Also before a project")

        orphans = [b for b in _db.list_bugs(None)
                   if b["project_id"] is None]
        assert [b["external_id"] for b in orphans] == ["BUG-001", "BUG-001"]

    def test_two_projects_may_each_have_their_own_bug_001(self, fresh_db):
        """The index is per project, and has to be.

        Every project starts its bugs at 001; a global constraint would
        make the second project's first bug ``BUG-002`` for no reason a
        person could explain.
        """
        first = _db.upsert_project("First Project")
        second = _db.upsert_project("Second Project")

        _file(first, "BUG-001", "Theirs")
        _file(second, "BUG-001", "Ours")

        assert _ids(first) == ["BUG-001"]
        assert _ids(second) == ["BUG-001"]


class TestFilingAtTheSameInstant:
    """The case the index exists for, at the layer that mints the id.

    ``tests/test_load_smoke.py`` asserts the same outcome over HTTP with
    ten signed-in sessions. This one hammers ``save_bug`` directly, which
    is both faster and harsher: no request overhead separates the writers,
    so they collide as hard as the engine will let them.
    """

    def _file_together(self, project_id, count: int) -> list[int]:
        gate = threading.Barrier(count)

        def _one(index: int) -> int:
            gate.wait(timeout=30)
            # Every writer mints the same id, which is what the read-then-
            # write in routes/bugs.py produces when they all read first.
            return _file(project_id, "BUG-001", f"Finding {index}")

        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(_one, range(count)))

    def test_twelve_at_once_get_twelve_distinct_ids(self, project):
        self._file_together(project, 12)

        minted = _ids(project)
        assert len(minted) == 12, minted
        assert len(set(minted)) == 12, sorted(minted)

    def test_not_one_finding_is_dropped(self, project):
        """The half that matters more.

        A duplicate id is a naming defect. A missing row is a tester's
        finding that no longer exists — and it is the failure the retry
        bound produces when it is set too low, silently, because the
        caller treats a failed write as best-effort.
        """
        self._file_together(project, 12)

        titles = {b["title"] for b in _db.list_bugs(project)}
        assert titles == {f"Finding {i}" for i in range(12)}

    def test_the_ids_are_contiguous(self, project):
        """No gaps and no leaps: twelve filings, BUG-001 to BUG-012.

        A retry that jumped by a random offset would also satisfy
        "distinct", and would leave a bug list numbered 1, 4, 9, 17 that
        looks like eleven bugs had been deleted.
        """
        self._file_together(project, 12)

        assert sorted(_ids(project)) == [f"BUG-{n:03d}"
                                         for n in range(1, 13)]
