"""TestFortge — editing an estimation, which means editing its inputs (E4.6).

Requirement 4 asks for the generated estimation to be editable. E4.1 left it
out of ``engine.editable`` on purpose: that substrate edits *columns*, and an
estimation is one JSON payload holding a computed structure — features, seven
phase rows, two platform scenarios, costs, PERT bands, a Brooks's-Law penalty.

The shape of the fix follows from one fact about the estimator:
:func:`engine.qa_estimator.compute_estimation` takes every coefficient as a
parameter and returns the whole result. **The estimate is a pure function of
its inputs.** So editing it means changing an input and calling that function
again — the same function that produced the numbers in the first place.

Why the phase hours are not directly editable
---------------------------------------------
The plan asks for a "grid of phases / hours / coefficients". The grid is here
and the coefficients are the knobs; the hours in it are recomputed, not typed.
Letting somebody overwrite a phase's hours would mean storing an override
layer, and then the estimate no longer follows from its inputs — you get a
number that cannot be re-derived or defended, which is the one thing an
estimate exists to be. Change the driver (test cases, minutes per case, the
stretch, PM overhead) and every dependent number moves with it.

That also delivers the acceptance criterion for free: the totals in the UI
equal the totals in the database because there is only one computation, on the
server, and the client sends no derived value at all. A client that posted
``total_hours`` would be ignored — see :data:`INPUTS`, which is the allowlist.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from engine.log import get_logger

log = get_logger(__name__)

MAX_FEATURES = 400
MAX_FEATURE_NAME = 200


class EstimationEditError(ValueError):
    """An edit that cannot be applied. Message is written for the user."""

    def __init__(self, message: str, *, field: str = ""):
        self.field_name = field
        super().__init__(message)


@dataclass(frozen=True)
class Input:
    """One editable driver of the estimate.

    ``low``/``high`` are guard rails, not opinions: they keep a typo from
    turning a 120-hour estimate into a 12,000-hour one, and they are wide
    enough that a legitimate value never hits them. A value outside them is
    refused and named rather than clamped — silently using a different number
    than the one somebody typed is how an estimate stops being trusted.
    """
    label: str
    kind: str                      # "number" | "int" | "text"
    low: float | None = None
    high: float | None = None
    help: str = ""


#: The allowlist. Everything else in the payload is derived, and a client that
#: sends a derived value gets a 400 naming it — not a silent drop, because a
#: caller who believes it set the total is worse off than one told it cannot.
INPUTS: dict[str, Input] = {
    "rate_usd": Input("Hourly rate (USD)", "number", 0, 10_000,
                      "Only affects the cost columns, never the hours."),
    "minutes_per_tc": Input("Minutes per test case", "int", 1, 600,
                            "The single biggest driver of the total."),
    "buffer": Input("Buffer multiplier", "number", 1.0, 3.0,
                    "1.12 in the reference template."),
    "additional_platforms": Input("Additional platforms", "int", 0, 100,
                                  "Drives the full-compatibility scenario."),
    "compatibility_rate": Input("Compatibility rate", "number", 0, 1),
    "bug_report_rate": Input("Bug-report share", "number", 0, 1),
    "pm_overhead": Input("PM overhead", "number", 0, 1),
    "max_testing_stretch": Input("MIN→MAX stretch", "number", 1.0, 5.0),
    "team_size": Input("Team size", "int", 1, 50,
                       "Above 1 this adds a Brooks's-Law communication "
                       "penalty rather than dividing the work."),
    "project_name": Input("Project name", "text"),
    "primary_platform": Input("Primary platform", "text"),
}

#: Keys in the stored input payload that are not user inputs.
_NOT_INPUTS = ("features", "platforms_list", "source", "source_ref")


def _coerce(name: str, spec: Input, value):
    if spec.kind == "text":
        text = "" if value is None else str(value).strip()
        if len(text) > MAX_FEATURE_NAME:
            raise EstimationEditError(
                f"{spec.label} is limited to {MAX_FEATURE_NAME} characters.",
                field=name)
        return text

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise EstimationEditError(f"{spec.label} must be a number.",
                                  field=name) from None
    if number != number or number in (float("inf"), float("-inf")):
        # NaN and the infinities survive float() and then poison every total
        # they touch — a JSON payload can carry them as strings.
        raise EstimationEditError(f"{spec.label} must be a real number.",
                                  field=name)
    if spec.kind == "int":
        if number != int(number):
            raise EstimationEditError(
                f"{spec.label} must be a whole number.", field=name)
        number = int(number)
    if spec.low is not None and number < spec.low:
        raise EstimationEditError(
            f"{spec.label} cannot be below {spec.low:g}.", field=name)
    if spec.high is not None and number > spec.high:
        raise EstimationEditError(
            f"{spec.label} cannot be above {spec.high:g}.", field=name)
    return number


def validate(changes: dict) -> dict:
    """Coerce an edit to the input set, or raise. Rejects the whole edit.

    A partly-applied estimation edit is the worst kind: the totals would be
    computed from a mixture of what the user asked for and what they did not.
    """
    if not isinstance(changes, dict) or not changes:
        raise EstimationEditError("Nothing to change.")
    unknown = [key for key in changes
               if key not in INPUTS and key != "features"]
    if unknown:
        raise EstimationEditError(
            "These are computed from the inputs and cannot be set directly: "
            + ", ".join(sorted(unknown))
            + ". Change what drives them instead.")
    out = {}
    for name, value in changes.items():
        if name == "features":
            out["features"] = validate_features(value)
            continue
        out[name] = _coerce(name, INPUTS[name], value)
    return out


def validate_features(raw) -> list[dict]:
    """The feature list: a name, a test-case count, an optional comment."""
    if not isinstance(raw, list):
        raise EstimationEditError("Features must be a list.", field="features")
    if len(raw) > MAX_FEATURES:
        raise EstimationEditError(
            f"An estimation is limited to {MAX_FEATURES} features.",
            field="features")
    out: list[dict] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise EstimationEditError(
                f"Feature {position} is not an object.", field="features")
        name = str(item.get("name") or "").strip()
        if not name:
            raise EstimationEditError(
                f"Feature {position} needs a name.", field="features")
        if len(name) > MAX_FEATURE_NAME:
            raise EstimationEditError(
                f"Feature {position}'s name is limited to "
                f"{MAX_FEATURE_NAME} characters.", field="features")
        is_section = bool(item.get("is_section"))
        try:
            cases = int(item.get("test_cases") or 0)
        except (TypeError, ValueError):
            raise EstimationEditError(
                f"Feature {position}'s test-case count must be a whole "
                f"number.", field="features") from None
        if cases < 0:
            raise EstimationEditError(
                f"Feature {position} cannot have a negative test-case count.",
                field="features")
        if cases > 100_000:
            raise EstimationEditError(
                f"Feature {position}'s test-case count is implausible "
                f"({cases}).", field="features")
        out.append({
            "name": name,
            "test_cases": 0 if is_section else cases,
            "comment": str(item.get("comment") or "").strip()[:500],
            "is_section": is_section,
        })
    return out


def inputs_from_payload(payload: dict) -> dict:
    """The current value of every input, from a stored estimation payload.

    Read from the *result* payload when it has them: the result records the
    coefficients it was computed with, so it is the honest answer to "what
    produced these numbers". The input payload is what the form posted, which
    for older rows may be missing keys the estimator defaulted.
    """
    payload = payload or {}
    result = payload.get("result") or payload.get("result_payload") or payload
    stored_inputs = payload.get("input") or payload.get("input_payload") or {}
    out = {}
    for name in INPUTS:
        if name in result and result[name] not in (None, ""):
            out[name] = result[name]
        elif name in stored_inputs and stored_inputs[name] not in (None, ""):
            out[name] = stored_inputs[name]
    features = result.get("features") or stored_inputs.get("features") or []
    out["features"] = [
        {"name": f.get("name", ""), "test_cases": f.get("test_cases", 0),
         "comment": f.get("comment", ""),
         "is_section": bool(f.get("is_section"))}
        for f in features if isinstance(f, dict)
    ]
    return out


def recompute(inputs: dict) -> dict:
    """Run the estimator over an input set and return the result as a dict.

    The same function the generator called. Nothing here reimplements a
    formula, which is the whole point: a second implementation would drift,
    and the drift would show up as an estimate the XLSX export disagrees with.
    """
    from dataclasses import asdict

    from engine.qa_estimator import Feature, compute_estimation

    features = [
        Feature(name=f["name"], test_cases=int(f.get("test_cases") or 0),
                comment=f.get("comment", ""),
                is_section=bool(f.get("is_section")))
        for f in inputs.get("features") or []
    ]

    call = {name: inputs[name] for name in INPUTS if name in inputs}
    result = compute_estimation(features=features, **call)
    return asdict(result)


#: The numbers a person reads off the page, in the order the page shows them.
#: Used by the diff, so "what changed" is expressed in the terms the estimate
#: is discussed in rather than in every one of the sixty computed fields.
HEADLINE_FIELDS = (
    ("total_tc", "Test cases"),
    ("features_hours", "Feature hours"),
    ("one_plat_total_expected", "One platform — expected"),
    ("full_total_expected", "Full compatibility — expected"),
    ("cost_one_expected", "Cost, one platform"),
    ("cost_full_expected", "Cost, full compatibility"),
    ("pert_expected", "PERT expected"),
    ("team_size", "Team size"),
)


def diff(original: dict, current: dict) -> list[dict]:
    """What the edit changed, in the terms the estimate is discussed in.

    Returned for the UI's "the model said X, this says Y" panel. Only the
    headline numbers: a diff over all sixty computed fields is noise, and the
    argument with a client is never about ``one_plat_pm_min``.
    """
    original = original or {}
    current = current or {}
    out: list[dict] = []
    for key, label in HEADLINE_FIELDS:
        before = original.get(key)
        after = current.get(key)
        if before is None and after is None:
            continue
        if _close(before, after):
            continue
        out.append({
            "field": key, "label": label,
            "before": before, "after": after,
            "delta": _delta(before, after),
        })
    return out


def _close(a, b) -> bool:
    try:
        return abs(float(a or 0) - float(b or 0)) < 0.005
    except (TypeError, ValueError):
        return a == b


def _delta(before, after):
    try:
        return round(float(after or 0) - float(before or 0), 2)
    except (TypeError, ValueError):
        return None


def dump_original(result_payload: dict) -> str:
    """Serialise the generator's result for ``estimation.original_payload``."""
    return json.dumps(result_payload or {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def load_original(raw: str | None) -> dict:
    """Read it back. A corrupt value is no original, not an exception."""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError) as exc:
        log.warning("stored original estimation is not readable: %s", exc)
        return {}


def get(project_id: str, estimation_id: int | None = None) -> dict | None:
    """One estimation, with its inputs, its result, and the original.

    ``estimation_id=None`` means the project's latest, which is what the page
    shows. Returns ``None`` when there is nothing to edit.
    """
    from engine import db as _db

    if not project_id:
        return None
    with _db.session_scope() as sess:
        row = _row(sess, project_id, estimation_id)
        if row is None:
            return None
        return _public(row)


def _row(sess, project_id: str, estimation_id: int | None):
    from engine import db as _db

    query = sess.query(_db.Estimation).filter(
        _db.Estimation.project_id == project_id)
    if estimation_id is not None:
        return query.filter(_db.Estimation.id == int(estimation_id)
                            ).one_or_none()
    return query.order_by(_db.Estimation.created_at.desc()).first()


def _public(row) -> dict:
    result = dict(row.result_payload or {})
    original = load_original(row.original_payload)
    return {
        "id": int(row.id),
        "result": result,
        "inputs": inputs_from_payload({"result": result,
                                       "input": row.input_payload or {}}),
        "row_version": int(getattr(row, "row_version", 1) or 1),
        "ai_generated": bool(getattr(row, "ai_generated", True)),
        "edited_by": getattr(row, "edited_by", None),
        "edited_at": (row.edited_at.isoformat()
                      if getattr(row, "edited_at", None) else None),
        # Absent until the first edit: an unedited estimation *is* the
        # original, and returning a copy of itself would render a diff of
        # nothing against nothing.
        "original": original,
        "diff": diff(original, result) if original else [],
    }


def apply(project_id: str, changes: dict, *, estimation_id: int | None = None,
          expected_version: int | None = None,
          actor: str | None = None) -> dict:
    """Change inputs, recompute everything, store it. Returns the new state.

    The same guarantees the column editors give (E4.1): only declared inputs
    change, the whole edit is refused if any part of it is wrong, a stale
    ``row_version`` is a :class:`engine.db.WriteConflict`, provenance flips to
    human, and one audit row records what moved.

    What is different, and is the point of this task: the client sends no
    derived value. Every total is computed here, by the estimator, from the
    merged input set — so the numbers on the page are the numbers in the
    database because there is only one place they are produced.
    """
    from engine import db as _db

    if not project_id:
        raise EstimationEditError("No active project.")
    incoming = validate(changes)

    with _db.session_scope() as sess:
        row = _row(sess, project_id, estimation_id)
        if row is None:
            raise EstimationEditError("There is no estimation to edit yet.")

        current_version = int(getattr(row, "row_version", 1) or 1)
        if (expected_version is not None
                and int(expected_version) != current_version):
            raise _db.WriteConflict("estimation", int(expected_version),
                                    current_version)

        before = dict(row.result_payload or {})
        # First edit: keep what the generator produced. Later edits keep the
        # *generator's* original, not the previous edit — the interesting
        # comparison is always "the model said X".
        if not row.original_payload:
            row.original_payload = dump_original(before)

        merged = inputs_from_payload({"result": before,
                                      "input": row.input_payload or {}})
        merged.update(incoming)
        result = recompute(merged)

        changed = {name: [before.get(name), result.get(name)]
                   for name, _ in HEADLINE_FIELDS
                   if not _close(before.get(name), result.get(name))}
        for name, value in incoming.items():
            if name == "features":
                if (before.get("features") or []) != value:
                    changed["features"] = ["(edited)", f"{len(value)} features"]
                continue
            if not _close(before.get(name), value):
                changed[name] = [before.get(name), value]

        if not changed:
            # A no-op is not a write, for the same reason as everywhere else:
            # it would bump the version and manufacture a conflict for a
            # colleague who is mid-edit.
            return _public(row)

        row.input_payload = dict(row.input_payload or {}, **{
            key: value for key, value in merged.items()})
        row.result_payload = result
        row.total_hours = result.get("one_plat_total_expected") or None
        row.row_version = current_version + 1
        row.ai_generated = False
        row.edited_by = actor or None
        from datetime import datetime, timezone
        row.edited_at = datetime.now(timezone.utc)
        state = _public(row)

    _db.append_audit(entity="estimation", action="update",
                     entity_id=str(state["id"]), project_id=project_id,
                     user_id=actor, diff=changed)
    return state


def revert(project_id: str, *, estimation_id: int | None = None,
           expected_version: int | None = None,
           actor: str | None = None) -> dict:
    """Put the generator's numbers back.

    The counterpart to keeping the original: a lead who talked themselves into
    148 hours and then changed their mind should not have to remember what the
    model said. Provenance goes back to ``ai_generated=True`` — after this the
    row *is* what the generator produced, and E4.7's regeneration merge should
    treat it as such.
    """
    from engine import db as _db

    with _db.session_scope() as sess:
        row = _row(sess, project_id, estimation_id)
        if row is None:
            raise EstimationEditError("There is no estimation to revert.")
        original = load_original(row.original_payload)
        if not original:
            raise EstimationEditError(
                "This estimation has not been edited, so there is nothing to "
                "put back.")
        current_version = int(getattr(row, "row_version", 1) or 1)
        if (expected_version is not None
                and int(expected_version) != current_version):
            raise _db.WriteConflict("estimation", int(expected_version),
                                    current_version)
        row.result_payload = original
        row.total_hours = original.get("one_plat_total_expected") or None
        row.original_payload = None
        row.row_version = current_version + 1
        row.ai_generated = True
        row.edited_by = actor or None
        state = _public(row)

    _db.append_audit(entity="estimation", action="revert",
                     entity_id=str(state["id"]), project_id=project_id,
                     user_id=actor, diff={"result": ["(edited)", "(original)"]})
    return state


__all__ = ["HEADLINE_FIELDS", "INPUTS", "MAX_FEATURES", "EstimationEditError",
           "apply", "diff", "dump_original", "get", "inputs_from_payload",
           "load_original", "recompute", "revert", "validate",
           "validate_features"]
