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
"""
from __future__ import annotations

import secrets

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine.log import get_logger

log = get_logger(__name__)


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

    @app.route("/auth/login", methods=["GET", "POST"])
    def auth_login():
        if _perm.current_user() is not None:
            return redirect(_safe_next(request.args.get("next")))

        if request.method == "GET":
            return render_template("auth_login.html",
                                   next_url=request.args.get("next", ""))

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
            return render_template("auth_invite.html", invalid=True), 410

        org = _db.get_organization(invite["org_id"]) or {}

        if request.method == "GET":
            return render_template("auth_invite.html", invite=invite,
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
                org_name=org.get("name", "the team"),
                display_name=display_name,
                min_password_len=_auth.MIN_PASSWORD_LEN), 400

        try:
            pwd_hash = _auth.hash_password(password, email=email)
        except _auth.PasswordPolicyError as exc:
            flash(str(exc), "error")
            return render_template(
                "auth_invite.html", invite=invite,
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
