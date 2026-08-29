"""TestFortge — organisation members and invitations (E2.4).

  * GET  /org/members                       — the team (all members)
  * POST /org/members/invite                — admin: invite an address
  * POST /org/members/<user_id>/role        — admin: change a role
  * POST /org/members/<user_id>/remove      — admin: remove a member
  * POST /org/members/<user_id>/password    — admin: set a password
  * POST /org/invites/revoke                — admin: cancel an invitation
  * POST /org/invites/reissue               — admin: new link for one

With registration invite-only, this page is the **only** door into the
platform, which makes two things load-bearing rather than nice:

**The invite URL is shown on screen — even when the email went out.**
E0.4 sends the invitation now, but the link stays visible on every path.
Two reasons, and neither is caution for its own sake: an admin who watched
the address get typed wrong needs to forward it themselves, and a message
the provider accepted can still bounce. The free tier also allows only 100
a day, so "it was not emailed" is an ordinary outcome rather than a fault
— see ``_UNDELIVERED``.

**...and it can be asked for again.** The link lives in a flash message,
and a flash survives exactly one page load. An admin on an instance with
no mail provider who clicked away before copying it had no way back: the
token is deliberately absent from the pending list, so nothing on the page
could show it again. ``org_reissue_invite`` mints a fresh one for an
address that is already pending — the same replace-the-old-link move the
invite form already makes, reached without retyping the address and
without the admin having to work out that re-inviting is the workaround.

**Whether it was emailed is recorded**, in ``Invite.emailed_at``, and that
is not bookkeeping. An invitation that arrived in an inbox can only have
been opened by whoever reads that inbox, so the address is proven; a link
pasted into a chat proves nothing. ``routes/auth.py`` reads the column to
decide whether the new account's address counts as verified, rather than
assuming it does.

**The last-admin guard covers both doors.** Removing the only admin and
demoting the only admin reach the same unrecoverable state: nobody can
create a project, change settings, or promote anyone back. Both are
refused in ``engine.db``, so no route can forget, and both are refused
here with a message that says what to do instead.

**An admin can set a member's password**, and that is a recovery path
rather than a convenience. ``/auth/forgot`` already tells a locked-out
user to "ask whoever runs the server to reset your password for you" —
and until this route existed, that person had no way to do it: the only
``set_password_hash`` call in the application sat behind a reset token
that arrives by email, and this instance may have no mail transport
configured. Re-inviting is not a way round it either, because
``accept_invite`` creates the account and refuses when the address
already has one. So an account whose password was forgotten on a
mail-less instance was locked out permanently, while the page told the
user to ask for help that could not be given.

The password is never logged, never flashed back, and the same three
cleanups the email reset performs run here too — see
``org_set_password``.

The page is readable by every member and writable only by admins — the
owner's decision (§5.1 #4): a plain user seeing the team read-only
produces fewer "where do I look" messages than a 403 does.
"""
from __future__ import annotations

from flask import (Flask, flash, redirect, render_template, request, url_for)

from engine import auth as _auth
from engine import db as _db
from engine import mailer as _mailer
from engine import permissions as _perm
from engine.log import get_logger

from .auth import new_invite_token

log = get_logger(__name__)


def _members_redirect():
    return redirect(url_for("org_members"))


def org_name(org_id: str) -> str:
    return (_db.get_organization(org_id) or {}).get("name") or "your team"


#: Why an invitation was not emailed, in a sentence an admin can act on.
#:
#: Mapped rather than echoing ``delivery.reason``, which is a code for the
#: log. "daily_cap" tells an operator something; it tells a QA lead nothing,
#: and the useful part is what to do next — which for every one of these is
#: "send the link yourself", already in the message that follows.
_UNDELIVERED = {
    "not_configured": "This instance cannot send email yet, so it was not "
                      "emailed.",
    "sending_disabled": "Sending is switched off on this instance, so it "
                        "was not emailed.",
    "daily_cap": "Today's email allowance is used up, so it was not "
                 "emailed.",
    "invalid_address": "That address could not be emailed.",
}


def _undelivered(delivery) -> str:
    return _UNDELIVERED.get(
        delivery.reason,
        "The email could not be delivered just now.")


#: The opening sentence of the flash, by what the admin did and whether it
#: was emailed. Only the opening: everything after it — why it was not
#: emailed, and the link — is composed once in ``_issue_invite``, because
#: the link is the part that matters and it is handed over the same way
#: whether this is a first invitation or a replacement for one whose flash
#: scrolled away.
_LEAD = {
    ("create", True): "Invitation sent to %s as %s.",
    ("create", False): "Invitation created for %s as %s.",
    ("reissue", True): "A new invitation was sent to %s as %s. The earlier "
                       "link no longer works.",
    ("reissue", False): "A new link was issued for %s as %s. The earlier "
                        "link no longer works.",
}


def register(app: Flask) -> None:

    @app.route("/org/members", methods=["GET"])
    @_perm.require_login
    def org_members():
        org_id = _perm.current_org_id()
        if not org_id:
            # A real state, not an error: an admin who deleted their last
            # organisation lands here.
            return render_template("org_members.html", org=None,
                                   members=[], invites=[])
        org = _db.get_organization(org_id) or {}
        members = _db.list_org_members(org_id)
        # Only an admin may see pending invitations. To everyone else the
        # list of addresses somebody tried to recruit is none of their
        # business — and it is exactly the list worth phishing.
        invites = (_db.list_pending_invites(org_id)
                   if _perm.has_role("admin") else [])
        return render_template(
            "org_members.html", org=org, members=members, invites=invites,
            roles=_db.ORG_ROLES, me=_perm.current_user_id(),
            admin_count=_db.count_org_admins(org_id),
            # Whether this instance can send mail decides which sentence
            # the note under the form tells. It said "TestForTge does not
            # send email on this plan" from before E0.4 until now, which
            # was a promise the product had stopped keeping in both
            # directions — untrue where a key is set, and on an instance
            # without one it blamed the plan for a missing setting.
            # configured() and not state(): the page shows no
            # counter, and state() would cost an audit-table count on
            # every load of a page nobody opens to read a quota.
            mail_configured=_mailer.configured(),
        )

    @app.route("/org/members/invite", methods=["POST"])
    @_perm.require_role("admin")
    def org_invite():
        org_id = _perm.current_org_id()
        if not org_id:
            flash("Create an organisation before inviting people.", "error")
            return _members_redirect()

        email = _db.normalize_email(request.form.get("email"))
        role = (request.form.get("role") or "user").strip()

        # Shape check only — the address has to survive a round trip
        # through someone's inbox to be useful, so a regex here would be
        # theatre. What it catches is a typo'd or empty field.
        if not email or "@" not in email or email.startswith("@") \
                or email.endswith("@") or " " in email:
            flash("Enter a valid email address.", "error")
            return _members_redirect()
        if role not in _db.ORG_ROLES:
            flash("Pick a valid role.", "error")
            return _members_redirect()

        existing = _db.get_user_by_email(email)
        if existing is not None and _db.get_org_role(org_id, existing["id"]):
            flash(f"{email} is already on this team. Change their role "
                  f"below instead.", "info")
            return _members_redirect()

        return _issue_invite(org_id, email, role, action="create")

    @app.route("/org/invites/reissue", methods=["POST"])
    @_perm.require_role("admin")
    def org_reissue_invite():
        """Issue a fresh link for an address that is already invited.

        Keyed on **email, not token**, for the same reason
        ``org_revoke_invite`` is: the token is a bearer credential for
        somebody else's seat and never reaches the page. The role is read
        back from the pending invite rather than taken from the form, so a
        button that means "let me have the link again" cannot quietly
        become a promotion.

        It re-issues rather than re-displays because there is nothing to
        re-display: the token is stored, but showing it again would mean
        putting it in the pending list, and then every admin session's
        rendered HTML, browser cache and screenshot carries a live
        credential for every open seat. Minting a new one costs the old
        link — which is exactly what ``_db.create_invite`` already does
        when the same address is invited twice, and what the note under
        the invite form has always said happens.
        """
        org_id = _perm.current_org_id()
        email = _db.normalize_email(request.form.get("email"))
        if not (org_id and email):
            flash("That invitation is no longer active.", "info")
            return _members_redirect()

        # The pending list is the authority on both "is there one" and "at
        # what role", and it is already scoped to this organisation — so an
        # admin of one team cannot mint a link into another's.
        pending = next((i for i in _db.list_pending_invites(org_id)
                        if i.get("email") == email), None)
        if pending is None:
            # Claimed, cancelled, expired, or never existed. One message:
            # the difference does not change what the admin does next,
            # which is to invite the address again from the form above.
            flash("That invitation is no longer active. Invite the address "
                  "again to send a new link.", "info")
            return _members_redirect()

        role = pending.get("role") or "user"
        if role not in _db.ORG_ROLES:      # pragma: no cover — stored value
            role = "user"
        return _issue_invite(org_id, email, role, action="reissue")

    def _issue_invite(org_id: str, email: str, role: str, *, action: str):
        """Mint a token, store it, try to email it, hand over the link.

        Shared by the invite form and the reissue button so the two cannot
        drift — in particular so that the link keeps reaching the admin on
        the reissue path, which exists precisely because the first one did
        not reach them.
        """
        actor = _perm.current_user_id()
        token = new_invite_token()
        # This revokes any live invite for the address, which is what makes
        # the reissue path safe to press twice.
        if not _db.create_invite(org_id, email, role, token,
                                 invited_by_user_id=actor):
            flash("That invitation could not be created — see server logs.",
                  "error")
            return _members_redirect()

        _db.append_audit(entity="invite", action=action,
                         user_id=actor, org_id=org_id,
                         diff={"email": email, "role": role})

        link = url_for("auth_accept_invite", token=token, _external=True)

        # E0.4: try to send it. Synchronous, unlike the password-reset
        # dispatch — there is no address to protect here (the admin typed it
        # and is entitled to know what happened to it), and the outcome
        # decides what the flash says. Waiting a moment for a definite
        # answer beats "probably sent".
        delivery = _mailer.send(
            to=email, kind="invite",
            user_id=actor, org_id=org_id,
            subject=f"You have been invited to {org_name(org_id)} "
                    f"on TestForTge",
            text=(
                f"You have been invited to join {org_name(org_id)} on "
                f"TestForTge as {role}.\n\n"
                f"Open this link to set up your account:\n\n{link}\n\n"
                f"The invitation is valid for 7 days.\n"
            ))

        lead = _LEAD[(action, bool(delivery.sent))] % (email, role)
        if delivery.sent:
            _db.mark_invite_emailed(token)
            # The link is *still* shown. An admin who watched the address
            # get typed wrong needs to be able to forward it themselves, and
            # a delivery that Resend accepted can still bounce.
            tail = (f"If it does not arrive, this link works too — it is "
                    f"valid for 7 days and is cancelled if you invite the "
                    f"same address again: {link}")
        else:
            # The pre-E0.4 behaviour, kept as the fallback rather than
            # replaced by it: say what happened and hand over the link.
            # Claiming a message was sent would leave the admin waiting for
            # one that never arrives — and would make the invited address
            # count as proven when nothing proved it.
            tail = (f"{_undelivered(delivery)} Send them this link — it is "
                    f"valid for 7 days and is cancelled if you invite the "
                    f"same address again: {link}")

        flash(f"{lead} {tail}", "success")
        return _members_redirect()

    @app.route("/org/members/<user_id>/role", methods=["POST"])
    @_perm.require_role("admin")
    def org_change_role(user_id):
        org_id = _perm.current_org_id()
        role = (request.form.get("role") or "").strip()
        if not org_id or role not in _db.ORG_ROLES:
            flash("Pick a valid role.", "error")
            return _members_redirect()

        before = _db.get_org_role(org_id, user_id)
        if before is None:
            flash("That person is not on this team.", "error")
            return _members_redirect()

        if not _db.change_org_role(org_id, user_id, role):
            # The only way this fails with a valid role is the last-admin
            # guard. Say what to do rather than what rule fired.
            flash("You are the only admin on this team. Promote someone "
                  "else to admin first, then change your own role.", "error")
            return _members_redirect()

        _db.append_audit(entity="org_member", action="role_change",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         entity_id=user_id,
                         diff={"role": [before, role]})
        flash("Role updated.", "success")
        return _members_redirect()

    @app.route("/org/members/<user_id>/remove", methods=["POST"])
    @_perm.require_role("admin")
    def org_remove_member(user_id):
        org_id = _perm.current_org_id()
        if not org_id:
            return _members_redirect()

        before = _db.get_org_role(org_id, user_id)
        if before is None:
            # Idempotent: removing someone who is already gone is a no-op,
            # not an error — a double-submitted form should not scold.
            flash("That person is not on this team.", "info")
            return _members_redirect()

        if not _db.remove_org_member(org_id, user_id):
            flash("You are the only admin on this team. Promote someone "
                  "else to admin before removing yourself.", "error")
            return _members_redirect()

        # Their sessions die with their access. Leaving them alive would
        # mean a removed colleague keeps working until their cookie
        # happens to expire, which is not what "removed" means.
        dropped = _db.delete_sessions_for_user(user_id)
        _db.append_audit(entity="org_member", action="remove",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         entity_id=user_id,
                         diff={"role": before, "sessions_ended": dropped})
        log.info("user %s removed from org %s; %d session(s) ended",
                 user_id[:8], org_id[:8], dropped)
        # The account itself survives on purpose. Their name still has to
        # resolve on every test case they wrote and every bug they filed,
        # and a deleted row turns an audit trail into orphaned ids.
        flash("Removed from the team. Their test cases and bug reports "
              "stay, still attributed to them.", "success")
        return _members_redirect()

    @app.route("/org/members/<user_id>/password", methods=["POST"])
    @_perm.require_role("admin")
    def org_set_password(user_id):
        """Set a team member's password, for when no link can reach them.

        Scoped to this organisation through ``get_org_role``: without
        that check an admin of one team could set the password of any
        account whose id they could name, which is every account.

        Allowed on another admin deliberately. Within a team admins are
        peers, and the alternative — nobody can help a locked-out admin —
        is the state this route exists to end.
        """
        org_id = _perm.current_org_id()
        if not org_id:
            flash("Create an organisation before managing members.",
                  "error")
            return _members_redirect()

        if _db.get_org_role(org_id, user_id) is None:
            # Also the answer for a user id from another team: they are
            # not on this one, and saying more would confirm the id
            # exists somewhere.
            flash("That person is not on this team.", "error")
            return _members_redirect()

        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        if password != confirm:
            # Checked before the policy so the admin fixes the typo they
            # made rather than a policy complaint about the wrong one of
            # two different strings.
            flash("The two passwords do not match.", "error")
            return _members_redirect()

        member = _db.get_user(user_id) or {}
        try:
            # email= so the same policy applies as on the self-service
            # paths: a password equal to the address is refused here too.
            pwd_hash = _auth.hash_password(
                password, email=member.get("email"))
        except _auth.PasswordPolicyError as exc:
            flash(str(exc), "error")
            return _members_redirect()

        if not _db.set_password_hash(user_id, pwd_hash):
            flash("That password could not be saved — see server logs.",
                  "error")
            return _members_redirect()

        # The same three cleanups the email reset performs, for the same
        # reasons — a password change that leaves the old cookie working
        # is ceremony, a reset link still in flight was issued to whoever
        # asked for it, and leaving somebody locked out after their
        # password was just fixed is a support ticket.
        dropped = _db.delete_sessions_for_user(user_id)
        _db.revoke_auth_tokens(user_id)
        _db.clear_login_failures(user_id)

        # The audit row records that it happened and who did it. It does
        # NOT record the password, and neither does the log line: an
        # audit trail is read by more people than an inbox is.
        _db.append_audit(entity="user", action="password_set",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         entity_id=user_id,
                         diff={"email": member.get("email", ""),
                               "sessions_ended": dropped, "via": "admin"})
        log.info("password set by admin for %s; %d session(s) ended",
                 user_id[:8], dropped)
        flash(f"Password set for {member.get('email') or 'that member'}. "
              f"They have been signed out everywhere and can sign in with "
              f"the new one — tell them yourself, this page will not show "
              f"it again.", "success")
        return _members_redirect()

    @app.route("/org/invites/revoke", methods=["POST"])
    @_perm.require_role("admin")
    def org_revoke_invite():
        """Cancel the live invitation for an address.

        Keyed on **email, not token**. The token is a bearer credential
        for somebody else's seat, so it is deliberately absent from the
        pending list that renders on this page — which means a
        ``/org/invites/<token>/revoke`` route would have had nothing to
        put in the form action. Email is what the admin can see, and
        scoping the lookup to their own organisation is what stops it
        being a way to cancel another team's invitations.
        """
        org_id = _perm.current_org_id()
        email = _db.normalize_email(request.form.get("email"))
        if not (org_id and email):
            flash("That invitation is no longer active.", "info")
            return _members_redirect()

        revoked = _db.revoke_invites_for_email(org_id, email)
        if not revoked:
            # Already claimed, already cancelled, or never existed. One
            # message: an admin does not need the difference, and a
            # double-submitted form should not scold.
            flash("That invitation is no longer active.", "info")
            return _members_redirect()

        _db.append_audit(entity="invite", action="revoke",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         diff={"email": email, "count": revoked})
        flash(f"Invitation for {email} cancelled.", "success")
        return _members_redirect()


__all__ = ["register"]
