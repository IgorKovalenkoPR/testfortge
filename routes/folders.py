"""TestFortge — folders for test cases and checklist items.

  * GET    /api/folders                 — the tree and who is filed where
  * POST   /api/folders                 — create   {name, parent_id?}
  * PATCH  /api/folders/<id>            — rename   {name}
                                          or move  {parent_id, position?}
  * DELETE /api/folders/<id>            — contents move up to the parent
  * POST   /api/folders/assign          — file items {kind, ids, folder_id}
  * POST   /api/folders/refile          — file every item of {kind} by its
                                          "[Folder -> Sub]" summary prefix

One tree per project, shared by both kinds of item, so "Pricing page" holds
that page's cases and its checks (owner decision, 2026-10-19).

Writes sit behind ``EDITORS_ENABLED`` like every other change to shared pack
data: a folder move on a session-first workspace would edit a private copy
(ADR 0001). Reading the tree is not gated, so a read-only page still groups.

CSRF applies to every write — the page sends the token through
``editor-shared.js`` like the inline editors do.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from engine import db as _db
from engine import features as _features
from engine import permissions as _perm
from engine.log import get_logger

from ._shared import resolve_active_project

log = get_logger(__name__)

_KINDS = ("test_case", "checklist_item")


def _editors_enabled() -> bool:
    return _features.effective("EDITORS_ENABLED")


def _disabled():
    return jsonify({
        "error": "editors_disabled",
        "message": "Editing is not enabled on this instance yet.",
    }), 404


def _bad(message: str, code: int = 400):
    return jsonify({"error": "bad_request", "message": message}), code


def _invalidate(project_id: str) -> None:
    try:
        from engine import workspace as _workspace
        _workspace.invalidate(project_id)
    except Exception as exc:          # pragma: no cover — cache only
        log.debug("workspace invalidate after folder write skipped: %s", exc)


def _optional_id(value):
    """``None`` / "" → None (the root / Unfiled); anything else an int."""
    if value in (None, "", "root", "unfiled"):
        return None
    if isinstance(value, bool):
        raise ValueError(value)
    return int(value)


def snapshot(project_id: str) -> dict:
    """The tree plus both kinds' assignments — what the page renders."""
    return {
        "folders": _db.list_folders(project_id),
        "assignments": {kind: _db.load_folder_assignments(project_id, kind)
                        for kind in _KINDS},
    }


def register(app: Flask) -> None:

    def _write_shell():
        """Common preamble for every write: flag, JSON body, project."""
        if not _editors_enabled():
            return None, None, _disabled()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return None, None, _bad("Send a JSON object.")
        project_id = resolve_active_project(pin=False)
        if not project_id:
            return None, None, (jsonify({"error": "no_project",
                                         "message": "No active project."}),
                                400)
        return payload, project_id, None

    def _audit(action: str, project_id: str, entity_id, diff: dict) -> None:
        _db.append_audit(entity="folder", action=action,
                         entity_id=str(entity_id) if entity_id else None,
                         project_id=project_id,
                         user_id=_perm.current_user_id(), diff=diff)

    @app.route("/api/folders", methods=["GET"])
    @_perm.require_role("user")
    def api_folders_list():
        project_id = resolve_active_project(pin=False)
        if not project_id:
            return jsonify({"folders": [], "assignments": {
                kind: {} for kind in _KINDS}})
        return jsonify(snapshot(project_id))

    @app.route("/api/folders", methods=["POST"])
    @_perm.require_role("user")
    def api_folders_create():
        payload, project_id, error = _write_shell()
        if error is not None:
            return error
        try:
            parent_id = _optional_id(payload.get("parent_id"))
        except (TypeError, ValueError):
            return _bad("parent_id must be a folder id.")
        try:
            folder = _db.create_folder(project_id, payload.get("name"),
                                       parent_id)
        except _db.FolderError as exc:
            return _bad(str(exc))
        _audit("create", project_id, folder["id"],
               {"name": [None, folder["name"]],
                "parent_id": [None, parent_id]})
        return jsonify({"folder": folder}), 201

    @app.route("/api/folders/<int:folder_id>", methods=["PATCH"])
    @_perm.require_role("user")
    def api_folders_update(folder_id: int):
        payload, project_id, error = _write_shell()
        if error is not None:
            return error
        try:
            if "name" in payload:
                folder = _db.rename_folder(project_id, folder_id,
                                           payload.get("name"))
                _audit("rename", project_id, folder_id,
                       {"name": [None, folder["name"]]})
            elif "parent_id" in payload:
                try:
                    parent_id = _optional_id(payload.get("parent_id"))
                    position = payload.get("position")
                    position = None if position in (None, "") else int(position)
                except (TypeError, ValueError):
                    return _bad("parent_id and position must be numbers.")
                folder = _db.move_folder(project_id, folder_id, parent_id,
                                         position)
                _audit("move", project_id, folder_id,
                       {"parent_id": [None, parent_id]})
            else:
                return _bad("Send a new 'name' or a new 'parent_id'.")
        except _db.FolderError as exc:
            message = str(exc)
            return _bad(message, 404 if message == "No such folder." else 400)
        _invalidate(project_id)
        return jsonify({"folder": folder})

    @app.route("/api/folders/<int:folder_id>", methods=["DELETE"])
    @_perm.require_role("user")
    def api_folders_delete(folder_id: int):
        if not _editors_enabled():
            return _disabled()
        project_id = resolve_active_project(pin=False)
        if not project_id:
            return jsonify({"error": "no_project",
                            "message": "No active project."}), 400
        try:
            result = _db.delete_folder(project_id, folder_id)
        except _db.FolderError as exc:
            return _bad(str(exc), 404)
        _audit("delete", project_id, folder_id, {"moved": [None, result]})
        _invalidate(project_id)
        return jsonify(result)

    @app.route("/api/folders/assign", methods=["POST"])
    @_perm.require_role("user")
    def api_folders_assign():
        payload, project_id, error = _write_shell()
        if error is not None:
            return error
        kind = payload.get("kind")
        if kind not in _KINDS:
            return _bad(f"kind must be one of: {', '.join(_KINDS)}.")
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return _bad("ids must be a list.")
        try:
            folder_id = _optional_id(payload.get("folder_id"))
        except (TypeError, ValueError):
            return _bad("folder_id must be a folder id.")
        try:
            result = _db.assign_to_folder(project_id, kind, ids, folder_id,
                                          actor=_perm.current_user_id())
        except _db.FolderError as exc:
            return _bad(str(exc))
        if result["changed"]:
            _audit("assign", project_id, folder_id,
                   {"kind": [None, kind], "ids": [None, result["changed"]]})
        _invalidate(project_id)
        return jsonify(result)

    @app.route("/api/folders/refile", methods=["POST"])
    @_perm.require_role("user")
    def api_folders_refile():
        """Apply the ``[Folder -> Sub]`` prefix rule to stored items.

        An explicit action, so it files every prefixed item — including one
        moved by hand — where its prefix says. Unprefixed items stay put.
        """
        payload, project_id, error = _write_shell()
        if error is not None:
            return error
        kind = payload.get("kind")
        if kind not in _KINDS:
            return _bad(f"kind must be one of: {', '.join(_KINDS)}.")
        try:
            result = _db.refile_from_prefixes(project_id, kind)
        except _db.FolderError as exc:
            return _bad(str(exc))
        if result["changed"]:
            _audit("refile", project_id, None,
                   {"kind": [None, kind],
                    "ids": [None, result["changed"][:50]]})
        _invalidate(project_id)
        return jsonify({**result, **snapshot(project_id)})
