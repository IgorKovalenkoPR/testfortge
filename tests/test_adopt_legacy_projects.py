"""E1.6 — the projects that predate ORG_MODE, and the button that claims them.

``engine.db.adopt_orphan_projects`` was written, tested and never called.
That is not a cosmetic gap: the listings filter on ``Project.org_id``, so
on the day ``ORG_MODE`` goes on, every project created before it keeps
``org_id = NULL`` and disappears — from the picker, from the dashboard,
from the person who created it. The engine existed; the deployment had no
way to run it.

``tests/test_org_project_ownership.py`` already covers the sweep as a
function. What it cannot cover is the part that was missing, which is
reachability, so everything here goes through HTTP with CSRF on and asks
the question a person asks: **is the project on my screen now, and was it
absent a moment ago?**

Two properties beyond the happy path, both because the sweep answers
``0`` to two different questions:

* with several organisations on the server it *refuses* — there is
  nothing recording whose an orphan is, and guessing hands one team's
  work to another. The screen has to say that, in advance, rather than
  offer a button that does nothing;
* with no orphans it also returns ``0``, and that has to read as
  "nothing to do" rather than as the refusal.

**Mode**: authenticated throughout, and on a database of its own. Both are
load-bearing rather than tidiness. The sweep counts *organisations across
the whole server*, so on the shared suite database — where every test file
creates its own org — it would always take the refusing branch, and the
happy path would silently never be exercised.
"""
from __future__ import annotations

import pytest

from app import app as flask_app
from engine import db as _db
from engine import permissions as _perm


# ── The server this file describes ───────────────────────────────────

def _sole_server(tmp_path, monkeypatch, *, teams: int = 1):
    """A whole deployment with *teams* organisations and nothing else.

    Returns the first team's ids. ``teams=2`` is the ambiguous case: a
    second organisation exists, so bulk claiming must refuse.
    """
    monkeypatch.setenv("FLASK_DEBUG", "1")
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'adopt.db'}")
    monkeypatch.delenv("TESTFORTGE_DB", raising=False)
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    # CSRF on, unlike the rest of the suite: this route's only caller is a
    # form, and a form that stops carrying a token is a defect no
    # status-code test would see.
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", True)

    prev_engine, prev_session = _db._engine, _db._Session
    _db._engine = None
    _db._Session = None
    _db.init_db()

    org = _db.create_organization("The Only Team")
    admin = _db.create_user("admin@adopt.test", display_name="The Admin",
                            email_verified=True)
    member = _db.create_user("member@adopt.test", display_name="A Member",
                             email_verified=True)
    _db.add_org_member(org, admin, "admin")
    _db.add_org_member(org, member, "user")
    for n in range(1, teams):
        _db.create_organization(f"Another Team {n}")

    return {"org": org, "admin": admin, "member": member,
            "_prev": (prev_engine, prev_session)}


def _restore(state) -> None:
    if _db._engine is not None:
        _db._engine.dispose()
    _db._engine, _db._Session = state["_prev"]


@pytest.fixture
def sole_team(tmp_path, monkeypatch):
    """One organisation on the entire server — the case that can be swept."""
    state = _sole_server(tmp_path, monkeypatch, teams=1)
    try:
        yield state
    finally:
        _restore(state)


@pytest.fixture
def two_teams(tmp_path, monkeypatch):
    """Two organisations — the case the sweep refuses on purpose."""
    state = _sole_server(tmp_path, monkeypatch, teams=2)
    try:
        yield state
    finally:
        _restore(state)


class Person:
    """A signed-in browser that handles the token the way a browser does."""

    def __init__(self, user_id: str, org_id: str):
        self.client = flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess[_perm.SESSION_USER_KEY] = user_id
            sess[_perm.SESSION_ORG_KEY] = org_id

    def token(self) -> str:
        response = self.client.get("/api/csrf-token")
        assert response.status_code == 200, response.status_code
        return response.get_json()["token"]

    def claim(self, *, token: bool = True):
        payload = {"csrf_token": self.token()} if token else {}
        return self.client.post("/org/settings/adopt-projects", data=payload,
                                follow_redirects=True)

    def claim_flash(self) -> list[tuple[str, str]]:
        """The claim's own message, separated from the page it lands on.

        Needed rather than fastidious, and found by mutation: the settings
        page renders its own warning card about the ambiguous case, so an
        assertion that greps the followed-redirect body for that wording
        passes whether or not the route said anything at all. Deleting the
        route's up-front refusal left the body-grepping version of
        ``TestSeveralTeamsAreRefused`` green.

        So the redirect is not followed and the flash is read out of the
        session, where only the route can have put it.
        """
        response = self.client.post("/org/settings/adopt-projects",
                                    data={"csrf_token": self.token()},
                                    follow_redirects=False)
        assert response.status_code in (302, 303), response.status_code
        with self.client.session_transaction() as sess:
            return [(str(c), str(m)) for c, m in (sess.get("_flashes") or [])]

    def text(self, url: str) -> str:
        return self.client.get(url).get_data(as_text=True)


def _legacy_project(name: str) -> str:
    """A project as it exists on a deployment that predates the flag.

    No ``org_id`` — which is the whole condition. Written through
    ``upsert_project`` rather than by hand so it is the same row shape the
    app produced back then.
    """
    return _db.upsert_project(name)


def _org_of(project_id: str) -> str | None:
    return (_db.get_project(project_id) or {}).get("org_id")


def _refused(response) -> bool:
    """The request did not do the thing.

    Broad on purpose — the gate's 403, CSRF's 400 and a redirect carrying
    an error flash are all refusals, and which one a route picks is a UX
    decision. Every use of this sits beside an assertion that the data is
    unchanged; alone it would pass for the wrong reason.
    """
    return response.status_code >= 400 or "alert-error" in \
        response.get_data(as_text=True)


# ── The survey, before anything moves ────────────────────────────────

class TestTheSurvey:
    """``orphan_project_survey`` exists because ``0`` meant two things.

    The sweep returns ``0`` for "nothing to adopt" and for "I refuse to
    guess between several teams", and a screen cannot render those the
    same way. So the two facts are read up front instead.
    """

    def test_it_counts_the_unassigned_and_names_them(self, sole_team):
        _legacy_project("Ancient Regression Pack")
        survey = _db.orphan_project_survey()

        assert survey["count"] == 1
        assert survey["names"] == ["Ancient Regression Pack"]
        assert survey["ambiguous"] is False

    def test_a_project_that_has_a_team_is_not_in_it(self, sole_team):
        _db.upsert_project("Already Ours", org_id=sole_team["org"])
        assert _db.orphan_project_survey()["count"] == 0

    def test_it_reports_the_ambiguity_the_sweep_will_refuse_on(self,
                                                              two_teams):
        _legacy_project("Whose Is This")
        survey = _db.orphan_project_survey()

        assert survey["count"] == 1, "the orphan is still an orphan"
        assert survey["organisations"] == 2
        assert survey["ambiguous"] is True, (
            "the page would have offered a button for a sweep that refuses")

    def test_the_names_are_bounded_but_the_count_is_not(self, sole_team):
        """A deployment with hundreds of legacy projects must not render
        hundreds of list items into a settings page — while still being
        told the real number, which is the number that matters."""
        wanted = _db.ORPHAN_PREVIEW_LIMIT + 3
        for n in range(wanted):
            _legacy_project(f"Legacy {n:03d}")
        survey = _db.orphan_project_survey()

        assert survey["count"] == wanted
        assert len(survey["names"]) == _db.ORPHAN_PREVIEW_LIMIT


# ── The admin claims them ────────────────────────────────────────────

class TestAnAdminClaimsThem:
    """The acceptance criterion: invisible before, visible after."""

    def test_the_project_is_absent_before_the_claim_and_present_after(
            self, sole_team):
        project_id = _legacy_project("Pre-Teams Smoke Suite")
        admin = Person(sole_team["admin"], sole_team["org"])

        assert "Pre-Teams Smoke Suite" not in admin.text("/"), (
            "the project was already visible, so this test would pass "
            "whether or not the claim did anything")

        response = admin.claim()

        assert response.status_code == 200, response.status_code
        assert "Pre-Teams Smoke Suite" in admin.text("/"), (
            "the claim reported success and the project is still not in "
            "the picker")
        assert _org_of(project_id) == sole_team["org"]

    def test_the_page_says_how_many_and_which_before_asking(self, sole_team):
        _legacy_project("Nameable One")
        _legacy_project("Nameable Two")

        body = Person(sole_team["admin"], sole_team["org"]).text("/org/settings")

        assert "2 projects" in body
        assert "Nameable One" in body and "Nameable Two" in body
        assert "/org/settings/adopt-projects" in body, "no way to act on it"

    def test_the_card_is_absent_when_there_is_nothing_to_claim(self,
                                                               sole_team):
        """A permanent card reading zero trains people to skip the one
        screen that explains where their old work went."""
        body = Person(sole_team["admin"], sole_team["org"]).text("/org/settings")
        assert "/org/settings/adopt-projects" not in body

    def test_the_page_reports_the_number_it_actually_moved(self, sole_team):
        _legacy_project("Counted One")
        _legacy_project("Counted Two")
        _legacy_project("Counted Three")

        body = Person(sole_team["admin"], sole_team["org"]).claim(
        ).get_data(as_text=True)

        assert "3 projects claimed" in body

    def test_one_project_is_described_in_the_singular(self, sole_team):
        """Small, and the reason it is here: a bulk-migration message that
        says "1 projects" is the first thing an operator distrusts."""
        _legacy_project("The Only Straggler")
        body = Person(sole_team["admin"], sole_team["org"]).claim(
        ).get_data(as_text=True)

        assert "1 project claimed" in body

    def test_claiming_again_says_nothing_is_left_rather_than_refusing(
            self, sole_team):
        _legacy_project("Claimed Once")
        admin = Person(sole_team["admin"], sole_team["org"])
        admin.claim()

        response = admin.claim()

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "no unassigned projects" in body
        assert "alert-error" not in body, (
            "an empty sweep is not an error — a double-submitted form "
            "should not scold")

    def test_a_transfer_of_ownership_reaches_the_audit_trail(self,
                                                            sole_team):
        _legacy_project("Traceable Pack")
        Person(sole_team["admin"], sole_team["org"]).claim()

        entries = [e for e in _db.list_audit(org_id=sole_team["org"])
                   if e.get("action") == "adopt_orphans"]

        assert len(entries) == 1, "the ownership change left no record"
        entry = entries[0]
        assert entry["user_id"] == sole_team["admin"]
        assert entry["diff"]["count"] == 1
        assert "Traceable Pack" in entry["diff"]["names"], (
            "the record says how many moved but not which, so nobody "
            "reading it later can tell what changed hands")

    def test_a_claim_without_a_token_claims_nothing(self, sole_team):
        project_id = _legacy_project("Tokenless")

        response = Person(sole_team["admin"], sole_team["org"]).claim(
            token=False)

        assert response.status_code == 400
        assert _org_of(project_id) is None


# ── A plain user cannot ──────────────────────────────────────────────

class TestAPlainUserCannot:
    """Requirement 2: configuration is admin work. This is a bulk
    ownership transfer, which is the most consequential kind."""

    def test_the_request_is_refused_and_nothing_moves(self, sole_team):
        project_id = _legacy_project("Not Yours To Claim")

        response = Person(sole_team["member"], sole_team["org"]).claim()

        assert _refused(response), response.status_code
        assert _org_of(project_id) is None, (
            "the refusal was cosmetic — the projects were claimed anyway")

    def test_they_are_not_shown_the_card_either(self, sole_team):
        _legacy_project("Invisible To A Member")

        body = Person(sole_team["member"], sole_team["org"]).text(
            "/org/settings")

        assert "/org/settings/adopt-projects" not in body
        assert "Invisible To A Member" not in body, (
            "a member was shown the name of a project they cannot see and "
            "cannot claim — a puzzle, not information")

    def test_the_settings_page_still_renders_for_them(self, sole_team):
        """The read-only half of the page is the owner's decision (§5.1
        #4), so the new card must not be what takes it down."""
        response = Person(sole_team["member"], sole_team["org"]).client.get(
            "/org/settings")
        assert response.status_code == 200


# ── Several teams: the refusal, in words ─────────────────────────────

class TestSeveralTeamsAreRefused:
    """With more than one organisation there is no honest bulk answer.

    The sweep already declines. What E1.6 adds is that the *deployment*
    declines out loud — a button that submits and reports nothing is
    indistinguishable from a broken feature.
    """

    def test_the_page_explains_instead_of_offering_a_button(self, two_teams):
        _legacy_project("Ambiguous Pack")

        body = Person(two_teams["admin"], two_teams["org"]).text(
            "/org/settings")

        assert "Ambiguous Pack" in body, "the orphan is not even mentioned"
        assert "2 teams" in body
        assert "/org/settings/adopt-projects" not in body, (
            "a button was offered for a sweep that refuses")

    def test_the_post_refuses_in_words_and_nothing_moves(self, two_teams):
        """Posting directly, past the page that hid the button — the
        refusal has to live on the server, not in the template.

        Asserted on the flash rather than on the rendered page: see
        ``Person.claim_flash``. The page says the same thing on its own,
        which is what made the first version of this test worthless.
        """
        project_id = _legacy_project("Still Ambiguous")

        flashes = Person(two_teams["admin"], two_teams["org"]).claim_flash()

        assert flashes, (
            "the sweep's zero was passed through with nothing said, which "
            "reads as 'your projects are gone' rather than 'a decision is "
            "waiting for you'")
        category, message = flashes[-1]
        assert category == "error", (category, message)
        # The wording of the deliberate refusal, not of the generic
        # "something changed under us" fallback — those are different
        # answers and the route must not substitute one for the other.
        assert "2 teams" in message, message
        assert "guessing would hand one team's work to another" in message, \
            message
        assert _org_of(project_id) is None


# ── The harness itself ───────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    """A green result because the fixtures did nothing would be the least
    useful kind of green — the shared-database version of this file took
    the refusing branch every time and looked identical."""

    def test_the_run_is_authenticated_and_org_scoped(self, sole_team):
        assert _perm.auth_active() and _perm.org_active()

    def test_the_server_really_has_one_team(self, sole_team):
        assert _db.orphan_project_survey()["organisations"] == 1

    def test_the_two_roles_are_actually_different(self, sole_team):
        assert _db.get_org_role(sole_team["org"],
                                sole_team["admin"]) == "admin"
        assert _db.get_org_role(sole_team["org"],
                                sole_team["member"]) == "user"

    def test_csrf_enforcement_is_on(self, sole_team):
        assert flask_app.config["WTF_CSRF_ENABLED"] is True
