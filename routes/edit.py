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
        row = _editable.get(entity, project_id, entity_id)
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

        # The invalidation the repository's write helpers do for themselves;
        # this path writes through engine.editable, which does not know
        # about the per-request cache.
        try:
            from engine import workspace as _workspace
            _workspace.invalidate(project_id)
        except Exception as exc:      # pragma: no cover — cache only
            log.debug("workspace invalidate after edit skipped: %s", exc)

        return jsonify({"entity": entity, "item": item})


__all__ = ["register"]
