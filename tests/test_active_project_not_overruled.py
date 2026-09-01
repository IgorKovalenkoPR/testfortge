"""Creating a project and opening Test Execution put you back on the old one.

Walked on the auth preview, 2026-09-01:

    POST /projects/db/create  "Brand new empty project"
        → flash: "Project 'Brand new empty project' created and activated"
        → dashboard picker: Brand new empty project
    GET  /test-execution
        → picker: Untitled project 2026-09-01 21:11        ← the previous one
    GET  /
        → picker: Untitled project 2026-09-01 21:11        ← and it stuck

One visit to the execution page discarded the project the operator had just
made, on that page and on every page after it, because the re-pin is written
into the session.

``_maybe_restore_pack_from_db`` did it. The function is right to exist — an
operator reported in May that a session can sit on an auto-created "Untitled
project" while the real pack lives under a different project of theirs — but
it triggered on *emptiness*:

    if pack_test_cases() or pack_checklist():
        return
    …
    session["project_id"] = candidate["id"]

A project the operator has only just created is always empty. So is one they
picked in order to start something in it, which is the ordinary reason to
pick an empty project. Emptiness cannot tell "nobody chose this" from "this
is exactly what I chose".

``AUTOCREATED_KEY`` says it directly. ``ensure_active_project`` stamps the
placeholder it invents, ``_set_active_project`` clears the stamp on every
deliberate create/select/load, and the re-pin now runs only over a pin
nobody asked for. The next thing the operator does on that page is upload a
pack, so the project it lands in is not a cosmetic question.
"""
from __future__ import annotations

import pytest

from engine import db as _db
from routes._shared import AUTOCREATED_KEY, tc_to_dict
from engine.testcase_generator import TestCase


def _case(**kwargs):
    base = dict(id="TC-001", section="S", section_num=1, summary="A case",
                preconditions="", test_steps="1. Do it", test_data="",
                expected_result="It works", issues="", comment="",
                user_story_id="", category="Positive", priority="High",
                status="Unchecked")
    base.update(kwargs)
    return TestCase(**base)


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


def _sid(client):
    client.get("/")
    with client.session_transaction() as sess:
        from routes._shared import get_session_id
        return get_session_id(sess)


def _active(client):
    with client.session_transaction() as sess:
        return sess.get("project_id")


def _with_a_pack(client, make_project, name):
    """A project this session owns that has content — the thing the re-pin
    is looking for."""
    pid = make_project(name, owner_sid=_sid(client))
    _db.save_test_cases(pid, [tc_to_dict(_case())])
    return pid


class TestADeliberateChoiceSurvives:

    def test_a_freshly_created_project_stays_active(self, request, client,
                                                    make_project):
        _with_a_pack(client, make_project, f"Older {request.node.name}")
        client.post("/projects/db/create",
                    data={"project_name": f"New {request.node.name}"})
        chosen = _active(client)
        assert chosen, "the create route did not activate anything"

        client.get("/test-execution")
        assert _active(client) == chosen

    def test_and_it_is_still_active_on_the_next_page(self, request, client,
                                                     make_project):
        """The re-pin writes to the session, so the damage outlived the
        page that did it — the dashboard showed the old project too."""
        _with_a_pack(client, make_project, f"Older {request.node.name}")
        client.post("/projects/db/create",
                    data={"project_name": f"New {request.node.name}"})
        chosen = _active(client)
        client.get("/test-execution")
        client.get("/")
        assert _active(client) == chosen

    def test_picking_an_empty_project_sticks(self, request, client,
                                             make_project):
        """Selecting is the other half of the same intent, and an empty
        project is the ordinary thing to pick when starting work."""
        _with_a_pack(client, make_project, f"Older {request.node.name}")
        empty = make_project(f"Empty {request.node.name}",
                             owner_sid=_sid(client))
        client.post(f"/projects/db/select/{empty}")
        assert _active(client) == empty

        client.get("/test-execution")
        assert _active(client) == empty

    def test_the_page_shows_the_project_it_kept(self, request, client,
                                                make_project):
        """Reading the session is not enough — the picker is what the
        operator actually looks at."""
        _with_a_pack(client, make_project, f"Older {request.node.name}")
        name = f"New {request.node.name}"
        client.post("/projects/db/create", data={"project_name": name})
        body = client.get("/test-execution").get_data(as_text=True)
        assert name in body


class TestTheRecoveryItExistsForStillHappens:
    """The May report: a session pinned to an auto-created placeholder
    while the real pack sits under another project of the same owner."""

    def _placeholder(self, client):
        """Make the product invent a project, the way it does when the
        caller has nowhere to write.

        A first visit to /test-execution is enough: this session owns
        nothing, so ``ensure_active_project`` falls past its recovery
        branch to the auto-create. Asserting the id is truthy as well as
        stamped — an earlier version of this helper compared ``None`` with
        ``None`` and passed while creating nothing at all.
        """
        client.get("/test-execution")
        pid = _active(client)
        assert pid, "no project was auto-created"
        with client.session_transaction() as sess:
            assert sess.get(AUTOCREATED_KEY) == pid, (
                "the auto-created project was not stamped")
        return pid

    def test_the_placeholder_is_overruled(self, request, client,
                                          make_project):
        placeholder = self._placeholder(client)
        real = _with_a_pack(client, make_project, f"Real {request.node.name}")
        client.get("/test-execution")
        assert _active(client) == real, (
            "the re-pin no longer rescues the case it was written for")
        assert _active(client) != placeholder

    def test_nothing_moves_when_there_is_nowhere_better(self, request,
                                                        client,
                                                        make_project):
        """A control: with no other project holding content, the
        placeholder is still the right answer."""
        placeholder = self._placeholder(client)
        make_project(f"Also empty {request.node.name}",
                     owner_sid=_sid(client))
        client.get("/test-execution")
        assert _active(client) == placeholder

    def test_choosing_the_placeholder_protects_it(self, request, client,
                                                  make_project):
        """Deliberately picking the invented project makes it the
        operator's choice, and the stamp has to go with that."""
        placeholder = self._placeholder(client)
        _with_a_pack(client, make_project, f"Real {request.node.name}")
        client.post(f"/projects/db/select/{placeholder}")
        with client.session_transaction() as sess:
            assert AUTOCREATED_KEY not in sess
        client.get("/test-execution")
        assert _active(client) == placeholder


class TestTheStampItself:

    def test_a_created_project_is_never_stamped(self, request, client):
        client.post("/projects/db/create",
                    data={"project_name": f"Mine {request.node.name}"})
        with client.session_transaction() as sess:
            assert AUTOCREATED_KEY not in sess

    def test_a_stale_stamp_naming_another_project_is_inert(self, request,
                                                           client,
                                                           make_project):
        """The guard compares the stamp with the active pin rather than
        merely checking the stamp exists — an old stamp left by a project
        the caller has since moved off must not license overruling the
        one they are on now."""
        _with_a_pack(client, make_project, f"Real {request.node.name}")
        client.post("/projects/db/create",
                    data={"project_name": f"Mine {request.node.name}"})
        chosen = _active(client)
        with client.session_transaction() as sess:
            sess[AUTOCREATED_KEY] = "0" * 32       # some earlier placeholder
        client.get("/test-execution")
        assert _active(client) == chosen
