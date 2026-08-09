"""Organisation settings — routes/settings.py (E2.5).

The screen where E0.7–E0.9's machinery finally has a surface: the team's
own API key, the monthly allowance, and what was actually spent.

Two properties get the most attention. The stored API key must never travel
back to a browser or into the audit trail — an audit log is read by more
people than a settings form is. And the page must be readable by a plain
user, because someone whose generation quietly fell back to the rule
engines needs somewhere that says why, and a 403 does not say it.
"""

import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import llm_cost as _llm_cost
from engine import llm_keys as _llm_keys
from engine import permissions as _perm
from routes.settings import MAX_BUDGET_USD


@pytest.fixture(autouse=True)
def _db_ready():
    _db.init_db()


@pytest.fixture(autouse=True)
def _full_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    # Default to no platform key, so the "nothing is configured" branch is
    # the one under test unless a case says otherwise.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(_llm_keys.ENCRYPTION_KEY_ENV, raising=False)


def _email() -> str:
    return f"s-{secrets.token_hex(6)}@example.com"


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


GOOD_KEY = "sk-ant-" + "b" * 60


@pytest.fixture
def team():
    org = _db.create_organization(f"Acme {secrets.token_hex(4)}")
    pwd = _auth.hash_password("a perfectly good passphrase")
    admin = _db.create_user(_email(), password_hash=pwd)
    user = _db.create_user(_email(), password_hash=pwd)
    _db.add_org_member(org, admin, "admin")
    _db.add_org_member(org, user, "user")
    return {"org": org, "admin": admin, "user": user}


def _as(client, team, who):
    with client.session_transaction() as sess:
        sess.clear()
        sess[_perm.SESSION_USER_KEY] = team[who]
        sess[_perm.SESSION_ORG_KEY] = team["org"]


# ── Visibility ────────────────────────────────────────────────────

class TestPageAccess:
    def test_anonymous_is_sent_to_sign_in(self, anon_client):
        resp = anon_client.get("/org/settings")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_an_admin_sees_the_write_forms(self, client, team):
        _as(client, team, "admin")
        body = client.get("/org/settings").data
        assert b"/org/settings/general" in body
        assert b"/org/settings/budget" in body

    def test_a_plain_user_sees_the_page_but_no_forms(self, client, team):
        # Read-only, not 403 — the owner's decision, and the page is where
        # the answer to "why did generation get worse" lives.
        _as(client, team, "user")
        resp = client.get("/org/settings")
        assert resp.status_code == 200
        assert b"/org/settings/general" not in resp.data
        assert b"/org/settings/budget" not in resp.data

    def test_the_key_section_is_hidden_from_a_plain_user(self, client, team,
                                                        monkeypatch):
        # Credential management is not information a member needs.
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _llm_keys.set_org_key(team["org"], GOOD_KEY)
        _as(client, team, "user")
        body = client.get("/org/settings").data
        assert b"/org/settings/llm-key" not in body

    def test_a_user_with_no_team_gets_a_page_not_an_error(self, client):
        uid = _db.create_user(_email())
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = uid
            # …and no organisation. The shared client signs in with one, so
            # "a user with no team" has to say so rather than inherit it.
            sess.pop(_perm.SESSION_ORG_KEY, None)
        resp = client.get("/org/settings")
        assert resp.status_code == 200
        assert b"No team selected" in resp.data

    def test_the_model_routing_is_shown(self, client, team):
        # Knowing which model handles what explains cost differences
        # between features, so it is not admin-only.
        _as(client, team, "user")
        body = client.get("/org/settings").data
        assert b"authoring" in body and b"claude-" in body


# ── Team name ─────────────────────────────────────────────────────

class TestRename:
    def test_an_admin_renames_the_team(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/general", data={"name": "Renamed QA"})
        assert _db.get_organization(team["org"])["name"] == "Renamed QA"

    def test_a_plain_user_cannot(self, client, team):
        _as(client, team, "user")
        before = _db.get_organization(team["org"])["name"]
        resp = client.post("/org/settings/general", data={"name": "Hijacked"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert _db.get_organization(team["org"])["name"] == before

    def test_an_empty_name_is_refused(self, client, team):
        _as(client, team, "admin")
        before = _db.get_organization(team["org"])["name"]
        client.post("/org/settings/general", data={"name": "   "})
        assert _db.get_organization(team["org"])["name"] == before

    def test_an_over_long_name_is_refused(self, client, team):
        # The column is 160 chars; a silent truncation would be worse than
        # a refusal.
        _as(client, team, "admin")
        before = _db.get_organization(team["org"])["name"]
        client.post("/org/settings/general", data={"name": "x" * 200})
        assert _db.get_organization(team["org"])["name"] == before

    def test_the_rename_is_audited_with_both_values(self, client, team):
        _as(client, team, "admin")
        before = _db.get_organization(team["org"])["name"]
        client.post("/org/settings/general", data={"name": "Audited QA"})
        rows = _db.list_audit(org_id=team["org"], entity="organization")
        assert rows[0]["diff"]["name"] == [before, "Audited QA"]


# ── The team's own API key ────────────────────────────────────────

class TestByokKey:
    def test_an_admin_stores_a_key(self, client, team, monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _as(client, team, "admin")
        client.post("/org/settings/llm-key", data={"api_key": GOOD_KEY})
        assert _llm_keys.get_org_key(team["org"]) == GOOD_KEY

    def test_a_plain_user_cannot_store_one(self, client, team, monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _as(client, team, "user")
        resp = client.post("/org/settings/llm-key", data={"api_key": GOOD_KEY},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert _llm_keys.get_org_key(team["org"]) is None

    def test_storing_is_refused_when_the_server_cannot_encrypt(self, client,
                                                               team):
        # Refusing beats keeping a customer credential in plain text.
        _as(client, team, "admin")
        resp = client.post("/org/settings/llm-key",
                           data={"api_key": GOOD_KEY},
                           follow_redirects=True)
        assert b"TESTFORTGE_ENCRYPTION_KEY" in resp.data
        assert _db.has_org_secret(team["org"], "anthropic_api_key") is False

    def test_a_wrong_looking_key_is_refused_with_advice(self, client, team,
                                                        monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _as(client, team, "admin")
        resp = client.post("/org/settings/llm-key",
                           data={"api_key": "definitely-not-a-key"},
                           follow_redirects=True)
        assert b"sk-ant-" in resp.data          # tells them what to look for
        assert _db.has_org_secret(team["org"], "anthropic_api_key") is False

    def test_the_key_is_never_echoed_back(self, client, team, monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _as(client, team, "admin")
        saved = client.post("/org/settings/llm-key",
                            data={"api_key": GOOD_KEY}, follow_redirects=True)
        assert GOOD_KEY.encode() not in saved.data
        # …nor on any later render. There is no reveal, by design.
        assert GOOD_KEY.encode() not in client.get("/org/settings").data

    def test_the_page_shows_only_the_last_four_characters(self, client, team,
                                                          monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _llm_keys.set_org_key(team["org"], GOOD_KEY)
        _as(client, team, "admin")
        body = client.get("/org/settings").data.decode()
        assert "…bbbb" in body
        assert GOOD_KEY not in body

    def test_the_key_is_not_written_to_the_audit_trail(self, client, team,
                                                      monkeypatch):
        # An audit log is read by more people than a settings form is.
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _as(client, team, "admin")
        client.post("/org/settings/llm-key", data={"api_key": GOOD_KEY})
        rows = _db.list_audit(org_id=team["org"], entity="organization")
        assert GOOD_KEY not in str(rows)
        assert rows[0]["action"] == "set_llm_key"

    def test_an_admin_clears_the_key(self, client, team, monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _llm_keys.set_org_key(team["org"], GOOD_KEY)
        _as(client, team, "admin")
        client.post("/org/settings/llm-key/clear")
        assert _llm_keys.get_org_key(team["org"]) is None

    def test_clearing_nothing_is_harmless(self, client, team):
        _as(client, team, "admin")
        resp = client.post("/org/settings/llm-key/clear",
                           follow_redirects=True)
        assert resp.status_code == 200

    def test_replacing_a_key_keeps_exactly_one(self, client, team, monkeypatch):
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _as(client, team, "admin")
        client.post("/org/settings/llm-key", data={"api_key": GOOD_KEY})
        second = "sk-ant-" + "c" * 60
        client.post("/org/settings/llm-key", data={"api_key": second})
        assert _llm_keys.get_org_key(team["org"]) == second


# ── The monthly allowance ─────────────────────────────────────────

class TestBudget:
    def _limit(self, org):
        settings = (_db.get_organization(org) or {}).get("settings") or {}
        return settings.get("llm_budget_usd")

    def test_an_admin_sets_the_allowance(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/budget", data={"budget_usd": "12.50"})
        assert self._limit(team["org"]) == 12.5

    def test_a_plain_user_cannot(self, client, team):
        _as(client, team, "user")
        resp = client.post("/org/settings/budget", data={"budget_usd": "99"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 403
        assert self._limit(team["org"]) is None

    def test_zero_removes_the_cap(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/budget", data={"budget_usd": "0"})
        assert self._limit(team["org"]) == 0
        assert _llm_cost.org_budget_micros(
            {"llm_budget_usd": 0}) == 0

    @pytest.mark.parametrize("bad", ["", "lots", "-5", "1e999"])
    def test_a_nonsense_allowance_is_refused(self, client, team, bad):
        _as(client, team, "admin")
        client.post("/org/settings/budget", data={"budget_usd": bad})
        assert self._limit(team["org"]) is None

    def test_a_typoed_extra_zero_is_caught(self, client, team):
        # On a zero-budget platform this is the difference between $5 and
        # $500, and the field is free text.
        _as(client, team, "admin")
        resp = client.post("/org/settings/budget",
                           data={"budget_usd": str(MAX_BUDGET_USD + 1)},
                           follow_redirects=True)
        assert str(MAX_BUDGET_USD).encode() in resp.data
        assert self._limit(team["org"]) is None

    def test_the_ceiling_itself_is_allowed(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/budget",
                    data={"budget_usd": str(MAX_BUDGET_USD)})
        assert self._limit(team["org"]) == float(MAX_BUDGET_USD)

    def test_the_change_is_audited(self, client, team):
        _as(client, team, "admin")
        client.post("/org/settings/budget", data={"budget_usd": "7"})
        rows = _db.list_audit(org_id=team["org"], entity="organization")
        assert rows[0]["diff"]["llm_budget_usd"] == [None, 7.0]

    def test_updating_the_budget_does_not_drop_other_settings(self, client,
                                                              team):
        # The settings blob is shared by unrelated features; a form that
        # replaced it wholesale would silently clear whatever it did not
        # render, and the symptom would land somewhere else entirely.
        _db.update_org_settings(team["org"], {"retention_days": 90})
        _as(client, team, "admin")
        client.post("/org/settings/budget", data={"budget_usd": "3"})
        settings = _db.get_organization(team["org"])["settings"]
        assert settings["retention_days"] == 90
        assert settings["llm_budget_usd"] == 3.0


# ── What was spent ────────────────────────────────────────────────

class TestUsageReport:
    def test_spend_is_broken_down_by_feature(self, client, team):
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=team["org"], input_tokens=6000,
                             output_tokens=7000, cost_micros=82_000)
        _db.record_llm_usage(kind="consult", model="claude-sonnet-5",
                             org_id=team["org"], input_tokens=1500,
                             output_tokens=500, cost_micros=8_000)
        _as(client, team, "user")
        body = client.get("/org/settings").data
        assert b"authoring" in body and b"consult" in body
        # $0.09 total — displayed, not rounded away to $0.00.
        assert b"$0.09" in body

    def test_an_empty_month_says_so(self, client, team):
        _as(client, team, "user")
        assert b"No AI calls recorded" in client.get("/org/settings").data

    def test_being_over_the_allowance_is_stated_plainly(self, client, team):
        # This is the answer to "why did generation get worse", and it must
        # be visible to the plain user who noticed.
        _db.update_org_settings(team["org"], {"llm_budget_usd": 1})
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=team["org"],
                             cost_micros=2 * _llm_cost.MICROS_PER_USD)
        _as(client, team, "user")
        body = client.get("/org/settings").data
        assert b"allowance reached" in body
        assert b"falling back" in body

    def test_byok_spend_is_not_shown_against_the_allowance(self, client, team,
                                                           monkeypatch):
        # Capping somebody's spend on their own key would be strange; showing
        # it against a limit that does not apply would be misleading.
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _llm_keys.set_org_key(team["org"], GOOD_KEY)
        _db.update_org_settings(team["org"], {"llm_budget_usd": 1})
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=team["org"], key_source="org",
                             cost_micros=50 * _llm_cost.MICROS_PER_USD)
        _as(client, team, "admin")
        body = client.get("/org/settings").data
        assert b"its own Anthropic API key" in body
        assert b"allowance reached" not in body
        # The meter itself has to go, not just the "reached" wording. The
        # first version left a progress bar reading "X of $5.00 used this
        # month" directly under the paragraph saying the allowance does not
        # apply — the page contradicted itself and the test did not notice,
        # because it only looked for the words.
        # Scoped to the *allowance* meter. The page grew a second meter
        # in E0.12 (database capacity), which is shown to everyone and has
        # nothing to do with whose API key is paying — so the bare class
        # name stopped identifying the thing this test is about.
        assert b"capacity-meter-fill" in body, (
            "the capacity meter should be on the page regardless of BYOK")
        llm_card = body.split(b"AI usage and cost", 1)[1].split(b"<h2", 1)[0]
        assert b"settings-meter-fill" not in llm_card, (
            "a BYOK team is uncapped, so the allowance meter must not "
            "render against their spend")
        # …while the spend stays visible in the breakdown, as history.
        assert b"authoring" in body

    def test_a_byok_team_keeps_its_stored_allowance_in_the_form(self, client,
                                                               team,
                                                               monkeypatch):
        # The effective budget for a BYOK team is unlimited, but the field
        # must show what is stored — otherwise it renders 0 and the next
        # save silently deletes a cap they want back when the key goes.
        monkeypatch.setenv(_llm_keys.ENCRYPTION_KEY_ENV, _fernet_key())
        _llm_keys.set_org_key(team["org"], GOOD_KEY)
        _db.update_org_settings(team["org"], {"llm_budget_usd": 8})
        _as(client, team, "admin")
        body = client.get("/org/settings").data
        assert b'value="8.00"' in body

    def test_another_teams_spend_is_not_visible(self, client, team):
        theirs = _db.create_organization("Theirs")
        _db.record_llm_usage(kind="authoring", model="claude-sonnet-5",
                             org_id=theirs, cost_micros=99_000_000)
        _as(client, team, "user")
        assert b"$99.00" not in client.get("/org/settings").data


class TestNoKeyAnywhere:
    def test_the_page_says_what_still_works(self, client, team):
        # A large part of the product needs no LLM at all, and a bare
        # "unavailable" would suggest otherwise.
        _as(client, team, "admin")
        body = client.get("/org/settings").data
        assert b"No API key is configured" in body
        assert b"rule engines" in body

    def test_a_platform_key_is_reported_as_shared(self, client, team,
                                                  monkeypatch):
        """Read as a person reads it, not as bytes.

        M-2 moved this sentence into ``engine/i18n``, so it now arrives
        through ``{{ }}`` and Jinja escapes the apostrophe: the page says
        ``platform&#39;s``. Identical on screen, different in the buffer —
        so the assertion unescapes first rather than pinning the encoding
        of a punctuation mark.
        """
        import html as _html
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform")
        _as(client, team, "admin")
        page = _html.unescape(
            client.get("/org/settings").get_data(as_text=True))
        assert "platform's shared API key" in page
