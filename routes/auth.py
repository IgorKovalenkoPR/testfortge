"""TestFortge — sign-in, sign-up-by-invite, sign-out (E1.2 / E1.5).

  * GET/POST /auth/login                — email + password
  * POST     /auth/logout               — this session, or all of them
  * GET/POST /auth/accept/<token>       — claim an invite, set a password
  * GET      /auth/me                   — who am I (JSON, for the UI)

Registration is **invite-only**, per the owner's decision: there is no
public sign-up route at all. A platform on a free plan with no billing and
no abuse controls should not have a self-serve door, and an invite is
also the natural moment to decide someone's role.

Two things the routes are deliberately careful about:

**One failure message.** "No such account" and "wrong password" produce
the identical sentence, from a single constant in ``engine.auth``, because
telling them apart lets anyone enumerate a customer's staff directory.
The timing is equalised in ``engine.auth.verify_login``.

**The ``next`` parameter.** A sign-in page is the most attractive place in
any application for an open redirect — the user has just been asked to
trust it. ``_safe_next`` allows same-origin paths only.

**Why the page is on screen.** A timeout, a session the server could not
find, and a first visit are three different arrivals, and this page says a
different thing for each — see :func:`_why_you_are_here`. On the free plan
the middle one is the common case, so treating them alike would routinely
blame a user for the platform's own nap.
"""
from __future__ import annotations

import secrets

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from engine import auth as _auth
from engine import db as _db
from engine import oauth as _oauth
from engine import permissions as _perm
from engine.log import get_logger

log = get_logger(__name__)

#: Session key holding the invite a Google sign-in is redeeming.
#:
#: Carried in the session rather than round-tripped through the OAuth
#: ``state`` parameter, which is Authlib's to manage, and rather than
#: through the redirect URI, which has to match a value registered with
#: Google exactly and therefore cannot carry a token.
_PENDING_INVITE_KEY = "_pending_invite"


def _safe_next(raw: str | None, default_endpoint: str = "index") -> str:
    """Same-origin paths only.

    Mirrors ``routes.projects._safe_next_target``. Kept as its own copy
    rather than imported because the two have different defaults and
    tying them together would mean a change for one silently altering the
    other — on the one page where an open redirect matters most.
    """
    value = (raw or "").strip()
    if not value:
        return url_for(default_endpoint)
    if not value.startswith("/") or value.startswith("//"):
        return url_for(default_endpoint)
    if "://" in value or "\\" in value:
        return url_for(default_endpoint)
    return value


def _why_you_are_here() -> str | None:
    """The sentence explaining why the sign-in page is on screen (E1.5).

    Three arrivals reach this page needing three different answers, and
    collapsing them is the specific dishonesty this exists to avoid:

    * a **timeout** — the session had clocks and one of them ran out.
      ``engine.session_timeout`` puts the reason in the query string on its
      way out, because by then there is no session left to flash into;
    * a **vanished store** — the browser presented a session cookie and
      the server had nothing under it. On the free plan this is the common
      case, not an edge one: the service sleeps after ~15 idle minutes and
      ``SESSION_TYPE=filesystem`` is on an ephemeral disk. Telling that
      person "you were inactive too long" would blame them for the
      platform's own nap;
    * a **first visit**, or a deliberate sign-out — nothing to explain,
      and a page that invents a reason for it is worse than a silent one.

    The vanished-store test is "a cookie arrived and the session was
    completely empty", which only the timeout hook is early enough to see —
    see ``session_timeout.session_was_lost``. An anonymous visitor who has
    been here before carries at least ``lang``, so they do not trip it.
    """
    from engine import session_timeout as _timeout

    reason = _timeout.valid_reason(request.args.get(_timeout.REASON_PARAM))
    if reason:
        return _timeout.explain(reason)

    if _timeout.session_was_lost():
        return ("We could not find your session, so you will need to sign "
                "in again. On this plan the service sleeps when idle and "
                "sessions are not kept across a restart — nothing is wrong "
                "with your account, and your saved work is untouched.")
    return None


def _first_org_for(user_id: str) -> str | None:
    """Pick the organisation to activate on sign-in.

    Someone can belong to several (the owner asked for that). Until there
    is a picker, the first membership wins and the user can switch. A
    person with no membership at all gets ``None``, which the templates
    render as "you are not in a team yet" rather than an error — that is
    a real state, reached by an admin who deleted their last org.
    """
    try:
        with _db.session_scope() as sess:
            return sess.query(_db.OrgMember.org_id).filter(
                _db.OrgMember.user_id == user_id,
            ).order_by(_db.OrgMember.added_at.asc()).limit(1).scalar()
    except Exception as exc:  # pragma: no cover — never block sign-in
        log.warning("org lookup failed for %s: %s", user_id[:8], exc)
        return None


def register(app: Flask) -> None:
    """Attach the authentication routes."""

    # Built once at registration. ``None`` when GOOGLE_CLIENT_ID /
    # GOOGLE_CLIENT_SECRET are unset, in which case the button is hidden
    # rather than rendered to 500 at the callback.
    oauth = _oauth.build_oauth(app)

    @app.route("/auth/login", methods=["GET", "POST"])
    def auth_login():
        if _perm.current_user() is not None:
            return redirect(_safe_next(request.args.get("next")))

        if request.method == "GET":
            return render_template("auth_login.html",
                                   next_url=request.args.get("next", ""),
                                   google_enabled=oauth is not None,
                                   returning=_why_you_are_here())

        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        next_url = _safe_next(request.form.get("next"))

        result = _auth.verify_login(email, password)
        if not result.ok:
            # A locked account is the one case where being specific is
            # safe and useful: the person locked it themselves, so the
            # message reveals nothing they do not already know, and
            # without it they retry until they give up.
            if result.reason == "locked":
                flash(_auth.lockout_message(result.locked_until), "error")
            else:
                flash(_auth.GENERIC_LOGIN_FAILURE, "error")
            log.info("login failed for %r: %s",
                     _db.normalize_email(email)[:40], result.reason)
            return render_template(
                "auth_login.html",
                next_url=request.form.get("next", ""),
                email=email,
                google_enabled=oauth is not None,
            ), 401

        user = result.user or {}
        _perm.login_user(user["id"], org_id=_first_org_for(user["id"]))
        log.info("login ok for user %s", user["id"][:8])
        _db.append_audit(entity="user", action="login", user_id=user["id"],
                         org_id=_perm.current_org_id())
        return redirect(next_url)

    @app.route("/auth/logout", methods=["POST"])
    def auth_logout():
        uid = _perm.current_user_id()
        everywhere = (request.form.get("everywhere") or "").strip() == "1"
        if uid:
            _db.append_audit(entity="user", action="logout", user_id=uid,
                             org_id=_perm.current_org_id(),
                             diff={"everywhere": everywhere})
        _perm.logout_user(everywhere=everywhere)
        flash("Signed out." if not everywhere
              else "Signed out on every device.", "success")
        return redirect(url_for("auth_login"))

    @app.route("/auth/accept/<token>", methods=["GET", "POST"])
    def auth_accept_invite(token):
        """Claim an invite: create the account (or attach an existing one)
        and join the organisation at the invited role."""
        invite = _db.get_invite(token)
        if invite is None:
            # One message for expired, revoked, already-used and never
            # existed. Distinguishing them would confirm which tokens
            # were real to anyone spraying guesses at the endpoint.
            flash("That invitation link is no longer valid. Ask an admin "
                  "to send a new one.", "error")
            return render_template("auth_invite.html", invalid=True,
                                   google_enabled=oauth is not None), 410

        org = _db.get_organization(invite["org_id"]) or {}

        if request.method == "GET":
            return render_template("auth_invite.html", invite=invite,
                                   google_enabled=oauth is not None,
                                   org_name=org.get("name", "the team"),
                                   min_password_len=_auth.MIN_PASSWORD_LEN)

        email = invite["email"]
        existing = _db.get_user_by_email(email)

        if existing is not None:
            # Already has an account — joining a second team must not ask
            # them to set a new password, and must not let whoever holds
            # the link change the existing one.
            org_id = _db.consume_invite(token, existing["id"])
            if not org_id:
                flash("That invitation could not be claimed. Ask an admin "
                      "to send a new one.", "error")
                return redirect(url_for("auth_login"))
            _db.append_audit(entity="org_member", action="join",
                             user_id=existing["id"], org_id=org_id,
                             diff={"role": invite["role"], "via": "invite"})
            flash(f"You have joined {org.get('name', 'the team')}. "
                  f"Sign in to continue.", "success")
            return redirect(url_for("auth_login"))

        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        display_name = (request.form.get("display_name") or "").strip()

        if password != confirm:
            flash("The two passwords do not match.", "error")
            return render_template(
                "auth_invite.html", invite=invite,
                                   google_enabled=oauth is not None,
                org_name=org.get("name", "the team"),
                display_name=display_name,
                min_password_len=_auth.MIN_PASSWORD_LEN), 400

        try:
            pwd_hash = _auth.hash_password(password, email=email)
        except _auth.PasswordPolicyError as exc:
            flash(str(exc), "error")
            return render_template(
                "auth_invite.html", invite=invite,
                                   google_enabled=oauth is not None,
                org_name=org.get("name", "the team"),
                display_name=display_name,
                min_password_len=_auth.MIN_PASSWORD_LEN), 400

        user_id = _db.create_user(
            email, display_name=display_name or None,
            password_hash=pwd_hash,
            # The invite went to this address and only its holder could
            # open the link, so the address is proven. Sending a second
            # confirmation email would be ceremony — and on a free tier
            # capped at 100 messages a day, ceremony with a cost.
            email_verified=True,
        )
        if not user_id:
            # Lost a race with a concurrent claim of the same invite.
            flash("An account for that address already exists. Sign in "
                  "instead.", "error")
            return redirect(url_for("auth_login"))

        org_id = _db.consume_invite(token, user_id)
        if not org_id:
            # The invite went stale between the GET and the POST. The
            # account exists and is usable; they just have no team yet.
            log.warning("invite %s… became unclaimable after account "
                        "creation for %s", token[:8], user_id[:8])
            flash("Your account was created, but the invitation had "
                  "expired. Ask an admin to invite you again.", "error")
            return redirect(url_for("auth_login"))

        _db.append_audit(entity="user", action="create", user_id=user_id,
                         org_id=org_id,
                         diff={"role": invite["role"], "via": "invite"})
        _perm.login_user(user_id, org_id=org_id)
        flash(f"Welcome to {org.get('name', 'the team')}.", "success")
        return redirect(url_for("index"))

    # ── Google sign-in (E1.4) ─────────────────────────────────────

    @app.route("/auth/google", methods=["GET"])
    def auth_google_start():
        """Kick off the Authorization Code + PKCE flow.

        Authlib mints and stores ``state``, ``nonce`` and the PKCE
        verifier in the session; the callback below relies on it to check
        all three. We add only the invite token, when the user arrived
        from an invitation and chose Google instead of a password.
        """
        if oauth is None:
            flash("Google sign-in is not configured on this instance.",
                  "error")
            return redirect(url_for("auth_login"))

        invite_token = (request.args.get("invite") or "").strip()
        if invite_token:
            session[_PENDING_INVITE_KEY] = invite_token
        else:
            session.pop(_PENDING_INVITE_KEY, None)

        # Where to land afterwards. Validated on the way out, not here,
        # so a hostile value cannot survive in the session and be honoured
        # by some later redirect.
        session["_post_login_next"] = _safe_next(request.args.get("next"))

        redirect_uri = url_for("auth_google_callback", _external=True)
        return oauth.google.authorize_redirect(redirect_uri)

    @app.route("/auth/google/callback", methods=["GET"])
    def auth_google_callback():
        if oauth is None:
            return redirect(url_for("auth_login"))

        invite_token = session.pop(_PENDING_INVITE_KEY, None)
        next_url = _safe_next(session.pop("_post_login_next", None))

        try:
            # Exchanges the code, verifies the id_token's signature,
            # issuer, audience, expiry and nonce. Everything after this
            # line may treat the claims as authentic — and nothing before
            # it may.
            token = oauth.google.authorize_access_token()
        except Exception as exc:
            # Covers a mismatched state (CSRF on the OAuth flow), a
            # replayed or expired code, and a user who clicked "cancel".
            log.warning("google callback rejected: %s: %s",
                        type(exc).__name__, exc)
            flash(_oauth.GENERIC_REFUSAL, "error")
            return redirect(url_for("auth_login"))

        claims = (token or {}).get("userinfo") or {}
        if not claims:
            try:
                claims = oauth.google.parse_id_token(token) or {}
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("google id_token unreadable: %s", exc)
                claims = {}
        if not claims:
            flash(_oauth.GENERIC_REFUSAL, "error")
            return redirect(url_for("auth_login"))

        decision = _oauth.decide(claims, invite_token)
        log.info("google sign-in decision=%s reason=%s",
                 decision.action, decision.reason or "-")

        if decision.action == "refuse":
            # One message for every refusal. The reasons distinguish "no
            # account here" from "your address is unverified", and the
            # first is an account-enumeration oracle that needs no
            # password at all.
            flash(_oauth.GENERIC_REFUSAL, "error")
            return redirect(url_for("auth_login"))

        subject = str(claims.get("sub"))

        if decision.action == "provision":
            user_id = _db.create_user(
                decision.email,
                display_name=_oauth.display_name_from(claims),
                # No password: this account signs in with Google. Setting
                # one would be a credential nobody rotates.
                password_hash=None,
                # Google asserted email_verified for this address, and
                # engine.oauth.decide refuses to get here otherwise.
                email_verified=True,
            )
            if not user_id:
                # Lost a race with another claim of the same invite.
                flash(_oauth.GENERIC_REFUSAL, "error")
                return redirect(url_for("auth_login"))
            org_id = _db.consume_invite(decision.invite_token, user_id)
            if not org_id:
                log.warning("invite went stale during google provisioning "
                            "for user %s", user_id[:8])
                flash("Your account was created, but the invitation had "
                      "expired. Ask an admin to invite you again.", "error")
                return redirect(url_for("auth_login"))
            _db.link_identity(user_id, _oauth.PROVIDER, subject,
                              email=decision.email)
            _db.append_audit(entity="user", action="create", user_id=user_id,
                             org_id=org_id,
                             diff={"via": "google", "invite": True})
            _perm.login_user(user_id, org_id=org_id)
            flash("Welcome to TestForTge.", "success")
            return redirect(next_url)

        user_id = decision.user_id
        if decision.action == "link":
            if not _db.link_identity(user_id, _oauth.PROVIDER, subject,
                                     email=decision.email):
                # The identity is already bound to somebody else. Refuse
                # rather than re-point it — that would hand one person's
                # workspace to another.
                log.warning("google identity for user %s could not be "
                            "linked", (user_id or "")[:8])
                flash(_oauth.GENERIC_REFUSAL, "error")
                return redirect(url_for("auth_login"))
            _db.append_audit(entity="identity", action="link",
                             user_id=user_id, diff={"provider": "google"})

        _perm.login_user(user_id, org_id=_first_org_for(user_id))
        _db.append_audit(entity="user", action="login", user_id=user_id,
                         org_id=_perm.current_org_id(),
                         diff={"via": "google"})
        return redirect(next_url)

    @app.route("/auth/me", methods=["GET"])
    def auth_me():
        """Who the caller is — for the UI, and for a smoke check."""
        user = _perm.current_user()
        if user is None:
            return jsonify({"authenticated": False,
                            "auth_active": _perm.auth_active()})
        return jsonify({
            "authenticated": True,
            "auth_active": _perm.auth_active(),
            "org_active": _perm.org_active(),
            # Never the password hash, and never the whole row: this
            # endpoint is reachable by any signed-in session and its
            # response ends up in a browser's memory and cache.
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user.get("display_name"),
            },
            "org_id": _perm.current_org_id(),
            "role": _perm.current_role(),
        })


def new_invite_token() -> str:
    """Mint an invite token.

    Lives here rather than in the DB layer so the secret never comes from
    the database's random source, and so the members page (E2.4) and the
    tests use the same generator.
    """
    return secrets.token_urlsafe(32)


__all__ = ["register", "new_invite_token"]
