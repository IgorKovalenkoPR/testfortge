"""TestFortge — deterministic test-case generator (no model involved).

Walks the crawler's control inventory against
``engine/qa_knowledge/style/coverage_rules.yaml`` and emits house-style
test cases. Zero API cost, no key, no rate limit, no network call, no
data leaving the box — which is what makes it the free-tier answer, and
also the reason it works offline and reproduces byte-for-byte.

Why this exists alongside :mod:`engine.tc_author`
-------------------------------------------------
The author agent produces better prose and can judge which flows matter,
but it needs a paid LLM: the Anthropic API has no free tier, and the free
tiers of other vendors either train on submitted content or forbid
serving EEA/UK users — neither is acceptable for a QA vendor's client
requirements. So the LLM became the optional path and this became the
backbone.

The trade is honest: this module cannot judge relevance, read prose out
of an attachment, or invent an idiomatic section name for an unfamiliar
domain. What it can do is enumerate — which is where the reference corpus
gets its volume anyway. A form with a dozen controls yields 30-45 cases
there, and that comes from walking every control, not from insight.

Evidence discipline
-------------------
Every case is gated on a ``requires:`` token declared in the YAML. If the
crawled markup does not support the token, the case is not emitted. A
required-field negative appears only when the markup says ``required``; a
boundary case only when a real ``maxlength`` / ``min`` / ``max`` exists.
That keeps the suite free of cases that fail for the wrong reason — the
anti-pattern ``house_style.yaml`` names as "Inventing UI that the
artifacts do not evidence".
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

import yaml

from engine.log import get_logger
from engine.tc_author import AuthoredCase, navigation_step, normalise_case

_logger = get_logger(__name__)

_RULES_PATH = os.path.join(os.path.dirname(__file__), "qa_knowledge",
                           "style", "coverage_rules.yaml")

#: Guard against a pathological page (a 200-field form would otherwise
#: produce ~1,000 cases). Logged when it bites, never silent.
MAX_CASES_PER_FORM = int(os.environ.get("TC_RULES_MAX_PER_FORM", "120"))
MAX_FORMS = int(os.environ.get("TC_RULES_MAX_FORMS", "8"))

#: Same guard on the grid side. A 30-column sortable grid would produce
#: 60 sort cases alone, and a report that silently kept the first 40
#: reads as "everything is covered" — so every cap logs when it bites.
MAX_CASES_PER_GRID = int(os.environ.get("TC_RULES_MAX_PER_GRID", "40"))
MAX_GRIDS = int(os.environ.get("TC_RULES_MAX_GRIDS", "6"))
#: Per-check fan-out (one case per sortable column, per filter, …).
MAX_FAN_OUT = int(os.environ.get("TC_RULES_MAX_FAN_OUT", "6"))


# ── Rules asset ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_rules() -> dict:
    """Parsed coverage model. Empty dict if the asset is unusable."""
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        return doc if isinstance(doc, dict) else {}
    except Exception as exc:  # pragma: no cover — asset ships with repo
        _logger.warning("tc_rules: cannot load coverage rules: %s", exc)
        return {}


# ── Crawler type → coverage-model control type ───────────────────────

#: HTML input types map onto the eight control types the coverage model
#: declares. Anything unrecognised falls back to text_input, which is the
#: browser's own behaviour for an unknown ``type``.
_TYPE_MAP = {
    "text": "text_input", "search": "text_input", "tel": "text_input",
    "url": "text_input", "password": "text_input", "hidden": None,
    "submit": None, "button": None, "reset": None, "image": None,
    "email": "email_input",
    "number": "numeric_input", "range": "numeric_input",
    "date": "date_input", "datetime-local": "date_input",
    "month": "date_input", "week": "date_input", "time": "date_input",
    "select": "dropdown",
    "checkbox": "checkbox", "radio": "checkbox",
    "file": "file_upload",
    "textarea": "rich_text",
}


def control_type(field: dict) -> str | None:
    """Primary coverage-model key for a crawled field, or None to skip."""
    ftype = str(field.get("type") or "text").lower()
    if ftype in _TYPE_MAP:
        return _TYPE_MAP[ftype]
    return "text_input"


#: A control can legitimately draw from more than one set. A <textarea>
#: carries a maxlength like any text input *and* needs the pasted-markup
#: check, so it takes both rather than losing the boundary cases to a
#: single-type mapping.
_EXTRA_TYPES = {"textarea": ["text_input"]}


def control_types(field: dict) -> list[str]:
    """Every coverage-model key that applies to this control, in order."""
    primary = control_type(field)
    if primary is None:
        return []
    ftype = str(field.get("type") or "text").lower()
    return [primary] + [t for t in _EXTRA_TYPES.get(ftype, [])
                        if t != primary]


def group_radios(fields: list[dict]) -> list[dict]:
    """Collapse same-name radio inputs into a single choice control.

    A radio group is one decision for the user, so N members must not
    become N cases — on the real httpbin form that produced three
    identical "mark and unmark the \"size\" checkbox" rows. Modelled as a
    drop-down because that is the coverage set for "choose one of these",
    with the members' labels as the options.
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for field in fields:
        if str(field.get("type") or "").lower() != "radio":
            out.append(field)
            continue
        key = str(field.get("name") or field.get("id") or "")
        option = (str(field.get("label") or "").strip().rstrip(_LABEL_TRIM)
                  or str(field.get("value") or "").strip())
        if key and key in seen:
            group = seen[key]
            if option and option not in group["options"]:
                group["options"].append(option)
            group["required"] = group["required"] or bool(field.get("required"))
            continue
        group = {
            "name": key,
            # The group's own name, not a member's label — a member label
            # ("Large") would misname the control.
            "label": key or "choice",
            "type": "select",
            "required": bool(field.get("required")),
            "options": [option] if option else [],
            "_radio_group": True,
        }
        seen[key] = group
        out.append(group)
    return out


# ── Evidence tokens ──────────────────────────────────────────────────

def _has_evidence(token: str | None, field: dict, form: dict) -> bool:
    """Whether the markup justifies a case carrying ``token``.

    Unknown tokens return False on purpose: a case that asks for evidence
    this inventory cannot supply (an Odoo "Search More" picker, a file
    size cap HTML does not express) is skipped rather than guessed.
    """
    if not token:
        return True
    if token == "required":
        return bool(field.get("required"))
    if token == "not_required":
        return not field.get("required")
    if token == "maxlength":
        return bool(field.get("maxlength"))
    if token == "range":
        return bool(field.get("min") or field.get("max"))
    if token == "step":
        return bool(field.get("step"))
    if token == "options":
        return bool(field.get("options"))
    if token == "accept":
        return bool(field.get("accept"))
    if token == "sibling_date":
        dates = [f for f in (form.get("fields") or [])
                 if control_type(f) == "date_input"]
        return len(dates) > 1
    return False


def _has_grid_evidence(token: str | None, table: dict,
                       controls: dict) -> bool:
    """Whether the parsed grid justifies a ``list_surface`` check.

    Split from :func:`_has_evidence` because the two draw on different
    inventories: a field case asks a control about itself, a grid case
    asks the table *and* the controls the page renders around it.

    Unknown tokens return False, so the checks the coverage model carries
    for surfaces HTML cannot express — Group By, Advanced Search,
    favourite filters, the column picker, the record counter — stay out
    of a crawled site's pack instead of being invented.
    """
    if not token:
        return True
    table = table or {}
    controls = controls or {}
    if token == "columns":
        return len(table.get("columns") or []) >= 2
    if token == "rows":
        return int(table.get("row_count") or 0) > 0
    if token == "row_links":
        return bool(table.get("row_links"))
    if token == "checkboxes":
        return bool(table.get("has_checkboxes"))
    if token == "select_all":
        return bool(table.get("select_all"))
    if token == "sortable":
        return bool(table.get("sortable_columns"))
    if token == "pagination":
        return bool(controls.get("pagination"))
    if token == "search":
        return bool(controls.get("search"))
    if token == "filters":
        return bool(controls.get("filters"))
    if token == "bulk_actions":
        return bool(controls.get("bulk_actions"))
    if token == "create_control":
        return bool(controls.get("create_controls"))
    return False


# ── Naming ───────────────────────────────────────────────────────────

#: Trailing decoration real markup puts on labels: "Customer name:",
#: "E-mail *". Quoting it back into a step reads as if the colon were
#: part of the control name.
_LABEL_TRIM = " \t:*\u2022-\u2013\u2014"


def field_label(field: dict) -> str:
    """Human name for a control, preferring what a tester can see."""
    for key in ("label", "placeholder", "name", "id"):
        val = str(field.get(key) or "").strip().rstrip(_LABEL_TRIM).strip()
        if val:
            return val[:60]
    return "unnamed field"


def _control_noun(kind: str) -> str:
    return {
        "dropdown": "drop-down",
        "checkbox": "checkbox",
        "file_upload": "file field",
        "rich_text": "text area",
        "date_input": "date field",
        "numeric_input": "field",
        "email_input": "field",
    }.get(kind, "field")


def surface_name(page: dict, form: dict, index: int) -> str:
    """Section name, taken from what the page calls itself.

    House style wants sections named after the UI surface, so the form's
    own heading beats the URL. Falls back through h1 → title → an
    ordinal, never producing a bare "Functional".
    """
    for candidate in (form.get("heading"), page.get("h1"), page.get("title")):
        text = " ".join(str(candidate or "").split())
        if text:
            return text[:80]
    # Nothing on the page names itself — httpbin's form page has no <h1>
    # and no <title>, and "Form #1" tells a reader nothing. The URL path
    # is the last honest signal before falling back to an ordinal.
    titled = _url_label(page)
    if titled:
        # "forms/post" already says form; "Forms post form" does not.
        if "form" in titled.lower():
            return titled
        return titled + " form"
    return f"Form #{index}"


def _url_label(page: dict) -> str:
    """The URL path, humanised — "/checkout/step-2" -> "Checkout step 2"."""
    path = re.sub(r"^https?://[^/]+", "",
                  str(page.get("url") or "")).strip("/")
    if not path:
        return ""
    words = [w for w in re.split(r"[/\-_.]+", path) if w]
    if not words:
        return ""
    label = " ".join(words).strip()
    return (label[:1].upper() + label[1:])[:80]


#: Words that already say "this is a record list" — appending " grid" to
#: a caption that reads "Order list" would just stutter.
_GRID_WORDS = ("grid", "list", "table", "index", "results")

#: Section names go into every summary, so they stay shorter than the
#: form-side cap: a grid title also carries a column or action name.
_GRID_NAME_CAP = 60


def grid_section_name(page: dict, table: dict, index: int) -> str:
    """Section name for a grid, taken from what the page calls it.

    A ``<caption>`` is the table naming itself and wins outright. Without
    one the page heading is the next honest signal, but it names the
    whole page rather than the grid, so it takes a " grid" suffix — on a
    page carrying both a form and a grid the two sections have to be
    tellable apart.
    """
    caption = " ".join(str(table.get("caption") or "").split())
    if caption:
        return caption[:_GRID_NAME_CAP]
    for candidate in (page.get("h1"), page.get("title")):
        text = " ".join(str(candidate or "").split())
        if text:
            return _with_grid_suffix(text)
    titled = _url_label(page)
    if titled:
        return _with_grid_suffix(titled)
    return f"Grid #{index}"


def grid_section_names(page: dict, tables: list) -> list[str]:
    """Section names for one page's grids, disambiguated.

    Captionless grids all fall back to the page heading, so a page with
    two of them produced two identical section names and, with them,
    pairs of summaries a reader could not tell apart — w3schools'
    html_tables page ships exactly that. An ordinal is the honest
    discriminator: the page gives no other name to use.
    """
    names = [grid_section_name(page, t if isinstance(t, dict) else {}, i)
             for i, t in enumerate(tables or [], start=1)]
    totals: dict[str, int] = {}
    for name in names:
        totals[name] = totals.get(name, 0) + 1
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if totals[name] == 1:
            out.append(name)
            continue
        seen[name] = seen.get(name, 0) + 1
        out.append(f"{name} #{seen[name]}")
    return out


def _with_grid_suffix(text: str) -> str:
    if any(word in text.lower() for word in _GRID_WORDS):
        return text[:_GRID_NAME_CAP]
    return (text[:_GRID_NAME_CAP - 5] + " grid")


def _nav(page: dict, form: dict) -> str:
    """Step 1: breadcrumb to the surface under test."""
    url = str(page.get("url") or "").strip()
    heading = " ".join(str(form.get("heading") or "").split())
    if url and heading:
        return f"Go to {url} -> {heading}"
    if url:
        return f"Go to {url}"
    return navigation_step("")


# ── Concrete test data per control type ──────────────────────────────

def _sample(kind: str, field: dict) -> str:
    """A value a tester can actually type for the *valid* case.

    Deliberately independent of ``maxlength``: a valid value is an
    ordinary one. Describing it as "a 60-character value" would quietly
    turn the happy path into the boundary case, which has its own entry.
    """
    if kind == "email_input":
        return "tester@example.com"
    if kind == "numeric_input":
        lo = field.get("min")
        return str(lo) if lo not in (None, "") else "42"
    if kind == "date_input":
        return "tomorrow's date"
    if kind == "dropdown":
        opts = field.get("options") or []
        return str(opts[0]) if opts else "any available option"
    if kind == "file_upload":
        return "sample.pdf (1 MB)"
    return "Sample value 123"


def _test_data(kind: str, field: dict, objective: str) -> str:
    """Fill the Test Data column only where the case turns on a value."""
    low = objective.lower()
    bits: list[str] = []
    if "maximum allowed length" in low and field.get("maxlength"):
        bits.append(f"Exactly {field['maxlength']} characters")
    elif "one character over" in low and field.get("maxlength"):
        n = field["maxlength"]
        try:
            bits.append(f"{int(n) + 1} characters (maxlength is {n})")
        except (TypeError, ValueError):
            bits.append(f"One character over maxlength={n}")
    elif "minimum and the maximum" in low:
        bits.append(f"min={field.get('min')}, max={field.get('max')}")
    elif "inside the allowed range" in low:
        bits.append(f"A value between {field.get('min')} and "
                    f"{field.get('max')}")
    elif "negative value and a very large" in low:
        bits.append("-1 and 999999999")
    elif "letters into the numeric" in low:
        bits.append("abc")
    elif "no @, no domain" in low:
        bits.append("tester@, @example.com, tester.example.com")
    elif "non-latin" in low:
        bits.append("Тестове значення, 测试")
    elif "whitespace only" in low:
        bits.append("Three spaces")
    elif "existing value" in low or "typing a prefix" in low:
        opts = field.get("options") or []
        if opts:
            bits.append("Options: " + " / ".join(str(o) for o in opts[:6]))
    elif "rejected type" in low and field.get("accept"):
        bits.append(f"A type outside accept=\"{field['accept']}\"")
    elif "valid" in low:
        bits.append(f"{field_label(field)}: {_sample(kind, field)}")
    return "; ".join(b for b in bits if b)


# ── Case construction ────────────────────────────────────────────────

def _field_case(page: dict, form: dict, field: dict, kind: str,
                rule: dict, section: str) -> AuthoredCase:
    """One per-control case, phrased in the house grammar."""
    label = field_label(field)
    noun = _control_noun(kind)
    objective = str(rule.get("objective") or "").strip()
    category = str(rule.get("category") or "Positive").strip().title()

    fmt = {"label": label, "noun": noun, "section": section}

    # Phrasing lives in the YAML next to the rule. A `title:` template is
    # used verbatim; without one the generic composition below reads
    # acceptably ("Enter a valid value" -> "Verify that User can enter a
    # valid value in the ... field"). Composing every title generically
    # produced clumsy lines and duplicated nouns ("select an existing
    # value from the drop-down in the ... drop-down").
    title_tpl = str(rule.get("title") or "").strip()
    if title_tpl:
        summary = title_tpl.format(**fmt)
    else:
        summary = (f'Verify that User can {objective[0].lower()}'
                   f'{objective[1:]} in the "{label}" {noun}')

    step_tpl = str(rule.get("step") or "").strip()
    action = (step_tpl.format(**fmt) if step_tpl
              else f'{objective} in the "{label}" {noun}')

    steps = [_nav(page, form)]
    if category == "Negative":
        # Isolate the control under test: everything else stays valid, or
        # a failure cannot be attributed to this field.
        steps.append("Fill every other required control with a valid value")
    steps.append(action)
    submit = str(form.get("submit_text") or "").strip()
    steps.append(f'Click on the "{submit}" button' if submit
                 else "Submit the form")

    if category == "Negative":
        expected = (
            f'The form is not submitted. The "{label}" {noun} is '
            f'highlighted and a validation message names it. Nothing is '
            f'persisted.')
    else:
        expected = (
            f'The value is accepted in the "{label}" {noun} and no '
            f'validation message is shown for it.')

    case = AuthoredCase(
        summary=summary,
        preconditions=f"{section} is opened.",
        steps=steps,
        test_data=_test_data(kind, field, objective),
        expected_result=expected,
        category=category,
        priority="High" if field.get("required") else "Medium",
        section=section,
        testing_type="Functional",
    )
    case, _ = normalise_case(case)
    return case


def _form_level_cases(page: dict, form: dict, section: str,
                      required: list[dict]) -> list[AuthoredCase]:
    """The whole-form cases: happy path, required sweep, save, discard."""
    out: list[AuthoredCase] = []
    submit = str(form.get("submit_text") or "").strip() or "Submit"
    nav = _nav(page, form)
    req_names = ", ".join(f'"{field_label(f)}"' for f in required[:6])

    out.append(AuthoredCase(
        summary=f"Verify that User can submit {section} with the required "
                f"controls filled",
        preconditions=f"{section} is opened.",
        steps=[nav,
               "Fill every required control with a valid value",
               f'Click on the "{submit}" button'],
        test_data=("Required: " + req_names) if req_names else "",
        expected_result=("The form is submitted and the confirmation the "
                         "page defines is rendered. No validation message "
                         "remains on screen."),
        category="Positive", priority="High", section=section,
    ))

    if required:
        out.append(AuthoredCase(
            summary=f"Verify that User cannot submit {section} with the "
                    f"required controls left empty",
            preconditions=f"{section} is opened.",
            steps=[nav,
                   "Leave every required control empty",
                   f'Click on the "{submit}" button'],
            test_data=("Required: " + req_names) if req_names else "",
            expected_result=("The form is not submitted. Every empty "
                             "required control is highlighted and a "
                             "validation message names it. Nothing is "
                             "persisted."),
            category="Negative", priority="High", section=section,
        ))
        out.append(AuthoredCase(
            summary=f"Verify that the required controls on {section} are "
                    f"marked before submission",
            preconditions=f"{section} is opened.",
            steps=[nav, "Pay attention to the required controls"],
            test_data=("Required: " + req_names) if req_names else "",
            expected_result=("Every required control carries a visible "
                             "required marker before any submission "
                             "attempt."),
            category="Positive", priority="Medium", section=section,
        ))

    return [normalise_case(c)[0] for c in out]


# ── Grid cases ───────────────────────────────────────────────────────

#: ``fan_out:`` names an inventory list; each entry fills one fixed
#: placeholder and produces one case. Which list lives on the table and
#: which on the surrounding controls is fixed here rather than in the
#: YAML — the asset says *what* to iterate, this says *where it is*.
_FAN_OUT_SPEC = {
    "sortable_columns": ("column", "table"),
    "filters": ("filter", "controls"),
    "bulk_actions": ("action", "controls"),
    "create_controls": ("control", "controls"),
}

#: A quoted value inside a summary; keeps the 220-char lint ceiling out
#: of reach even with a long section name.
_FAN_OUT_VALUE_CAP = 40


def _fmt(template: Any, values: dict) -> str:
    """Fill a YAML phrasing template, or return "" if it cannot be."""
    text = str(template or "").strip()
    if not text:
        return ""
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        _logger.warning("tc_rules: unusable template %r (%s)", text[:60], exc)
        return ""


def _fan_out_values(rule: dict, table: dict,
                    controls: dict) -> list[tuple[str, str] | None]:
    """The (placeholder, value) pairs a check fans out over.

    ``[None]`` means "emit exactly one case, no fan-out".
    """
    key = str(rule.get("fan_out") or "").strip()
    if not key:
        return [None]
    spec = _FAN_OUT_SPEC.get(key)
    if not spec:
        _logger.warning("tc_rules: unknown fan_out list %r on check %r",
                        key, rule.get("id"))
        return []
    placeholder, source = spec
    inventory = (table if source == "table" else controls).get(key) or []
    values = [" ".join(str(v).split())[:_FAN_OUT_VALUE_CAP]
              for v in inventory if str(v).strip()]
    if len(values) > MAX_FAN_OUT:
        _logger.info("tc_rules: check %r fans out over %d %s, kept %d "
                     "(cap %d)", rule.get("id"), len(values), key,
                     MAX_FAN_OUT, MAX_FAN_OUT)
        values = values[:MAX_FAN_OUT]
    return [(placeholder, v) for v in values]


def _grid_test_data(rule: dict, controls: dict, values: dict) -> str:
    """Fill the Test Data column where the case turns on a named value."""
    bits: list[str] = []
    for placeholder, caption in (("column", "Column"), ("filter", "Filter"),
                                 ("action", "Bulk action"),
                                 ("control", "Control")):
        if values.get(placeholder):
            bits.append(f'{caption}: "{values[placeholder]}"')
    rule_id = str(rule.get("id") or "")
    if rule_id in ("reach", "row_rendering"):
        bits.append("Columns: " + values["columns"])
    elif rule_id.startswith("pagination"):
        labels = [str(x) for x in (controls.get("pagination_labels") or [])]
        if labels:
            bits.append("Pager controls: " + " / ".join(labels[:6]))
    return "; ".join(bits)


def enumerate_grid(page: dict, table: dict, controls: dict, section: str,
                   checks: list[dict]) -> list[AuthoredCase]:
    """Every ``list_surface`` case the parsed grid actually justifies.

    ``controls`` is page-level: the crawler walks a token stream, not a
    tree, so "the pager below this table" is not knowable — what is
    knowable is "this page renders a pager". On a page with two grids
    both inherit it. The alternative, dropping the surrounding controls
    entirely, would lose sorting, paging and bulk actions on every real
    grid, which is most of what a list surface is.
    """
    row_count = int(table.get("row_count") or 0)
    columns = [str(c) for c in (table.get("columns") or []) if str(c).strip()]
    base: dict[str, Any] = {
        "grid": section,
        "columns": (", ".join(f'"{c}"' for c in columns[:8])
                    or "the columns it declares"),
        "row_count": row_count,
        "search": (list(controls.get("search_labels") or []) or ["Search"])[0],
        "column": "", "filter": "", "action": "", "control": "",
    }
    nav = _grid_nav(page, section)
    precondition = (f"{section} is opened and holds at least one record."
                    if row_count else f"{section} is opened.")

    out: list[AuthoredCase] = []
    for rule in checks:
        if not isinstance(rule, dict):
            continue
        if not _has_grid_evidence(rule.get("requires"), table, controls):
            continue
        for pair in _fan_out_values(rule, table, controls):
            values = dict(base)
            if pair is not None:
                values[pair[0]] = pair[1]
            case = _grid_case(rule, values, controls, nav, precondition,
                              section)
            if case is not None:
                out.append(case)
    return out


def _grid_case(rule: dict, values: dict, controls: dict, nav: str,
               precondition: str, section: str) -> AuthoredCase | None:
    """One grid case, or None when the asset cannot phrase it."""
    summary = _fmt(rule.get("title"), values)
    action = _fmt(rule.get("step"), values)
    expected = _fmt(rule.get("expected"), values)
    if not (summary and action and expected):
        # The evidence was there but the phrasing was not. Emitting a
        # half-written case would be worse than emitting none, so say so
        # loudly instead of dropping it silently.
        _logger.warning("tc_rules: check %r has evidence but no %s template",
                        rule.get("id"),
                        "title" if not summary
                        else ("step" if not action else "expected"))
        return None

    case = AuthoredCase(
        summary=summary,
        preconditions=precondition,
        steps=[nav, action],
        test_data=_grid_test_data(rule, controls, values),
        expected_result=expected,
        category=str(rule.get("category") or "Positive").strip().title(),
        priority=str(rule.get("priority") or "Medium").strip().title(),
        section=section,
        testing_type="Functional",
    )
    case, _ = normalise_case(case)
    return case


def _grid_nav(page: dict, section: str) -> str:
    """Step 1: breadcrumb to the grid."""
    url = str(page.get("url") or "").strip()
    if url:
        return f"Go to {url} -> {section}"
    return navigation_step("")


# ── Public API ───────────────────────────────────────────────────────

def enumerate_from_pages(pages: list[dict]) -> list[AuthoredCase]:
    """Deterministic case pack from a crawler control inventory.

    Walks two surfaces per page, in the order ``enumeration_order``
    declares: the data grids the crawler recognised, then the forms.
    Returns [] when neither is present — there is nothing to enumerate
    honestly, and a caller should keep whatever baseline coverage it
    already has rather than receive invented cases.
    """
    rules = load_rules()
    per_type = (((rules.get("create_form") or {})
                 .get("per_control_type")) or {})
    grid_checks = [c for c in (((rules.get("list_surface") or {})
                                .get("checks")) or []) if isinstance(c, dict)]
    if not per_type and not grid_checks:
        _logger.warning("tc_rules: coverage model has neither "
                        "create_form.per_control_type nor list_surface.checks")
        return []

    out: list[AuthoredCase] = []
    forms_seen = 0
    grids_seen = 0
    capped_forms = capped_grids = False

    for page in pages or []:
        controls = page.get("grid_controls") or {}
        tables = page.get("tables") or []
        sections = grid_section_names(page, tables)
        for table, section in zip(tables, sections):
            if not isinstance(table, dict):
                continue
            if grids_seen >= MAX_GRIDS:
                if not capped_grids:
                    _logger.info("tc_rules: stopped after %d grids (cap)",
                                 MAX_GRIDS)
                    capped_grids = True
                break
            grids_seen += 1
            grid_cases = enumerate_grid(page, table, controls, section,
                                        grid_checks)
            if len(grid_cases) > MAX_CASES_PER_GRID:
                _logger.info(
                    "tc_rules: %s produced %d grid cases, kept %d (cap %d)",
                    section, len(grid_cases), MAX_CASES_PER_GRID,
                    MAX_CASES_PER_GRID)
                grid_cases = grid_cases[:MAX_CASES_PER_GRID]
            out.extend(grid_cases)

        for index, form in enumerate(page.get("forms") or [], start=1):
            if forms_seen >= MAX_FORMS:
                if not capped_forms:
                    _logger.info("tc_rules: stopped after %d forms (cap)",
                                 MAX_FORMS)
                    capped_forms = True
                break
            fields = group_radios([f for f in (form.get("fields") or [])
                                   if isinstance(f, dict)])
            fields = [f for f in fields if control_type(f)]
            if not fields:
                continue
            forms_seen += 1
            section = surface_name(page, form, index)
            required = [f for f in fields if f.get("required")]

            form_cases = _form_level_cases(page, form, section, required)
            field_cases: list[AuthoredCase] = []
            for field in fields:
                for kind in control_types(field):
                    spec = per_type.get(kind) or {}
                    for rule in spec.get("cases") or []:
                        if not _has_evidence(rule.get("requires"),
                                             field, form):
                            continue
                        field_cases.append(_field_case(
                            page, form, field, kind, rule, section))

            budget = MAX_CASES_PER_FORM - len(form_cases)
            if len(field_cases) > budget:
                _logger.info(
                    "tc_rules: %s produced %d field cases, kept %d (cap %d)",
                    section, len(field_cases), max(budget, 0),
                    MAX_CASES_PER_FORM)
                field_cases = field_cases[:max(budget, 0)]
            out.extend(form_cases)
            out.extend(field_cases)

    return out


def enumerate_from_artifacts(artifacts: Any) -> list[AuthoredCase]:
    """Convenience wrapper over :class:`engine.tc_author.Artifacts`."""
    pages = getattr(artifacts, "pages", None) or []
    return enumerate_from_pages(pages)


__all__ = [
    "enumerate_from_artifacts", "enumerate_from_pages", "enumerate_grid",
    "control_type", "control_types", "field_label", "group_radios",
    "surface_name", "grid_section_name", "grid_section_names", "load_rules",
    "MAX_CASES_PER_FORM", "MAX_FORMS",
    "MAX_CASES_PER_GRID", "MAX_GRIDS", "MAX_FAN_OUT",
]
