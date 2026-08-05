"""TestFortge — the edit endpoint every editor posts to (E4.1).

  * GET   /api/edit/<entity>/<entity_id>   — the row, with its version
  * PATCH /api/edit/<entity>/<entity_id>   — change declared fields

One route for all four editors, because the interesting parts — the field
allowlist, validation, the version check, provenance and the audit row —
are identical for a test case, a checklist item and a bug report. Four
copies would be four chances to leave one out.

Gated on ``EDITORS_ENABLED``, which ``engine.features`` makes conditional on
``WORKSPACE_DB_FIRST``: editing a Flask session edits a private copy of
shared team data, so the editors cannot come on before the workspace does
(ADR 0001).

Why PATCH and not POST
----------------------
The request carries only the fields that changed, which is what PATCH
means. It also keeps the endpoint out of reach of a plain HTML form — forms
cannot send PATCH — so there is no path where a mis-scoped form submission
lands here by accident. E4.2's front-end component uses fetch.

CSRF still applies: Flask-WTF protects every non-GET method, and the
component sends the token from the page's meta tag like the rest of the app.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from engine import db as _db
from engine import editable as _editable
from engine import features as _features
from engine import permissions as _perm
from engine.log import get_logger

from ._shared import resolve_active_project

log = get_logger(__name__)


def _editors_enabled() -> bool:
    return _features.effective("EDITORS_ENABLED")


def _disabled():
    return jsonify({
        "error": "editors_disabled",
        "message": "Editing is not enabled on this instance yet.",
    }), 404


def _invalidate(project_id: str) -> None:
    """Drop the per-request cache after a write.

    The repository's own write helpers do this for themselves; these
    handlers write through ``engine.editable``, which does not know the
    cache exists. Without it a page rendered in the same request as the
    write shows the value from before it.
    """
    try:
        from engine import workspace as _workspace
        _workspace.invalidate(project_id)
    except Exception as exc:          # pragma: no cover — cache only
        log.debug("workspace invalidate after edit skipped: %s", exc)


def _house_style(entity: str, changes: dict) -> dict:
    """Advisory wording notes for the fields that just changed (E4.3).

    Returned alongside a successful save, never instead of one. The house
    style was measured from the team's own reference plan, so it is advice
    for the person writing the case — a rule that refused the save would
    eventually be wrong about a real case, and the cost of that is somebody
    unable to record what they tested.
    """
    if entity not in ("test_case", "checklist_item"):
        return {}
    try:
        from engine import tc_author
    except Exception as exc:          # pragma: no cover — advisory only
        log.debug("house-style advice unavailable: %s", exc)
        return {}
    out = {}
    for field, value in changes.items():
        try:
            findings = tc_author.house_style_findings(field, str(value or ""))
        except Exception as exc:      # pragma: no cover — advisory only
            log.debug("house-style advice failed on %s: %s", field, exc)
            continue
        if findings:
            out[field] = findings
    return out


def register(app: Flask) -> None:

    @app.route("/api/edit/<entity>/<entity_id>", methods=["GET"])
    @_perm.require_role("user")
    def api_edit_get(entity: str, entity_id: str):
        if not _editors_enabled():
            return _disabled()
        try:
            _editable.entity(entity)
        except _editable.UnknownEntity as exc:
            return jsonify({"error": "unknown_entity",
                            "message": str(exc)}), 404

        project_id = resolve_active_project(pin=False)
        try:
            row = _editable.get(entity, project_id, entity_id)
        except _editable.AmbiguousEntity as exc:
            return jsonify({"error": "ambiguous_id",
                            "message": str(exc)}), 409
        if row is None:
            # Same answer whether the row is missing or belongs to another
            # project: distinguishing them would confirm an id exists
            # somewhere the caller cannot see.
            return jsonify({"error": "not_found",
                            "message": "No such item in this project."}), 404
        return jsonify({
            "entity": entity,
            "item": row,
            "editable_fields": _editable.editable_fields(entity),
        })

    @app.route("/api/edit/<entity>/<entity_id>", methods=["PATCH"])
    @_perm.require_role("user")
    def api_edit_patch(entity: str, entity_id: str):
        if not _editors_enabled():
            return _disabled()

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({
                "error": "bad_request",
                "message": "Send a JSON object of the fields to change.",
            }), 400

        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            return jsonify({
                "error": "bad_request",
                "message": "Nothing to change: 'changes' must be a "
                           "non-empty object.",
            }), 400

        # Optional so a first-cut client can work without version tracking,
        # required in practice by E4.2 — which sends the version it
        # rendered, so a colleague's concurrent edit is a 409 rather than a
        # silent overwrite.
        raw_version = payload.get("row_version")
        expected_version = None
        if raw_version is not None:
            try:
                expected_version = int(raw_version)
            except (TypeError, ValueError):
                return jsonify({
                    "error": "bad_request",
                    "message": "row_version must be a whole number.",
                }), 400

        project_id = resolve_active_project(pin=False)
        actor = _perm.current_user_id()

        try:
            item = _editable.patch(entity, project_id, entity_id, changes,
                                   expected_version=expected_version,
                                   actor=actor)
        except _editable.UnknownEntity as exc:
            return jsonify({"error": "unknown_entity",
                            "message": str(exc)}), 404
        except _editable.FieldNotEditable as exc:
            # 400 and named, not ignored. A client that believes it saved a
            # field the server dropped is worse off than one told it failed.
            return jsonify({"error": "field_not_editable",
                            "message": str(exc),
                            "fields": exc.names}), 400
        except _editable.ValidationFailed as exc:
            return jsonify({"error": "validation_failed",
                            "message": str(exc),
                            "field": exc.field_name}), 400
        except _editable.AmbiguousEntity as exc:
            # Not a 409-and-reload: the id will still be duplicated after a
            # reload. Named so the message can say what is actually wrong.
            log.warning("ambiguous id %s %s: %s", entity, entity_id, exc)
            return jsonify({"error": "ambiguous_id",
                            "message": str(exc)}), 409
        except _editable.EntityNotFound:
            return jsonify({"error": "not_found",
                            "message": "No such item in this project."}), 404
        except _db.WriteConflict as exc:
            log.info("edit conflict on %s %s: %s", entity, entity_id, exc)
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed this item while you were "
                            "editing. Reload to see their version, then "
                            "make your change again."),
                "expected_version": exc.expected,
                "current_version": exc.actual,
            }), 409

        _invalidate(project_id)
        return jsonify({"entity": entity, "item": item,
                        "warnings": _house_style(entity, changes)})

    # ── Whole items: create and delete (E4.3) ─────────────────────
    #
    # Requirement 5 asks for new and deleted test cases, requirement 7 for
    # hand-written bugs. Both go through ``engine.editable``, so the field
    # allowlist, the audit row and the "this entity does not allow it"
    # refusal are declared once per entity rather than written twice.

    @app.route("/api/edit/<entity>", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_create(entity: str):
        if not _editors_enabled():
            return _disabled()

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "bad_request",
                            "message": "Send a JSON object."}), 400
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            return jsonify({
                "error": "bad_request",
                "message": "'values' must be an object of field values.",
            }), 400

        project_id = resolve_active_project(pin=False)
        actor = _perm.current_user_id()
        try:
            item = _editable.create(entity, project_id, values, actor=actor)
        except _editable.UnknownEntity as exc:
            return jsonify({"error": "unknown_entity",
                            "message": str(exc)}), 404
        except _editable.NotCreatable as exc:
            return jsonify({"error": "not_creatable",
                            "message": str(exc)}), 405
        except _editable.FieldNotEditable as exc:
            return jsonify({"error": "field_not_editable",
                            "message": str(exc),
                            "fields": exc.names}), 400
        except _editable.ValidationFailed as exc:
            return jsonify({"error": "validation_failed",
                            "message": str(exc),
                            "field": exc.field_name}), 400
        except _editable.EntityNotFound as exc:
            return jsonify({"error": "no_project",
                            "message": str(exc)}), 400

        _invalidate(project_id)
        return jsonify({"entity": entity, "item": item}), 201

    @app.route("/api/edit/<entity>/<entity_id>", methods=["DELETE"])
    @_perm.require_role("user")
    def api_edit_delete(entity: str, entity_id: str):
        if not _editors_enabled():
            return _disabled()

        # A DELETE carries no body in most clients, so the version comes from
        # the query string. Optional for the same reason it is optional on
        # PATCH, and sent by the editor for the same reason: deleting an item
        # somebody else just edited destroys work newer than what the
        # deleter was looking at.
        raw_version = request.args.get("row_version")
        expected_version = None
        if raw_version not in (None, ""):
            try:
                expected_version = int(raw_version)
            except (TypeError, ValueError):
                return jsonify({
                    "error": "bad_request",
                    "message": "row_version must be a whole number.",
                }), 400

        project_id = resolve_active_project(pin=False)
        actor = _perm.current_user_id()
        try:
            removed = _editable.remove(entity, project_id, entity_id,
                                       expected_version=expected_version,
                                       actor=actor)
        except _editable.UnknownEntity as exc:
            return jsonify({"error": "unknown_entity",
                            "message": str(exc)}), 404
        except _editable.NotDeletable as exc:
            return jsonify({"error": "not_deletable",
                            "message": str(exc)}), 405
        except _editable.AmbiguousEntity as exc:
            log.warning("ambiguous id %s %s: %s", entity, entity_id, exc)
            return jsonify({"error": "ambiguous_id",
                            "message": str(exc)}), 409
        except _editable.EntityNotFound:
            return jsonify({"error": "not_found",
                            "message": "No such item in this project."}), 404
        except _db.WriteConflict as exc:
            log.info("delete conflict on %s %s: %s", entity, entity_id, exc)
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed this item while you were "
                            "looking at it. Reload to see their version "
                            "before deleting it."),
                "expected_version": exc.expected,
                "current_version": exc.actual,
            }), 409

        _invalidate(project_id)
        return jsonify({"entity": entity, "deleted": removed.get("id")})

    # ── Test steps as a list (E4.3) ───────────────────────────────

    @app.route("/api/edit/test_case/<entity_id>/steps", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_test_case_steps(entity_id: str):
        """Add, edit, delete or reorder one step of one case.

        The whole point of doing this here rather than in the browser: the
        operation is applied to the row as it is *now*, under the same
        version check as any other edit. A client that rewrote the blob
        itself would be sending a value computed from what it last read, so
        two people reordering different steps would silently overwrite each
        other with a perfectly valid-looking list.
        """
        if not _editors_enabled():
            return _disabled()

        from engine import tc_steps

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "bad_request",
                            "message": "Send a JSON object."}), 400

        op = str(payload.get("op") or "")
        raw_version = payload.get("row_version")
        expected_version = None
        if raw_version is not None:
            try:
                expected_version = int(raw_version)
            except (TypeError, ValueError):
                return jsonify({
                    "error": "bad_request",
                    "message": "row_version must be a whole number.",
                }), 400

        project_id = resolve_active_project(pin=False)
        actor = _perm.current_user_id()

        try:
            current = _editable.get("test_case", project_id, entity_id)
        except _editable.AmbiguousEntity as exc:
            return jsonify({"error": "ambiguous_id",
                            "message": str(exc)}), 409
        if current is None:
            return jsonify({"error": "not_found",
                            "message": "No such test case in this "
                                       "project."}), 404
        # Checked before the operation is applied, so a stale reorder is
        # refused rather than computed and then discarded.
        if (expected_version is not None
                and expected_version != current["row_version"]):
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed these steps while you were "
                            "editing. Reload to see their version, then make "
                            "your change again."),
                "expected_version": expected_version,
                "current_version": current["row_version"],
            }), 409

        try:
            blob = tc_steps.apply(
                current.get("test_steps") or "", op,
                index=payload.get("index"),
                text=str(payload.get("text") or ""),
                delta=payload.get("delta") or 0)
        except tc_steps.StepError as exc:
            return jsonify({"error": "bad_step",
                            "message": str(exc)}), 400

        try:
            item = _editable.patch(
                "test_case", project_id, entity_id, {"test_steps": blob},
                expected_version=expected_version, actor=actor)
        except _editable.ValidationFailed as exc:
            return jsonify({"error": "validation_failed",
                            "message": str(exc),
                            "field": exc.field_name}), 400
        except _editable.EntityNotFound:
            return jsonify({"error": "not_found",
                            "message": "No such item in this project."}), 404
        except _db.WriteConflict as exc:
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed these steps while you were "
                            "editing. Reload, then make your change again."),
                "expected_version": exc.expected,
                "current_version": exc.actual,
            }), 409

        _invalidate(project_id)
        return jsonify({
            "entity": "test_case",
            "item": item,
            "steps": tc_steps.parse(item.get("test_steps")),
            "warnings": _house_style("test_case",
                                     {"test_steps": item.get("test_steps")}),
        })

    # ── Development harness ───────────────────────────────────────
    #
    # The smallest page that exercises the component against the real
    # endpoint: three fields of two kinds, sharing one row version. Behind
    # FLASK_DEBUG as well as EDITORS_ENABLED, so it cannot appear on a
    # deployed instance even if somebody flips the editing flag.
    if app.debug:
        @app.route("/_dev/inline-edit", methods=["GET"])
        @_perm.require_role("user")
        def dev_inline_edit_harness():
            from flask import render_template

            from engine import workspace as _workspace
            if not _editors_enabled():
                return _disabled()
            pid = resolve_active_project(pin=False)
            items = []
            for row in _workspace.test_cases(pid)[:5]:
                item = _editable.get("test_case", pid, row.get("id"))
                if item:
                    items.append(dict(row, **item))
            return render_template("_ie_harness.html", items=items)


__all__ = ["register"]
