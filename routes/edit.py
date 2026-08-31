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

from engine import bug_workflow as _bug_workflow
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


def _checklist_pack(project_id: str):
    """The stored pack and its version, for a read-modify-write.

    The version is read here and handed to the write, so a concurrent change
    between the two is refused (E3.5) instead of silently overwritten.
    """
    from engine import db as _db
    from engine import workspace as _workspace
    items = list(_workspace.checklist(project_id) or [])
    version = _db.pack_versions(project_id).get("checklist")
    return items, version


def _write_checklist_pack(project_id: str, items, expected_version):
    """Store a reordered pack. Provenance survives; the pack version bumps.

    ``save_checklist`` deletes and re-inserts, which is what makes
    ``ORDER BY id`` agree with the new order, and it carries each row's
    ``row_version``/``ai_generated`` across on the public id.
    """
    from engine import workspace as _workspace
    _workspace.save_checklist(project_id, items,
                              expected_version=expected_version)
    _invalidate(project_id)


def _number_new_checklist_item(project_id: str, item: dict,
                               actor: str | None) -> dict:
    """Give a hand-added item the next free number in its section (E4.4).

    ``editable.create`` inserts at the end of the pack, which is where an
    appended item belongs — but it knows nothing about ``item_num``. The
    number is assigned here, by the append rule the house style requires:
    one past the highest sibling, siblings untouched.
    """
    from engine import checklist_order as _order
    try:
        items, version = _checklist_pack(project_id)
        others = [row for row in items if row.get("id") != item.get("id")]
        number = _order.next_number(others, item.get("section") or "")
        numbered = _editable.patch("checklist_item", project_id, item["id"],
                                   {"item_num": number}, actor=actor)
        # And into its section's block. ``editable.create`` appends to the end
        # of the *pack*, which is the end of whichever section happens to be
        # last — so adding an item to "Header" while "Page footer" was last
        # rendered a second "Header" heading at the bottom of the table.
        # Measured in the browser; the number alone was not enough.
        items, version = _checklist_pack(project_id)
        _write_checklist_pack(project_id,
                              _order.regroup_item(items, item["id"]), version)
        return numbered
    except Exception as exc:      # pragma: no cover — cosmetic if it fails
        log.warning("could not number new checklist item %s: %s",
                    item.get("id"), exc)
        return item


def _regroup_after_section_change(project_id: str, entity_id: str) -> None:
    """Move a relocated item into its new section's block, and renumber it.

    The editor uses the dedicated endpoint, but the generic PATCH accepts
    ``section`` too — and an item left where it was in the pack makes the page
    render a second heading for the same section further down.
    """
    from engine import checklist_order as _order
    try:
        items, version = _checklist_pack(project_id)
        if not any(row.get("id") == entity_id for row in items):
            return
        _write_checklist_pack(project_id,
                              _order.regroup_item(items, entity_id), version)
    except Exception as exc:      # pragma: no cover
        log.warning("could not regroup checklist after %s changed section: %s",
                    entity_id, exc)


def _regroup_after_bulk_section_change(project_id: str, entity_ids) -> None:
    """Put every relocated item in its block, and renumber, in one write."""
    from engine import checklist_order as _order
    if not entity_ids:
        return
    try:
        items, version = _checklist_pack(project_id)
        present = {str(row.get("id")) for row in items}
        for entity_id in entity_ids:
            if str(entity_id) in present:
                items = _order.regroup_item(items, str(entity_id))
        _write_checklist_pack(project_id, items, version)
    except Exception as exc:      # pragma: no cover
        log.warning("could not regroup checklist after a bulk section "
                    "change: %s", exc)


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
        except _bug_workflow.TransitionRefused as exc:
            # 403 when it is about who you are, 400 when it is about the move.
            # A client that showed "try again" for the first would be lying;
            # the answer there is "ask an admin".
            code = 403 if exc.reason == "needs_role" else 400
            return jsonify({"error": "transition_refused",
                            "reason": exc.reason,
                            "field": "status",
                            "message": str(exc)}), code
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
        if entity == "checklist_item" and "section" in changes:
            _regroup_after_section_change(project_id, entity_id)
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
        except _bug_workflow.TransitionRefused as exc:
            code = 403 if exc.reason == "needs_role" else 400
            return jsonify({"error": "transition_refused",
                            "reason": exc.reason,
                            "message": str(exc)}), code
        except _editable.EntityNotFound as exc:
            return jsonify({"error": "no_project",
                            "message": str(exc)}), 400

        _invalidate(project_id)
        if entity == "checklist_item":
            item = _number_new_checklist_item(project_id, item, actor)
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

    # ── Bulk operations (E4.9) ────────────────────────────────────
    #
    # Bug reports have had a toolbar since Sprint 4; test cases and checklist
    # items did not. One endpoint for every entity rather than a third and
    # fourth copy of it — the substrate already owns the allowlist, the
    # guards, the provenance and the audit shape.

    def _bulk_payload():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return None, jsonify({"error": "bad_request",
                                  "message": "Send a JSON object."}), 400
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return None, jsonify({
                "error": "bad_request",
                "message": "Select at least one item.",
            }), 400
        return payload, None, None

    @app.route("/api/edit/<entity>/bulk", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_bulk(entity: str):
        if not _editors_enabled():
            return _disabled()
        payload, error, code = _bulk_payload()
        if error is not None:
            return error, code
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            return jsonify({"error": "bad_request",
                            "message": "Nothing to change."}), 400

        project_id = resolve_active_project(pin=False)
        actor = _perm.current_user_id()
        try:
            result = _editable.patch_many(entity, project_id, payload["ids"],
                                          changes, actor=actor)
        except _editable.UnknownEntity as exc:
            return jsonify({"error": "unknown_entity",
                            "message": str(exc)}), 404
        except _editable.FieldNotEditable as exc:
            return jsonify({"error": "field_not_editable",
                            "message": str(exc), "fields": exc.names}), 400
        except _editable.ValidationFailed as exc:
            return jsonify({"error": "validation_failed",
                            "message": str(exc),
                            "field": exc.field_name}), 400
        except _editable.EntityNotFound as exc:
            return jsonify({"error": "no_project",
                            "message": str(exc)}), 400

        if entity == "checklist_item" and "section" in changes:
            # Same invariant the single-row PATCH restores, and the bulk path
            # broke it: measured in the browser, moving three items into one
            # section left the third numbered 2.1 inside section 1. Done once
            # over the pack rather than per row — twenty rewrites for one
            # action is a lot of writes to reach the same answer.
            _regroup_after_bulk_section_change(project_id, result.changed)

        _invalidate(project_id)
        return jsonify({
            "entity": entity,
            "changed": result.changed, "unchanged": result.unchanged,
            "missing": result.missing, "refused": result.refused,
            "message": result.message(),
        })

    @app.route("/api/edit/<entity>/bulk-delete", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_bulk_delete(entity: str):
        """A separate endpoint, not an ``action`` on the one above.

        Deleting is the operation nobody should reach by mis-typing a field
        name, and keeping it on its own URL means a future role gate has
        something to attach to — which is how ``routes/bugs.py`` ended up
        gating its bulk delete to admins.
        """
        if not _editors_enabled():
            return _disabled()
        payload, error, code = _bulk_payload()
        if error is not None:
            return error, code

        project_id = resolve_active_project(pin=False)
        try:
            result = _editable.remove_many(entity, project_id, payload["ids"],
                                           actor=_perm.current_user_id())
        except _editable.UnknownEntity as exc:
            return jsonify({"error": "unknown_entity",
                            "message": str(exc)}), 404
        except _editable.NotDeletable as exc:
            return jsonify({"error": "not_deletable",
                            "message": str(exc)}), 405
        except _editable.ValidationFailed as exc:
            return jsonify({"error": "validation_failed",
                            "message": str(exc)}), 400
        except _editable.EntityNotFound as exc:
            # "No active project" — the same answer the other three
            # endpoints on this surface give. This one had no clause for it
            # and raised out of the handler, so the toolbar's Delete
            # answered 500 where its Edit answered 400, for a caller who had
            # merely cleared their session.
            return jsonify({"error": "no_project",
                            "message": str(exc)}), 400

        _invalidate(project_id)
        return jsonify({"entity": entity, "deleted": result.changed,
                        "missing": result.missing,
                        "message": result.message()})

    # ── Checklist order and sections (E4.4) ───────────────────────
    #
    # Three operations that a field patch cannot express, because order is
    # row order and ``item_num`` describes a position. All three are pack
    # read-modify-writes under the pack version, so a colleague's concurrent
    # change is a 409 rather than a silent overwrite. What renumbers and what
    # deliberately does not is set by the measured house style — see
    # engine/checklist_order.py.

    def _checklist_op(entity_id: str, mutate, *, success):
        """Shared shell: load the pack, apply ``mutate``, store it."""
        from engine import checklist_order as _order
        from engine import db as _db

        if not _editors_enabled():
            return _disabled()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "bad_request",
                            "message": "Send a JSON object."}), 400

        project_id = resolve_active_project(pin=False)
        if not project_id:
            return jsonify({"error": "no_project",
                            "message": "No active project."}), 400

        items, version = _checklist_pack(project_id)
        try:
            updated = mutate(items, payload)
        except _order.OrderError as exc:
            return jsonify({"error": "bad_order",
                            "message": str(exc)}), 400

        try:
            _write_checklist_pack(project_id, updated, version)
        except _db.WriteConflict as exc:
            log.info("checklist order conflict: %s", exc)
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed this checklist while you "
                            "were editing. Reload to see their version, then "
                            "make your change again."),
                "expected_version": exc.expected,
                "current_version": exc.actual,
            }), 409

        stored = list(_checklist_pack(project_id)[0])
        _db.append_audit(entity="checklist_item", action="reorder",
                         entity_id=entity_id, project_id=project_id,
                         user_id=_perm.current_user_id(),
                         diff=success(payload))
        return jsonify({
            "entity": "checklist_item",
            "items": [{"id": row.get("id"), "item_num": row.get("item_num"),
                       "section": row.get("section")} for row in stored],
        })

    @app.route("/api/edit/checklist_item/<entity_id>/move", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_checklist_move(entity_id: str):
        """Move one item up or down within its section."""
        from engine import checklist_order as _order
        return _checklist_op(
            entity_id,
            lambda items, payload: _order.move(items, entity_id,
                                               payload.get("delta") or 0),
            success=lambda payload: {"move": [entity_id,
                                              payload.get("delta")]})

    @app.route("/api/edit/checklist_item/<entity_id>/section",
               methods=["POST"])
    @_perm.require_role("user")
    def api_edit_checklist_relocate(entity_id: str):
        """Move one item into another section, appended at its end."""
        from engine import checklist_order as _order
        return _checklist_op(
            entity_id,
            lambda items, payload: _order.relocate(
                items, entity_id, str(payload.get("section") or "")),
            success=lambda payload: {"section": [None,
                                                 payload.get("section")]})

    @app.route("/api/edit/checklist/rename-section", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_checklist_rename_section():
        """Rename a section across every item in it.

        One audit row for the whole operation rather than one per item: the
        person performed one action, and N rows would bury the edits that
        matter. Nothing is renumbered — see engine/checklist_order.py for why
        a rename that would merge two sections is refused instead.
        """
        def _mutate(items, payload):
            from engine import checklist_order as _order
            _order.rename_section(items, str(payload.get("from") or ""),
                                  str(payload.get("to") or ""))
            return items

        return _checklist_op(
            "*", _mutate,
            success=lambda payload: {"section": [payload.get("from"),
                                                 payload.get("to")]})

    # ── Estimation (E4.6) ─────────────────────────────────────────
    #
    # Not part of the generic ``/api/edit/<entity>`` surface, because the
    # thing being edited is not a row of columns: it is one JSON payload
    # holding a computed structure. What it shares is the contract — an
    # allowlist, a whole-or-nothing edit, ``row_version`` → 409, provenance,
    # one audit row. See engine/estimation_edit.py for why the phase hours
    # are recomputed rather than typed.

    def _estimation_state(payload):
        raw_version = (payload or {}).get("row_version")
        if raw_version is None:
            return None, None
        try:
            return int(raw_version), None
        except (TypeError, ValueError):
            return None, jsonify({
                "error": "bad_request",
                "message": "row_version must be a whole number.",
            })

    @app.route("/api/edit/estimation", methods=["GET"])
    @_perm.require_role("user")
    def api_edit_estimation_get():
        if not _editors_enabled():
            return _disabled()
        from engine import estimation_edit as _est
        state = _est.get(resolve_active_project(pin=False))
        if state is None:
            return jsonify({"error": "not_found",
                            "message": "No estimation for this project "
                                       "yet."}), 404
        return jsonify({"entity": "estimation", "item": state,
                        "inputs": {name: spec.label
                                   for name, spec in _est.INPUTS.items()}})

    @app.route("/api/edit/estimation", methods=["PATCH"])
    @_perm.require_role("user")
    def api_edit_estimation_patch():
        """Change inputs; every total is recomputed here.

        The client sends drivers only. A derived value in the payload is a
        400 naming it — not a silent drop, because a caller that believes it
        set the total is worse off than one told it cannot.
        """
        if not _editors_enabled():
            return _disabled()
        from engine import estimation_edit as _est

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "bad_request",
                            "message": "Send a JSON object."}), 400
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            return jsonify({
                "error": "bad_request",
                "message": "Nothing to change: 'changes' must be a "
                           "non-empty object.",
            }), 400
        version, error = _estimation_state(payload)
        if error is not None:
            return error, 400

        project_id = resolve_active_project(pin=False)
        try:
            state = _est.apply(project_id, changes, expected_version=version,
                               actor=_perm.current_user_id())
        except _est.EstimationEditError as exc:
            return jsonify({"error": "bad_input", "field": exc.field_name,
                            "message": str(exc)}), 400
        except _db.WriteConflict as exc:
            log.info("estimation conflict: %s", exc)
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed this estimation while you "
                            "were editing. Reload to see their version, then "
                            "make your change again."),
                "expected_version": exc.expected,
                "current_version": exc.actual,
            }), 409

        _invalidate(project_id)
        return jsonify({"entity": "estimation", "item": state})

    @app.route("/api/edit/estimation/revert", methods=["POST"])
    @_perm.require_role("user")
    def api_edit_estimation_revert():
        """Put the generator's numbers back."""
        if not _editors_enabled():
            return _disabled()
        from engine import estimation_edit as _est

        payload = request.get_json(silent=True) or {}
        version, error = _estimation_state(payload)
        if error is not None:
            return error, 400

        project_id = resolve_active_project(pin=False)
        try:
            state = _est.revert(project_id, expected_version=version,
                                actor=_perm.current_user_id())
        except _est.EstimationEditError as exc:
            return jsonify({"error": "bad_input",
                            "message": str(exc)}), 400
        except _db.WriteConflict as exc:
            return jsonify({
                "error": "conflict",
                "message": ("Someone else changed this estimation. Reload "
                            "before reverting it."),
                "expected_version": exc.expected,
                "current_version": exc.actual,
            }), 409

        _invalidate(project_id)
        return jsonify({"entity": "estimation", "item": state})

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
