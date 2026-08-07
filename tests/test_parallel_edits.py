"""E3.7 — two people in one project, editing at the same time.

Optimistic concurrency was built in E3.5 (packs) and E4.1 (rows), and both
are covered: ``tests/test_write_conflicts.py`` pins the pack counter and
answers 409 over HTTP with a real interleaved write, and
``tests/test_editors_contract.py::test_a_stale_version_is_a_409`` pins the
row check for every registered entity.

What none of them has is a **second person**. Every existing conflict test
uses one client and a stale version number, which proves the guard fires
but says nothing about the properties that only exist when the two writers
are different people:

* the loser's text is **nowhere** — not merged, not partially applied;
* the winner's text is what a third party reads back;
* ``edited_by`` names the winner, so the provenance the regeneration skip
  (E4.7) and the audit trail both rest on is not the loser's;
* the loser is told **what to do**, not merely refused. A 409 with no
  guidance is the silent-overwrite problem moved one step along: the user
  learns their work did not save and not that reloading recovers it.

And one gap that is not about people: the pack counter has a real
thread-race test and ``row_version`` does not. Both are conditional single
UPDATEs, but they are different statements in different modules, and "the
other one is tested" is not a property.

**Mode.** Authenticated, org mode, editors on — named rather than
inherited. The editors do not exist with the flags off, and the two-person
scenario does not exist without organisations.

Browser-level companion: ``tests/test_e2e_golden_paths.py``
``TestTwoPeopleInOneTeam::test_a_conflict_keeps_the_losers_words_on_screen``
covers the three things only a rendered page has — that the editor stays
open, that the typed text survives, and that a Reload button appears.
"""
from __future__ import annotations

import secrets
import threading

import pytest

from app import app as flask_app
from engine import db as _db
from engine import permissions as _perm

#: Unique per run: conftest cannot always delete the scratch database on
#: Windows, and ``row_version`` survives a pack save, so a fixed project
#: name would make every version assertion depend on how many times the
#: suite has been run. Measured in E4.10 — three tests failed on the
#: second invocation and passed on the first.
_RUN = secrets.token_hex(4)


@pytest.fixture(autouse=True)
def _editing_on(monkeypatch):
    """The mode this file describes, named rather than inherited."""
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    # ``TESTING`` is not decoration. This file builds its own clients rather
    # than using the shared ``client`` fixture, and ``engine.basic_auth``
    # bypasses its gate only when ``TESTING`` is set — so without this every
    # request answers 401 from the perimeter and never reaches the code under
    # test. Worth knowing why it bites here and nowhere else: the gate reads
    # its credentials at import time, and ``config.py`` calls
    # ``load_dotenv()``, which walks *up* from the working directory. Inside a
    # git worktree that finds the parent checkout's ``.env`` and switches the
    # gate on, so this is only reproducible in a worktree.
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    # CSRF on, unlike most of the suite: a conflicting PATCH is a real
    # request from a real page, and a token that stopped being minted would
    # otherwise pass here and 400 in production.
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", True)
    _db.init_db()
    return True


def _tc(n: int, summary: str = "") -> dict:
    return {"id": f"TC-{n:03d}", "section": "Checkout", "section_num": 1,
            "summary": summary or f"Verify that case {n} works",
            "preconditions": "", "test_steps": "", "test_data": "",
            "expected_result": "The order is confirmed", "issues": "",
            "comment": "", "user_story_id": "", "category": "Functional",
            "priority": "High", "status": "", "testing_type": "Functional"}


def _cl(n: int) -> dict:
    return {"id": f"CL-{n:03d}", "section": "Checkout", "section_num": 1,
            "objective": f"check {n}", "priority": "High",
            "category": "Functional", "comment": "", "expected_result": "",
            "user_story_id": "", "testing_type": "Functional"}


@pytest.fixture
def team(request):
    """One organisation, two people in it, one project with a pack.

    Two *people*, which is the whole subject. Both are admins because the
    role boundary is E2's subject and not this file's — making one a plain
    user would mean a refusal that has nothing to do with concurrency.
    """
    tag = secrets.token_hex(4)
    org = _db.create_organization(f"Parallel {tag}")
    people = {}
    for who in ("ana", "bo"):
        uid = _db.create_user(f"{who}-{tag}@parallel.test",
                              display_name=who.title(), email_verified=True)
        _db.add_org_member(org, uid, "admin")
        people[who] = uid
    project = _db.upsert_project(
        name=f"E3.7 {request.node.name} {_RUN}"[:180], org_id=org)
    _db.save_test_cases(project, [_tc(1), _tc(2)])
    _db.save_checklist(project, [_cl(1)])
    return {"org": org, "project": project, "tag": tag, **people}


class Person:
    """One signed-in browser, with its own cookie jar and CSRF token.

    A client each rather than one client that swaps identity: the point of
    the file is two sessions existing at once, and a shared cookie jar
    would let whichever session wrote last decide who both of them are.
    """

    def __init__(self, user_id: str, org_id: str, project_id: str):
        self.user_id = user_id
        self.client = flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = user_id
            sess[_perm.SESSION_ORG_KEY] = org_id
            sess["project_id"] = project_id

    def token(self) -> str:
        response = self.client.get("/api/csrf-token")
        assert response.status_code == 200, response.status_code
        return response.get_json()["token"]

    def open_case(self, case_id: str) -> dict:
        """Read the row the way the page does — which is where the version
        the next write presents comes from."""
        response = self.client.get(f"/api/edit/test_case/{case_id}",
                                   headers={"X-CSRFToken": self.token()})
        assert response.status_code == 200, response.status_code
        return response.get_json()["item"]

    def edit_case(self, case_id: str, changes: dict, version):
        return self.client.patch(
            f"/api/edit/test_case/{case_id}",
            json={"changes": changes, "row_version": version},
            headers={"X-CSRFToken": self.token()})


@pytest.fixture
def people(team):
    def _person(name: str) -> Person:
        return Person(team[name], team["org"], team["project"])
    return _person


def _rows(project: str) -> dict[str, dict]:
    return {r["id"]: r for r in _db.load_test_cases(project)}


# ── One row, two people ──────────────────────────────────────────────

class TestTwoPeopleEditingOneTestCase:
    """The scenario E3.7 names, at the level a person experiences it."""

    def test_the_second_save_is_refused_and_told_how_to_recover(
            self, people, team):
        ana, bo = people("ana"), people("bo")

        # Both open the same case, so both hold the same version — which is
        # what makes this a race rather than a sequence.
        seen_by_ana = ana.open_case("TC-001")
        seen_by_bo = bo.open_case("TC-001")
        assert seen_by_ana["row_version"] == seen_by_bo["row_version"]
        version = seen_by_ana["row_version"]

        first = ana.edit_case("TC-001", {"summary": "Ana's wording"}, version)
        assert first.status_code == 200, first.get_data(as_text=True)[:300]

        second = bo.edit_case("TC-001", {"summary": "Bo's wording"}, version)

        assert second.status_code == 409, second.status_code
        body = second.get_json()
        assert body["error"] == "conflict"
        # Refusing without saying what to do is the silent overwrite moved
        # one step: the user learns it did not save, not that reload fixes
        # it. Their own three sentences of test steps are still on screen.
        assert "reload" in body["message"].lower(), body["message"]
        assert body["expected_version"] == version
        assert body["current_version"] == version + 1, (
            "the response does not tell the client which version is "
            "current, so a retry has to guess or reload blindly")

    def test_the_losers_words_are_nowhere(self, people, team):
        """Not merged, not partially applied, not in a second row."""
        ana, bo = people("ana"), people("bo")
        version = ana.open_case("TC-001")["row_version"]

        ana.edit_case("TC-001", {"summary": "Ana's wording"}, version)
        bo.edit_case("TC-001", {"summary": "Bo's wording"}, version)

        rows = _rows(team["project"])
        assert rows["TC-001"]["summary"] == "Ana's wording"
        assert "Bo" not in str(rows), (
            "the refused edit left a trace somewhere in the pack")
        assert len(rows) == 2, "the refused edit created a row of its own"

    def test_the_version_advances_once_not_twice(self, people, team):
        """A refused write must not bump the counter.

        If it did, the loser's retry with the version they just read would
        fail forever — they would be locked out of a row by their own
        rejected attempt.
        """
        ana, bo = people("ana"), people("bo")
        version = ana.open_case("TC-001")["row_version"]

        ana.edit_case("TC-001", {"summary": "Ana's wording"}, version)
        bo.edit_case("TC-001", {"summary": "Bo's wording"}, version)

        assert bo.open_case("TC-001")["row_version"] == version + 1

    def test_the_provenance_names_the_winner(self, people, team):
        """``edited_by`` carries who changed a row, and the regeneration
        skip (E4.7) and the audit trail both read it. Attributing the row
        to the person whose write was refused would be a quiet lie in the
        one field whose job is to say who is responsible."""
        ana, bo = people("ana"), people("bo")
        version = ana.open_case("TC-001")["row_version"]

        ana.edit_case("TC-001", {"summary": "Ana's wording"}, version)
        bo.edit_case("TC-001", {"summary": "Bo's wording"}, version)

        item = bo.open_case("TC-001")
        assert item["edited_by"] == team["ana"], (
            f"the row is attributed to {item['edited_by']}, who is not the "
            f"person whose edit survived")
        assert item["ai_generated"] is False

    def test_the_loser_recovers_by_reloading_and_saving_again(self, people,
                                                             team):
        """The refusal has to be a step in a workflow, not a dead end —
        otherwise "reload and make your change again" is advice the product
        does not actually honour."""
        ana, bo = people("ana"), people("bo")
        version = ana.open_case("TC-001")["row_version"]
        ana.edit_case("TC-001", {"summary": "Ana's wording"}, version)
        bo.edit_case("TC-001", {"summary": "Bo's wording"}, version)

        # Bo reloads — which is exactly re-reading the row — and retries.
        fresh = bo.open_case("TC-001")
        assert fresh["summary"] == "Ana's wording", \
            "the reload did not show Ana's version"
        retry = bo.edit_case("TC-001", {"summary": "Bo's second attempt"},
                             fresh["row_version"])

        assert retry.status_code == 200, retry.get_data(as_text=True)[:300]
        item = _rows(team["project"])["TC-001"]
        assert item["summary"] == "Bo's second attempt"
        assert bo.open_case("TC-001")["edited_by"] == team["bo"]

    def test_two_people_editing_different_rows_do_not_collide(self, people,
                                                              team):
        """The guard has to be per row. One that fired across the pack would
        make a two-person team slower than a one-person one, and the
        symptom — a conflict on an untouched row — reads as a bug in the
        editor rather than as a guard being too wide."""
        ana, bo = people("ana"), people("bo")
        v1 = ana.open_case("TC-001")["row_version"]
        v2 = bo.open_case("TC-002")["row_version"]

        assert ana.edit_case("TC-001", {"summary": "Ana on one"},
                             v1).status_code == 200
        assert bo.edit_case("TC-002", {"summary": "Bo on two"},
                            v2).status_code == 200

        rows = _rows(team["project"])
        assert rows["TC-001"]["summary"] == "Ana on one"
        assert rows["TC-002"]["summary"] == "Bo on two"

    def test_two_fields_of_one_row_are_one_version(self, people, team):
        """``row_version`` is per row, so Ana's second field must present
        the version her first save produced. The component updates the
        row's other fields after a save for exactly this reason; the server
        side of that agreement is asserted here."""
        ana = people("ana")
        version = ana.open_case("TC-001")["row_version"]

        assert ana.edit_case("TC-001", {"summary": "New summary"},
                             version).status_code == 200
        stale = ana.edit_case("TC-001", {"comment": "And a comment"}, version)
        assert stale.status_code == 409, (
            "one person editing two fields of a row got no conflict, so the "
            "version is not per row after all")
        assert ana.edit_case("TC-001", {"comment": "And a comment"},
                             version + 1).status_code == 200


# ── The pack, two people ─────────────────────────────────────────────

class TestTwoPeopleSavingOnePack:
    """``save_test_cases`` is wipe-and-replace, so the hazard here is not a
    lost field but a lost colleague: whoever writes second deletes
    everything the first added, and it looks like a clean save."""

    def test_a_stale_pack_save_is_refused_and_the_first_rows_survive(
            self, team):
        project = team["project"]
        version = _db.pack_versions(project)["test_cases"]

        # Ana adds a case.
        _db.save_test_cases(project, [_tc(1), _tc(2), _tc(3)],
                            expected_version=version)

        # Bo, who loaded the page before that, saves the pack he can see.
        with pytest.raises(_db.WriteConflict) as caught:
            _db.save_test_cases(project, [_tc(1), _tc(2)],
                                expected_version=version)

        assert caught.value.expected == version
        assert sorted(_rows(project)) == ["TC-001", "TC-002", "TC-003"], (
            "the refused save still deleted the case Ana added")

    def test_the_checklist_pack_is_guarded_the_same_way(self, team):
        project = team["project"]
        version = _db.pack_versions(project)["checklist"]
        _db.save_checklist(project, [_cl(1), _cl(2)],
                           expected_version=version)
        with pytest.raises(_db.WriteConflict):
            _db.save_checklist(project, [_cl(1)], expected_version=version)
        assert len(_db.load_checklist(project)) == 2

    def test_the_two_packs_do_not_block_each_other(self, team):
        """A checklist save must not invalidate a test-case save in flight.
        Two counters rather than one project-wide version, because a shared
        one would refuse writes that never touched the same data."""
        project = team["project"]
        before = _db.pack_versions(project)
        _db.save_checklist(project, [_cl(1), _cl(2)],
                           expected_version=before["checklist"])
        # The test-case version is untouched, so a save holding it works.
        _db.save_test_cases(project, [_tc(1)],
                            expected_version=before["test_cases"])
        assert sorted(_rows(project)) == ["TC-001"]


# ── A real race, on a row ────────────────────────────────────────────

class TestARealRaceOnOneRow:
    """The pack counter has a thread-race test (``test_write_conflicts.py``
    ``TestRealConcurrency``) and ``row_version`` did not.

    Both are conditional single UPDATEs, but they are different statements
    in different modules, so "the other one is tested" is not a property of
    this one. What this can and cannot prove is stated in the test.
    """

    def test_a_write_landing_between_the_read_and_the_claim_is_refused(
            self, team, monkeypatch):
        """The deterministic half, and the one that actually guards this.

        The eight-thread test below is the honest simulation and a poor
        guard. Measured, with the check written the way it originally was —
        a read of ``row_version`` into Python, a comparison, then a write —
        it passed **ten out of ten** isolated runs and only failed inside
        the full suite, where a busier database widens the window between
        the read and the write. A test that needs the machine to be loaded
        is not a test.

        So the interleaving is forced instead of hoped for: the colleague's
        write happens *inside* our read, from another thread with its own
        transaction. That is the exact ordering a read-then-compare gets
        wrong, and it is reproducible on an idle machine.
        """
        from engine import editable as _editable

        project = team["project"]
        with flask_app.test_request_context():
            version = _editable.get("test_case", project,
                                    "TC-001")["row_version"]

        real_one = _editable._one
        interleaved = threading.Event()
        colleague: list[str] = []

        def _the_colleague_saves() -> None:
            try:
                with flask_app.test_request_context():
                    _editable.patch("test_case", project, "TC-001",
                                    {"summary": "The colleague's wording"},
                                    expected_version=version,
                                    actor=team["ana"])
                colleague.append("saved")
            except Exception as exc:            # pragma: no cover
                colleague.append(f"{type(exc).__name__}: {exc}")

        def _read_then_let_them_in(sess, config, project_id, entity_id):
            row = real_one(sess, config, project_id, entity_id)
            # Once, and only for the outer read: the colleague's own call
            # arrives here too and must go straight through.
            if not interleaved.is_set():
                interleaved.set()
                thread = threading.Thread(target=_the_colleague_saves)
                thread.start()
                thread.join(timeout=30)
            return row

        monkeypatch.setattr(_editable, "_one", _read_then_let_them_in)

        with flask_app.test_request_context():
            with pytest.raises(_db.WriteConflict):
                _editable.patch("test_case", project, "TC-001",
                                {"summary": "Mine, from a stale read"},
                                expected_version=version, actor=team["bo"])

        monkeypatch.setattr(_editable, "_one", real_one)
        assert colleague == ["saved"], colleague
        with flask_app.test_request_context():
            after = _editable.get("test_case", project, "TC-001")
        assert after["summary"] == "The colleague's wording", (
            "the stale write overwrote the colleague's edit")
        assert after["row_version"] == version + 1
        assert after["edited_by"] == team["ana"]

    def test_eight_threads_produce_exactly_one_winner(self, team):
        from engine import editable as _editable

        project = team["project"]
        version = _db.load_test_cases(project)[0].get("row_version", 1)
        # Read through the same path the route uses, so the number under
        # test is the one a caller would present.
        with flask_app.test_request_context():
            version = _editable.get("test_case", project,
                                    "TC-001")["row_version"]

        results: list[object] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def _attempt(n: int) -> None:
            start.wait()
            try:
                with flask_app.test_request_context():
                    _editable.patch("test_case", project, "TC-001",
                                    {"summary": f"writer {n}"},
                                    expected_version=version)
                outcome = f"won-{n}"
            except _db.WriteConflict:
                outcome = "conflict"
            except Exception as exc:            # pragma: no cover
                outcome = f"error-{type(exc).__name__}: {exc}"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=_attempt, args=(n,))
                   for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        winners = [r for r in results if str(r).startswith("won-")]
        errors = [r for r in results if str(r).startswith("error-")]
        assert not errors, errors
        assert len(results) == 8, f"a thread never finished: {results}"
        assert len(winners) == 1, (
            f"{len(winners)} writers were told they had saved, so at least "
            f"one person's change was silently overwritten: {results}")

        # …and the row moved exactly once.
        with flask_app.test_request_context():
            after = _editable.get("test_case", project, "TC-001")
        assert after["row_version"] == version + 1
        assert after["summary"] == winners[0].replace("won-", "writer "), (
            "the stored text belongs to a writer who was refused")


# ── The harness ──────────────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    """Everything above rests on these, and a green run because the setup
    did nothing would be the least useful kind of green."""

    def test_the_two_people_are_different_people(self, team):
        assert team["ana"] != team["bo"]

    def test_both_are_in_the_organisation_that_owns_the_project(self, team):
        for who in ("ana", "bo"):
            assert _db.get_org_role(team["org"], team[who]) == "admin"
        assert (_db.get_project(team["project"]) or {}).get("org_id") == \
            team["org"]

    def test_the_editors_are_actually_switched_on(self):
        from engine import features
        assert features.effective("EDITORS_ENABLED"), (
            "the edit endpoints would 404 and every 409 above would be a "
            "404 nobody checked the reason for")

    def test_each_person_reaches_the_row(self, people):
        for who in ("ana", "bo"):
            assert people(who).open_case("TC-001")["id"] == "TC-001"

    def test_the_row_carries_a_version_to_present(self, people):
        assert people("ana").open_case("TC-001")["row_version"] >= 1
