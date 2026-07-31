"""TestFortge — deterministic low-level checklist generator (no model).

Walks the crawler's page records into the shape of the team's own reviewed
deliverable: a Header section, one content section per surface, a Footer
section, hierarchical numbering, one observable check per row.

Why this exists alongside :mod:`engine.checklist_author`
--------------------------------------------------------
Same trade as :mod:`engine.tc_rules` versus :mod:`engine.tc_author`. The
authoring agent writes better prose and can judge which sections matter,
but it needs a paid LLM. This module cannot judge relevance — what it can
do is *enumerate*, and a low-level checklist is an enumeration by
definition. The reference deliverable spends 13 of its 57 rows on a single
contact form; that volume comes from walking every field, not from
insight.

Evidence discipline
-------------------
Every row is gated on something the markup actually said. No header logo
in the markup means no "clicking the logo opens the Homepage" row. No
``required`` attribute means no empty-value row. A site that does not
name its social links gets no social row rather than a guessed one — the
anti-pattern ``checklist_style.yaml`` calls "inventing UI the artifacts do
not evidence".

Shape (measured from the reference — see
``qa_knowledge/style/checklist_style.yaml`` for the full provenance):

    1   Header              7 checks in the reference
    2   Page Content       38 checks
    3   Footer             12 checks
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from engine import glossary
from engine.log import get_logger

_logger = get_logger(__name__)

#: Content sections beyond this are dropped and reported as a gap. A
#: checklist nobody can walk in a sitting is not a deliverable.
MAX_CONTENT_SECTIONS = 12
#: Per-surface caps. The reference spends 38 rows on one page; a 50-heading
#: marketing page would otherwise bury the form checks that matter.
MAX_HEADINGS_PER_SECTION = 24
MAX_FIELD_CHECKS_PER_FORM = 40


@dataclass
class Check:
    """One row of the checklist."""
    objective: str
    section: str
    category: str = "Positive"
    priority: str = "Medium"
    testing_type: str = "Functional"
    #: 2 = a check, 3 = a sub-check of the preceding level-2 row. Level 1
    #: is the section row itself and is not a Check.
    depth: int = 2
    #: Assigned by :func:`assign_numbers` — "1.1", "2.7.1".
    number: str = ""


@dataclass
class Section:
    name: str
    checks: list[Check] = field(default_factory=list)
    number: str = ""


@dataclass
class LowLevelChecklist:
    #: '"Mobile Application Testing Services" page' — the scope banner.
    surface: str = ""
    url: str = ""
    sections: list[Section] = field(default_factory=list)
    #: Surfaces or controls the crawl could not evidence. Surfaced to the
    #: operator rather than silently omitted.
    gaps: list[str] = field(default_factory=list)
    source: str = "deterministic"

    def all_checks(self) -> list[Check]:
        return [c for s in self.sections for c in s.checks]

    @property
    def total(self) -> int:
        return sum(len(s.checks) for s in self.sections)


# ── Numbering ────────────────────────────────────────────────────────

def assign_numbers(checklist: LowLevelChecklist) -> LowLevelChecklist:
    """Stamp hierarchical numbers onto sections and checks.

    Section ``n``, level-2 check ``n.m``, level-3 sub-check ``n.m.k``. A
    level-3 row that appears before any level-2 row in its section is
    promoted rather than dropped — a stray sub-check is a content bug, and
    losing the row would hide it.
    """
    for s_ix, section in enumerate(checklist.sections, start=1):
        section.number = str(s_ix)
        level2 = 0
        level3 = 0
        for check in section.checks:
            if check.depth >= 3 and level2 > 0:
                level3 += 1
                check.number = f"{s_ix}.{level2}.{level3}"
            else:
                level2 += 1
                level3 = 0
                check.depth = 2
                check.number = f"{s_ix}.{level2}"
    return checklist


# ── Helpers ──────────────────────────────────────────────────────────

def _clean(text: Any, limit: int = 160) -> str:
    """Collapse whitespace and clip. Crawled labels arrive with newlines."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit].rstrip()


def _objective(text: str) -> str:
    """Normalise one objective through the terminology rules."""
    return glossary.normalise_text(_clean(text, 220), kind="objective")


def _page_name(url: str, title: str = "") -> str:
    """Human name for a surface: its title, else its path, else the host."""
    if title:
        return _clean(title, 70)
    path = (urlparse(url).path or "").strip("/")
    if path:
        last = path.split("/")[-1]
        return last.replace("-", " ").replace("_", " ").strip().title()
    return _clean(urlparse(url).netloc or url, 70)


def _destination_name(href: str) -> str:
    """Name of the page an href points at, from its last path segment."""
    try:
        path = (urlparse(href).path or "").strip("/")
    except Exception:  # pragma: no cover — defensive
        return ""
    if not path:
        return "Homepage"
    last = path.split("/")[-1]
    last = re.sub(r"\.(html?|php|aspx?)$", "", last, flags=re.I)
    return last.replace("-", " ").replace("_", " ").strip().title()


def _is_cta(label: str) -> bool:
    return bool(re.search(
        r"contact|talk|touch|quote|demo|start|book|call|consult|hire|"
        r"get\s+in|request|estimate", label, re.IGNORECASE))


#: A control whose only name is a symbol, a single letter or a random
#: token is not something a tester can be asked to fill in. Real pages
#: ship these constantly — a decorative "Δ", a CSRF token, a honeypot
#: input — and a row naming one is a row that cannot be executed.
#: ``^_`` catches framework internals wholesale — Contact Form 7 ships
#: ``_wpcf7_ak_hp_textarea`` (an Akismet honeypot), Rails ships
#: ``_method``. No user-facing control is named with a leading underscore.
_JUNK_LABEL_RE = re.compile(
    r"^[\W_]*$|^.{1,2}$|^[0-9a-f]{16,}$|^_", re.I)
_HONEYPOT_RE = re.compile(
    r"honey|_hp_|\bhp\b|captcha|csrf|xsrf|nonce|_token|recaptcha|antispam|"
    r"wpcf7|ak_js|^utm_|^gclid$|^hidden", re.IGNORECASE)


def _field_label(fld: dict) -> str:
    """The name a tester would use for this control, or ``""`` if none.

    Returns empty rather than a placeholder when every candidate name is
    junk — the caller then skips the field instead of emitting a row nobody
    can act on.
    """
    for key in ("label", "aria_label", "placeholder", "name", "id"):
        val = _clean(fld.get(key), 60)
        if val and not _JUNK_LABEL_RE.match(val):
            return val
    return ""


def _is_noise_field(fld: dict, label: str) -> bool:
    """True for controls a checklist must not name."""
    if not label:
        return True
    blob = " ".join(str(fld.get(k) or "") for k in ("name", "id", "class"))
    return bool(_HONEYPOT_RE.search(blob) or _HONEYPOT_RE.search(label))


def _form_signature(form: dict, fields: list[dict]) -> tuple:
    """Identity of a form by where it posts and what it declares.

    Marketing pages render the same contact form three or four times — once
    per CTA. The reference deliverable writes ONE form block and
    disambiguates with an "in the Contact section" suffix, so emitting a
    15-row block per copy is 45 wasted rows a reviewer would send back.

    Keyed on ``(action, field-type multiset)`` rather than on labels: the
    copies of one form routinely differ by a missing ``aria-label`` or a
    placeholder, and a label-sensitive key let two identical copies through
    as separate forms.
    """
    # Strip the fragment and query: Contact Form 7 gives each placement of
    # the same form a unique anchor ("…#wpcf7-f42-o1", "-o2", "-o3"), which
    # made three identical copies look like three different forms.
    action = _clean(form.get("action"), 200).lower()
    action = action.split("#")[0].split("?")[0].rstrip("/")
    types = tuple(sorted(str(f.get("type") or "text").lower()
                         for f in fields))
    if action:
        return (action, types)
    # No action attribute — fall back to labels so two genuinely different
    # unrouted forms are not collapsed into one.
    return (types, tuple(sorted(_field_label(f).lower() for f in fields)))


def _guess_form_name(form: dict, fields: list[dict], submit: str) -> str:
    """A name a tester would recognise, from what the form declares."""
    for key in ("name", "id"):
        val = _clean(form.get(key), 40)
        if val and not _JUNK_LABEL_RE.match(val):
            return val.replace("-", " ").replace("_", " ").title()
    types = {str(f.get("type") or "text").lower() for f in fields}
    labels = " ".join(_field_label(f).lower() for f in fields)
    if "password" in types:
        return "Sign Up" if labels.count("password") > 1 else "Log In"
    if "search" in types or re.search(r"\bsearch\b", labels):
        return "Search"
    if "email" in types and re.search(
            r"message|description|comment|project|inquiry|enquiry", labels):
        return "Contact"
    if _is_cta(submit) or re.search(r"subscribe|newsletter", labels + submit,
                                    re.I):
        return "Subscribe" if re.search(r"subscribe|newsletter",
                                        labels + submit, re.I) else "Contact"
    if "email" in types:
        return "Contact"
    return ""


# ── Header ───────────────────────────────────────────────────────────

def header_section(page: dict) -> Section:
    """The shared chrome sweep. Reference rows 1.1 … 1.7."""
    sec = Section(name="Header")
    add = sec.checks.append

    if page.get("has_header_logo"):
        add(Check(_objective(
            "Verify that the Homepage is opened after clicking the logo"),
            "Header", priority="High"))

    groups = [g for g in (page.get("nav_groups") or [])
              if isinstance(g, dict) and g.get("region") == "header"
              and g.get("label")]
    for grp in groups:
        label = _clean(grp["label"], 60)
        add(Check(_objective(
            f'Verify that all sub-items are visible and clickable from '
            f'the "{label}" drop-down menu'), "Header", priority="High"))

    # Top-level links the menus do not already cover. The reference lists
    # these separately ('Verify that the user is redirected to the "Cases"
    # page after clicking the "Case Studies" link') because they are one
    # click, not a menu.
    #
    # The ``in_menu`` flag from the crawler is the authority on which links
    # sit inside a drop-down. Filtering against the menus' `children` lists
    # instead does not work — those are capped at 20 per menu and a
    # mega-menu overflows the cap, which is how an early version produced a
    # 16-row Header where the reference writes 7.
    grouped = {_clean(g.get("label"), 60) for g in groups}
    plain = 0
    for link in (page.get("header_links") or []):
        if plain >= 6:
            break
        text = _clean(link.get("text"), 60)
        href = str(link.get("href") or "")
        if not text or text in grouped or _is_cta(text) or link.get("in_menu"):
            continue
        dest = _destination_name(href)
        if not dest:
            continue
        grouped.add(text)
        plain += 1
        add(Check(_objective(
            f'Verify that the user is redirected to the "{dest}" page '
            f'after clicking the "{text}" link'), "Header"))

    ctas = [_clean(link.get("text"), 60)
            for link in (page.get("header_links") or [])
            if _is_cta(_clean(link.get("text"), 60))]
    ctas += [_clean(b, 60) for b in (page.get("buttons") or [])
             if _is_cta(_clean(b, 60))]
    seen: set[str] = set()
    for label in ctas:
        if not label or label in seen:
            continue
        seen.add(label)
        add(Check(_objective(
            f"Verify that the [{label}] button opens the Contact Form"),
            "Header", priority="High"))
        if len(seen) >= 3:
            break

    return sec


# ── Footer ───────────────────────────────────────────────────────────

def footer_section(page: dict) -> Section:
    """The shared chrome sweep. Reference rows 3.1 … 3.12."""
    sec = Section(name="Footer")
    add = sec.checks.append

    if page.get("has_footer_logo"):
        add(Check(_objective(
            "Verify that the Homepage is opened after clicking the logo"),
            "Footer"))

    if page.get("email_links"):
        add(Check(_objective(
            "Verify that clicking the email opens the default mail client"),
            "Footer"))
    if page.get("phone_links"):
        add(Check(_objective(
            "Verify that clicking the phone number opens the call "
            "application"), "Footer"))

    socials = [_clean(s.get("network"), 30)
               for s in (page.get("social_links") or [])
               if isinstance(s, dict) and s.get("network")]
    if socials:
        # One row naming the networks found, exactly as the reference does
        # — not a generic "social icons" row, and not one row each.
        names = ", ".join(socials[:8])
        add(Check(_objective(
            f"Verify that clicking each social media icon ({names}) opens "
            f"the corresponding page"), "Footer"))

    for grp in (page.get("nav_groups") or []):
        if not isinstance(grp, dict) or grp.get("region") != "footer":
            continue
        label = _clean(grp.get("label"), 60)
        if not label:
            continue
        add(Check(_objective(
            f'Verify that all sub-items are visible and clickable from '
            f'the "{label}" menu'), "Footer"))

    for legal in (page.get("legal_links") or []):
        text = _clean((legal or {}).get("text"), 60) \
            or _destination_name(str((legal or {}).get("href") or ""))
        if not text:
            continue
        if any(text.lower() in c.objective.lower() for c in sec.checks):
            continue
        add(Check(_objective(
            f'Verify that clicking the "{text}" link redirects to the '
            f'{text} page'), "Footer"))

    return sec


def _footer_gap(page: dict, section: Section) -> str:
    """Report a fat but unstructured Footer instead of quietly thinning it.

    The reference gives the Footer 12 rows, three of them one-per-menu. A
    site whose Footer is a flat pile of anchors yields none of those, and
    the operator should hear that from us rather than count the rows.
    """
    links = len(page.get("footer_links") or [])
    groups = sum(1 for g in (page.get("nav_groups") or [])
                 if isinstance(g, dict) and g.get("region") == "footer")
    if links >= 12 and groups == 0:
        return (f"The Footer declares {links} links but no menu structure "
                f"the markup exposes, so the per-menu rows could not be "
                f"derived — {len(section.checks)} Footer rows instead of the "
                f"usual 12. Group them in the UI or add them by hand.")
    return ""


# ── Page content ─────────────────────────────────────────────────────

def content_section(page: dict, *, name: str = "Page Content") -> Section:
    """One section per surface: its headed blocks, grids and forms."""
    sec = Section(name=name)
    add = sec.checks.append

    title = _clean(page.get("title"), 90)
    h1 = _clean(page.get("h1"), 90)
    if h1:
        add(Check(_objective(
            f'Verify that the "{h1}" heading is visible and matches the '
            f"design"), name, priority="High"))
    elif title:
        add(Check(_objective(
            f'Verify that the "{title}" page title is displayed in the '
            f"browser tab"), name))

    # One level-2 row per h2, its h3s nested as level-3 rows. That IS the
    # reference structure: row 2.3 is the "Mobile Testing by Platform"
    # section and 2.3.1 is the behaviour of the blocks inside it. Falling
    # back to the flat `headings` list keeps older crawl records working.
    levels = [h for h in (page.get("heading_levels") or [])
              if isinstance(h, dict) and _clean(h.get("text"), 90)]
    if not levels:
        levels = [{"level": 2, "text": h}
                  for h in (page.get("headings") or [])]

    seen: set[str] = set()
    emitted = 0
    dropped = 0
    have_parent = False
    for entry in levels:
        text = _clean(entry.get("text"), 90)
        level = int(entry.get("level") or 2)
        if not text or text == h1 or text in seen:
            continue
        seen.add(text)
        if emitted >= MAX_HEADINGS_PER_SECTION:
            dropped += 1
            continue
        if level <= 2:
            add(Check(_objective(
                f'Verify that the "{text}" section is visible and matches '
                f"the design"), name))
            have_parent = True
        else:
            # A sub-heading is a block inside the section above it, so the
            # row asserts the block, not the whole section.
            add(Check(_objective(
                f'Verify that the "{text}" block is visible and matches the '
                f"design"), name, depth=3 if have_parent else 2))
        emitted += 1
    if dropped:
        _logger.info("checklist_rules: %d headings beyond the cap on %s",
                     dropped, page.get("url"))

    _add_grid_checks(page, sec, name)
    _add_form_checks(page, sec, name)
    return sec


def _add_grid_checks(page: dict, sec: Section, name: str) -> None:
    add = sec.checks.append
    controls = page.get("grid_controls") or {}
    for table in (page.get("tables") or []):
        if not isinstance(table, dict):
            continue
        caption = _clean(table.get("caption"), 60)
        label = f'"{caption}" grid' if caption else "data grid"
        add(Check(_objective(
            f"Verify that the {label} is displayed with every column it "
            f"declares"), name, priority="High"))
        for col in [str(c) for c in (table.get("sortable_columns") or [])][:6]:
            add(Check(_objective(
                f'Verify that the {label} is sorted by the "{_clean(col, 40)}"'
                f" column after clicking its header"), name, depth=3))
        if table.get("row_links"):
            add(Check(_objective(
                f"Verify that the record details page is opened after "
                f"clicking a row of the {label}"), name, depth=3))
        if table.get("select_all"):
            add(Check(_objective(
                f"Verify that every row of the {label} is marked after "
                f"marking the header checkbox"), name, depth=3))
    if controls.get("pagination"):
        add(Check(_objective(
            "Verify that the next portion of records is displayed after "
            "clicking the pagination control"), name, depth=3))
    for flt in [str(f) for f in (controls.get("filters") or [])][:6]:
        add(Check(_objective(
            f'Verify that only matching records remain after applying the '
            f'"{_clean(flt, 40)}" filter'), name, depth=3))


def _add_form_checks(page: dict, sec: Section, name: str) -> None:
    """One level-2 row per form, one level-3 row per field per rule.

    This is where a low-level checklist earns its name — the reference
    spends 13 rows on one contact form.
    """
    add = sec.checks.append
    forms = [f for f in (page.get("forms") or []) if isinstance(f, dict)]

    # Deduplicate identical forms before spending rows on them. On the
    # reference page four copies of the same contact form are rendered, one
    # per CTA; expanding each produced 60 rows where the team writes 15.
    unique: list[tuple[dict, list[dict], int]] = []
    seen_sigs: dict[tuple, int] = {}
    for form in forms:
        fields = [x for x in (form.get("fields") or form.get("inputs") or [])
                  if isinstance(x, dict)]
        fields = [x for x in fields
                  if str(x.get("type") or "text").lower()
                  not in ("hidden", "submit", "button", "image", "reset")
                  and not _is_noise_field(x, _field_label(x))]
        if not fields:
            continue
        sig = _form_signature(form, fields)
        if sig in seen_sigs:
            unique[seen_sigs[sig]] = (
                unique[seen_sigs[sig]][0], unique[seen_sigs[sig]][1],
                unique[seen_sigs[sig]][2] + 1)
            continue
        seen_sigs[sig] = len(unique)
        unique.append((form, fields, 1))

    for f_ix, (form, fields, copies) in enumerate(unique, start=1):
        submit = _clean(form.get("submit_text"), 40) or "Submit"
        form_name = _guess_form_name(form, fields, submit)
        suffix = f" in the {form_name} section" if form_name else ""
        label = f"{form_name} Form" if form_name else f"Form #{f_ix}"

        add(Check(_objective(
            f"Verify that the {label} is displayed with every field it "
            f"declares"), name, priority="High"))
        if copies > 1:
            # Say it out loud rather than silently covering one copy: the
            # tester needs to know the same block appears N times.
            add(Check(_objective(
                f"Verify that all {copies} placements of the {label} on the "
                f"page behave identically"), name, depth=3))

        emitted = 0
        consent = False
        for fld in fields:
            if emitted >= MAX_FIELD_CHECKS_PER_FORM:
                break
            ftype = str(fld.get("type") or "text").lower()
            flabel = _field_label(fld)
            if ftype == "checkbox" and re.search(
                    r"agree|consent|policy|terms|privacy", flabel, re.I):
                consent = True
                continue

            add(Check(_objective(
                f"Verify that the {flabel} field accepts valid data{suffix}"),
                name, depth=3))
            emitted += 1

            if fld.get("required"):
                add(Check(_objective(
                    f"Verify that the {flabel} field does not accept an "
                    f"empty value on submit{suffix}"), name,
                    category="Negative", depth=3))
                emitted += 1

            fmt = _format_rule(ftype, fld)
            if fmt:
                add(Check(_objective(
                    f"Verify that an error message is shown for {fmt} in "
                    f"the {flabel} field{suffix}"), name,
                    category="Negative", depth=3))
                emitted += 1

        add(Check(_objective(
            f"Verify that the [{submit}] button is clickable{suffix}"),
            name, depth=3))
        if any(x.get("required") for x in fields):
            add(Check(_objective(
                f"Verify that the {label} is not submitted with empty "
                f"required fields{suffix}"), name,
                category="Negative", depth=3))
        if consent:
            add(Check(_objective(
                f"Verify that the {label} is not submitted without marking "
                f"the Consent checkbox{suffix}"), name,
                category="Negative", depth=3))
        add(Check(_objective(
            f"Verify that a success message is displayed after "
            f"submission{suffix}"), name, depth=3))


def _format_rule(ftype: str, fld: dict) -> str:
    """The broken-format phrase for a field, or ``""`` if none is evidenced."""
    if ftype == "email":
        return "an invalid email format"
    if ftype == "tel":
        return "an invalid phone number format"
    if ftype == "url":
        return "an invalid URL format"
    if ftype in ("number", "range"):
        if fld.get("min") not in (None, "") or fld.get("max") not in (None, ""):
            return "a value outside the allowed range"
        return "a non-numeric value"
    if fld.get("pattern"):
        return "a value that does not match the required format"
    if fld.get("maxlength"):
        return f"a value longer than {fld['maxlength']} characters"
    return ""


# ── Entry point ──────────────────────────────────────────────────────

def build_checklist(pages: list[dict], *, url: str = "") -> LowLevelChecklist:
    """Walk crawler pages into a numbered low-level checklist.

    Header and Footer come from the first page — the chrome is shared, and
    ``checklist_style.yaml`` says to reuse it rather than re-derive it per
    page. Each crawled surface then gets its own content section, because
    sections are surfaces a tester walks.
    """
    pages = [p for p in (pages or []) if isinstance(p, dict)]
    out = LowLevelChecklist(url=url or (pages[0].get("url") if pages else ""))
    if not pages:
        out.gaps.append(
            "No page was crawled, so no checklist could be derived from the "
            "product. Supply a reachable URL or attach the design.")
        return out

    primary = pages[0]
    out.surface = f'"{_page_name(primary.get("url", ""), primary.get("title", ""))}" page'

    header = header_section(primary)
    if header.checks:
        out.sections.append(header)
    else:
        out.gaps.append(
            "The markup declared no Header region, so the Header sweep was "
            "omitted. Header checks are usually 7 rows.")

    content_pages = pages[:MAX_CONTENT_SECTIONS]
    single = len(content_pages) == 1
    for page in content_pages:
        name = "Page Content" if single else \
            f'Page Content — {_page_name(page.get("url", ""), page.get("title", ""))}'
        sec = content_section(page, name=name)
        if sec.checks:
            out.sections.append(sec)
    if len(pages) > MAX_CONTENT_SECTIONS:
        out.gaps.append(
            f"{len(pages) - MAX_CONTENT_SECTIONS} further crawled surfaces "
            f"were not expanded — the cap is {MAX_CONTENT_SECTIONS} content "
            f"sections per checklist.")

    footer = footer_section(primary)
    if footer.checks:
        out.sections.append(footer)
        gap = _footer_gap(primary, footer)
        if gap:
            out.gaps.append(gap)
    else:
        out.gaps.append(
            "The markup declared no Footer region, so the Footer sweep was "
            "omitted. Footer checks are usually 12 rows.")

    return assign_numbers(out)


#: Section name → the ID prefix the module has always used. Keeps the
#: visible IDs recognisable next to the older template-driven items.
_SECTION_PREFIX = {
    "Header": "HDR",
    "Footer": "FTR",
    "Page Content": "CNT",
}


def to_checklist_items(checklist: LowLevelChecklist) -> list:
    """Convert to the module's ``ChecklistItem`` dataclass, in sheet order.

    Carries ``item_num`` and ``depth`` through so the sheet, the UI and the
    export all render the same hierarchy the reference deliverable uses.
    """
    from engine.testcase_generator import ChecklistItem

    out: list = []
    counters: dict[str, int] = {}
    for section in checklist.sections:
        base = section.name.split("—")[0].strip()
        prefix = _SECTION_PREFIX.get(base, "CNT")
        for check in section.checks:
            counters[prefix] = counters.get(prefix, 0) + 1
            out.append(ChecklistItem(
                id=f"{prefix}_{counters[prefix]:03d}",
                section=section.name,
                objective=check.objective,
                category=check.category,
                priority=check.priority,
                item_num=check.number,
                depth=check.depth,
            ))
            try:
                out[-1].testing_type = check.testing_type
                out[-1].status = "Unchecked"
            except Exception:  # pragma: no cover — defensive
                pass
    return out


def lint_checklist(checklist: LowLevelChecklist) -> list[str]:
    """Wording findings across every row. Empty list == compliant."""
    out: list[str] = []
    for check in checklist.all_checks():
        for issue in glossary.lint_text(check.objective, kind="objective"):
            out.append(f"{check.number}: {issue}")
    return out


__all__ = [
    "Check", "Section", "LowLevelChecklist",
    "build_checklist", "assign_numbers", "lint_checklist",
    "to_checklist_items",
    "header_section", "footer_section", "content_section",
    "MAX_CONTENT_SECTIONS",
]
