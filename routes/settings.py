"""TestFortge — organisation settings and configuration (E2.5).

  * GET  /org/settings                  — the screen (everyone in the team)
  * POST /org/settings/general          — admin: rename the organisation
  * POST /org/settings/llm-key          — admin: store the team's own API key
  * POST /org/settings/llm-key/clear    — admin: forget it
  * POST /org/settings/budget           — admin: monthly LLM allowance
  * POST /org/settings/adopt-projects   — admin: claim pre-ORG_MODE projects

This is where the machinery built in E0.7–E0.9 finally has a surface. Until
now the per-org Anthropic key, the monthly budget and the usage meter were
reachable only from a Python shell, which is a strange thing to ask of a QA
lead.

Readable by every member, writable only by admins — the owner's decision
(§5.1 #4). Read-only is not a courtesy here: a plain user whose generation
quietly fell back to the deterministic engine needs somewhere that says
"the team is over its monthly allowance", and a 403 does not say that.

Claiming the projects that predate the flag
-------------------------------------------
``engine.db.adopt_orphan_projects`` has existed and been tested since E2,
and until now no route called it — so on any deployment that switched
``ORG_MODE`` on, every project created beforehand kept ``org_id = NULL``
and vanished from listings that filter on it, including for the person who
created it. The button below is that missing caller. It is here rather than
on the members page because it is configuration and a one-off migration,
not team management.

Two things it must not do. It must not present the sweep's refusal as
nothing-to-do: the function answers ``0`` both when there are no orphans
and when several organisations exist and it declines to guess whose they
are, and those need opposite words. And it must not act silently — the
count and the names are shown before the button, because this is a
transfer of ownership and the audit entry it writes should not be the
first place anyone finds out what moved.

The key never travels back
--------------------------
The stored API key is never rendered, not even to an admin: the page shows
whether one is configured and its last four characters, which is enough to
answer "which key is this" and useless to anyone reading the response over
someone's shoulder or out of a proxy log. There is no edit-in-place — a
replacement is a fresh paste, because "reveal so you can edit" is how a
credential ends up in a screenshot.
"""
from __future__ import annotations

from flask import (Flask, flash, redirect, render_template, request, url_for)

from engine import db as _db
from engine import features as _features
from engine import llm_cost as _llm_cost
from engine import llm_keys as _llm_keys
from engine import llm_models as _llm_models
from engine import permissions as _perm
from engine.log import get_logger

log = get_logger(__name__)

#: Cap on the monthly allowance an admin may set for their own org, in USD.
#:
#: Not a security control — an org admin can already spend the operator's
#: money up to whatever this is. It exists because a typo'd extra zero on a
#: zero-budget platform is the difference between $5 and $500, and the field
#: is a free-text number.
MAX_BUDGET_USD = 500


def _settings_redirect():
    return redirect(url_for("org_settings"))


def _require_admin_or_flash():
    """True when the caller may write. Flashes and returns False otherwise.

    The write routes carry ``@require_role("admin")`` so this never fires in
    practice — it is the belt to that braces, for the day somebody adds a
    POST here and forgets the decorator.
    """
    if _perm.has_role("admin"):
        return True
    flash("Only admins can change team settings.", "error")
    return False


def register(app: Flask) -> None:

    @app.route("/org/settings", methods=["GET"])
    @_perm.require_login
    def org_settings():
        org_id = _perm.current_org_id()
        if not org_id:
            return render_template("org_settings.html", org=None)

        org = _db.get_organization(org_id) or {}
        org_settings_blob = org.get("settings") or {}
        is_admin = _perm.has_role("admin")
        byok_configured = _db.has_org_secret(org_id, "anthropic_api_key")

        # ``key_source`` matters, and getting it wrong is not cosmetic: with
        # the default ("platform") a team using its own key still got a
        # progress bar reading "X of $9.00 used this month", directly under
        # a paragraph saying the allowance does not apply to them. Passing
        # "org" short-circuits to unlimited, so the meter disappears and the
        # page stops contradicting itself. Their historical platform-key
        # spend stays visible in the breakdown below, where it is history
        # rather than a limit.
        budget = _llm_cost.budget_state(
            org_id, org_settings_blob,
            key_source="org" if byok_configured else "platform")
        usage = _db.llm_usage_summary(org_id)

        return render_template(
            "org_settings.html",
            org=org,
            is_admin=is_admin,
            # ── LLM key (E0.9) ──
            byok_available=_llm_keys.is_configured(),
            byok_configured=byok_configured,
            byok_hint=_llm_keys.redact(_llm_keys.get_org_key(org_id))
                      if is_admin else None,
            platform_key_present=bool(_llm_keys.platform_key()),
            # ── Budget + usage (E0.7) ──
            budget=budget,
            # The form field reads the *stored* allowance, not the effective
            # one. They differ for a BYOK team, where the effective budget is
            # unlimited — pre-filling the input from that would show 0, and
            # saving the form would then silently delete a cap the team
            # wants back the moment they remove their key.
            budget_limit_usd=(
                _llm_cost.org_budget_micros(org_settings_blob)
                / _llm_cost.MICROS_PER_USD),
            budget_spent=_llm_cost.format_usd(budget["spent_micros"]),
            budget_limit=_llm_cost.format_usd(budget["limit_micros"]),
            max_budget_usd=MAX_BUDGET_USD,
            usage=usage,
            usage_total=_llm_cost.format_usd(usage["total_micros"]),
            format_usd=_llm_cost.format_usd,
            micros_per_usd=_llm_cost.MICROS_PER_USD,
            # ── Model routing, for transparency ──
            models=_llm_models.snapshot(),
            # ── Legacy projects waiting to be claimed (E1.6) ──
            # Surveyed for admins only. A plain user cannot run the sweep,
            # and a count of projects they have never been able to see is
            # not information — it is a puzzle.
            orphans=_db.orphan_project_survey() if is_admin else None,
            # ── Not built yet, shown so the screen is honest ──
            storage_configurable=_features.is_enabled(
                "STORAGE_BACKEND_CONFIGURABLE"),
        )

    @app.route("/org/settings/general", methods=["POST"])
    @_perm.require_role("admin")
    def org_settings_general():
        org_id = _perm.current_org_id()
        if not (org_id and _require_admin_or_flash()):
            return _settings_redirect()

        name = (request.form.get("name") or "").strip()
        if not name:
            flash("The team name cannot be empty.", "error")
            return _settings_redirect()
        if len(name) > 160:
            flash("That name is too long (160 characters maximum).", "error")
            return _settings_redirect()

        before = (_db.get_organization(org_id) or {}).get("name")
        with _db.session_scope() as sess:
            row = sess.get(_db.Organization, org_id)
            if row is None:
                flash("Team not found.", "error")
                return _settings_redirect()
            row.name = name
        _db.append_audit(entity="organization", action="rename",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         diff={"name": [before, name]})
        flash("Team name updated.", "success")
        return _settings_redirect()

    @app.route("/org/settings/llm-key", methods=["POST"])
    @_perm.require_role("admin")
    def org_settings_set_llm_key():
        org_id = _perm.current_org_id()
        if not (org_id and _require_admin_or_flash()):
            return _settings_redirect()

        api_key = request.form.get("api_key") or ""
        try:
            _llm_keys.set_org_key(org_id, api_key)
        except _llm_keys.BYOKUnavailable as exc:
            # Encryption is not configured on this instance. Say so rather
            # than storing a customer credential in the clear.
            log.warning("BYOK refused for org=%s: %s", org_id[:8], exc)
            flash("This instance cannot store API keys yet — "
                  "TESTFORTGE_ENCRYPTION_KEY is not set. Ask whoever runs "
                  "the server.", "error")
            return _settings_redirect()
        except ValueError as exc:
            # A shape problem, with a message written for the person who
            # just pasted something. Never echo the value back.
            flash(str(exc), "error")
            return _settings_redirect()

        _db.append_audit(entity="organization", action="set_llm_key",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         # The key itself is deliberately absent. An audit
                         # trail is read by more people than the settings
                         # form is.
                         diff={"key": _llm_keys.redact(api_key)})
        flash("API key saved. Your team's generation now bills to your own "
              "Anthropic account, and the platform allowance no longer "
              "applies.", "success")
        return _settings_redirect()

    @app.route("/org/settings/llm-key/clear", methods=["POST"])
    @_perm.require_role("admin")
    def org_settings_clear_llm_key():
        org_id = _perm.current_org_id()
        if not (org_id and _require_admin_or_flash()):
            return _settings_redirect()
        if _llm_keys.clear_org_key(org_id):
            _db.append_audit(entity="organization", action="clear_llm_key",
                             user_id=_perm.current_user_id(), org_id=org_id)
            flash("API key removed. Generation falls back to the platform "
                  "key and its monthly allowance.", "success")
        else:
            flash("No API key was configured.", "info")
        return _settings_redirect()

    @app.route("/org/settings/budget", methods=["POST"])
    @_perm.require_role("admin")
    def org_settings_budget():
        org_id = _perm.current_org_id()
        if not (org_id and _require_admin_or_flash()):
            return _settings_redirect()

        raw = (request.form.get("budget_usd") or "").strip()
        try:
            budget = float(raw)
        except ValueError:
            flash("Enter the monthly allowance as a number, for example 5 "
                  "or 12.50.", "error")
            return _settings_redirect()
        if budget < 0:
            flash("The allowance cannot be negative.", "error")
            return _settings_redirect()
        if budget > MAX_BUDGET_USD:
            # Catches the typo'd extra zero, which on a zero-budget
            # platform is the difference between $5 and $500.
            flash(f"That is above the {MAX_BUDGET_USD} USD ceiling for a "
                  f"single team. Ask whoever runs the server to raise it.",
                  "error")
            return _settings_redirect()

        before = ((_db.get_organization(org_id) or {}).get("settings")
                  or {}).get("llm_budget_usd")
        if not _db.update_org_settings(org_id, {"llm_budget_usd": budget}):
            flash("Could not save the allowance — see server logs.", "error")
            return _settings_redirect()

        _db.append_audit(entity="organization", action="set_budget",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         diff={"llm_budget_usd": [before, budget]})
        if budget == 0:
            flash("Monthly allowance removed — generation is no longer "
                  "capped for this team.", "success")
        else:
            flash(f"Monthly allowance set to ${budget:,.2f}.", "success")
        return _settings_redirect()

    @app.route("/org/settings/adopt-projects", methods=["POST"])
    @_perm.require_role("admin")
    def org_settings_adopt_projects():
        """Claim every project that has no organisation into this one.

        The survey runs again here rather than trusting a hidden field from
        the form: the page may have been open for an hour, and a count the
        browser remembers is not a count the database agrees with.
        """
        org_id = _perm.current_org_id()
        if not (org_id and _require_admin_or_flash()):
            return _settings_redirect()

        before = _db.orphan_project_survey()

        # Answered before the sweep is asked, because the sweep cannot say
        # which of these two it meant.
        if before["ambiguous"]:
            flash(
                f"There are {before['organisations']} teams on this server, "
                f"so claiming in bulk is refused — there is no way to tell "
                f"which team the {before['count']} unassigned project"
                f"{'' if before['count'] == 1 else 's'} belong to, and "
                f"guessing would hand one team's work to another. Whoever "
                f"runs the server can assign them individually.", "error")
            return _settings_redirect()
        if not before["count"]:
            flash("There are no unassigned projects to claim.", "info")
            return _settings_redirect()

        moved = _db.adopt_orphan_projects(org_id)
        if not moved:
            # Something changed under us — most plausibly a second
            # organisation created between the survey and the sweep, which
            # is exactly the case the sweep refuses. Reporting "0 claimed"
            # as a success would be the silent zero this route exists to
            # avoid.
            log.warning("adopt refused or raced for org=%s: survey said %d",
                        org_id[:8], before["count"])
            flash("Nothing was claimed. The server's teams changed while "
                  "this page was open — reload and look again.", "error")
            return _settings_redirect()

        # An ownership transfer, so it goes in the trail with the names.
        # Bounded by the same limit the page uses: an audit row is not a
        # place to put three hundred strings.
        _db.append_audit(entity="project", action="adopt_orphans",
                         user_id=_perm.current_user_id(), org_id=org_id,
                         diff={"count": moved, "names": before["names"]})
        log.info("org %s claimed %d unassigned project(s)", org_id[:8], moved)
        flash(f"{moved} project{'' if moved == 1 else 's'} claimed for this "
              f"team — {'it is' if moved == 1 else 'they are'} now in your "
              f"project list.", "success")
        return _settings_redirect()


__all__ = ["register", "MAX_BUDGET_USD"]
