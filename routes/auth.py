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
import threading

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from engine import auth as _auth
from engine import db as _db
from engine import mailer as _mailer
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


#: Sends handed to a background thread, so something can wait for them.
#:
#: Read by :func:`wait_for_dispatch`, which exists for two callers: the test
#: suite, which cannot assert on a message that has not been sent yet, and a
#: graceful shutdown, which should not drop a reset somebody is waiting for.
#: Pruned on every dispatch so a long-running process does not accumulate
#: finished thread objects.
_DISPATCHED: list[threading.Thread] = []


def _dispatch(fn, *args) -> threading.Thread:
    """Run *fn* off the request path.

    Not an optimisation. ``/auth/forgot`` must not reveal whether an address
    has an account, and the *message* being identical is only half of that:
    the branch that finds a user creates a token and makes an HTTPS call to
    the provider, while the branch that finds nobody returns immediately.
    That difference is measurable from outside and it is an account
    enumeration oracle — the same one ``engine.auth`` burns a dummy Argon2
    hash to close on the sign-in page.

    Doing the work in a thread makes the response time independent of what
    was found, which is the property that actually matters. The thread's own
    duration differs and nobody can observe it.
    """
    thread = threading.Thread(target=fn, args=args, daemon=True,
                              name="mail-dispatch")
    _DISPATCHED[:] = [t for t in _DISPATCHED if t.is_alive()][-8:]
    _DISPATCHED.append(thread)
    thread.start()
    return thread


def wait_for_dispatch(timeout: float = 15.0) -> None:
    """Block until the sends started so far have finished."""
    for thread in list(_DISPATCHED):
        thread.join(timeout=timeout)


def _link(url_root: str, path: str) -> str:
    """An absolute URL, built without a request context.

    ``url_for(..., _external=True)`` needs one, and these links are built
    inside :func:`_dispatch`'s thread where there is none. The route captures
    ``request.url_root`` — a plain string — and hands it over.

    The path is spelled out rather than reversed from the endpoint, which
    couples this to the ``@app.route`` decorators below. That coupling is
    checked: ``tests/test_password_reset.py`` asserts every link this builds
    resolves to the route it names, so the literal cannot drift out of step
    with the rule.
    """
    return f"{(url_root or '/').rstrip('/')}{path}"


def _send_reset(url_root: str, email: str) -> None:
    """Issue and send a reset link — or do nothing, quietly.

    Runs in a thread, so it must never raise into nowhere and must never
    log the address in a way that turns the log into a mailing list.

    Silence is the correct behaviour for every negative case here. Nobody is
    waiting on a distinction: the page has already told the caller that if
    the address has an account, a link is on its way, and that sentence is
    true whether or not it does.
    """
    try:
        user = _db.get_user_by_email(email)
        if user is None or not user.get("is_active", True):
            return
        # A Google-only account has no password to reset. Issuing one would
        # let anyone holding the address convert an OIDC account into a
        # password account, which is a way in, not a recovery.
        if not user.get("password_hash"):
            log.info("reset asked for an account with no password")
            return

        token = secrets.token_urlsafe(32)
        if not _db.create_auth_token("reset", user["id"], email, token):
            log.warning("reset token could not be created")
            return
        # The right to send is claimed before the provider is called, so a
        # double-submitted form spends one message rather than two.
        if not _db.claim_auth_token_send(token):
            return

        link = _link(url_root, f"/auth/reset/{token}")
        minutes = _db.RESET_TTL_MINUTES
        delivery = _mailer.send(
            to=email, kind="reset", user_id=user["id"],
            subject="Reset your TestForTge password",
            text=(
                f"Somebody asked to reset the password for this address on "
                f"TestForTge.\n\n"
                f"To choose a new one, open this link within {minutes} "
                f"minutes:\n\n{link}\n\n"
                f"If that was not you, you can ignore this message — "
                f"nothing has changed, and the link only works once.\n"
            ))
        if delivery.needs_fallback:
            # Nothing to fall back *to* on this page: the person who needs
            # the link is not the person looking at the screen. Logged at
            # warning so an operator can see that resets are not arriving,
            # which is otherwise invisible — the page says the same thing
            # either way, by design.
            log.warning("reset link could not be delivered: %s",
                        delivery.reason)
    except Exception as exc:      # pragma: no cover — a thread must not die
        log.warning("reset dispatch failed: %s", exc)


def _send_verify(url_root: str, user: dict) -> None:
    """Issue and send an address-confirmation link."""
    try:
        email = _db.normalize_email(user.get("email"))
        if not email:
            return
        token = secrets.token_urlsafe(32)
        if not _db.create_auth_token("verify", user["id"], email, token):
            return
        if not _db.claim_auth_token_send(token):
            return
        link = _link(url_root, f"/auth/verify/{token}")
        _mailer.send(
            to=email, kind="verify", user_id=user["id"],
            subject="Confirm your TestForTge address",
            text=(
                "Please confirm that this address belongs to you by opening "
                f"this link:\n\n{link}\n\n"
                "It works once and expires in a day.\n"
            ))
    except Exception as exc:      # pragma: no cover
        log.warning("verify dispatch failed: %s", exc)


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
                flash(g.t.get("auth_login_failed",
                              _auth.GENERIC_LOGIN_FAILURE), "error")
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
        flash(g.t.get("auth_signed_out_everywhere",
                      "Signed out on every device.") if everywhere
              else g.t.get("auth_signed_out", "Signed out."), "success")
        return redirect(url_for("auth_login"))

    # ── Password reset (E1.3 / E1.7) ──────────────────────────────

    @app.route("/auth/forgot", methods=["GET", "POST"])
    def auth_forgot():
        """Ask for a reset link.

        **The answer never depends on what was found.** Same page, same
        status, same sentence, whether the address has an account, has a
        Google-only account, is deactivated, or was invented on the spot.
        Anything else here is the account-enumeration leak that
        ``engine.auth`` already closes on the sign-in page, reopened one
        route along — and this one is easier to spray, because it takes no
        password.

        The timing half is closed by :func:`_dispatch`; see its docstring.
        """
        if _perm.current_user() is not None:
            # Already signed in: they can change their password from
            # settings, and issuing a reset link would email a live
            # credential to solve a problem they do not have.
            return redirect(url_for("index"))

        if request.method == "GET":
            return render_template("auth_forgot.html")

        email = _db.normalize_email(request.form.get("email"))
        if email and _mailer.plausible_address(email):
            _dispatch(_send_reset, request.url_root, email)
        # …and if it was not plausible, the page still says the same thing.
        # "That is not an email address" would be a fair message, but it is
        # also a free oracle for a script: any response that varies with the
        # input teaches the script something.
        return render_template("auth_forgot.html", submitted=True,
                               email_configured=_mailer.configured())

    @app.route("/auth/reset/<token>", methods=["GET", "POST"])
    def auth_reset(token):
        """Set a new password from a one-time link.

        The token is *inspected* on GET and *consumed* on POST. Consuming on
        GET would burn the link on a mail client's link preview — some fetch
        every URL in a message — and the user would arrive at a dead page
        having done nothing wrong.
        """
        held = _db.get_auth_token(token, "reset")
        if held is None:
            # One answer for expired, used, revoked and never existed. 410
            # rather than 404 because the URL shape was right; the same
            # choice the invite route makes.
            flash(g.t.get("auth_reset_link_dead",
                          "That reset link is no longer valid. Ask for a new "
                          "one."),
                  "error")
            return render_template("auth_reset.html", invalid=True), 410

        if request.method == "GET":
            return render_template(
                "auth_reset.html", token=token,
                min_password_len=_auth.MIN_PASSWORD_LEN)

        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""

        def _again(message: str):
            flash(message, "error")
            return render_template(
                "auth_reset.html", token=token,
                min_password_len=_auth.MIN_PASSWORD_LEN), 400

        if password != confirm:
            return _again("The two passwords do not match.")
        try:
            pwd_hash = _auth.hash_password(password, email=held["email"])
        except _auth.PasswordPolicyError as exc:
            return _again(str(exc))

        # Claimed only once the new password is known to be acceptable —
        # otherwise a typo'd confirmation would spend the link and force the
        # user to start over from their inbox.
        claimed = _db.consume_auth_token(token, "reset")
        if claimed is None:
            flash(g.t.get("auth_reset_link_dead",
                          "That reset link is no longer valid. Ask for a new "
                          "one."),
                  "error")
            return render_template("auth_reset.html", invalid=True), 410

        user_id = claimed["user_id"]
        if not _db.set_password_hash(user_id, pwd_hash):
            log.error("reset consumed for %s but the hash would not save",
                      user_id[:8])
            flash(g.t.get("auth_reset_save_failed",
                          "Your password could not be saved — see server "
                          "logs."),
                  "error")
            return render_template("auth_reset.html", invalid=True), 500

        # Three cleanups, and each one is the point rather than tidiness.
        #
        # Every session goes: a reset is what somebody does when they think
        # the account is compromised, and leaving the intruder's cookie
        # working would make the reset ceremony.
        dropped = _db.delete_sessions_for_user(user_id)
        # Every other live token goes: any reset link still in flight was
        # issued to whoever asked for it.
        _db.revoke_auth_tokens(user_id)
        # The lockout counter goes: proving control of the inbox is a
        # stronger claim than five wrong guesses, and leaving them locked
        # out after a successful reset is a support ticket.
        _db.clear_login_failures(user_id)

        _db.append_audit(entity="user", action="password_reset",
                         user_id=user_id,
                         diff={"sessions_ended": dropped, "via": "email"})
        log.info("password reset for %s; %d session(s) ended",
                 user_id[:8], dropped)
        flash(g.t.get("auth_password_changed",
                      "Your password has been changed and you have been "
                      "signed out "
              "everywhere else. Sign in with the new one."), "success")
        return redirect(url_for("auth_login"))

    # ── Address confirmation (E1.3 / E1.7) ────────────────────────

    @app.route("/auth/verify/<token>", methods=["GET"])
    def auth_verify(token):
        """Confirm an address from a one-time link.

        A GET that changes state, which is normally the wrong shape — but
        the alternative is a page with a button that a mail client cannot
        press, and the thing being changed is a flag the token's holder
        already controls. It grants nothing: the worst a replayed link can
        do is confirm an address that is already confirmed.
        """
        claimed = _db.consume_auth_token(token, "verify")
        if claimed is None:
            return render_template("auth_verify.html", invalid=True), 410

        if not _db.mark_email_verified(claimed["user_id"], claimed["email"]):
            # The account's address changed after this link was sent, so the
            # link proves an address the account no longer uses. Same page
            # as an expired link: from the holder's side it is the same
            # situation — this link cannot confirm anything.
            log.info("verify link no longer matches its account")
            return render_template("auth_verify.html", invalid=True), 410

        _db.append_audit(entity="user", action="email_verified",
                         user_id=claimed["user_id"])
        log.info("address confirmed for %s", claimed["user_id"][:8])
        return render_template("auth_verify.html", confirmed=True,
                               email=claimed["email"])

    @app.route("/auth/verify/request", methods=["POST"])
    @_perm.require_login
    def auth_verify_request():
        """Send myself a confirmation link.

        The caller is the account's owner, signed in, asking about their own
        address — so there is nothing to leak here and the answer can be
        specific.
        """
        user = _perm.current_user()
        if user is None:                       # pragma: no cover — gated
            return redirect(url_for("auth_login"))
        if user.get("email_verified"):
            flash(g.t.get("auth_already_confirmed",
                          "Your address is already confirmed."), "info")
            return redirect(request.referrer or url_for("index"))

        if not _mailer.configured():
            # Honest rather than hopeful: with no provider there is no way
            # to prove an address, and pretending otherwise would leave
            # them clicking a button that does nothing.
            flash(g.t.get("auth_no_email_transport",
                          "This instance cannot send email yet, so addresses "
                  "cannot be confirmed. Ask whoever runs the server."),
                  "error")
            return redirect(request.referrer or url_for("index"))

        _dispatch(_send_verify, request.url_root, dict(user))
        flash(g.t.get("auth_confirmation_sent",
                      "A confirmation link is on its way to %(email)s.")
              % {"email": user.get("email")}, "success")
        return redirect(request.referrer or url_for("index"))

    @app.route("/auth/accept/<token>", methods=["GET", "POST"])
    def auth_accept_invite(token):
        """Claim an invite: create the account (or attach an existing one)
        and join the organisation at the invited role."""
        invite = _db.get_invite(token)
        if invite is None:
            # One message for expired, revoked, already-used and never
            # existed. Distinguishing them would confirm which tokens
            # were real to anyone spraying guesses at the endpoint.
            flash(g.t.get("auth_invite_link_dead",
                          "That invitation link is no longer valid. Ask an "
                          "admin "
                  "to send a new one."), "error")
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
                flash(g.t.get("auth_invite_unclaimable",
                              "That invitation could not be claimed. Ask an "
                              "admin "
                      "to send a new one."), "error")
                return redirect(url_for("auth_login"))
            _db.append_audit(entity="org_member", action="join",
                             user_id=existing["id"], org_id=org_id,
                             diff={"role": invite["role"], "via": "invite"})
            flash(g.t.get("auth_joined_org",
                          "You have joined %(org)s. Sign in to continue.")
                  % {"org": org.get("name", "the team")}, "success")
            return redirect(url_for("auth_login"))

        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        display_name = (request.form.get("display_name") or "").strip()

        if password != confirm:
            flash(g.t.get("auth_passwords_differ",
                          "The two passwords do not match."), "error")
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
            # Proven only if the invitation was actually **emailed** to this
            # address (E0.4 records that in ``Invite.emailed_at``). Then only
            # whoever reads that inbox could have opened the link, and a
            # second confirmation message would be ceremony — on a tier
            # capped at 100 a day, ceremony with a cost.
            #
            # When it was not emailed, the admin handed the link over some
            # other way and nothing about the address has been established.
            # This used to read ``email_verified=True`` unconditionally,
            # which was recording an assumption as a fact — and while no
            # provider existed, the assumption was false every single time.
            email_verified=invite.get("emailed_at") is not None,
        )
        if not user_id:
            # Lost a race with a concurrent claim of the same invite.
            flash(g.t.get("auth_account_exists",
                          "An account for that address already exists. Sign "
                          "in "
                  "instead."), "error")
            return redirect(url_for("auth_login"))

        org_id = _db.consume_invite(token, user_id)
        if not org_id:
            # The invite went stale between the GET and the POST. The
            # account exists and is usable; they just have no team yet.
            log.warning("invite %s… became unclaimable after account "
                        "creation for %s", token[:8], user_id[:8])
            flash(g.t.get("auth_invite_expired",
                          "Your account was created, but the invitation had "
                  "expired. Ask an admin to invite you again."), "error")
            return redirect(url_for("auth_login"))

        _db.append_audit(entity="user", action="create", user_id=user_id,
                         org_id=org_id,
                         diff={"role": invite["role"], "via": "invite"})
        _perm.login_user(user_id, org_id=org_id)
        flash(g.t.get("auth_welcome_org", "Welcome to %(org)s.")
              % {"org": org.get("name", "the team")}, "success")
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
            flash(g.t.get("auth_google_off",
                          "Google sign-in is not configured on this "
                          "instance."),
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
            flash(g.t.get("auth_google_refused",
                          _oauth.GENERIC_REFUSAL), "error")
            return redirect(url_for("auth_login"))

        claims = (token or {}).get("userinfo") or {}
        if not claims:
            try:
                claims = oauth.google.parse_id_token(token) or {}
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("google id_token unreadable: %s", exc)
                claims = {}
        if not claims:
            flash(g.t.get("auth_google_refused",
                          _oauth.GENERIC_REFUSAL), "error")
            return redirect(url_for("auth_login"))

        decision = _oauth.decide(claims, invite_token)
        log.info("google sign-in decision=%s reason=%s",
                 decision.action, decision.reason or "-")

        if decision.action == "refuse":
            # One message for every refusal. The reasons distinguish "no
            # account here" from "your address is unverified", and the
            # first is an account-enumeration oracle that needs no
            # password at all.
            flash(g.t.get("auth_google_refused",
                          _oauth.GENERIC_REFUSAL), "error")
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
                flash(g.t.get("auth_google_refused",
                              _oauth.GENERIC_REFUSAL), "error")
                return redirect(url_for("auth_login"))
            org_id = _db.consume_invite(decision.invite_token, user_id)
            if not org_id:
                log.warning("invite went stale during google provisioning "
                            "for user %s", user_id[:8])
                flash(g.t.get("auth_invite_expired",
                              "Your account was created, but the invitation "
                              "had "
                      "expired. Ask an admin to invite you again."), "error")
                return redirect(url_for("auth_login"))
            _db.link_identity(user_id, _oauth.PROVIDER, subject,
                              email=decision.email)
            _db.append_audit(entity="user", action="create", user_id=user_id,
                             org_id=org_id,
                             diff={"via": "google", "invite": True})
            _perm.login_user(user_id, org_id=org_id)
            flash(g.t.get("auth_welcome",
                          "Welcome to TestForTge."), "success")
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
                flash(g.t.get("auth_google_refused",
                              _oauth.GENERIC_REFUSAL), "error")
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
