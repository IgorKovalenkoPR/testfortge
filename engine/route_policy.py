"""TestFortge — the access policy for every route, in one table (E2.3).

Eighty-eight endpoints. Decorating each by hand would have produced an
eighty-file diff in which one missing line looks exactly like the
eighty-seven correct ones — the failure mode E2.2 exists to prevent. So
the policy is a table, and a single ``before_request`` hook applies it.

Roles, as the owner specified them (requirement 2):

* ``admin`` — creates projects, changes settings and configuration.
* ``user``  — everything else: test cases, checklists, execution, bugs,
  estimation, metrics.
* ``login`` — any signed-in person, whether or not they are in a team yet.
  Used for the shell (dashboard, guide): a user whose organisation was
  deleted must still be able to reach a page that tells them so, rather
  than a 403 on every URL including the one that would explain it.

Why a hook rather than wrapping the view functions
--------------------------------------------------
Wrapping post-registration would sit underneath ``csrf.exempt`` (which
keys on ``module.name``) and every other decorator already applied, and
any subtlety there becomes a security bug rather than a rendering bug. A
``before_request`` hook touches nothing: it reads ``request.endpoint`` and
either returns a refusal or gets out of the way.

Fail-closed
-----------
An endpoint that is in neither :data:`POLICY` nor :data:`OPEN` is
**refused** once ``AUTH_ENABLED`` is on. A new route is therefore
unreachable until somebody classifies it, which is the opposite of the
usual accident. ``tests/test_permissions.py`` turns that from a runtime
surprise into a build failure.
"""
from __future__ import annotations

from engine.log import get_logger

log = get_logger(__name__)

#: Endpoints that are public on purpose, each with its reason.
#:
#: Kept as a dict rather than a set so the reason travels with the entry —
#: an allowlist of bare names accretes entries nobody can justify or
#: remove.
OPEN: dict[str, str] = {
    # ── Ops probes ────────────────────────────────────────────────
    "healthz": "liveness probe; external monitors call it with no "
               "credentials by design",
    "readyz": "readiness probe, same as above",
    "metrics": "operator telemetry, gated separately by OPS_ENDPOINTS_TOKEN",

    # ── The authentication surface ────────────────────────────────
    "auth_login": "you cannot require a session to create one",
    "auth_logout": "must work even from a session already half gone",
    "auth_accept_invite": "the invitation IS the credential",
    "auth_google_start": "starts the flow that produces the session",
    "auth_google_callback": "finishes that flow; Authlib's state and nonce "
                            "are what authenticate this request, not a "
                            "session that does not exist yet",
    "auth_me": "answers 'nobody' when nobody is signed in; the UI needs "
               "that answer to render a sign-in link",
    "api_csrf_token": "needed BEFORE any POST, including the sign-in POST",

    # ── Static ────────────────────────────────────────────────────
    "static": "css, js and images — gating these would break the sign-in "
              "page's own styling",

    # ── Token-authenticated machine callers ───────────────────────
    #
    # Each carries its own bearer/token check and is csrf-exempt for that
    # reason. A session role gate is the wrong control for a caller that
    # has no session at all — the Chrome extension, CI, the MCP service.
    "api_recorder_session_start": "extension token auth",
    "api_recorder_session_finish": "extension token auth",
    "api_browser_poll": "extension short-poll, token auth",
    "api_browser_result": "extension result post, token auth",
    "automation_allure_results": "CI posts with AUTOMATION_INGEST_TOKEN",
    "test_cases_review_session": "reached by a one-time token in the URL, "
                                 "from a browser that may never sign in",
    "test_cases_review_session_save": "same one-time token",
}


#: endpoint → minimum role.
#:
#: The split follows the owner's wording: an admin creates projects and
#: changes settings and configuration; a user does the QA work. So
#: *switching* to a project is user-level, while *creating*, *renaming*,
#: *deleting* or *restructuring* one is not.
POLICY: dict[str, str] = {
    # ── The shell — reachable before you are in a team ────────────
    "index": "login",
    # ── Dashboard (E7) ────────────────────────────────────────────
    # Saving your own widget layout and exporting the numbers you can already
    # see are ordinary member actions. ``dashboard_targets`` is absent on
    # purpose: it carries @require_role("admin") and is self-enforcing.
    "dashboard_layout": "login",
    "dashboard_export_csv": "login",
    "guide_page": "login",

    # ── Projects: creating and reconfiguring is admin work ────────
    "db_create_project": "admin",
    "db_rename_project": "admin",
    "delete_project": "admin",
    "db_move_artifacts": "admin",     # restructures where work lives
    # Claiming the projects that predate ORG_MODE transfers ownership of
    # every unassigned one at once (E1.6). Self-enforcing via
    # @require_role("admin"), and listed anyway so the table shows what the
    # deployment's most consequential one-off action needs — the same
    # reason manual_run_assign is listed below.
    "org_settings_adopt_projects": "admin",
    # …but picking which existing project you are working in is not.
    "db_select_project": "user",
    "load_project": "user",
    "new_session": "user",            # clears the caller's own workspace
    # "Save current work" is user-level, even though the underlying helper
    # upserts and would create a row for an unknown name. Requirement 2
    # withholds *creating projects*, and this is the button that persists
    # work somebody already did — while the session is still the source of
    # truth (see docs/plans/adr/0001), gating it would mean a plain user's
    # test cases can be lost on a dyno restart. Explicit creation stays
    # admin-only above, which is what the requirement is actually about.
    # The distinction disappears with E3, when saving stops being an act.
    "save_project": "user",

    # ── Diagnostics echo configuration back ───────────────────────
    "estimation_diag": "admin",
    "test_execution_diag": "admin",
    "debug_walkthrough_dispatch": "admin",

    # ── Estimation ────────────────────────────────────────────────
    "estimation_page": "user",
    "estimation_run": "user",
    "estimation_run_async": "user",
    "estimation_status": "user",
    "estimation_to_test_cases": "user",
    "estimation_export": "user",

    # ── Test cases ────────────────────────────────────────────────
    "test_cases_page": "user",
    "test_cases_run_async": "user",
    "test_cases_status": "user",
    "test_cases_upload": "user",
    "test_cases_update_walkthrough_meta": "user",
    "test_cases_update_step_kind": "user",
    "api_pack_info": "user",

    # ── Checklist ─────────────────────────────────────────────────
    "checklist_page": "user",
    "checklist_run_async": "user",
    "checklist_status": "user",
    "checklist_upload": "user",

    # ── Execution ─────────────────────────────────────────────────
    "test_execution_page": "user",
    "test_execution_auto_run": "user",
    "test_execution_results": "user",
    "test_execution_run_status": "user",
    "test_execution_generate_account": "user",
    "test_execution_live": "user",
    "test_execution_live_frame": "user",
    "test_execution_live_strip": "user",
    "test_execution_live_info": "user",
    "manual_run_start": "user",
    "manual_run_page": "user",
    "manual_run_resume": "user",
    "manual_runs_page": "user",
    # manual_run_assign carries @require_role("admin") and is
    # self-enforcing, but the fail-closed table still needs to know it
    # exists — an unclassified endpoint is refused outright.
    "manual_run_assign": "admin",
    "manual_run_verdict": "user",
    "manual_run_finish": "user",

    # ── Automation ────────────────────────────────────────────────
    "automation_page": "user",
    "automation_run": "user",
    "automation_run_async": "user",
    "automation_status": "user",
    "automation_run_detail": "user",
    "automation_bundle": "user",
    "automation_asset": "user",
    "automation_generate_account": "user",

    # ── Bugs ──────────────────────────────────────────────────────
    "bug_reports_page": "user",
    "create_bug_report": "user",
    # Mixed bag: most bulk actions are ordinary triage, but `delete`
    # destroys evidence. The route is user-level and the handler asks for
    # admin on that one action — see routes/bugs.py. Splitting it into two
    # endpoints would change a URL the toolbar posts to for no gain.
    "bugs_bulk": "user",
    "bugs_reset": "admin",            # wipes every bug on the project
    "export_bug_reports": "user",
    "export_bug_reports_csv": "user",

    # ── Export ────────────────────────────────────────────────────
    "export": "user",

    # ── Tedgie ────────────────────────────────────────────────────
    "chat_route": "user",
    "chat_stream_route": "user",
    "chat_history_route": "user",
    "chat_reset_route": "user",
    "chat_bug_form_route": "user",

    # ── Metrics ───────────────────────────────────────────────────
    "metrics_history_route": "user",
}


class PolicyError(RuntimeError):
    """Raised when the table is internally inconsistent."""


def validate() -> list[str]:
    """Problems with the table itself, independent of any app.

    Checked at install time and by the test suite: an endpoint listed in
    both tables, or given a role that does not exist, would otherwise
    behave in whichever way the lookup order happened to produce.
    """
    from engine import db as _db

    problems: list[str] = []
    both = sorted(set(POLICY) & set(OPEN))
    if both:
        problems.append(
            f"listed as both open and role-gated: {both}. One of the two "
            f"entries is a mistake and the lookup order decides which wins."
        )
    allowed = set(_db.ORG_ROLES) | {"login"}
    for endpoint, role in sorted(POLICY.items()):
        if role not in allowed:
            problems.append(
                f"{endpoint} requires {role!r}, which is not a role "
                f"({', '.join(sorted(allowed))})."
            )
    return problems


def policy_for(endpoint: str | None) -> str | None:
    """The minimum role for *endpoint*, or ``None`` if it is not gated.

    ``None`` covers both "open on purpose" and "not classified" — the
    caller distinguishes them, because they get opposite treatment.
    """
    if not endpoint:
        return None
    return POLICY.get(endpoint)


def is_open(endpoint: str | None) -> bool:
    return bool(endpoint) and endpoint in OPEN


def unclassified(app) -> list[str]:
    """Endpoints on *app* that are neither open, gated, nor self-enforcing.

    Self-enforcing means the view carries ``_required_role`` from one of
    the decorators in :mod:`engine.permissions` — the newer routes annotate
    themselves at the definition, which reads better there and is equally
    binding.
    """
    out = []
    for rule in app.url_map.iter_rules():
        endpoint = rule.endpoint
        if endpoint in OPEN or endpoint in POLICY:
            continue
        view = app.view_functions.get(endpoint)
        if view is not None and hasattr(view, "_required_role"):
            continue
        out.append(endpoint)
    return sorted(set(out))


def install(app) -> None:
    """Attach the enforcement hook.

    Registered last, so it runs after the HTTP Basic gate — that gate is
    the outer perimeter during the rollout and should reject before any of
    this machinery runs.
    """
    problems = validate()
    for problem in problems:
        log.error("route policy: %s", problem)
    if problems:
        # Refuse to boot on an inconsistent table rather than serve
        # requests under a policy nobody can predict.
        raise PolicyError("; ".join(problems))

    from flask import request

    from engine import permissions as _perm

    @app.before_request
    def _enforce_route_policy():
        # While AUTH_ENABLED is off there is no identity to check, and the
        # app behaves exactly as it did before this programme.
        if not _perm.auth_active():
            return None

        endpoint = request.endpoint
        # No endpoint means no matching rule — Flask is about to 404, and
        # a 401 here would leak which URLs exist.
        if endpoint is None or endpoint in OPEN:
            return None

        view = app.view_functions.get(endpoint)
        if view is not None and hasattr(view, "_required_role"):
            # Self-enforcing; the decorator already ran or is about to.
            return None

        minimum = POLICY.get(endpoint)
        if minimum is None:
            # Fail closed. A route nobody classified is unreachable rather
            # than open — the opposite of the usual accident. The coverage
            # test makes this a build failure, so it should never be seen
            # in production.
            log.error(
                "route %r has no access policy — refusing. Add it to "
                "engine/route_policy.POLICY or OPEN.", endpoint)
            return _perm._deny_forbidden("user")

        if _perm.current_user() is None:
            return _perm._deny_unauthenticated()

        if minimum == "login":
            return None

        if not _perm.has_role(minimum):
            return _perm._deny_forbidden(minimum)
        return None

    log.info("Route policy installed: %d gated, %d open.",
             len(POLICY), len(OPEN))


__all__ = ["POLICY", "OPEN", "PolicyError", "validate", "policy_for",
           "is_open", "unclassified", "install"]
