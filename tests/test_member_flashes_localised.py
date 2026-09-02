"""The only door into the platform answered in English.

Rule 6's second file. Registration is invite-only, so `/org/members` is the
one page a team grows through — and all twenty-four of its `flash()` calls
passed a bare literal, so every message an admin reads while inviting,
promoting, removing or unlocking somebody was English whatever language they
had chosen.

Twenty-one come through the dictionary now, on seventeen keys. Measured with
``lang="ua"``:

    POST /org/members/invite   (blank address)  →  "Введіть коректну
                                                    адресу пошти."
    POST /org/members/<id>/role (bad role)      →  "Виберіть коректну роль."

Three things this file pins beyond "it is translated".

**A shared sentence shares its key.** "The two passwords do not match."
already exists as ``auth_passwords_differ`` from the sign-in flow. It reuses
that rather than growing ``om_passwords_differ``, because two keys holding
one sentence is how the two copies drift.

**A key that answered for two sentences was split.** "That invitation is no
longer active." was the fallback at three call sites, and a fourth said more
— "…Invite the address again to send a new link." One key could only ever
have rendered one of them, so the longer one is
``om_invite_inactive_reissue``.

**The English is each fallback verbatim**, extracted from the source rather
than retyped, and a test re-derives that from the AST so the dictionary and
the call site cannot drift.

Two are left, and ``members.py``'s ratchet entry is 2 rather than deleted
because that is the honest number. Neither takes a literal: the invitation
flash is composed from the module-level ``_LEAD`` table and one of two
``tail`` branches with ``_undelivered`` inside them — four templates and a
helper, not a key — and ``str(exc)`` is a ``PasswordPolicyError``, the same
one ``auth.py`` still has.
"""
from __future__ import annotations

import ast
import pathlib
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout
from engine.i18n import TRANSLATIONS

EN = TRANSLATIONS["en"]
UA = TRANSLATIONS["ua"]
ROUTE = (pathlib.Path(__file__).resolve().parent.parent / "routes"
         / "members.py")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


@pytest.fixture
def team():
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    admin = _db.create_user(
        f"a-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, admin, "admin")
    return {"org": org, "admin": admin}


def _client(app, team, lang):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = team["admin"]
        sess[_perm.SESSION_ORG_KEY] = team["org"]
        sess["lang"] = lang
        _timeout.stamp(sess)
    return c


def _flashes(client):
    with client.session_transaction() as sess:
        out = [str(m[1]) for m in sess.get("_flashes", [])]
        sess["_flashes"] = []
    return out


class TestTheTeamPageSpeaksUkrainian:

    def test_a_blank_address_is_refused_in_ukrainian(self, app, team):
        client = _client(app, team, "ua")
        client.post("/org/members/invite", data={"email": "",
                                                 "role": "user"})
        said = " ".join(_flashes(client))
        assert said.strip() == UA["om_bad_email"], said

    def test_a_bad_role_is_refused_in_ukrainian(self, app, team):
        client = _client(app, team, "ua")
        client.post(f"/org/members/{team['admin']}/role",
                    data={"role": "wizard"})
        said = " ".join(_flashes(client))
        assert said.strip() == UA["om_bad_role"], said

    def test_english_still_reads_as_it_did(self, app, team):
        client = _client(app, team, "en")
        client.post("/org/members/invite", data={"email": "",
                                                 "role": "user"})
        assert " ".join(_flashes(client)).strip() == \
            "Enter a valid email address."

    def test_a_stranger_is_not_on_this_team_in_ukrainian(self, app, team):
        client = _client(app, team, "ua")
        client.post(f"/org/members/{'f' * 32}/role", data={"role": "user"})
        said = " ".join(_flashes(client))
        assert said.strip() == UA["om_not_a_member"], said


class TestTheLastAdminGetsTheRightSentence:
    """Two messages, two keys. One key would have made the demotion
    refusal talk about removal or the other way round."""

    def test_demoting_yourself(self, app, team):
        client = _client(app, team, "ua")
        client.post(f"/org/members/{team['admin']}/role",
                    data={"role": "user"})
        said = " ".join(_flashes(client))
        assert said.strip() == UA["om_only_admin_role"], said

    def test_removing_yourself(self, app, team):
        client = _client(app, team, "ua")
        client.post(f"/org/members/{team['admin']}/remove")
        said = " ".join(_flashes(client))
        assert said.strip() == UA["om_only_admin_remove"], said

    def test_the_two_are_not_the_same_sentence(self):
        for table in (EN, UA):
            assert table["om_only_admin_role"] != \
                table["om_only_admin_remove"]


class TestOneSentenceOneKey:

    def test_the_password_mismatch_is_shared_with_the_sign_in_flow(self):
        """Not a second key holding the same words. Asserted on the
        source, because the point is which key the route asks for."""
        source = ROUTE.read_text(encoding="utf-8")
        assert 'g.t.get("auth_passwords_differ"' in source
        assert "om_passwords_differ" not in source

    def test_the_two_invitation_sentences_have_two_keys(self):
        assert EN["om_invite_inactive"] != EN["om_invite_inactive_reissue"]
        assert EN["om_invite_inactive"] in EN["om_invite_inactive_reissue"], (
            "the longer one should still open with the shorter sentence — "
            "if it no longer does, they are unrelated messages and this "
            "assertion is the wrong one")

    def test_no_key_in_this_route_answers_for_two_sentences(self):
        fallbacks: dict[str, set] = {}
        source = ROUTE.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) == 2
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "t"
                    and isinstance(node.args[0], ast.Constant)):
                continue
            try:
                text = ast.literal_eval(node.args[1])
            except Exception:
                continue
            fallbacks.setdefault(node.args[0].value, set()).add(text)
        overloaded = {k: sorted(v) for k, v in fallbacks.items()
                      if len(v) > 1}
        assert not overloaded, overloaded


class TestEnglishIsUntouched:

    @staticmethod
    def _fallbacks():
        source = ROUTE.read_text(encoding="utf-8")
        out = {}
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) == 2
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "t"
                    and isinstance(node.args[0], ast.Constant)):
                continue
            try:
                out[node.args[0].value] = ast.literal_eval(node.args[1])
            except Exception:
                continue
        return out

    def test_there_are_keys_to_check(self):
        found = self._fallbacks()
        assert len(found) >= 15, sorted(found)

    def test_every_english_value_is_its_fallback_verbatim(self):
        wrong = {key: (EN.get(key), fallback)
                 for key, fallback in self._fallbacks().items()
                 if EN.get(key) != fallback}
        assert not wrong, wrong

    @pytest.mark.parametrize("key,name", [
        ("om_already_member", "%(email)s"),
        ("om_invite_cancelled", "%(email)s"),
        ("om_password_set", "%(who)s"),
    ])
    def test_both_languages_carry_the_placeholder(self, key, name):
        for table in (EN, UA):
            assert name in table[key], (key, name)
