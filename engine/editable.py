"""TestFortge — one way to edit a generated artefact (E4.1).

Requirements 4 to 7 all ask for the same thing in four places: let the user
fix what the generator got wrong, in the estimation, the test cases, the
checklist and the bug reports. Four bespoke editors would be four field
allowlists, four validators, four version checks and four audit calls — and
the one that forgot a piece would look exactly like the three that did not.

So there is one substrate and a registry. Adding an editable entity is a
:class:`Entity` declaration; it is not new code.

What a patch guarantees
-----------------------
* **Only declared fields change.** Anything else is a 400 naming the
  offending keys, not a silent no-op — a client that thinks it saved
  something is worse than one that knows it failed.
* **Values are validated and coerced** before they reach the database, so
  a closed vocabulary stays closed and a text column cannot be overrun.
* **The row is scoped to its project.** An id alone is never enough; a
  caller who guesses another team's id gets a 404.
* **Optimistic concurrency.** ``row_version`` must match, or the write is
  refused with the same :class:`engine.db.WriteConflict` the pack-level
  guard raises (E3.5) — one exception type, one 409 contract.
* **Provenance.** A human edit flips ``ai_generated`` to False, which is
  what stops the next Generate click from quietly reverting it (E4.7 is
  the merge policy that reads the flag).
* **An audit row**, with the before and after of every field that actually
  changed.

A no-op patch is deliberately not a write: it does not bump the version and
does not audit. Otherwise an editor that PATCHes on every keystroke would
fill the audit log with nothing and manufacture conflicts for a colleague.
"""
from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timezone
from typing import Any

from engine.log import get_logger

log = get_logger(__name__)


# ── Errors ────────────────────────────────────────────────────────

class EditError(RuntimeError):
    """Base for edit failures a route should turn into a 4xx."""


class UnknownEntity(EditError):
    """No such editable entity is registered."""


class EntityNotFound(EditError):
    """No such row in this project. Also the answer for another project's
    row: telling the two apart would confirm the id exists somewhere."""


class FieldNotEditable(EditError):
    """The patch named a field that is not in the allowlist.

    Carries the offending names so the message can say which, without
    echoing the values — a rejected payload may be anything.
    """

    def __init__(self, entity: str, names: list[str]):
        self.entity = entity
        self.names = sorted(names)
        super().__init__(
            f"{entity} has no editable field(s): {', '.join(self.names)}")


class ValidationFailed(EditError):
    """A value was the wrong shape. Message is written for the user."""

    def __init__(self, field_name: str, message: str):
        self.field_name = field_name
        super().__init__(message)


# ── The declaration ───────────────────────────────────────────────

@dataclass(frozen=True)
class Field:
    """One editable field and the rules for it.

    ``choices`` is only used where a closed vocabulary genuinely exists.
    Bug severity and priority have one; a test case's ``priority`` and
    ``category`` do not — those are free text the generators write, and
    inventing an enum here would reject data the product itself produces.
    """
    kind: str = "text"                       # text | choice | int | bool
    max_length: int | None = None
    required: bool = False                   # non-empty after stripping
    choices: tuple[str, ...] = ()
    min_value: int | None = None
    max_value: int | None = None
    label: str = ""                          # for the message, when set


@dataclass(frozen=True)
class Entity:
    """An editable artefact: where it lives and what may change."""
    name: str
    model_name: str                          # attribute on engine.db
    id_column: str                           # column carrying the public id
    fields: dict[str, Field]
    audit_entity: str = ""

    def audit_name(self) -> str:
        return self.audit_entity or self.name


def _text(max_length: int, *, required: bool = False) -> Field:
    return Field(kind="text", max_length=max_length, required=required)


def _choice(choices, *, required: bool = False) -> Field:
    return Field(kind="choice", choices=tuple(choices), required=required)


def _registry() -> dict[str, Entity]:
    """Built lazily so importing this module does not import the vocabularies."""
    from engine.bug_report import (BUG_PRIORITIES, BUG_SEVERITIES,
                                   BUG_STATUSES)

    return {
        # ── Requirement 5: edit generated test cases ──
        "test_case": Entity(
            name="test_case", model_name="TestCase", id_column="external_id",
            fields={
                "summary": _text(2000, required=True),
                "section": _text(200),
                "preconditions": _text(4000),
                "test_steps": _text(20000),
                "test_data": _text(4000),
                "expected_result": _text(8000, required=True),
                "issues": _text(2000),
                "comment": _text(4000),
                # Free text on purpose: the generators write their own
                # vocabulary here and a closed list would reject it.
                "priority": _text(20),
                "category": _text(60),
                "status": _text(20),
                "testing_type": _text(40),
                "suite": _text(20),
            },
        ),
        # ── Requirement 6: edit generated checklist items ──
        "checklist_item": Entity(
            name="checklist_item", model_name="ChecklistItem",
            id_column="external_id",
            fields={
                "objective": _text(2000, required=True),
                "section": _text(200),
                "comments": _text(4000),
                "priority": _text(20),
                "category": _text(60),
                "status": _text(20),
                "testing_type": _text(40),
                "item_num": _text(40),
            },
        ),
        # ── Requirement 7: edit bug reports ──
        "bug": Entity(
            name="bug", model_name="BugReport", id_column="external_id",
            audit_entity="bug",
            fields={
                "title": _text(500, required=True),
                # Here a closed vocabulary is real, and enforcing it is what
                # keeps severity filters and metrics meaningful.
                "severity": _choice(BUG_SEVERITIES, required=True),
                "priority": _choice(BUG_PRIORITIES, required=True),
                "status": _choice(BUG_STATUSES, required=True),
                "environment": _text(200),
                "browser": _text(120),
                "os": _text(120),
                "version": _text(60),
                "preconditions": _text(4000),
                "steps_to_reproduce": _text(20000, required=True),
                "actual_result": _text(8000, required=True),
                "expected_result": _text(8000, required=True),
                "comment": _text(8000),
                "assignee": _text(120),
                "bug_area": _text(20),
            },
        ),
    }


_ENTITIES: dict[str, Entity] | None = None


def entities() -> dict[str, Entity]:
    global _ENTITIES
    if _ENTITIES is None:
        _ENTITIES = _registry()
    return _ENTITIES


def entity(name: str) -> Entity:
    try:
        return entities()[name]
    except KeyError:
        raise UnknownEntity(
            f"{name!r} is not editable. Registered: "
            f"{', '.join(sorted(entities()))}."
        ) from None


def editable_fields(name: str) -> list[str]:
    """Field names a client may send, for a form or an API discovery call."""
    return sorted(entity(name).fields)


# ── Validation ────────────────────────────────────────────────────

def _coerce(entity_name: str, field_name: str, spec: Field,
            value: Any) -> Any:
    label = spec.label or field_name.replace("_", " ")

    if spec.kind == "bool":
        return bool(value)

    if spec.kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValidationFailed(
                field_name, f"{label.capitalize()} must be a whole number.")
        if spec.min_value is not None and number < spec.min_value:
            raise ValidationFailed(
                field_name,
                f"{label.capitalize()} cannot be below {spec.min_value}.")
        if spec.max_value is not None and number > spec.max_value:
            raise ValidationFailed(
                field_name,
                f"{label.capitalize()} cannot be above {spec.max_value}.")
        return number

    # Everything else arrives as text. ``None`` is treated as clearing the
    # field rather than as an error: a form that omits an optional input
    # sends an empty value, and refusing that would make optional fields
    # unclearable.
    text = "" if value is None else str(value)
    text = text.strip()

    if spec.required and not text:
        raise ValidationFailed(
            field_name, f"{label.capitalize()} cannot be empty.")

    if spec.kind == "choice":
        if not text and not spec.required:
            return ""
        if text not in spec.choices:
            raise ValidationFailed(
                field_name,
                f"{label.capitalize()} must be one of: "
                f"{', '.join(spec.choices)}.")
        return text

    if spec.max_length is not None and len(text) > spec.max_length:
        # Refused, not truncated. Silently shortening someone's test steps
        # is a data-loss bug that presents as a formatting quirk.
        raise ValidationFailed(
            field_name,
            f"{label.capitalize()} is too long "
            f"({len(text)} characters; the limit is {spec.max_length}).")
    return text


def validate(entity_name: str, changes: dict) -> dict:
    """Coerce a patch, or raise. Returns the values ready to apply.

    Rejects the whole patch when any field is wrong, rather than applying
    the valid half: a partly-applied edit is the hardest kind to notice.
    """
    config = entity(entity_name)
    unknown = [k for k in changes if k not in config.fields]
    if unknown:
        raise FieldNotEditable(entity_name, unknown)
    return {name: _coerce(entity_name, name, config.fields[name], value)
            for name, value in changes.items()}


# ── Reading ───────────────────────────────────────────────────────

def _model(config: Entity):
    from engine import db as _db
    return getattr(_db, config.model_name)


def _row_to_public(config: Entity, row) -> dict:
    """The row as a client sees it: declared fields plus edit metadata."""
    from engine import db as _db
    full = _db._row_to_dict(row)
    out = {name: full.get(name) for name in config.fields}
    out["id"] = full.get(config.id_column)
    out["row_version"] = int(full.get("row_version") or 1)
    out["ai_generated"] = bool(full.get("ai_generated"))
    out["edited_by"] = full.get("edited_by")
    out["edited_at"] = full.get("edited_at")
    return out


def get(entity_name: str, project_id: str, entity_id: str) -> dict | None:
    """One row, scoped to its project. ``None`` when there is no such row."""
    from engine import db as _db
    config = entity(entity_name)
    if not (project_id and entity_id):
        return None
    model = _model(config)
    with _db.session_scope() as sess:
        row = sess.query(model).filter(
            model.project_id == project_id,
            getattr(model, config.id_column) == entity_id,
        ).one_or_none()
        return _row_to_public(config, row) if row is not None else None


# ── Writing ───────────────────────────────────────────────────────

def patch(entity_name: str, project_id: str, entity_id: str, changes: dict,
          *, expected_version: int | None = None,
          actor: str | None = None) -> dict:
    """Apply an edit and return the row as the client should now see it.

    Raises :class:`FieldNotEditable`, :class:`ValidationFailed`,
    :class:`EntityNotFound` or :class:`engine.db.WriteConflict`.
    """
    from engine import db as _db

    config = entity(entity_name)
    if not project_id:
        raise EntityNotFound("no active project")
    values = validate(entity_name, changes)     # before touching the DB

    model = _model(config)
    with _db.session_scope() as sess:
        row = sess.query(model).filter(
            model.project_id == project_id,
            getattr(model, config.id_column) == entity_id,
        ).one_or_none()
        if row is None:
            raise EntityNotFound(
                f"no {entity_name} {entity_id!r} in this project")

        current = int(row.row_version or 1)
        if expected_version is not None and int(expected_version) != current:
            raise _db.WriteConflict(entity_name, int(expected_version),
                                    current)

        diff = {}
        for name, new_value in values.items():
            old_value = getattr(row, name, None)
            if old_value == new_value:
                continue
            diff[name] = [old_value, new_value]
            setattr(row, name, new_value)

        if not diff:
            # A no-op is not a write: bumping the version here would
            # manufacture a conflict for a colleague, and auditing it would
            # bury the real edits.
            return _row_to_public(config, row)

        row.row_version = current + 1
        row.ai_generated = False
        row.edited_by = actor or None
        row.edited_at = datetime.now(timezone.utc)
        result = _row_to_public(config, row)

    # Outside the transaction: an audit failure must not roll back the edit
    # it is describing. ``append_audit`` swallows its own errors.
    _db.append_audit(entity=config.audit_name(), action="update",
                     entity_id=entity_id, project_id=project_id,
                     user_id=actor, diff=diff)
    return result


__all__ = [
    "Entity", "Field",
    "EditError", "UnknownEntity", "EntityNotFound", "FieldNotEditable",
    "ValidationFailed",
    "entities", "entity", "editable_fields", "validate", "get", "patch",
]
