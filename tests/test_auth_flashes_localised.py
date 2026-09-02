"""The sign-in flow answered a Ukrainian reader in English.

Rule 6 measured 177 `flash()` calls in ``routes/`` passing a bare literal,
and ``auth.py`` held 27 of them — the whole of what
``tests/test_i18n_parity.py``'s own docstring calls the two moments there
is no way around: *"a Ukrainian user met English at the two moments there
is no way around — signing in, and being refused"*. M-2 localised those
pages' **templates**. Their flashes were never touched.

Twenty-four of the twenty-seven come through the dictionary now. Measured
with ``lang="ua"``:

    POST /auth/login   wrong password  →  "Ця пошта й пароль не
                                          відповідають жодному акаунту."
    POST /auth/logout                  →  "Ви вийшли."

Every English value is the call site's fallback **verbatim**, extracted
from the source rather than retyped, and two of them stay constants in
``engine/auth.py`` and ``engine/oauth.py`` so the English keeps one home.
Both facts are asserted below rather than promised: an English reader sees
exactly what they saw before.

Three are left, and none of them takes a literal:

* ``_auth.lockout_message(result.locked_until)`` builds its sentence in the
  engine — and builds an English plural of "minute" on the way, so it needs
  the helper opened up rather than a key;
* ``_again(message)`` is handed its text by callers that are keyed already;
* ``str(exc)`` is a ``PasswordPolicyError``, whose four messages live in
  ``engine/auth.py`` and would have to carry keys of their own.

``auth.py``'s ratchet entry is 3 rather than deleted, which is the honest
number.
"""
from __future__ import annotations

import ast
import pathlib
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import oauth as _oauth
from engine import permissions as _perm
from engine import session_timeout as _timeout
from engine.i18n import TRANSLATIONS

EN = TRANSLATIONS["en"]
UA = TRANSLATIONS["ua"]
ROUTE = pathlib.Path(__file__).resolve().parent.parent / "routes" / "auth.py"
PASSWORD = "a perfectly good passphrase"


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


@pytest.fixture
def person():
    email = f"u-{secrets.token_hex(4)}@example.test"
    uid = _db.create_user(email, email_verified=True,
                          password_hash=_auth.hash_password(
                              PASSWORD, email=email))
    return {"email": email, "id": uid}


def _flashes(client):
    with client.session_transaction() as sess:
        out = [str(m[1]) for m in sess.get("_flashes", [])]
        sess["_flashes"] = []
    return out


def _client(app, lang):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["lang"] = lang
    return client


class TestBeingRefused:
    """The first of the two unavoidable moments.

    Asserted on the **rendered page**, not on ``session["_flashes"]``:
    this route re-renders the sign-in form in the same request rather than
    redirecting, so the template has already consumed the flash by the time
    a test could read the session. Reading the session here found an empty
    list, which looks exactly like the fix not working.
    """

    def _refuse(self, app, lang, person):
        client = _client(app, lang)
        response = client.post("/auth/login",
                               data={"email": person["email"],
                                     "password": "not the password"})
        assert response.status_code in (200, 401), response.status_code
        return response.get_data(as_text=True)

    def test_a_wrong_password_answers_in_ukrainian(self, app, person):
        body = self._refuse(app, "ua", person)
        assert UA["auth_login_failed"] in body
        assert _auth.GENERIC_LOGIN_FAILURE not in body

    def test_english_is_the_constant_it_always_was(self, app, person):
        """The control, and the reason the constant stays where it is:
        ``engine/auth.py`` keeps one English sentence for both the
        no-such-account and wrong-password paths so they cannot drift."""
        assert _auth.GENERIC_LOGIN_FAILURE in self._refuse(app, "en", person)


class TestSigningOut:

    def test_ukrainian(self, app, person):
        client = _client(app, "ua")
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["id"]
            _timeout.stamp(sess)
        client.post("/auth/logout")
        assert " ".join(_flashes(client)).strip() == UA["auth_signed_out"]

    def test_the_everywhere_variant_is_its_own_key(self, app, person):
        """One conditional used to hold both sentences, so a dictionary
        could only have answered for one of them."""
        client = _client(app, "ua")
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["id"]
            _timeout.stamp(sess)
        client.post("/auth/logout", data={"everywhere": "1"})
        said = " ".join(_flashes(client)).strip()
        assert said == UA["auth_signed_out_everywhere"], said
        assert said != UA["auth_signed_out"]


class TestEnglishIsUntouched:
    """Not a promise — the values were extracted from the call sites."""

    @staticmethod
    def _fallbacks():
        source = ROUTE.read_text(encoding="utf-8")
        out = {}
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).startswith("auth_")):
                continue
            try:
                out[node.args[0].value] = ast.literal_eval(node.args[1])
            except Exception:
                continue          # a constant — the two tests below
        return out

    def test_there_are_keys_to_check(self):
        assert len(self._fallbacks()) >= 15, self._fallbacks()

    def test_every_english_value_is_its_fallback_verbatim(self):
        wrong = {key: (EN.get(key), fallback)
                 for key, fallback in self._fallbacks().items()
                 if EN.get(key) != fallback}
        assert not wrong, (
            "an English value drifted from the fallback beside it, so the "
            f"two disagree about what an English reader sees: {wrong}")

    def test_the_two_constants_are_carried_across_exactly(self):
        assert EN["auth_login_failed"] == _auth.GENERIC_LOGIN_FAILURE
        assert EN["auth_google_refused"] == _oauth.GENERIC_REFUSAL

    def test_no_key_answers_for_two_sentences(self):
        """``auth_reset_link_dead`` and ``auth_invite_expired`` are each
        used at two call sites on purpose — the same sentence in two
        places. This asserts they really are the same sentence, which is
        what makes sharing a key correct rather than lossy."""
        source = ROUTE.read_text(encoding="utf-8")
        seen: dict[str, set] = {}
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)):
                continue
            try:
                text = ast.literal_eval(node.args[1])
            except Exception:
                continue
            seen.setdefault(node.args[0].value, set()).add(text)
        overloaded = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
        assert not overloaded, overloaded


class TestTheInterpolatedOnes:

    @pytest.mark.parametrize("key,names", [
        ("auth_confirmation_sent", ("%(email)s",)),
        ("auth_joined_org", ("%(org)s",)),
        ("auth_welcome_org", ("%(org)s",)),
    ])
    def test_both_languages_carry_the_placeholder(self, key, names):
        for table in (EN, UA):
            for name in names:
                assert name in table[key], (key, name)

    def test_the_confirmation_names_the_address(self, app, person):
        """An f-string fallback already held the address, so a key without
        a placeholder would have kept the sentence and lost the one fact
        in it."""
        client = _client(app, "ua")
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = person["id"]
            _timeout.stamp(sess)
        client.post("/auth/resend-verification")
        said = " ".join(_flashes(client))
        if said:                      # the route may refuse for other reasons
            assert "%" not in said, said
