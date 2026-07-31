"""TestFortge — Gherkin (BDD) representation of a test case.

A test case exists in this product for two different readers:

* a **tester**, who needs the TestFort columns — Preconditions, numbered
  Steps, Test Data, Expected Result — and reads them in a spreadsheet;
* a **runner**, which needs Given / When / Then so a step-definition
  library can bind each line to code.

Rather than keep two hand-maintained copies, the manual form stays the
source of truth and this module derives the Gherkin from it. That
direction matters: the manual columns are what a client signs off, and a
generated `.feature` that drifts from the signed-off case is worse than no
`.feature` at all.

Mapping
-------
::

    preconditions          → Given          (one clause per sentence)
    step 1, if navigation   → Given
    remaining action steps  → When / And
    expected result         → Then / And    (numbered clauses split)
    test data               → a data table on the last When
    observation-only steps  → dropped, recorded as a `#` comment

The mapping is deliberately 1:1 on the step list rather than
BDD-idiomatic. Textbook BDD would fold every setup action into Given and
leave a single When; that reads better and automates worse, because the
step-definition library in PR-4 has to bind the steps the tester actually
wrote. A reversible translation is worth more here than a pure one.

Observation steps ("Pay attention to …", "Try to find …") are the one
thing that cannot survive: they are assertions in imperative clothing, and
a step definition cannot act on them. Whatever they observe is already in
the expected result, so they are dropped — but recorded as a comment, so a
reader can see that a line was removed and why, rather than wondering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from engine.log import get_logger

_logger = get_logger(__name__)

#: The two formats a test case can be stored in. Checklists are manual
#: only — a checklist row is one observation with no steps to bind, so
#: there is nothing for a runner to execute.
FORMATS = ("manual", "gherkin")
DEFAULT_FORMAT = "manual"

KEYWORDS = ("Given", "When", "Then", "And", "But")

#: House-style verbs that observe rather than act. See the module
#: docstring for why they cannot become steps.
_OBSERVATION_RE = re.compile(
    r"^\s*(?:pay\s+attention\s+to|try\s+to\s+find|observe|note)\b",
    re.IGNORECASE)

#: Step 1 is navigation in the house style; recognise it so it lands in
#: Given rather than When.
_NAVIGATION_RE = re.compile(
    r"^\s*(?:go\s+to|navigate\s+to|open|visit|launch)\b", re.IGNORECASE)

#: "1. ", "2) ", "- " — numbering the exporter adds and the author must not.
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)")


# ── Data model ───────────────────────────────────────────────────────

@dataclass
class Step:
    keyword: str            # Given | When | Then | And | But
    text: str
    #: Rows of a Gherkin data table, header row first.
    table: list[list[str]] = field(default_factory=list)
    #: A triple-quoted doc string attached to the step.
    doc_string: str = ""

    def render(self, indent: str = "    ") -> list[str]:
        out = [f"{indent}{self.keyword} {self.text}"]
        if self.doc_string:
            out.append(f'{indent}  """')
            out.extend(f"{indent}  {line}"
                       for line in self.doc_string.splitlines())
            out.append(f'{indent}  """')
        for row in self.table:
            out.append(f"{indent}  | " + " | ".join(row) + " |")
        return out


@dataclass
class Scenario:
    name: str
    steps: list[Step] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: Lines the translation could not express as steps, kept as comments
    #: so nothing disappears silently.
    notes: list[str] = field(default_factory=list)

    def render(self, indent: str = "  ") -> list[str]:
        out: list[str] = []
        if self.tags:
            out.append(indent + " ".join(self.tags))
        out.append(f"{indent}Scenario: {self.name}")
        for step in self.steps:
            out.extend(step.render(indent + "  "))
        # Notes come last: they record what the translation could not
        # express, so they read as a footnote rather than as a preamble.
        for note in self.notes:
            out.append(f"{indent}  # {note}")
        return out


@dataclass
class Feature:
    name: str
    description: str = ""
    scenarios: list[Scenario] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def render(self) -> str:
        out: list[str] = []
        if self.tags:
            out.append(" ".join(self.tags))
        out.append(f"Feature: {self.name}")
        if self.description:
            out.extend(f"  {line}" for line in
                       self.description.splitlines())
        for scenario in self.scenarios:
            out.append("")
            out.extend(scenario.render())
        return "\n".join(out) + "\n"


# ── Tags ─────────────────────────────────────────────────────────────

def _tag(value: Any, *, lower: bool = True) -> str:
    """Sanitise a value into a single Gherkin tag, or ``""``.

    Cucumber tags cannot contain whitespace, and a tag with a stray
    character silently fails to match on the command line — which reads as
    "the test did not run" rather than as a bad tag.

    ``lower=False`` keeps the case: the test-case id is a citable
    identifier that appears in bug reports and status updates, so
    ``@TC-SC1_004`` has to stay recognisable. Tags ARE case-sensitive in
    Cucumber, so this is a real distinction, not cosmetics.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-",
                  str(value or "").strip()).strip("-")
    if lower:
        slug = slug.lower()
    return f"@{slug}" if slug else ""


def tags_for(tc: Any) -> list[str]:
    """Tags a runner can filter on: id, category, priority, suite, type."""
    out: list[str] = []
    tc_id = str(getattr(tc, "id", "") or "").strip()
    if tc_id:
        out.append(_tag(f"TC-{tc_id}", lower=False))
    for attr in ("category", "priority", "suite", "testing_type"):
        tag = _tag(getattr(tc, attr, "") or "")
        # "@functional" from testing_type and "@medium" from priority are
        # useful; an empty attribute contributes nothing.
        if tag and tag not in out:
            out.append(tag)
    return [t for t in out if t]


# ── Manual → Gherkin ─────────────────────────────────────────────────

def _clauses(text: str) -> list[str]:
    """Split a precondition or expected result into one clause per line.

    The house style numbers independent post-conditions ("1. … 2. …") and
    otherwise separates facts with ". ", so both are handled.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    # Numbered form first — "1. X 2. Y" on one line, or one per line.
    numbered = re.split(r"(?:^|\s)\d+[.)]\s+", raw)
    parts = [p for p in numbered if p.strip()] if len(numbered) > 2 else None
    if parts is None:
        parts = [p for p in re.split(r"\n+", raw) if p.strip()]
        if len(parts) == 1:
            parts = [p for p in re.split(r"(?<=[a-z0-9)\"'])\.\s+", raw)
                     if p.strip()]
    return [re.sub(r"\s+", " ", p).strip().rstrip(".") for p in parts
            if p.strip()]


def _steps_from_text(text: str) -> list[str]:
    """Split a ``test_steps`` blob into individual step strings."""
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [l for l in re.split(r"\n+", raw) if l.strip()]
    if len(lines) == 1:
        # A single line carrying "1. … 2. …" — the exporter's own format.
        split = re.split(r"(?:^|\s)(?=\d+[.)]\s)", raw)
        if len(split) > 1:
            lines = [s for s in split if s.strip()]
    return [_LEADING_NUMBER_RE.sub("", l).strip() for l in lines
            if _LEADING_NUMBER_RE.sub("", l).strip()]


def _first_person(text: str) -> str:
    """Turn an imperative house step into a Gherkin first-person clause.

    ``'Click on the "Save" button'`` → ``'I click on the "Save" button'``.

    A clause that already reads as a STATE rather than an action keeps its
    subject and only loses its opening capital: a precondition like
    ``'The "…" form is opened'`` must not become ``'I the "…" form is
    opened'``. The article test is case-insensitive precisely because
    preconditions are written capitalised.
    """
    s = re.sub(r"\s+", " ", (text or "").strip()).rstrip(".")
    if not s:
        return ""
    if re.match(r"^I\b", s):
        return s
    if re.match(r"^(?:the|a|an|there|it|every|all|no)\b", s, re.IGNORECASE):
        return s[0].lower() + s[1:]
    return "I " + s[0].lower() + s[1:]


def _declarative(text: str) -> str:
    """Lower-case the opening word of an expected-result clause."""
    s = re.sub(r"\s+", " ", (text or "").strip()).rstrip(".")
    if not s:
        return ""
    # Keep an acronym or a quoted label as written.
    if s[:2].isupper() or s.startswith('"'):
        return s
    return s[0].lower() + s[1:]


def _data_table(test_data: str) -> tuple[list[list[str]], str]:
    """Parse ``test_data`` into a Gherkin table, or fall back to a docstring.

    Returns ``(table, doc_string)`` — exactly one is populated. A table is
    only produced when every fragment actually looks like ``field: value``;
    forcing prose into two columns would invent structure.
    """
    raw = (test_data or "").strip()
    if not raw:
        return [], ""
    fragments = [f.strip() for f in re.split(r"[\n;]+|,\s*(?=[A-Z])", raw)
                 if f.strip()]
    rows: list[list[str]] = []
    for frag in fragments:
        if ":" not in frag:
            return [], raw
        key, _, val = frag.partition(":")
        key, val = key.strip().strip('"'), val.strip()
        if not key or not val or len(key) > 60:
            return [], raw
        rows.append([key, val])
    if not rows:
        return [], raw
    return [["field", "value"]] + rows, ""


def scenario_from_test_case(tc: Any) -> Scenario:
    """Derive one :class:`Scenario` from a manual test case."""
    name = re.sub(r"\s+", " ", str(getattr(tc, "summary", "") or "").strip())
    scenario = Scenario(name=name or "Unnamed scenario", tags=tags_for(tc))

    given: list[str] = _clauses(getattr(tc, "preconditions", "") or "")
    raw_steps = _steps_from_text(getattr(tc, "test_steps", "") or "")

    actions: list[str] = []
    for step in raw_steps:
        if _OBSERVATION_RE.match(step):
            # An assertion in imperative clothing — see module docstring.
            scenario.notes.append(
                f"observation step dropped (asserted in Then): {step}")
            continue
        if not actions and _NAVIGATION_RE.match(step):
            given.append(step)
            continue
        actions.append(step)

    for i, clause in enumerate(given):
        scenario.steps.append(Step("Given" if i == 0 else "And",
                                   _first_person(clause)))
    for i, action in enumerate(actions):
        scenario.steps.append(Step("When" if i == 0 else "And",
                                   _first_person(action)))

    table, doc = _data_table(getattr(tc, "test_data", "") or "")
    if table or doc:
        data_step = Step("And", "I use the following test data:",
                         table=table, doc_string=doc)
        # Attach to the action block when there is one, so the data reads
        # as part of what the user did.
        if actions:
            scenario.steps.append(data_step)
        else:
            scenario.steps.insert(len(given), data_step)

    then = _clauses(getattr(tc, "expected_result", "") or "")
    for i, clause in enumerate(then):
        scenario.steps.append(Step("Then" if i == 0 else "And",
                                   _declarative(clause)))

    if not any(s.keyword == "When" for s in scenario.steps):
        # A display-only case has no action. Gherkin still needs a When for
        # the runner to hang a trigger on, and "I open the page" is what
        # the case actually does.
        insert_at = len(given)
        scenario.steps.insert(
            insert_at, Step("When", "I look at the page"))
    return scenario


def feature_from_test_cases(cases: Iterable[Any], *,
                            name: str = "",
                            description: str = "") -> Feature:
    """Build one :class:`Feature` from cases that share a section."""
    cases = list(cases or [])
    feature_name = name or (
        str(getattr(cases[0], "section", "") or "").strip()
        if cases else "") or "Test cases"
    feature = Feature(name=feature_name, description=description)
    for tc in cases:
        feature.scenarios.append(scenario_from_test_case(tc))
    return feature


def features_from_test_cases(cases: Iterable[Any]) -> list[Feature]:
    """One Feature per section, in first-appearance order."""
    grouped: dict[str, list[Any]] = {}
    for tc in cases or []:
        section = str(getattr(tc, "section", "") or "").strip() or "General"
        grouped.setdefault(section, []).append(tc)
    return [feature_from_test_cases(v, name=k) for k, v in grouped.items()]


def gherkin_for_test_case(tc: Any) -> str:
    """The ``.feature`` text for one case, Scenario block only.

    Stored on ``TestCase.gherkin`` so the editor can show and the operator
    can hand-edit it. The Feature header is added when the export groups
    scenarios into files.
    """
    return "\n".join(scenario_from_test_case(tc).render(indent="")) + "\n"


def feature_filename(section: str) -> str:
    """A stable, filesystem-safe ``.feature`` name for a section."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(section or "").strip()).strip("-")
    return (slug.lower() or "test-cases") + ".feature"


# ── Parsing ──────────────────────────────────────────────────────────

_TAG_LINE_RE = re.compile(r"^\s*@\S+(?:\s+@\S+)*\s*$")
_STEP_RE = re.compile(rf"^\s*({'|'.join(KEYWORDS)})\s+(.*\S)\s*$")


def parse(text: str) -> Feature | None:
    """Parse ``.feature`` text. Returns ``None`` when there is no Feature.

    Deliberately small: it reads what :meth:`Feature.render` writes, plus
    the hand-edits an operator is likely to make. It is not a
    specification-complete Gherkin parser — no Rule, no Scenario Outline,
    no Background — and :func:`lint` reports those rather than mis-reading
    them.
    """
    if not (text or "").strip():
        return None
    feature: Feature | None = None
    scenario: Scenario | None = None
    step: Step | None = None
    pending_tags: list[str] = []
    in_doc = False
    doc_lines: list[str] = []

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if in_doc:
            if stripped == '"""':
                in_doc = False
                if step is not None:
                    step.doc_string = "\n".join(doc_lines)
                doc_lines = []
            else:
                doc_lines.append(stripped)
            continue

        if not stripped:
            continue
        if stripped.startswith("#"):
            if scenario is not None:
                scenario.notes.append(stripped.lstrip("#").strip())
            continue
        if _TAG_LINE_RE.match(stripped):
            pending_tags = stripped.split()
            continue
        if stripped.startswith("Feature:"):
            feature = Feature(name=stripped[len("Feature:"):].strip(),
                              tags=pending_tags)
            pending_tags = []
            scenario = step = None
            continue
        if stripped.startswith("Scenario:") or \
                stripped.startswith("Example:"):
            if feature is None:
                feature = Feature(name="")
            scenario = Scenario(
                name=stripped.split(":", 1)[1].strip(), tags=pending_tags)
            pending_tags = []
            feature.scenarios.append(scenario)
            step = None
            continue
        if stripped == '"""':
            in_doc = True
            doc_lines = []
            continue
        if stripped.startswith("|") and step is not None:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            step.table.append(cells)
            continue
        m = _STEP_RE.match(stripped)
        if m and scenario is not None:
            step = Step(m.group(1), m.group(2))
            scenario.steps.append(step)
            continue
        # Anything else before the first Scenario is the Feature narrative.
        if feature is not None and scenario is None:
            feature.description = (feature.description + "\n" + stripped
                                   ).strip()
    return feature


def lint(text: str) -> list[str]:
    """Structural findings for ``.feature`` text. Empty list == valid."""
    issues: list[str] = []
    if not (text or "").strip():
        return ["empty feature text"]

    for unsupported in ("Scenario Outline:", "Scenario Template:",
                        "Rule:", "Background:", "Examples:"):
        if re.search(rf"^\s*{re.escape(unsupported)}", text, re.M):
            issues.append(
                f"{unsupported.rstrip(':')} is not supported — the parser "
                f"would silently drop it")

    if not re.search(r"^\s*Feature\s*:", text, re.M):
        # parse() tolerates a bare Scenario so a hand-edited fragment still
        # round-trips, but a .feature file without a Feature line is not a
        # file cucumber will load.
        issues.append("no Feature: line")

    feature = parse(text)
    if feature is None:
        issues.append("nothing parseable")
        return issues
    if not feature.name.strip():
        issues.append("Feature has no name")
    if not feature.scenarios:
        issues.append("Feature has no Scenario")

    for ix, scenario in enumerate(feature.scenarios, 1):
        where = f"scenario {ix}"
        if not scenario.name.strip():
            issues.append(f"{where}: no name")
        if not scenario.steps:
            issues.append(f"{where}: no steps")
            continue
        first = scenario.steps[0].keyword
        if first in ("And", "But"):
            issues.append(
                f"{where}: opens with \"{first}\", which has no preceding "
                f"step to continue")
        kinds = {s.keyword for s in scenario.steps}
        if "When" not in kinds:
            issues.append(f"{where}: no When step — nothing is exercised")
        if "Then" not in kinds:
            issues.append(f"{where}: no Then step — nothing is asserted")
        for s_ix, step in enumerate(scenario.steps, 1):
            if not step.text.strip():
                issues.append(f"{where} step {s_ix}: empty text")
            if step.table:
                widths = {len(r) for r in step.table}
                if len(widths) > 1:
                    issues.append(
                        f"{where} step {s_ix}: data table rows have "
                        f"different column counts {sorted(widths)}")
    return issues


def is_valid(text: str) -> bool:
    return not lint(text)


# ── Format helpers ───────────────────────────────────────────────────

def coerce_format(value: Any) -> str:
    """Normalise a stored / submitted format value onto :data:`FORMATS`."""
    val = str(value or "").strip().lower()
    if val in ("bdd", "gherkin", "feature", "automation"):
        return "gherkin"
    return "manual"


def ensure_gherkin(tc: Any) -> str:
    """Return the case's Gherkin, deriving it when it has none stored.

    Derivation is **lazy on purpose**. Stamping the Gherkin onto every row
    at generation time would mean two copies of the same case in the
    database, and the moment a tester edits a step the copies disagree —
    with the stale one being what the runner executes. So the column holds
    only text an operator hand-edited, and everything else is derived from
    the manual columns at read time. Hand-edited text always wins.
    """
    existing = str(getattr(tc, "gherkin", "") or "")
    if existing.strip():
        # Returned verbatim, not stripped: an operator's blank line or
        # trailing newline is their formatting, and silently normalising it
        # would make a hand-edit look like it did not save.
        return existing
    return gherkin_for_test_case(tc)


def is_automation_targeted(tc: Any) -> bool:
    """True when this case is meant to be run by the automation module."""
    return coerce_format(getattr(tc, "tc_format", DEFAULT_FORMAT)) == "gherkin"


def apply_format(tc_dicts: Any, fmt: Any) -> list[dict]:
    """Stamp the requested format onto freshly generated case dicts.

    Only ``tc_format`` is written. ``gherkin`` stays empty so it keeps its
    meaning — "an operator edited this by hand" — and the runnable text is
    derived by :func:`ensure_gherkin` whenever it is needed.

    There is no third "both" option, and that is not an omission: the
    manual columns are populated either way, so "manual + BDD" and "BDD"
    would be the same row. The flag records only whether the automation
    module should pick the case up.
    """
    wanted = coerce_format(fmt)
    out: list[dict] = []
    for d in (tc_dicts or []):
        if isinstance(d, dict):
            d = dict(d)
            d["tc_format"] = wanted
        out.append(d)
    return out


__all__ = [
    "FORMATS", "DEFAULT_FORMAT", "KEYWORDS",
    "Step", "Scenario", "Feature",
    "scenario_from_test_case", "feature_from_test_cases",
    "features_from_test_cases", "gherkin_for_test_case",
    "feature_filename", "tags_for",
    "parse", "lint", "is_valid", "coerce_format", "ensure_gherkin",
    "is_automation_targeted", "apply_format",
]
