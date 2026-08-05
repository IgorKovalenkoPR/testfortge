"""TestFortge — KPI targets and per-person dashboard layout (E7.2, E7.3).

Two settings that belong to different owners, which is the only interesting
thing about them:

* **KPI targets and their thresholds** belong to the *project*. "Pass rate
  should be above 90%" is a team agreement, not a personal preference, and a
  dashboard where two people see different colours for the same number is
  worse than one with no colours at all. Admin-writable, everyone-readable —
  the same rule Admin Settings follows (§5.1).

* **Which widgets are shown, and in what order,** belongs to the *person*. A
  test lead and an automation engineer open the same project to look at
  different things, and making that a project setting means they argue about
  it in a settings screen instead of just working.

Both live in JSON columns rather than tables of rows. They are read whole,
written whole, and never queried across projects — a table would buy indexing
nobody needs and cost a migration plus a join on every dashboard load.

RAG, and why the direction is declared
--------------------------------------
"Above target is good" is true of a pass rate and false of defect density or
open-bug count. A single comparison would quietly colour a project green for
having more bugs than its target. So every KPI declares which way is better,
and :func:`evaluate` uses it.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Kpi:
    """One measurable, and what "good" means for it."""
    key: str                      # a key of the dashboard metrics dict
    label: str
    higher_is_better: bool
    unit: str = ""
    #: The default target, used until a project sets its own. Chosen to be
    #: plausible rather than aspirational — a target nobody meets is a red
    #: dashboard people stop reading.
    default_target: float | None = None
    #: How far below (or above) target still counts as amber, as a fraction
    #: of the target. Beyond it is red.
    amber_band: float = 0.1
    #: The metrics key this KPI is computed *from*, when it is a ratio. While
    #: that key is zero the KPI has no status at all.
    #:
    #: Measured on a fresh project: a pass rate of 0% against a 90% target
    #: came out red, which tells a team they are failing when the honest
    #: answer is "nothing has been executed yet". A red dashboard on day one
    #: is a dashboard people learn to ignore.
    denominator_key: str = ""


#: The KPIs the dashboard can track. Keys must exist in the metrics dict —
#: asserted in the tests, because a typo here is a widget that silently
#: reports zero.
KPIS: tuple[Kpi, ...] = (
    Kpi("exec_pass_rate", "Pass rate", True, "%", 90.0,
        denominator_key="exec_total"),
    Kpi("exec_total", "Cases executed", True, "", None),
    Kpi("tc_total", "Test cases", True, "", None),
    Kpi("bug_total", "Bugs found", False, "", None),
    Kpi("runs_count", "Runs", True, "", None),
)

#: Derived KPIs, computed from the metrics rather than read from them.
DERIVED = ("defect_density",)

#: Only blocks that exist as separate blocks on the page.
#:
#: An earlier version listed "coverage", "execution" and "bugs" — but those
#: three are tabs inside one shared card in ``templates/index.html``, so a
#: switch for any of them would have been a control that does nothing. A
#: preferences screen offering settings with no effect is worse than a shorter
#: one.
DEFAULT_WIDGETS: tuple[str, ...] = (
    "kpis", "metrics", "projects",
)

#: Widgets a person can hide or reorder. Anything not here is chrome.
WIDGET_LABELS: dict[str, str] = {
    "kpis": "KPI tiles, with targets",
    "metrics": "Test metrics (coverage, execution, bugs, environments)",
    "projects": "Saved projects",
}


# ── KPI targets (project-level) ────────────────────────────────────

def kpi_by_key(key: str) -> Kpi | None:
    for kpi in KPIS:
        if kpi.key == key:
            return kpi
    return None


def targets(project_id: str) -> dict:
    """``{kpi key: target}`` for a project — its own, or the defaults."""
    from engine import db as _db

    stored = {}
    if project_id:
        try:
            stored = _db.get_project_setting(project_id, "kpi_targets") or {}
        except Exception as exc:      # pragma: no cover — never break a render
            log.debug("kpi targets unavailable: %s", exc)
            stored = {}
    out = {kpi.key: kpi.default_target for kpi in KPIS
           if kpi.default_target is not None}
    for key, value in (stored or {}).items():
        if kpi_by_key(key) is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def set_targets(project_id: str, values: dict) -> dict:
    """Store a project's targets. Unknown keys and junk are dropped.

    An empty value **removes** a target rather than storing zero — "no target"
    and "target of nought" colour a tile very differently, and a form that
    turns a cleared field into 0 would paint every KPI green.
    """
    from engine import db as _db

    cleaned: dict[str, float] = {}
    for key, raw in (values or {}).items():
        if kpi_by_key(key) is None:
            continue
        text = str(raw).strip() if raw is not None else ""
        if not text:
            continue
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{key} must be a number, not {text!r}") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError(f"{key} must be a real number.")
        cleaned[key] = number
    _db.set_project_setting(project_id, "kpi_targets", cleaned)
    return cleaned


def evaluate(metrics: dict, project_targets: dict) -> list[dict]:
    """Each KPI with its value, its target and a RAG status.

    ``status`` is ``"green"`` / ``"amber"`` / ``"red"``, or ``""`` when there
    is no target — an untargeted number is just a number, and colouring it
    would invent an opinion the team has not expressed.
    """
    out = []
    for kpi in KPIS:
        raw = (metrics or {}).get(kpi.key, 0)
        try:
            value = float(raw or 0)
        except (TypeError, ValueError):
            value = 0.0
        target = project_targets.get(kpi.key)
        no_data = False
        if kpi.denominator_key:
            try:
                no_data = float((metrics or {}).get(kpi.denominator_key)
                                or 0) <= 0
            except (TypeError, ValueError):
                no_data = True
        out.append({
            "key": kpi.key, "label": kpi.label, "unit": kpi.unit,
            "value": value, "target": target,
            "higher_is_better": kpi.higher_is_better,
            "no_data": no_data,
            "status": "" if no_data else _status(value, target, kpi),
        })
    return out


def _status(value: float, target, kpi: Kpi) -> str:
    if target is None:
        return ""
    try:
        target = float(target)
    except (TypeError, ValueError):
        return ""
    band = abs(target) * kpi.amber_band
    if kpi.higher_is_better:
        if value >= target:
            return "green"
        return "amber" if value >= target - band else "red"
    if value <= target:
        return "green"
    return "amber" if value <= target + band else "red"


# ── Widget layout (per person) ─────────────────────────────────────

def layout(owner: str) -> dict:
    """``{"order": [...], "hidden": [...]}`` for this person.

    ``owner`` is a user id when signed in and the session id otherwise, so the
    preference follows the person in both eras — the same two-era treatment the
    project list gets.

    **Order and hidden are stored separately, and that is the whole design.**
    The first version stored just the visible list and repaired it by appending
    any widget it did not mention — which handles "a release added a widget"
    and makes hiding one impossible, because the next read puts it straight
    back. Caught by its own smoke test: hiding four widgets left all six
    showing. With an explicit hidden set, a widget missing from *both* lists is
    new and appears; a widget in ``hidden`` stays hidden.
    """
    from engine import db as _db

    if not owner:
        return {"order": list(DEFAULT_WIDGETS), "hidden": []}
    try:
        stored = _db.get_user_setting(owner, "dashboard_widgets")
    except Exception as exc:      # pragma: no cover
        log.debug("widget preference unavailable: %s", exc)
        stored = None
    return _clean_layout(stored)


def _clean_layout(stored) -> dict:
    """A stored preference, repaired against the current widget set."""
    if isinstance(stored, list):
        # The shape the first version wrote: a visible list and nothing else.
        # Read as an order with nothing hidden, so an early adopter keeps their
        # order instead of being reset.
        order, hidden = stored, []
    elif isinstance(stored, dict):
        order = stored.get("order") or []
        hidden = stored.get("hidden") or []
    else:
        return {"order": list(DEFAULT_WIDGETS), "hidden": []}

    order = [name for name in order
             if isinstance(name, str) and name in WIDGET_LABELS]
    hidden = [name for name in hidden
              if isinstance(name, str) and name in WIDGET_LABELS]
    seen = set(order)
    for name in DEFAULT_WIDGETS:
        if name not in seen:
            # New since this preference was saved: show it, in its default
            # place. A release that adds a widget must not hide it from
            # everybody who has ever saved a layout.
            order.append(name)
    return {"order": order, "hidden": hidden}


def widgets(owner: str) -> list[str]:
    """The widgets this person sees, in order — hidden ones excluded."""
    state = layout(owner)
    hidden = set(state["hidden"])
    return [name for name in state["order"] if name not in hidden]


def set_widgets(owner: str, names) -> list[str]:
    """Store which widgets are shown, in order. Everything else is hidden.

    Takes the visible set because that is what a form of checkboxes posts;
    the hidden set is derived, so the two cannot contradict each other.
    """
    from engine import db as _db

    if not owner:
        return list(DEFAULT_WIDGETS)
    seen, order = set(), []
    for name in (names or []):
        if (isinstance(name, str) and name in WIDGET_LABELS
                and name not in seen):
            order.append(name)
            seen.add(name)
    hidden = [name for name in DEFAULT_WIDGETS if name not in seen]
    _db.set_user_setting(owner, "dashboard_widgets",
                         {"order": order, "hidden": hidden})
    return order


def hidden_widgets(owner: str) -> list[str]:
    """Widgets this person has switched off."""
    return list(layout(owner)["hidden"])


__all__ = ["DEFAULT_WIDGETS", "DERIVED", "KPIS", "WIDGET_LABELS", "Kpi",
           "evaluate", "hidden_widgets", "kpi_by_key", "layout", "set_targets",
           "set_widgets", "targets", "widgets"]
