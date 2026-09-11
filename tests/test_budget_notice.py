"""Past the allowance, the product says so — on every page, to every role.

The defect this closes. When an organisation spends its monthly AI
allowance, ``engine.llm_client.check_budget`` raises
``LLMBudgetExceeded``; it subclasses ``LLMUnavailable``, so every
generator catches it alongside "no key" and "the API is down" and falls
through to its rule engine. That is the right behaviour — a thinner
answer beats an error page — but it was **silent**. The log said why. One
page said why, ``/org/settings``, and since 2026-09-11 that page is
admins-only, so the person who notices the output getting worse is
exactly the person who could no longer read the reason.

The notice is a banner in the shell rather than a message threaded out of
each generator, and that is a claim about the condition rather than about
effort: being over the allowance is a state of the *team* that lasts
until the month turns or an admin raises the cap. A per-generation notice
would appear once, on whichever page happened to trigger it, and be gone
on reload. A banner is true whenever it is shown and disappears the
moment it stops being true — which is what the two halves of
``TestItTracksTheCondition`` assert.

``TestTheSharedState``     unit — one resolver, so the page and the
                           banner cannot disagree; BYOK is exempt.
``TestItTracksTheCondition`` integration — under the cap, over it, and
                           back under.
``TestBothRolesAreTold``   functional — the user is the one who notices,
                           so the user is the one it is for.
``TestTheChatSaysItToo``   the one fallback that can name itself.
"""

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import llm_cost as _llm_cost
from engine import llm_keys as _llm_keys
from engine import permissions as _perm

USD = _llm_cost.MICROS_PER_USD


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _full_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")


def _email() -> str:
    return f"bn-{secrets.token_hex(6)}@example.com"


@pytest.fixture
def team():
    org = _db.create_organization(f"Team {secrets.token_hex(4)}")
    out = {"org": org}
    for role in ("admin", "user"):
        uid = _db.create_user(
            _email(), password_hash=_auth.hash_password("a passphrase here"),
            email_verified=True)
        _db.add_org_member(org, uid, role)
        out[role] = uid
    return out


def _as(client, team, role):
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = team[role]
        sess[_perm.SESSION_ORG_KEY] = team["org"]


def _spend(org, usd, *, key_source="platform"):
    _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                         org_id=org, key_source=key_source,
                         cost_micros=int(usd * USD))


def _cap(org, usd):
    _db.update_org_settings(org, {"llm_budget_usd": usd})


def _fernet_key() -> str:
    """An encryption key for the BYOK test, minted rather than hard-coded."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _page(client, path="/guide"):
    """A page every role can reach, so only the banner varies."""
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


# ── Unit: one resolver for the state ──────────────────────────────

class TestTheSharedState:
    """``org_budget_state`` resolves the org's settings and key itself.

    The Settings page and the banner both ask it. Two hand-rolled copies
    of ``key_source="org" if has_org_secret(...) else "platform"`` is two
    chances to tell a team on its own key that they have run out of an
    allowance that never applied to them.
    """

    def test_under_the_cap_is_not_over(self, app, team):
        _cap(team["org"], 5)
        _spend(team["org"], 1)
        with app.app_context():
            assert _llm_cost.org_budget_state(team["org"])["over"] is False

    def test_over_the_cap_is_over(self, app, team):
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        with app.app_context():
            state = _llm_cost.org_budget_state(team["org"])
        assert state["over"] is True
        assert state["spent_micros"] == 2 * USD
        assert state["limit_micros"] == 1 * USD

    def test_exactly_at_the_cap_is_over(self, app, team):
        # The gate in llm_client is ``spent >= limit``, so the banner has
        # to agree: a notice that appears one cent after generation has
        # already changed behaviour explains the wrong moment.
        _cap(team["org"], 2)
        _spend(team["org"], 2)
        with app.app_context():
            assert _llm_cost.org_budget_state(team["org"])["over"] is True

    def test_a_byok_team_is_never_over(self, app, team, monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _llm_keys.set_org_key(team["org"], "sk-ant-" + "x" * 40)
        _cap(team["org"], 1)
        _spend(team["org"], 99)
        with app.app_context():
            assert _llm_cost.org_budget_state(team["org"])["over"] is False

    def test_no_cap_means_never_over(self, app, team):
        _cap(team["org"], 0)
        _spend(team["org"], 500)
        with app.app_context():
            assert _llm_cost.org_budget_state(team["org"])["over"] is False

    def test_no_org_is_answered_not_raised(self, app):
        """The banner runs on every render, including for somebody with no
        organisation. It must return, not explode."""
        with app.app_context():
            assert _llm_cost.org_budget_state(None)["over"] is False


# ── Integration: the banner follows the state ─────────────────────

class TestItTracksTheCondition:
    def test_nothing_is_shown_under_the_cap(self, client, team):
        _cap(team["org"], 5)
        _spend(team["org"], 1)
        _as(client, team, "user")
        body = _page(client)
        assert "budget-banner" not in body

    def test_the_banner_appears_over_the_cap(self, client, team):
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        _as(client, team, "user")
        body = _page(client)
        assert "budget-banner" in body
        assert "monthly AI allowance" in body

    def test_it_says_how_much_of_how_much(self, client, team):
        # "You are over" without the figures is an assertion; with them it
        # is something an admin can act on.
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        _as(client, team, "user")
        body = _page(client)
        assert "$2.00" in body and "$1.00" in body

    def test_it_goes_away_when_the_cap_is_raised(self, client, team):
        """The half that a per-generation message could not have: the
        notice stops being shown the moment it stops being true."""
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        _as(client, team, "user")
        assert "budget-banner" in _page(client)

        _cap(team["org"], 50)

        assert "budget-banner" not in _page(client)

    def test_another_teams_spend_does_not_raise_it(self, client, team):
        theirs = _db.create_organization("Theirs")
        _spend(theirs, 99)
        _cap(team["org"], 1)
        _as(client, team, "user")
        assert "budget-banner" not in _page(client)

    def test_it_is_on_every_page_not_one(self, client, team):
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        _as(client, team, "user")
        for path in ("/", "/test-cases", "/checklist", "/bug-reports",
                     "/guide"):
            resp = client.get(path)
            if resp.status_code != 200:
                continue
            assert "budget-banner" in resp.get_data(as_text=True), path

    def test_an_anonymous_visitor_is_not_shown_it(self, anon_client, team):
        """The sign-in page belongs to nobody, and a stranger has no team
        to be told about."""
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        body = anon_client.get("/auth/login").get_data(as_text=True)
        assert "budget-banner" not in body


# ── Functional: who is told, and what they can do ─────────────────

class TestBothRolesAreTold:
    @pytest.fixture(autouse=True)
    def _over(self, team):
        _cap(team["org"], 1)
        _spend(team["org"], 2)

    def test_a_plain_user_is_told(self, client, team):
        # The whole point. The user is who notices the output getting
        # thinner, and /org/settings — where this sentence used to live —
        # refuses them now.
        _as(client, team, "user")
        body = _page(client)
        assert "monthly AI allowance" in body
        assert "rule engines" in body

    def test_a_plain_user_is_pointed_at_a_person_not_a_403(self, client,
                                                           team):
        _as(client, team, "user")
        body = _page(client)
        assert "An admin on your team can raise it." in body
        # Offering them the Settings link would be offering a refusal.
        assert 'href="/org/settings"' not in body

    def test_an_admin_is_pointed_at_the_control(self, client, team):
        _as(client, team, "admin")
        body = _page(client)
        assert "Raise the allowance in Settings" in body
        assert 'href="/org/settings"' in body

    def test_the_settings_page_and_the_banner_agree(self, client, team):
        """Both read ``org_budget_state``. A page saying "allowance
        reached" under a banner saying nothing is worse than either."""
        _as(client, team, "admin")
        body = client.get("/org/settings").get_data(as_text=True)
        assert "allowance reached" in body
        assert "budget-banner" in body

    def test_it_is_translated(self, client, team):
        """``t.get(key, 'English')`` renders English in every language and
        no comparison of the two dictionaries can see it — the M-2 defect.
        So the Ukrainian page is read, not the Ukrainian dictionary."""
        _as(client, team, "user")
        body = _page(client, "/guide?lang=ua")
        assert "місячний ліміт AI" in body
        assert "monthly AI allowance" not in body


# ── The one fallback that can name itself ─────────────────────────

class TestTheChatSaysItToo:
    """Tedgie's streaming path already knows *which* refusal it hit.

    Everywhere else "no allowance" and "no key" arrive as the same
    ``LLMUnavailable``, and inventing a reason from a signal we do not
    have would be worse than the banner alone.
    """

    #: A question with no deterministic handler, so the request really
    #: reaches the budget gate. "hello" does not: ``try_fast_path``
    #: answers greetings, glossary terms and ISTQB topics before any key
    #: is resolved, and a canned answer that never wanted the model has no
    #: fallback to explain. Worth stating, because the first version of
    #: this test asked "hello" and failed for that reason.
    QUESTION = "Write me a haiku about our release train"

    @classmethod
    def _stream(cls, client, lang="en"):
        resp = client.get("/chat/stream",
                          query_string={"message": cls.QUESTION,
                                        "lang": lang})
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_the_question_really_reaches_the_gate(self):
        """Without this the three tests below could all be measuring a
        fast-path answer that never asked for the model."""
        from engine import chatbot as _chatbot
        assert _chatbot.try_fast_path(self.QUESTION, "en") is None

    def test_the_reply_says_where_it_came_from(self, client, team,
                                               monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "x" * 40)
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        _as(client, team, "user")
        assert "monthly AI allowance" in self._stream(client)

    def test_it_is_silent_when_the_team_is_under_the_cap(self, client, team,
                                                         monkeypatch):
        """Without this the test above passes on a note that is always
        appended, which would put the sentence on every rule-based reply
        the product has ever given."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "x" * 40)
        _cap(team["org"], 50)
        _spend(team["org"], 1)
        _as(client, team, "user")
        assert "monthly AI allowance" not in self._stream(client)

    def test_no_key_at_all_does_not_blame_the_allowance(self, client, team,
                                                        monkeypatch):
        # The other reason the same fallback fires. Saying "you are over
        # your allowance" to an instance that simply has no key would send
        # an admin looking for spend that does not exist.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _cap(team["org"], 1)
        _spend(team["org"], 2)
        _as(client, team, "user")
        assert "monthly AI allowance" not in self._stream(client)
