"""TestFortge — organisation members and invitations (E2.4).

  * GET  /org/members                       — the team (all members)
  * POST /org/members/invite                — admin: invite an address
  * POST /org/members/<user_id>/role        — admin: change a role
  * POST /org/members/<user_id>/remove      — admin: remove a member
  * POST /org/invites/<token>/revoke        — admin: cancel an invitation

With registration invite-only, this page is the **only** door into the
platform, which makes two things load-bearing rather than nice:

**The invite URL is shown on screen.** Email delivery is E0.4 and is not
built yet, and the free tier it will use allows 100 messages a day. The
identity spike called a copy-and-paste fallback acceptable for shipping,
and it stays the primary mechanism until E0.4 lands — so the admin always
gets a link they can send however they like.

**The last-admin guard covers both doors.** Removing the only admin and
demoting the only admin reach the same unrecoverable state: nobody can
create a project, change settings, or promote anyone back. Both are
refused in ``engine.db``, so no route can forget, and both are refused
here with a message that says what to do instead.

The page is readable by every member and writable only by admins — the
owner's decision (§5.1 #4): a plain user seeing the team read-only
produces fewer "where do I look" messages than a 403 does.
"""
from __future__ import annotations

from flask import (Flask, flash, redirect, render_template, request, url_for)

from engine import db as _db
from engine import permissions as _perm
from engine.log import get_logger

from .auth import new_invite_token

log = get_logger(__name__)


def _members_redirect():
    return redirect(url_for("org_members"))


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

        token = new_invite_token()
        if not _db.create_invite(org_id, email, role, token,
                                 invited_by_user_id=_perm.current_user_id()):
            flash("That invitation could not be created — see server logs.",
                  "error")
            return _members_redirect()

        _db.append_audit(entity="invite", action="create",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         diff={"email": email, "role": role})

        link = url_for("auth_accept_invite", token=token, _external=True)
        # Flashed as the link itself, not "an email has been sent": saying
        # the latter while E0.4 is unbuilt would be a lie, and the admin
        # would wait for a message that never arrives.
        flash(f"Invitation created for {email} as {role}. Send them this "
              f"link — it is valid for 7 days and is cancelled if you "
              f"invite the same address again: {link}", "success")
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
