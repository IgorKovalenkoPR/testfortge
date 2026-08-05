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

from sqlalchemy.exc import IntegrityError as _IntegrityError

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


class AmbiguousEntity(EditError):
    """Two rows in this project share the public id being addressed.

    Real, not hypothetical: the site-aware checklist generator emits
    duplicate ids (measured — an 82-item pack with ``CNT_001`` twice), and
    this substrate keys every entity on that id. Editing "the" item is then
    undefined, and picking either row would be a silent coin toss over
    somebody's test documentation. So it is refused and named, which is also
    what makes the underlying duplicate visible instead of latent.
    """


class NotCreatable(EditError):
    """This entity is edited in place only — nothing creates one here."""


class NotDeletable(EditError):
    """This entity is edited in place only — nothing deletes one here."""


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
    # ── Creating and deleting whole items (E4.3) ───────────────────
    #
    # Requirement 5 asks for new and deleted test cases and requirement 7
    # for hand-written bugs, which is the same pair of operations over a
    # different entity. Declaring them here means the second editor gets
    # them by setting two flags instead of by writing them again.
    creatable: bool = False
    deletable: bool = False
    # ── Per-field guards (E4.5) ────────────────────────────────────
    #
    # ``field → callable(old, new, has_role)`` that raises to refuse the
    # change. Validation answers "is this value the right shape"; a guard
    # answers "may this value follow that one, from this person" — which
    # needs the old value and the actor, and so cannot live in ``Field``.
    #
    # Declared on the entity rather than checked in the route, because a
    # rule enforced in one handler is a rule the next handler forgets. Every
    # caller of ``patch`` gets it.
    field_guards: dict = _dc_field(default_factory=dict)
    # ``TC-`` → TC-001, TC-002 … Only ids matching the prefix are counted,
    # so a project full of generator ids (SC1_004, REC_002) still starts a
    # hand-written case at TC-001.
    id_prefix: str = ""
    # Columns a new row needs that are not editable fields. Kept explicit
    # rather than relying on model defaults for anything a person would
    # notice, so a new item is not subtly different from a generated one.
    create_defaults: dict = _dc_field(default_factory=dict)

    def audit_name(self) -> str:
        return self.audit_entity or self.name


def _text(max_length: int, *, required: bool = False) -> Field:
    return Field(kind="text", max_length=max_length, required=required)


def _choice(choices, *, required: bool = False) -> Field:
    return Field(kind="choice", choices=tuple(choices), required=required)


def _bug_status_guard(old, new, has_role) -> None:
    """Refuse a status move the workflow does not allow (E4.5).

    Imported lazily so this module keeps loading without the bug vocabulary,
    and so a circular import cannot form: ``bug_workflow`` reads
    ``bug_report``, which knows nothing about editing.
    """
    from engine import bug_workflow
    bug_workflow.check(old, new, has_role=has_role)


def _registry() -> dict[str, Entity]:
    """Built lazily so importing this module does not import the vocabularies."""
    from engine.bug_report import (BUG_PRIORITIES, BUG_SEVERITIES,
                                   BUG_STATUSES)

    return {
        # ── Requirement 5: edit generated test cases ──
        "test_case": Entity(
            name="test_case", model_name="TestCase", id_column="external_id",
            creatable=True, deletable=True, id_prefix="TC-",
            # ``status`` matches what the generators write, so a new case
            # appears in the execution list in the same state as the rest.
            create_defaults={"status": "Unchecked", "priority": "Medium",
                             "testing_type": "Functional",
                             "category": "Positive", "section": "Manual",
                             "tc_format": "manual", "trigger": "manual"},
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
            # Unblocked by E4.4a: an item can only be addressed — created,
            # deleted or edited — once its public id identifies one row.
            creatable=True, deletable=True, id_prefix="CL-",
            create_defaults={"status": "Unchecked", "priority": "Medium",
                             "testing_type": "Functional",
                             "category": "Positive", "section": "Manual"},
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
            # Requirement 7's other half: a bug somebody files by hand. The
            # older ``POST /create-bug-report`` form stays — it is how a
            # tester files one mid-run — and this is the same act from the
            # editor, through the same allowlist and audit.
            #
            # Not deletable. Deleting a bug destroys evidence somebody
            # gathered, and ``routes/bugs.py`` already decided that question:
            # bulk delete is admin-only there. Offering a per-row delete here
            # to every member would quietly undo that.
            creatable=True, id_prefix="BUG-",
            create_defaults={"severity": "Minor", "priority": "Medium",
                             "status": "Open"},
            field_guards={"status": _bug_status_guard},
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


def _one(sess, config: Entity, project_id: str, entity_id: str):
    """The single row with this public id, or None. Raises if there are two.

    Every read and write goes through here so the duplicate-id guard cannot
    be present in two paths and missing from the third.
    """
    from sqlalchemy.exc import MultipleResultsFound

    model = _model(config)
    query = sess.query(model).filter(
        model.project_id == project_id,
        getattr(model, config.id_column) == entity_id,
    )
    try:
        return query.one_or_none()
    except MultipleResultsFound:
        raise AmbiguousEntity(
            f"{entity_id!r} identifies more than one {config.name} in this "
            f"project, so it cannot be edited. The duplicate ids have to be "
            f"resolved first."
        ) from None


def get(entity_name: str, project_id: str, entity_id: str) -> dict | None:
    """One row, scoped to its project. ``None`` when there is no such row."""
    from engine import db as _db
    config = entity(entity_name)
    if not (project_id and entity_id):
        return None
    with _db.session_scope() as sess:
        row = _one(sess, config, project_id, entity_id)
        return _row_to_public(config, row) if row is not None else None


# ── Writing ───────────────────────────────────────────────────────

def _default_has_role(role: str) -> bool:
    """Whether the current actor holds ``role``.

    Falls back to True outside a request: the CLI, the detached
    ``runner_worker`` and the migration scripts have no session to ask, and
    refusing there would break the automated paths in the name of a rule
    written for people using the UI.
    """
    try:
        from engine import permissions
        return bool(permissions.has_role(role))
    except Exception:      # pragma: no cover — no request context at all
        return True


def patch(entity_name: str, project_id: str, entity_id: str, changes: dict,
          *, expected_version: int | None = None,
          actor: str | None = None, has_role=None) -> dict:
    """Apply an edit and return the row as the client should now see it.

    Raises :class:`FieldNotEditable`, :class:`ValidationFailed`,
    :class:`EntityNotFound`, :class:`engine.db.WriteConflict`, or whatever a
    registered field guard raises (E4.5 — ``bug_workflow.TransitionRefused``).
    """
    from engine import db as _db

    config = entity(entity_name)
    if not project_id:
        raise EntityNotFound("no active project")
    values = validate(entity_name, changes)     # before touching the DB
    role_oracle = has_role or _default_has_role

    with _db.session_scope() as sess:
        row = _one(sess, config, project_id, entity_id)
        if row is None:
            raise EntityNotFound(
                f"no {entity_name} {entity_id!r} in this project")

        current = int(row.row_version or 1)
        if expected_version is not None and int(expected_version) != current:
            raise _db.WriteConflict(entity_name, int(expected_version),
                                    current)

        changed = {name: (getattr(row, name, None), new_value)
                   for name, new_value in values.items()
                   if getattr(row, name, None) != new_value}

        # Guards run as a complete pass before anything is applied — the same
        # contract ``validate`` has, so a patch is never half-refused. Only
        # for fields that actually change: re-posting the value a form
        # rendered is not a transition and must not be refused as one.
        for name, (old_value, new_value) in changed.items():
            guard = config.field_guards.get(name)
            if guard is not None:
                guard(old_value, new_value, role_oracle)

        diff = {}
        for name, (old_value, new_value) in changed.items():
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


@dataclass
class BulkResult:
    """What a bulk operation did. Counts, not a guess."""
    entity: str
    #: What happened to the ``changed`` rows, for the message — a delete
    #: reporting "1 updated" is a small lie that reads as a bug.
    verb: str = "updated"
    changed: list[str] = _dc_field(default_factory=list)
    unchanged: list[str] = _dc_field(default_factory=list)
    missing: list[str] = _dc_field(default_factory=list)
    refused: dict = _dc_field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.changed) + len(self.unchanged) + len(self.missing) \
            + len(self.refused)

    def message(self) -> str:
        parts = [f"{len(self.changed)} {self.verb}"]
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} already had that value")
        if self.missing:
            parts.append(f"{len(self.missing)} not found")
        if self.refused:
            parts.append(f"{len(self.refused)} refused")
        return ", ".join(parts) + "."


def patch_many(entity_name: str, project_id: str, entity_ids, changes: dict,
               *, actor: str | None = None, has_role=None) -> BulkResult:
    """Apply the same change to several rows — E4.9.

    Bug reports have had a bulk toolbar since Sprint 4; test cases and
    checklist items did not, and writing a second and third copy of it would
    be three field allowlists, three validators and three audit shapes. This
    is the substrate's version, so a bulk change goes through exactly what a
    single edit goes through:

    * the same allowlist and validation, run **once** — a bulk edit with a bad
      value is refused whole, before any row is touched;
    * the same per-field guards (E4.5), per row, because the answer can differ
      per row: closing five bugs is allowed for three of them and not for the
      two already closed;
    * the same provenance — every changed row becomes ``ai_generated=False``,
      so a later regeneration keeps it (E4.7).

    **One audit row for the operation**, not one per item: the person performed
    one action, and N rows would bury the individual edits that matter.

    No ``expected_version``. A bulk action is chosen from a list the user is
    looking at, not from one row's rendered value, so there is no single
    version to check — and refusing all twenty because one moved would be
    worse than the per-row report this returns.
    """
    from engine import db as _db

    config = entity(entity_name)
    if not project_id:
        raise EntityNotFound("no active project")
    ids = [str(i) for i in (entity_ids or []) if str(i or "").strip()]
    if not ids:
        raise ValidationFailed("ids", "Select at least one item.")
    values = validate(entity_name, changes)      # once, before any write
    role_oracle = has_role or _default_has_role

    result = BulkResult(entity=entity_name)
    model = _model(config)
    column = getattr(model, config.id_column)
    now = datetime.now(timezone.utc)

    with _db.session_scope() as sess:
        rows = {str(getattr(row, config.id_column)): row
                for row in sess.query(model).filter(
                    model.project_id == project_id, column.in_(ids)).all()}
        result.missing = [i for i in ids if i not in rows]

        for entity_id in ids:
            row = rows.get(entity_id)
            if row is None:
                continue
            changed_fields = {
                name: (getattr(row, name, None), value)
                for name, value in values.items()
                if getattr(row, name, None) != value
            }
            if not changed_fields:
                result.unchanged.append(entity_id)
                continue
            try:
                for name, (old_value, new_value) in changed_fields.items():
                    guard = config.field_guards.get(name)
                    if guard is not None:
                        guard(old_value, new_value, role_oracle)
            except Exception as exc:
                # Named and skipped, not fatal: a toolbar spanning rows in
                # different states is the normal case, and refusing the whole
                # operation because one row cannot move is not what the user
                # asked for.
                result.refused[entity_id] = str(exc)
                continue
            for name, (_, new_value) in changed_fields.items():
                setattr(row, name, new_value)
            row.row_version = int(row.row_version or 1) + 1
            row.ai_generated = False
            row.edited_by = actor or None
            row.edited_at = now
            result.changed.append(entity_id)

    if result.changed:
        _db.append_audit(
            entity=config.audit_name(), action="bulk_update",
            entity_id=f"{len(result.changed)} items", project_id=project_id,
            user_id=actor,
            diff={name: ["(various)", value] for name, value in values.items()}
            | {"items": [None, ", ".join(result.changed[:20])]})
    return result


def remove_many(entity_name: str, project_id: str, entity_ids,
                *, actor: str | None = None) -> BulkResult:
    """Delete several rows. Honours ``deletable`` — see :func:`remove`."""
    from engine import db as _db

    config = entity(entity_name)
    if not config.deletable:
        raise NotDeletable(f"{entity_name} cannot be deleted here.")
    if not project_id:
        raise EntityNotFound("no active project")
    ids = [str(i) for i in (entity_ids or []) if str(i or "").strip()]
    if not ids:
        raise ValidationFailed("ids", "Select at least one item.")

    result = BulkResult(entity=entity_name, verb="deleted")
    model = _model(config)
    column = getattr(model, config.id_column)
    with _db.session_scope() as sess:
        rows = sess.query(model).filter(
            model.project_id == project_id, column.in_(ids)).all()
        found = {str(getattr(row, config.id_column)) for row in rows}
        result.missing = [i for i in ids if i not in found]
        for row in rows:
            sess.delete(row)
        result.changed = sorted(found)

    if result.changed:
        _db.append_audit(
            entity=config.audit_name(), action="bulk_delete",
            entity_id=f"{len(result.changed)} items", project_id=project_id,
            user_id=actor,
            diff={"items": [", ".join(result.changed[:20]), None]})
        _bump_pack_version(config, project_id)
    return result


def _next_public_id(sess, config: Entity, project_id: str) -> str:
    """``TC-007`` — one past the highest number already using the prefix.

    Only ids matching the prefix are counted, so a project whose generated
    cases are called ``SC1_004`` still starts its first hand-written case at
    ``TC-001``.
    """
    import re as _re

    model = _model(config)
    column = getattr(model, config.id_column)
    pattern = _re.compile(
        rf"^{_re.escape(config.id_prefix)}(\d+)$")
    highest = 0
    for (value,) in sess.query(column).filter(
            model.project_id == project_id,
            column.like(f"{config.id_prefix}%")).all():
        match = pattern.match(str(value or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{config.id_prefix}{highest + 1:03d}"


def create(entity_name: str, project_id: str, values: dict | None = None,
           *, actor: str | None = None) -> dict:
    """Add one item by hand and return it as the client should see it.

    The new row is ``ai_generated=False`` from birth: nobody generated it, and
    E4.7's regeneration merge reads that flag to decide what it may overwrite.
    A hand-written case surviving a Generate click is the point.
    """
    from engine import db as _db

    config = entity(entity_name)
    if not config.creatable:
        raise NotCreatable(f"{entity_name} cannot be created by hand.")
    if not project_id:
        raise EntityNotFound("no active project")

    supplied = validate(entity_name, values or {})
    # Required fields that were not supplied become empty rather than
    # rejected: the flow is "add a row, then fill it in", and a modal that
    # refuses until every required field is typed is a worse version of the
    # editor that follows.
    row_values = dict(config.create_defaults)
    row_values.update(supplied)

    model = _model(config)
    attempts = 3
    while True:
        try:
            with _db.session_scope() as sess:
                public_id = _next_public_id(sess, config, project_id)
                row = model(project_id=project_id, **row_values)
                setattr(row, config.id_column, public_id)
                row.ai_generated = False
                row.row_version = 1
                row.edited_by = actor or None
                row.edited_at = datetime.now(timezone.utc)
                sess.add(row)
                sess.flush()
                result = _row_to_public(config, row)
            break
        except Exception as exc:                # noqa: BLE001 — see below
            # Two people pressing "add" at once mint the same number. With
            # the unique index in place that is an IntegrityError, and the
            # fix is simply to look again — the second attempt sees the
            # committed row and picks the next number. Bounded, so a
            # genuinely broken insert does not spin.
            attempts -= 1
            if attempts <= 0 or not isinstance(exc, _IntegrityError):
                raise
            log.info("id collision creating %s, retrying: %s",
                     entity_name, exc)

    _db.append_audit(entity=config.audit_name(), action="create",
                     entity_id=result["id"], project_id=project_id,
                     user_id=actor,
                     diff={k: [None, v] for k, v in row_values.items()})
    _bump_pack_version(config, project_id)
    return result


def remove(entity_name: str, project_id: str, entity_id: str,
           *, expected_version: int | None = None,
           actor: str | None = None) -> dict:
    """Delete one item. Returns what was deleted, for the audit and the UI.

    The version check applies here too: deleting an item somebody else has
    just edited destroys work that is newer than what the deleter was
    looking at, which is exactly the case optimistic locking exists for.
    """
    from engine import db as _db

    config = entity(entity_name)
    if not config.deletable:
        raise NotDeletable(f"{entity_name} cannot be deleted here.")
    if not project_id:
        raise EntityNotFound("no active project")

    with _db.session_scope() as sess:
        row = _one(sess, config, project_id, entity_id)
        if row is None:
            raise EntityNotFound(
                f"no {entity_name} {entity_id!r} in this project")
        current = int(row.row_version or 1)
        if expected_version is not None and int(expected_version) != current:
            raise _db.WriteConflict(entity_name, int(expected_version),
                                    current)
        removed = _row_to_public(config, row)
        sess.delete(row)

    _db.append_audit(entity=config.audit_name(), action="delete",
                     entity_id=entity_id, project_id=project_id,
                     user_id=actor,
                     diff={k: [v, None] for k, v in removed.items()
                           if k in config.fields and v})
    _bump_pack_version(config, project_id)
    return removed


def _bump_pack_version(config: Entity, project_id: str) -> None:
    """Tell pack-level writers that the pack changed underneath them.

    E3.5 guards wipe-and-replace saves with ``project.tc_version`` /
    ``cl_version``. Adding or deleting a row changes the pack, so a save
    that started before this must be refused rather than silently reinstate
    a deleted case or drop a new one. Editing a *field* deliberately does
    not bump it — that is what the per-row version is for.
    """
    from engine import db as _db

    kind = _PACK_COUNTERS.get(config.name)
    if not kind:
        return
    try:
        _db.bump_pack_version(project_id, kind)
    except Exception as exc:                    # pragma: no cover
        # A missed bump costs a conflict that should have been raised; it
        # must not cost the create or delete that already committed.
        log.warning("could not bump %s version for %s: %s",
                    kind, project_id, exc)


# Which pack counter each entity belongs to. Bugs have none: they are
# appended, never wipe-and-replaced, so there is no pack write to conflict
# with.
_PACK_COUNTERS = {
    "test_case": "test_cases",
    "checklist_item": "checklist",
}


__all__ = [
    "Entity", "Field",
    "AmbiguousEntity", "EditError", "UnknownEntity", "EntityNotFound",
    "FieldNotEditable", "NotCreatable", "NotDeletable", "ValidationFailed",
    "BulkResult",
    "entities", "entity", "editable_fields", "validate", "get", "patch",
    "create", "remove", "patch_many", "remove_many",
]
