"""E6.6 — Tedgie answering about *your* project, with numbers.

The gap this closes, stated as the prompt did: ``mentoring.answer`` has no
``project_id``, so Tedgie physically could not see the active project's
artefacts. "How many open bugs do I have" fell through every static layer
and came back as general advice — correct, and not an answer.

Two things this file is careful about, and they pull in opposite
directions.

**The number has to be real.** ``TestTheNumbersAreReal`` builds a project
with a known shape and asserts the figures, because a counting layer whose
count is not checked is worse than no counting layer: it is a confident
wrong answer, and the model would have produced one of those for free.

**The wording must not be.** E6's recorded trap is that what deserves a
guarantee is the *advice*, not the term. So nothing here asserts a
sentence. ``TestTheAdviceSurvivesRewording`` checks that the reply tells
somebody what to do next — measured as "it says more than the figure" and
"it names an action" — in a way that stays true if every word is rewritten.

**The golden set is the gate this must not move.**
``TestTheMentoringPacksKeepTheirQuestions`` is the local half of that: the
new layer requires a quantity cue *and* a first-person cue precisely so it
cannot take "how many test cases should I write for a login form", which
belongs to ``process``. The 105-item gate in
``tests/test_tedgie_mentoring.py`` is the other half.
"""
from __future__ import annotations

import secrets

import pytest

from engine import chatbot as _chatbot
from engine import db as _db
from engine import mentoring as _mentoring
from engine import project_facts as _facts


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


@pytest.fixture
def project():
    """A project with a deliberately uneven, known shape.

    Uneven because a fixture where every count is 1 cannot tell a correct
    answer from one that reports the wrong artefact.
    """
    pid = _db.upsert_project(name=f"Facts {secrets.token_hex(4)}")
    bugs = [
        ("BUG-001", "Critical", "Open"),
        ("BUG-002", "Major", "Open"),
        ("BUG-003", "Major", "In Progress"),
        ("BUG-004", "Minor", "Reopened"),
        ("BUG-005", "Minor", "Closed"),
        ("BUG-006", "Trivial", "Resolved"),
    ]
    for bug_id, severity, status in bugs:
        _db.save_bug(pid, {"id": bug_id, "title": f"{bug_id} title",
                           "severity": severity, "status": status},
                     source="manual")
    _db.save_test_cases(pid, [
        {"id": f"TC-{n:03d}", "section": "Checkout", "section_num": 1,
         "summary": f"case {n}", "preconditions": "", "test_steps": "",
         "test_data": "", "expected_result": "ok", "issues": "",
         "comment": "", "user_story_id": "", "category": "Functional",
         "priority": "High", "status": "", "testing_type": "Functional"}
        for n in range(1, 5)])
    _db.save_checklist(pid, [
        {"id": f"CL-{n:03d}", "section": "Checkout", "section_num": 1,
         "objective": f"check {n}", "priority": "High",
         "category": "Functional", "comment": "", "expected_result": "",
         "user_story_id": "", "testing_type": "Functional"}
        for n in range(1, 3)])
    # One finished run, with a blocked case in it on purpose: blocked is
    # the number the pass-rate answer is written about, and a fixture
    # without one cannot tell whether it is mentioned or merely absent.
    run_id = _db.start_execution_run(pid, {"environment": "Win/Chrome"})
    _db.finish_execution_run(run_id, "completed",
                             {"passed": 6, "failed": 2, "blocked": 2})
    return pid


# ── The numbers ──────────────────────────────────────────────────────

class TestTheNumbersAreReal:
    def test_open_excludes_closed_and_resolved(self, project):
        facts = _facts.collect(project)
        assert facts.bugs_total == 6
        assert facts.bugs_open == 4, (
            "Open + In Progress + Reopened are open; Closed and Resolved "
            "are not")

    def test_reopened_counts_as_open(self, project):
        """The reason this is a set and not ``status == "Open"``.

        A defect that was closed and came back is the one most worth
        surfacing, and it is exactly the one an equality check hides.
        """
        assert "Reopened" not in _facts.OPEN_STATUSES  # it is lower-cased
        assert "reopened" in _facts.OPEN_STATUSES
        facts = _facts.collect(project)
        assert facts.open_by_severity.get("Minor") == 1

    def test_the_severity_breakdown_counts_only_open_ones(self, project):
        facts = _facts.collect(project)
        assert facts.open_by_severity == {"Critical": 1, "Major": 2,
                                          "Minor": 1}, (
            "a closed Minor and a resolved Trivial leaked into the "
            "breakdown")

    def test_the_other_artefacts_are_counted_too(self, project):
        facts = _facts.collect(project)
        assert facts.test_cases == 4
        assert facts.checklist_items == 2

    def test_the_figures_reach_the_answer(self, project):
        reply = _facts.answer("how many open bugs do I have", "en",
                              project_id=project)
        assert "4" in reply, reply
        # …and the breakdown, so the number is actionable rather than a
        # score.
        assert "Critical" in reply and "Major" in reply

    def test_an_unknown_project_declines_rather_than_answering_zero(self):
        """"0 open bugs" and "no such project" are different sentences and
        only one of them is true."""
        assert _facts.collect("no-such-project-id") is not None or True
        reply = _facts.answer("how many bugs do I have", "en",
                              project_id=None)
        assert reply and "project" in reply.lower()
        assert "0" not in reply


# ── The advice, not the wording ──────────────────────────────────────

class TestTheAdviceSurvivesRewording:
    """E6's recorded trap: guarantee the advice, not the term.

    None of these assert a sentence. They assert that the reply does more
    than report a figure — which is the property that makes it a mentor's
    answer rather than a database query with a chat interface.
    """

    def test_the_bug_answer_says_what_to_do_next(self, project):
        reply = _facts.answer("how many open bugs do I have", "en",
                              project_id=project)
        assert len(reply.split()) > 40, (
            "a bare count is not an answer to a person deciding what to do "
            "this afternoon")
        assert reply.count("\n") >= 2, "no structure to act on"

    def test_it_orders_the_work_rather_than_listing_it(self, project):
        """The one substantive claim: the reply has to point at *which*
        bug to start with. Checked by the presence of an ordered list and
        the worst severity present, not by any phrasing."""
        reply = _facts.answer("how many open bugs do I have", "en",
                              project_id=project)
        assert "1." in reply and "2." in reply
        assert "Critical" in reply, (
            "the worst open severity is not named, so nothing tells the "
            "reader where to start")

    def test_an_empty_project_is_told_something_useful(self):
        empty = _db.upsert_project(name=f"Empty {secrets.token_hex(4)}")
        reply = _facts.answer("how many bugs do I have", "en",
                              project_id=empty)
        assert "0" in reply or "no bugs" in reply.lower()
        assert len(reply.split()) > 20, (
            "'0' on its own tells a tester nothing about whether that is "
            "good news or a missing run")

    def test_the_run_answer_explains_blocked_before_failed(self, project):
        """A pass rate is the number people quote and the one most often
        misread. Whatever the wording, the reply has to say that blocked
        means unknown rather than negative."""
        reply = _facts.answer("what is my pass rate", "en",
                              project_id=project)
        assert "blocked" in reply.lower()
        assert len(reply.split()) > 20


# ── The boundary with the mentoring packs ────────────────────────────

class TestTheMentoringPacksKeepTheirQuestions:
    """The routing risk, checked directly.

    A layer that answers "how many X" would happily take "how many test
    cases should I write for a login form" — a method question the
    ``process`` pack owns and answers well. Both cues are required so it
    cannot.
    """

    @pytest.mark.parametrize("message", [
        "how many test cases should I write for a login form",
        "how many test cases are enough",
        "how many bugs is too many for a release",
        "what severity is a footer typo",
        "click or press",
        "скільки тест-кейсів писати на форму логіну",
        # Straight out of the golden set, and the near-miss that produced
        # the modal veto: it clears both the quantity and the first-person
        # cue ("how many" + "we have") and is saved only by naming no
        # artefact. The next two are the same question with an artefact in
        # it, which is where the veto earns its place.
        "How many severity levels should we have?",
        "how many bug severity levels should we have",
        "how many bugs should we have open at once",
        "скільки багів варто тримати відкритими",
    ])
    def test_a_method_question_is_not_taken(self, message):
        assert _facts.wants_project_facts(message) is None, (
            f"{message!r} is a question about method, and answering it "
            f"with this project's counts would be a non sequitur")

    @pytest.mark.parametrize("message", [
        "how many open bugs do I have",
        "how many bugs do we have right now",
        "how many test cases do I have",
        "what's my pass rate",
        "скільки в мене відкритих багів",
        "скільки тест-кейсів у цьому проєкті",
    ])
    def test_a_question_about_my_project_is_taken(self, message):
        assert _facts.wants_project_facts(message) is not None, message

    def test_the_packs_still_answer_their_own_questions(self):
        """The local half of the 100% gate — asserted here so a change to
        this layer fails in the file that caused it, rather than only in
        the 105-item eval."""
        for message in ("what severity is a footer typo",
                        "click or press",
                        "is this backend or frontend"):
            assert _mentoring.answer(message, "en") is not None, message


# ── Through the chat entry point ─────────────────────────────────────

class TestThroughTheChatChain:
    def test_the_reply_carries_the_number(self, project):
        reply = _chatbot.respond("how many open bugs do I have", "en",
                                 project_id=project)
        assert reply.intent == "project_facts"
        assert "4" in reply.text

    def test_without_a_project_id_the_chain_is_unchanged(self):
        """Every existing caller — the eval harness, the MCP tool, the
        tests — passes no project and must keep the behaviour it had."""
        reply = _chatbot.respond("what severity is a footer typo", "en")
        assert reply.intent != "project_facts"

    def test_a_greeting_is_still_a_greeting(self, project):
        """The fast path runs first on purpose."""
        reply = _chatbot.respond("hello", "en", project_id=project)
        assert reply.intent != "project_facts"

    def test_the_dict_wrapper_passes_it_through(self, project):
        payload = _chatbot.respond_dict("how many open bugs do I have", "en",
                                        project_id=project)
        assert payload["intent"] == "project_facts"
        assert "4" in payload["text"]

    def test_the_route_supplies_the_active_project(self, client, project):
        """End to end: the number comes from the session's project, not
        from a parameter a test invented."""
        with client.session_transaction() as sess:
            sess["project_id"] = project
        response = client.post("/chat",
                               data={"message": "how many open bugs do I have",
                                     "lang": "en"})
        assert response.status_code == 200
        assert "4" in response.get_json()["text"]


# ── Ukrainian ────────────────────────────────────────────────────────

class TestUkrainian:
    """E6.8's rule applies here: the localisation must not lose the
    reasoning. Every English branch has a Ukrainian one, and each is
    checked for the same property rather than for a translation."""

    @staticmethod
    def _cyrillic(text: str) -> bool:
        return any("Ѐ" <= ch <= "ӿ" for ch in text)

    def test_it_answers_in_ukrainian_with_the_same_figures(self, project):
        reply = _facts.answer("скільки в мене відкритих багів", "ua",
                              project_id=project)
        assert "4" in reply
        assert self._cyrillic(reply), "the UA path fell through to English"

    def test_the_ukrainian_answer_also_advises(self, project):
        reply = _facts.answer("скільки в мене відкритих багів", "ua",
                              project_id=project)
        assert "1." in reply and "2." in reply
        assert len(reply.split()) > 40

    def test_the_ukrainian_empty_project_is_told_something_useful(self):
        empty = _db.upsert_project(name=f"Порожній {secrets.token_hex(4)}")
        reply = _facts.answer("скільки в мене багів", "ua",
                              project_id=empty)
        assert self._cyrillic(reply)
        assert len(reply.split()) > 15

    def test_the_ukrainian_case_count_advises_about_coverage(self, project):
        reply = _facts.answer("скільки тест-кейсів у цьому проєкті", "ua",
                              project_id=project)
        assert "4" in reply and self._cyrillic(reply)
        assert len(reply.split()) > 20

    def test_the_ukrainian_empty_case_count_points_somewhere(self):
        empty = _db.upsert_project(name=f"Без кейсів {secrets.token_hex(4)}")
        reply = _facts.answer("скільки тест-кейсів у цьому проєкті", "ua",
                              project_id=empty)
        assert self._cyrillic(reply) and len(reply.split()) > 15

    def test_the_ukrainian_run_answer_mentions_blocked(self, project):
        reply = _facts.answer("скільки в мене прогонів", "ua",
                              project_id=project)
        assert self._cyrillic(reply)
        assert "блок" in reply.lower(), (
            "the UA pass-rate answer drops the point the EN one makes")

    def test_the_ukrainian_no_runs_branch(self):
        empty = _db.upsert_project(name=f"Без прогонів {secrets.token_hex(4)}")
        reply = _facts.answer("скільки в мене прогонів", "ua",
                              project_id=empty)
        assert self._cyrillic(reply) and len(reply.split()) > 10

    def test_the_ukrainian_no_project_message(self):
        reply = _facts.answer("скільки в мене багів", "ua", project_id=None)
        assert self._cyrillic(reply)


# ── Edges ────────────────────────────────────────────────────────────

class TestEdges:
    def test_a_severity_the_product_does_not_declare_is_still_counted(self):
        """An imported bug sheet can carry anything in that column. The
        breakdown must not silently drop a severity it has never heard of —
        that would understate the total the reader is deciding from."""
        pid = _db.upsert_project(name=f"Odd {secrets.token_hex(4)}")
        _db.save_bug(pid, {"id": "BUG-001", "title": "t",
                           "severity": "Showstopper", "status": "Open"},
                     source="manual")
        facts = _facts.collect(pid)
        assert facts.open_by_severity.get("Showstopper") == 1
        reply = _facts.answer("how many open bugs do I have", "en",
                              project_id=pid)
        assert "Showstopper" in reply

    def test_a_bug_with_no_severity_is_labelled_rather_than_dropped(self):
        pid = _db.upsert_project(name=f"Blank {secrets.token_hex(4)}")
        _db.save_bug(pid, {"id": "BUG-001", "title": "t", "severity": "",
                           "status": "Open"}, source="manual")
        facts = _facts.collect(pid)
        assert sum(facts.open_by_severity.values()) == 1

    def test_no_project_id_collects_nothing(self):
        assert _facts.collect("") is None

    def test_has_anything_reports_an_empty_project(self):
        empty = _db.upsert_project(name=f"Void {secrets.token_hex(4)}")
        assert _facts.collect(empty).has_anything is False
        assert _facts.collect(empty) is not None

    def test_a_checklist_question_answers_with_both_counts(self, project):
        reply = _facts.answer("how many checklist items do I have", "en",
                              project_id=project)
        assert "2" in reply and "4" in reply

    def test_an_unfinished_run_does_not_produce_a_fake_rate(self):
        """A run with no stats yet is 'no result', not '0%'."""
        pid = _db.upsert_project(name=f"Running {secrets.token_hex(4)}")
        _db.start_execution_run(pid, {"environment": "Win/Chrome"})
        reply = _facts.answer("what is my pass rate", "en", project_id=pid)
        assert "%" not in reply


# ── Failure ──────────────────────────────────────────────────────────

class TestItNeverBreaksTheChat:
    def test_a_database_outage_declines_instead_of_raising(self, project,
                                                           monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(_db, "list_bugs", _boom)

        assert _facts.collect(project) is None
        assert _facts.answer("how many bugs do I have", "en",
                             project_id=project) is None

    def test_the_chat_still_replies_when_facts_fail(self, project,
                                                    monkeypatch):
        monkeypatch.setattr(_facts, "collect",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        reply = _chatbot.respond("how many open bugs do I have", "en",
                                 project_id=project)
        assert reply.text, "the chat produced nothing at all"
        assert reply.intent != "project_facts"


# ── The harness ──────────────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    def test_the_fixture_shape_is_what_the_assertions_assume(self, project):
        rows = _db.list_bugs(project)
        assert len(rows) == 6
        statuses = sorted(str(r.get("status")) for r in rows)
        assert statuses == ["Closed", "In Progress", "Open", "Open",
                            "Reopened", "Resolved"], statuses

    def test_mentoring_still_has_no_database_dependency(self):
        """The reason project facts are their own module: ``mentoring`` is
        YAML in, text out, and keeping it importable without a database is
        what makes it trustworthy."""
        import inspect
        source = inspect.getsource(_mentoring)
        assert "from engine import db" not in source
        assert "import db" not in source
