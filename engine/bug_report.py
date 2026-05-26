"""
TestFortge — Bug Report Module (Jira-style, ISTQB-aligned)

Generates structured bug reports following ISTQB defect reporting standards
and Jira-style formatting. Supports linking to failed test cases or
checklist items and exporting to Markdown.

ISTQB-mandatory fields (per ISTQB Foundation Level syllabus, Defect
Management chapter): identifier, title, severity, priority, status,
environment, preconditions, **steps to reproduce**, actual result,
expected result, frequency, found-in build, affects version, attachments,
linked items, reporter. We always emit non-empty values for the
mandatory ones — auto-generated bugs from a failed checklist item used
to ship empty ``preconditions`` and ``steps_to_reproduce`` which made
them un-actionable; that's now closed in :func:`engine.qa_testers`.

Jira best-practice fields layered on top: assignee, component, labels,
comment, reporter, created. Issue type is implicitly "Bug" — every
record in this module is a defect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ── Constants ──────────────────────────────────────────────────────

BUG_SEVERITIES = ["Critical", "Major", "Minor", "Trivial"]

BUG_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]

BUG_STATUSES = ["Open", "In Progress", "Resolved", "Closed", "Reopened"]

# How often the defect can be reproduced. ISTQB-mandatory field.
# Auto-generated bugs from deterministic real_check / simulator runs are
# always "Always" because the same input produces the same status.
BUG_FREQUENCIES = ["Always", "Sometimes", "Rarely", "Once"]


# ── Data model ─────────────────────────────────────────────────────

@dataclass
class BugReport:
    """A single defect, ISTQB-aligned and Jira-friendly.

    Mandatory ISTQB fields are required positional/keyword args; Jira
    nice-to-haves (assignee, labels, comment, etc.) default to empty so
    the manual create-bug form can submit a partial record without
    breaking auto-generated bugs that fill everything.
    """

    id: str                    # e.g. "BUG-001"
    title: str                 # Short bug title (negated active-voice statement)
    severity: str              # "Critical", "Major", "Minor", "Trivial"
    priority: str              # "Highest", "High", "Medium", "Low", "Lowest"
    status: str                # "Open", "In Progress", "Resolved", "Closed", "Reopened"
    environment: str           # "Windows / Chrome / Desktop / 1920x1080"
    preconditions: str         # State the system must be in before step 1
    steps_to_reproduce: str    # Numbered steps, e.g. "1. Open ...\n2. Click ..."
    actual_result: str         # What actually happened (observed)
    expected_result: str       # What should have happened (per requirement)
    # ── ISTQB-mandatory metadata ──
    frequency: str = "Always"          # Reproducibility: Always/Sometimes/Rarely/Once
    affects_version: str = ""          # Product version where the defect was found
    found_in_build: str = ""           # Build identifier (env + ISO timestamp)
    # ── Linkage / traceability ──
    attachments: list[str] = field(default_factory=list)
    linked_item_id: str = ""   # linked test case or checklist item ID
    linked_item_type: str = "" # "test_case" or "checklist"
    # ── Jira workflow metadata ──
    reporter: str = ""         # tester name (the person who filed the bug)
    assignee: str = ""         # developer who will fix it
    created_at: str = ""       # ISO 8601 UTC
    component: str = ""        # e.g. "Authentication", "Search", "UI"
    labels: list[str] = field(default_factory=list)
    comment: str = ""
    # Optional int row id from the DB (engine.db.BugReport.id). Used by
    # the bulk-edit toolbar on /bug-reports — checkboxes carry this so
    # POST /bugs/bulk can address rows without dragging the slug-style
    # external id through SQL. Defaults to 0 so older sessions and the
    # auto-bug factory don't have to pass it explicitly.
    db_id: int = 0
    # ── PR-H: smart-filing metadata ──
    # ``defect_class`` mirrors the walkthrough heuristic's class tag
    # (``broken_image``, ``axe_critical``, …) so dedup can group by
    # defect type without parsing labels. Empty for TC-driven bugs.
    defect_class: str = ""
    # ``page_url`` is the URL where the finding was raised. Captured
    # separately from steps_to_reproduce text so cross-run dedup can
    # match by structured field rather than parse free-form steps.
    page_url: str = ""
    # ``dedup_signature`` is a stable identifier across runs computed
    # from ``(defect_class, element_signature, page_url)``. Two bugs
    # with the same signature describe the same defect — the second
    # one is suppressed by the smart-filing pre-insert check.
    dedup_signature: str = ""
    # ``occurrence_count`` is incremented every time a re-run finds
    # the same defect (instead of creating a duplicate row). The UI
    # can surface "found 4× across runs" without changing the bug
    # body. Default 1 so first occurrence reads naturally.
    occurrence_count: int = 1
    # ``annotation_status`` records the PR-D′/F annotation outcome
    # per bug so Render Logs (which strip Python application logs in
    # the current host config) are no longer the only diagnostic
    # channel. Values: "annotated:<path>", "skipped:<reason>",
    # "failed:<exception>". Empty when the finding had no selector
    # to annotate.
    annotation_status: str = ""


# ── ID generation ──────────────────────────────────────────────────

def generate_bug_id(existing_bugs: list[BugReport]) -> str:
    """Return the next sequential bug ID like 'BUG-001', 'BUG-002', etc."""
    if not existing_bugs:
        return "BUG-001"

    max_num = 0
    for bug in existing_bugs:
        try:
            num = int(bug.id.split("-", 1)[1])
            if num > max_num:
                max_num = num
        except (IndexError, ValueError):
            continue

    return f"BUG-{max_num + 1:03d}"


# ── Factory: create bug from a failed item ─────────────────────────

_PRIORITY_TO_SEVERITY = {
    "High": "Major",
    "Medium": "Minor",
    "Low": "Trivial",
}


def create_bug_from_failed_item(
    item,
    item_type: str,
    environment_str: str = "",
    tester_name: str = "",
    comment: str = "",
) -> BugReport:
    """Create a BugReport pre-filled from a failed test case or checklist item.

    Parameters
    ----------
    item : TestCase | ChecklistItem
        The item that failed during execution.
    item_type : str
        Either ``"test_case"`` or ``"checklist"``.
    environment_str : str
        Free-text environment description, e.g. "Windows / Chrome / Desktop".
    tester_name : str
        Name of the reporter / tester.
    comment : str
        Optional comment or additional context.

    Returns
    -------
    BugReport
        A new bug report with status "Open" and auto-filled fields.
    """
    severity = _PRIORITY_TO_SEVERITY.get(
        getattr(item, "priority", "Medium"), "Minor"
    )

    if item_type == "test_case":
        title = f"[BUG] {item.summary}"
        steps = getattr(item, "test_steps", "")
        expected = getattr(item, "expected_result", "")
        preconditions = getattr(item, "preconditions", "")
    else:
        # checklist
        title = f"[BUG] {item.objective}"
        steps = ""
        expected = ""
        preconditions = ""

    return BugReport(
        id="",  # assigned later via generate_bug_id
        title=title,
        severity=severity,
        priority=getattr(item, "priority", "Medium"),
        status="Open",
        environment=environment_str,
        preconditions=preconditions,
        steps_to_reproduce=steps,
        actual_result="",
        expected_result=expected,
        attachments=[],
        linked_item_id=getattr(item, "id", ""),
        linked_item_type=item_type,
        reporter=tester_name,
        assignee="",
        created_at=datetime.now(timezone.utc).isoformat(),
        component=getattr(item, "section", ""),
        labels=[],
        comment=comment,
    )


# ── PR-G: humanise URL paths + CDN-hash filenames for titles ───────
#
# Walkthrough bug titles used to contain raw CDN hash filenames like
# ``6a0dc9966e1d43dd88d9b8a5_Frame%202136140580.svg`` — opaque to
# PMs/stakeholders. The two helpers below let title templates print
# friendly substitutes (``"a page graphic"``, ``"the Careers page"``)
# while the raw values still land in Steps to Reproduce + Developer
# Detail where they belong.

# Webflow / Wix / similar CDNs prefix asset filenames with a long
# alphanumeric hex hash (the asset id). We treat any 16+ char hex
# prefix followed by an underscore as a CDN hash and substitute a
# generic phrase in titles. The full filename remains in the bug body.
_CDN_HASH_PREFIX_RE = re.compile(r"^[a-f0-9]{16,}_", re.I)


def _is_cdn_hash_filename(name: str) -> bool:
    """Return True when *name* looks like a CDN-emitted asset (long
    hex prefix). The substring after the prefix is usually the
    original filename minus extension, but it's mangled enough that
    showing it in a bug title is more noise than signal.
    """
    if not name:
        return False
    base = name.rsplit("/", 1)[-1]
    return bool(_CDN_HASH_PREFIX_RE.match(base))


def _humanise_url_page(url: str) -> str:
    """Convert a URL into a short human-readable page reference for
    bug titles.

    Examples:
        ``https://example.com/``               → "the homepage"
        ``https://example.com/index.html``     → "the homepage"
        ``https://example.com/careers``        → "the Careers page"
        ``https://example.com/contact-us``     → "the Contact Us page"
        ``https://example.com/blog/2024/foo``  → "the Foo page"

    Falls back to "the page" when *url* is empty or unparseable.
    """
    if not url:
        return "the page"
    try:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(url)
        # Bare strings ("not-a-url") parse as a relative path with no
        # scheme — refuse those so a typo doesn't turn into "the
        # Not A Url page". An absolute path ("/careers") is allowed
        # since it's a valid form for in-app relative URLs.
        if not parsed.scheme and not url.startswith("/"):
            return "the page"
        path = unquote(parsed.path or "")
    except Exception:
        return "the page"
    path = path.strip("/")
    if not path or path.lower() in ("index", "index.html", "index.htm", "home"):
        return "the homepage"
    last = path.split("/")[-1]
    # Strip file extension — most user-facing routes are extensionless
    # but we cover the index.html / about.php edge cases.
    if "." in last and not last.startswith("."):
        last = last.rsplit(".", 1)[0]
    # ``contact-us`` → ``Contact Us``; ``senior_engineer`` → ``Senior Engineer``.
    name = re.sub(r"[-_]+", " ", last).strip().title()
    if not name:
        return "the page"
    return f"the {name} page"


# ── PR-F: walkthrough-message → passive-voice transform ────────────
#
# PR-C′ rewrote ``engine.qa_testers._make_bug_summary`` so TC-driven
# bugs got passive-voice titles with after/while trigger clauses. But
# walkthrough bugs (every bug in the ART project export) flow through
# :func:`create_bug_from_walkthrough_finding` below, which built the
# title via ``f"[{area}] {message}"`` — the heuristic's free-text
# message was used as-is. That re-introduced positive-voice titles
# like:
#
#   • "[Accessibility] Select element must have an accessible name —
#      affects 2 elements on this page"  (reads as a requirement, not
#      a defect)
#   • "[JS] A JavaScript error happened during the user journey"
#     (active voice; "happened" reads casual)
#   • "[Images] Broken image on the page — foo.avif did not load …"
#     (passive but no after/while trigger)
#
# The transforms below rewrite the heuristic's message into a
# passive-voice headline without the "must have" framing. The list
# is ordered most-specific first.

_WALKTHROUGH_MESSAGE_TRANSFORMS: list[tuple[re.Pattern[str], str]] = [
    # "Select element must have an accessible name — affects 2 elements
    # on this page" → with URL: "Accessible name is missing from select
    # element on the Jobs page (affects …)"; without URL: "Accessible
    # name is missing from select element (affects …)". The
    # ``{on_page}`` substitution is conditional — empty when no URL
    # was plumbed through — to avoid the redundant "on the page (…
    # on this page)" mouthful.
    (
        re.compile(
            r"^(?P<subject>.+?)\s+must have\s+(?:an?\s+)?(?P<thing>.+?)\s*(?:—\s*(?P<tail>.+))?$",
            re.I,
        ),
        "{thing_cap} is missing from {subject_lower}{on_page}{tail_paren}",
    ),
    # "Broken image on the page — "alt text" did not load (visitors ...)"
    # → ""Alt Text" graphic is missing on the Careers page (visitors ...)"
    # PR-G: alt-aware archetype — when the heuristic captured an
    # ``alt`` attribute we use it verbatim (much more meaningful than
    # a CDN-hash filename). Runs BEFORE the filename archetype so
    # quoted alt strings don't get scooped by the ``\S+?`` filename
    # capture. ``{on_page_always}`` always renders ("on the page" or
    # "on the Careers page") because broken-image titles need the
    # destination context even without a URL.
    (
        re.compile(
            r"^Broken image on the page\s*—\s*\"(?P<alt>[^\"]+)\"\s+did not load\b\s*(?P<tail>\(.+\))?\s*$",
            re.I,
        ),
        "\"{alt_cap}\" graphic is missing {on_page_always}{tail_space}",
    ),
    # "Broken image on the page — foo.avif did not load (visitors ...)"
    # → either (filename meaningful) "Image foo.avif is missing on the
    # Careers page (...)" or (CDN hash) "A page graphic is missing on
    # the Careers page (...)". The template chooses based on the
    # ``filename_human`` derived substitution computed at render time.
    (
        re.compile(
            r"^Broken image on the page\s*—\s*(?P<filename>\S+?)\s+did not load\b\s*(?P<tail>\(.+\))?\s*$",
            re.I,
        ),
        "{filename_human} {on_page_always}{tail_space}",
    ),
    # "A JavaScript error happened during the user journey"
    # → "JavaScript error is raised during the user journey"
    # Optional ``A`` / ``An`` article prefix so "An async exception
    # happened while loading…" also lands in this archetype.
    (
        re.compile(
            r"^(?:An?\s+)?(?P<subject>.+?)\s+happened\s+(?P<adv>during|while|after)\s+(?P<rest>.+)$",
            re.I,
        ),
        "{subject_cap} is raised {adv} {rest}",
    ),
    # Generic "<X> did not load (<tail>)" → "<X> is not loaded after page navigation (<tail>)"
    (
        re.compile(
            r"^(?P<subject>.+?)\s+did not load\b\s*(?P<tail>\(.+\))?\s*$",
            re.I,
        ),
        "{subject_cap} is not loaded after page navigation{tail_space}",
    ),
    # "<X> failed to <Y>" → "<X> did not <Y> as expected"
    (
        re.compile(r"^(?P<subject>.+?)\s+failed to\s+(?P<verb>.+?)\s*$", re.I),
        "{subject_cap} did not {verb} as expected",
    ),
]


def _walkthrough_passive_title(
    area: str, message: str, *, url: str = "",
) -> str:
    """Compose the headline for a walkthrough-source bug.

    Format: ``[Area] <passive-voice transformed message>``.

    *url* is consumed by the archetype templates to inject a friendly
    page reference ("the Careers page") in place of the generic "on
    the page" phrasing. When the URL is empty or unparseable the
    helper returns "the page" so existing tests stay green.

    When the raw message doesn't fit any archetype below we keep it
    verbatim — heuristic phrasings that already read clearly (Chrome
    console error snippets, HTTP server-error lines) should not be
    forced through a grammatical rewrite.
    """
    title_area = area or "Page"
    if not message:
        return f"[{title_area}] Walkthrough finding"

    page_name = _humanise_url_page(url)
    has_url = bool((url or "").strip())
    # Two substitutions for the templates to choose between:
    #   - ``on_page``        — empty when no URL, "on {page_name}"
    #                          when one was provided. Used by archetypes
    #                          whose heuristic tail already mentions
    #                          "on this page" to avoid duplication.
    #   - ``on_page_always`` — always "on {page_name}" (falls back to
    #                          "on the page"). Used by archetypes
    #                          whose semantics require destination
    #                          context regardless (broken images).
    on_page = f" on {page_name}" if has_url else ""
    on_page_always = f"on {page_name}"

    for pat, template in _WALKTHROUGH_MESSAGE_TRANSFORMS:
        m = pat.match(message)
        if not m:
            continue
        groups = m.groupdict()
        # Compute derived substitutions per archetype.
        rendered: dict[str, str] = {
            "page_name": page_name,
            "on_page": on_page,
            "on_page_always": on_page_always,
        }
        for key, val in groups.items():
            v = (val or "").strip()
            rendered[key] = v
            rendered[f"{key}_cap"] = v[0].upper() + v[1:] if v else ""
            rendered[f"{key}_lower"] = v[0].lower() + v[1:] if v else ""
        # ``tail`` group needs special handling — wrap with spacing
        # only if non-empty so we don't emit a stray trailing space.
        tail = (groups.get("tail") or "").strip()
        rendered["tail_paren"] = f" ({tail})" if tail and not tail.startswith("(") else (
            f" {tail}" if tail else ""
        )
        rendered["tail_space"] = f" {tail}" if tail else ""
        # PR-G filename humanisation: when the filename capture is a
        # CDN hash, substitute "A page graphic is missing"; otherwise
        # keep the original filename in a friendlier "Image foo.svg
        # is missing" shape. The template references
        # ``{filename_human}`` and we compute it here so the template
        # itself stays declarative.
        fn = (groups.get("filename") or "").strip()
        if fn:
            if _is_cdn_hash_filename(fn):
                rendered["filename_human"] = "A page graphic is missing"
            else:
                rendered["filename_human"] = f"Image {fn} is missing"
        try:
            transformed = template.format(**rendered).strip()
        except Exception:
            continue
        if transformed:
            # Collapse stray double spaces left by optional groups.
            transformed = re.sub(r"\s{2,}", " ", transformed)
            return f"[{title_area}] {transformed}"

    # No archetype matched — fall back to the original "[Area] message"
    # form. Beats forcing a grammatical rewrite that mangles the
    # heuristic's diagnostic detail.
    return f"[{title_area}] {message}"


# ── PR-H: dedup-signature builder ──────────────────────────────────
#
# Cross-run deduplication groups bugs by their *intent* — same
# defect on same element on same page across multiple runs. The
# signature is a stable hex digest of ``(defect_class, element,
# page_url_path)`` where:
#
#   * ``element`` is normalised to its first non-empty selector
#     when axe gives a chain list (``["#All-vacancies"]``);
#   * ``page_url`` is reduced to ``(scheme, host, path)`` so query
#     params (cache busters, analytics) don't fragment groups.
#
# We use SHA-1 truncated to 16 hex chars — long enough to avoid
# accidental collisions across the project's bug catalogue,
# short enough to keep ``extra.dedup_signature`` readable in logs.

import hashlib as _hashlib


def _normalise_element_for_dedup(element) -> str:
    """Collapse ``element`` to a single normalised selector string.

    Walkthrough findings sometimes store ``element`` as a list (axe
    targets), sometimes as a string (everything else). Cross-run
    dedup needs the SAME shape regardless of which heuristic emitted
    the finding — otherwise two runs that found the same defect
    would compute different signatures.
    """
    if not element:
        return ""
    if isinstance(element, (list, tuple)):
        for item in element:
            s = str(item or "").strip()
            if s:
                return s
        return ""
    return str(element).strip()


def _normalise_url_for_dedup(url: str) -> str:
    """Reduce ``url`` to ``scheme://host/path`` so query strings and
    fragments don't fragment dedup groups across reruns (analytics
    cache busters, session ids, etc.).
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except Exception:
        return url.strip()
    scheme = (p.scheme or "").lower()
    netloc = (p.netloc or "").lower()
    path = p.path or ""
    # Trailing slash is non-significant for our purposes.
    path = path.rstrip("/") or "/"
    if scheme and netloc:
        return f"{scheme}://{netloc}{path}"
    return path


def compute_dedup_signature(
    defect_class: str, element, page_url: str,
) -> str:
    """Return the stable dedup signature for a walkthrough finding.

    The signature is a 16-char hex SHA-1 digest of the
    pipe-separated ``(defect_class|normalised_element|normalised_url)``
    triple. Empty inputs are tolerated — the function never raises so
    the bug-saving path stays robust to partial finding records.
    """
    parts = [
        (defect_class or "").strip().lower(),
        _normalise_element_for_dedup(element),
        _normalise_url_for_dedup(page_url),
    ]
    payload = "|".join(parts).encode("utf-8", errors="replace")
    return _hashlib.sha1(payload).hexdigest()[:16]


# ── Factory: create bug from a walkthrough finding ─────────────────


def create_bug_from_walkthrough_finding(
    finding: dict,
    *,
    environment_str: str = "",
    tester_name: str = "",
    base_url: str = "",
) -> BugReport:
    """Synthesise a :class:`BugReport` from a walkthrough finding dict.

    Walkthrough mode runs without test cases — the heuristics emit
    free-text findings (see :mod:`engine.walkthrough_runner` module
    docstring for the schema). This factory converts each finding into
    a bug record:

    * ``linked_item_type = "walkthrough"`` so the bug-listing UI can
      filter the audit trail by source
    * ``linked_item_id``  = the synthetic ``WALK-NNN`` id the runner
      attached (stable across reruns of the same walkthrough)
    * ``severity`` / ``priority`` come from
      :func:`engine.bug_template.severity_priority` with the area name
      + url joined as the area-hint blob, so weighted priority still
      kicks in (auth/checkout urls → Highest priority)
    * ``steps_to_reproduce`` is synthesised from the URL: "1. Open URL.
      2. Observe the defect on the page" — adequate for cosmetic and
      a11y findings, augmented with the finding's ``fix_hint`` for
      interaction-shape findings (hamburger / search / form)
    * ``labels`` carries ``defect:<class>`` + ``source:walkthrough`` so
      backlog filtering by failure mode works the same as for the
      TC-driven path

    The bug returned has ``id=""`` — callers assign the next id via
    :func:`generate_bug_id` exactly like the TC-driven factory.
    """
    # Local import keeps the cross-module dependency at the call site
    # instead of the file header; bug_template imports bug_report-side
    # constants in a future revision and we want the import graph to
    # stay acyclic.
    from engine.bug_template import severity_priority

    area     = str(finding.get("area") or "")
    message  = str(finding.get("message") or "")
    cls      = str(finding.get("defect_class") or "unknown")
    sev_from_finding = str(finding.get("severity") or "")
    url      = str(finding.get("url") or "")
    element  = str(finding.get("element") or "")
    fix_hint = str(finding.get("fix_hint") or "")
    user_impact = str(finding.get("user_impact") or "")
    dev_detail  = str(finding.get("dev_detail") or "")
    screenshot  = str(finding.get("screenshot") or "")
    tc_id    = str(finding.get("tc_id") or "")

    sev_computed, pri_computed = severity_priority(cls, area, url)
    # Prefer the finding's explicit severity when it lines up with the
    # ladder (Critical/Major/Minor/Trivial); fall back to the computed
    # one otherwise. The heuristics already set severity per defect
    # class so this normally short-circuits to ``sev_from_finding``.
    severity = sev_from_finding if sev_from_finding in (
        "Critical", "Major", "Minor", "Trivial",
    ) else sev_computed

    # PR-F: route through the passive-voice transformer so walkthrough
    # bugs get the same "is not <pp> after/while <trigger>" treatment
    # PR-C′ gave TC-driven bugs. Falls back to ``[Area] message`` when
    # no archetype matches so heuristic messages that are already
    # grammatical (e.g. console JS errors) are left untouched.
    # PR-G: forward ``url`` so the title can swap the generic
    # "on the page" phrasing for a human-readable "the Careers page".
    title = _walkthrough_passive_title(area, message, url=url)

    str_lines = [f"1. Open {url or base_url or '<application URL>'}."]
    if element:
        str_lines.append(f"2. Locate the element matching: {element}")
        observe_idx = 3
    else:
        observe_idx = 2
    str_lines.append(
        f"{observe_idx}. Observe the defect described in the actual "
        "result."
    )
    steps_to_reproduce = "\n".join(str_lines)

    actual_lines = [message] if message else []
    if user_impact:
        actual_lines.append(f"User impact: {user_impact}")
    if dev_detail:
        actual_lines.append(f"Developer detail: {dev_detail}")
    actual_result = "\n\n".join(actual_lines) or "Walkthrough finding"

    expected_lines = [
        "The element should render and behave correctly according to "
        "the design and accessibility expectations."
    ]
    if fix_hint:
        expected_lines.append(f"Suggested fix direction: {fix_hint}")
    expected_result = "\n\n".join(expected_lines)

    labels = [f"defect:{cls}", "source:walkthrough"]
    if area:
        labels.append(f"area:{area.lower().replace(' ', '_')}")

    attachments = [screenshot] if screenshot else []

    # PR-H: compute the cross-run dedup signature so the smart-filing
    # check in routes/execution.py can group reruns by intent without
    # parsing free-form bug body text. ``element`` is fed in raw so
    # ``_normalise_element_for_dedup`` handles list/string variance
    # at one place.
    dedup_signature = compute_dedup_signature(
        defect_class=cls,
        element=finding.get("element"),
        page_url=url,
    )
    # PR-H: annotation_status comes from the LiveExecutor annotation
    # block (set in :meth:`engine.live_executor._walk_one` for every
    # code path — success / skipped / failed). Surfacing it via the
    # bug record lets us diagnose the silent-failure mode of PR-D′/F
    # without depending on Render's app log capture.
    annotation_status = str(finding.get("annotation_status") or "")

    return BugReport(
        id="",
        title=title,
        severity=severity,
        priority=pri_computed,
        status="Open",
        environment=environment_str,
        preconditions=(
            f"Walkthrough run starting at {base_url}."
            if base_url else "Walkthrough run."
        ),
        steps_to_reproduce=steps_to_reproduce,
        actual_result=actual_result,
        expected_result=expected_result,
        frequency="Always",
        attachments=attachments,
        linked_item_id=tc_id,
        linked_item_type="walkthrough",
        reporter=tester_name,
        assignee="",
        created_at=datetime.now(timezone.utc).isoformat(),
        component=area,
        labels=labels,
        comment="",
        # PR-H smart-filing metadata.
        defect_class=cls,
        page_url=url,
        dedup_signature=dedup_signature,
        occurrence_count=1,
        annotation_status=annotation_status,
    )


# ── Factory: create bug from a LiveExecutor early-exit ────────────


# ``early_exit_reason`` strings produced by
# :class:`engine.live_executor.LiveExecutor`. Kept in sync with
# ``live_executor.py``'s assignments to ``early_reason``.
_EARLY_EXIT_OOM_PREFIX = "oom_budget_exceeded"
_EARLY_EXIT_WALL_CLOCK = "wall_deadline_exceeded"


def create_bug_from_early_exit(
    reason: str,
    *,
    run_id: str = "",
    base_url: str = "",
    environment_str: str = "",
    tester_name: str = "",
    rss_mb: int = 0,
    budget_mb: int = 0,
    cases_done: int = 0,
    cases_total: int = 0,
) -> BugReport:
    """Synthesise an infra-level :class:`BugReport` from a LiveExecutor
    early-exit reason.

    Stage 3's :class:`engine.live_executor.OomGuard` and wall-clock
    deadline write ``early_exit_reason`` into the worker's result.json
    when the run is cut short — operators see a "Stopped early" badge
    in the UI but had no backlog entry to track follow-up against. This
    factory produces one infrastructure bug per early-exit so the
    follow-up (raise the memory budget, shrink the URL plan, profile a
    leak) lives on the project's regular Bug Reports board next to
    every other defect.

    Severity / priority rationale
    -----------------------------
    * OOM is ``Major`` / ``High`` — partial run completes (Stage 3
      writes ``status=oom_exit``), so it's not "system unusable" but
      every long run loses cases past the cap.
    * Wall-clock exit is ``Minor`` / ``Medium`` — the run hit the
      configured ceiling and stopped on a fully-controlled boundary;
      operator action is usually "raise the timeout", not "fix a bug".

    The returned bug has ``id=""`` — the caller assigns it via
    :func:`generate_bug_id` exactly like the other factories.
    """
    raw = (reason or "").strip()
    is_oom = raw.startswith(_EARLY_EXIT_OOM_PREFIX)
    is_wall_clock = raw.startswith(_EARLY_EXIT_WALL_CLOCK)

    if is_oom:
        severity = "Major"
        priority = "High"
        defect_class = "early_exit_oom"
        title = "[Test Run] Live executor stopped early: out-of-memory"
        actual_lines = [
            f"The LiveExecutor reached its memory budget mid-run and "
            f"shut down cleanly to avoid a SIGKILL from the host. "
            f"Reason reported by OomGuard: {raw}.",
        ]
        if rss_mb and budget_mb:
            actual_lines.append(
                f"Resident set size at the time of the check: "
                f"{rss_mb} MB (budget {budget_mb} MB)."
            )
        expected_lines = [
            "The run should complete every queued page and test case "
            "without the worker hitting the per-process RSS ceiling.",
            "Suggested follow-up: profile the Playwright session for "
            "leaked contexts, lower ``max_pages`` / ``max_form_fills`` "
            "in the live config, or raise ``MEMORY_BUDGET_MB`` if the "
            "host has headroom.",
        ]
    elif is_wall_clock:
        severity = "Minor"
        priority = "Medium"
        defect_class = "early_exit_wall_clock"
        title = "[Test Run] Live executor stopped early: wall-clock deadline"
        actual_lines = [
            f"The LiveExecutor hit its wall-clock deadline mid-run and "
            f"stopped before draining the URL queue. "
            f"Reason: {raw}.",
        ]
        expected_lines = [
            "The run should fit inside the configured "
            "``device_timeout_ms`` budget, OR the budget should be "
            "raised to match the project's actual coverage size.",
        ]
    else:
        # Future-proofing: unknown early-exit string still surfaces as
        # a bug rather than silently disappearing.
        severity = "Major"
        priority = "High"
        defect_class = "early_exit_unknown"
        title = "[Test Run] Live executor stopped early"
        actual_lines = [
            f"The LiveExecutor reported a non-empty early_exit_reason: "
            f"{raw or '(empty)'}.",
        ]
        expected_lines = [
            "The run should complete every queued page and test case. "
            "Inspect the worker log for the exit cause.",
        ]

    if cases_total:
        actual_lines.append(
            f"Cases completed before the early exit: "
            f"{cases_done} of {cases_total}."
        )

    steps_lines = [
        f"1. Open {base_url or '<application URL>'} in the configured "
        "environment.",
        "2. Trigger /test-execution with the same TC pack and live "
        "settings (max_pages, viewport, memory budget) used in this "
        "run.",
        "3. Wait until the run reports the early-exit status in "
        "/test-execution/results.",
    ]

    labels = [
        f"defect:{defect_class}",
        "source:live_executor",
        "area:test_run_infra",
    ]

    actual_result = "\n\n".join(actual_lines)
    expected_result = "\n\n".join(expected_lines)
    preconditions = (
        f"LiveExecutor run id {run_id} against {base_url or '<base URL>'}."
        if run_id
        else f"LiveExecutor run against {base_url or '<base URL>'}."
    )

    return BugReport(
        id="",
        title=title,
        severity=severity,
        priority=priority,
        status="Open",
        environment=environment_str,
        preconditions=preconditions,
        steps_to_reproduce="\n".join(steps_lines),
        actual_result=actual_result,
        expected_result=expected_result,
        frequency="Always",
        attachments=[],
        linked_item_id=run_id or "LIVE-RUN",
        linked_item_type="live_executor",
        reporter=tester_name,
        assignee="",
        created_at=datetime.now(timezone.utc).isoformat(),
        component="Test Run Infrastructure",
        labels=labels,
        comment="",
    )


# ── Serialisation helpers ──────────────────────────────────────────

def bug_to_dict(bug: BugReport) -> dict:
    """Convert a BugReport to a plain dictionary for session storage."""
    return {
        "id": bug.id,
        "title": bug.title,
        "severity": bug.severity,
        "priority": bug.priority,
        "status": bug.status,
        "environment": bug.environment,
        "preconditions": bug.preconditions,
        "steps_to_reproduce": bug.steps_to_reproduce,
        "actual_result": bug.actual_result,
        "expected_result": bug.expected_result,
        "frequency": bug.frequency,
        "affects_version": bug.affects_version,
        "found_in_build": bug.found_in_build,
        "attachments": list(bug.attachments),
        "linked_item_id": bug.linked_item_id,
        "linked_item_type": bug.linked_item_type,
        "reporter": bug.reporter,
        "assignee": bug.assignee,
        "created_at": bug.created_at,
        "component": bug.component,
        "labels": list(bug.labels),
        "comment": bug.comment,
        "db_id": bug.db_id,
        # PR-H smart-filing metadata. ``save_bug`` collects all
        # non-canonical keys into ``extra`` so these land in the
        # ``BugReport.extra`` JSON column without a schema change.
        "defect_class": bug.defect_class,
        "page_url": bug.page_url,
        "dedup_signature": bug.dedup_signature,
        "occurrence_count": bug.occurrence_count,
        "annotation_status": bug.annotation_status,
    }


def dict_to_bug(d: dict) -> BugReport:
    """Reconstruct a BugReport from a dictionary.

    Tolerates older snapshots that pre-date the ISTQB metadata fields:
    ``frequency`` / ``affects_version`` / ``found_in_build`` default to
    empty (or "Always" for frequency) when missing so a project saved
    before this revision still loads cleanly.
    """
    return BugReport(
        id=d.get("id", ""),
        title=d.get("title", ""),
        severity=d.get("severity", "Minor"),
        priority=d.get("priority", "Medium"),
        status=d.get("status", "Open"),
        environment=d.get("environment", ""),
        preconditions=d.get("preconditions", ""),
        steps_to_reproduce=d.get("steps_to_reproduce", ""),
        actual_result=d.get("actual_result", ""),
        expected_result=d.get("expected_result", ""),
        frequency=d.get("frequency", "Always"),
        affects_version=d.get("affects_version", ""),
        found_in_build=d.get("found_in_build", ""),
        attachments=d.get("attachments", []),
        linked_item_id=d.get("linked_item_id", ""),
        linked_item_type=d.get("linked_item_type", ""),
        reporter=d.get("reporter", ""),
        assignee=d.get("assignee", ""),
        created_at=d.get("created_at", ""),
        component=d.get("component", ""),
        labels=d.get("labels", []),
        comment=d.get("comment", ""),
        db_id=int(d.get("db_id") or 0),
        # PR-H smart-filing metadata. Tolerates older snapshots that
        # pre-date these fields by defaulting empty / 1.
        defect_class=d.get("defect_class", ""),
        page_url=d.get("page_url", ""),
        dedup_signature=d.get("dedup_signature", ""),
        occurrence_count=int(d.get("occurrence_count") or 1),
        annotation_status=d.get("annotation_status", ""),
    )


# ── Markdown export ────────────────────────────────────────────────

def export_bug_report_markdown(bug: BugReport) -> str:
    """Export a single bug report as Jira-style Markdown.

    Returns a formatted string suitable for inclusion in a larger
    Markdown document or for standalone display.
    """
    lines: list[str] = []

    # Header
    lines.append(f"## {bug.id}: {bug.title}")
    lines.append("")

    # Summary table — ISTQB-mandatory metadata first, Jira workflow second.
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Severity | {bug.severity} |")
    lines.append(f"| Priority | {bug.priority} |")
    lines.append(f"| Status | {bug.status} |")
    lines.append(f"| Frequency | {bug.frequency or 'Always'} |")
    lines.append(f"| Environment | {bug.environment} |")
    if bug.affects_version:
        lines.append(f"| Affects Version | {bug.affects_version} |")
    if bug.found_in_build:
        lines.append(f"| Found in Build | {bug.found_in_build} |")
    if bug.component:
        lines.append(f"| Component | {bug.component} |")
    if bug.reporter:
        lines.append(f"| Reporter | {bug.reporter} |")
    if bug.assignee:
        lines.append(f"| Assignee | {bug.assignee} |")
    if bug.created_at:
        lines.append(f"| Created | {bug.created_at} |")
    if bug.labels:
        lines.append(f"| Labels | {', '.join(bug.labels)} |")
    lines.append("")

    # Preconditions
    if bug.preconditions:
        lines.append("### Preconditions")
        lines.append(bug.preconditions)
        lines.append("")

    # Steps to reproduce
    if bug.steps_to_reproduce:
        lines.append("### Steps to Reproduce")
        lines.append(bug.steps_to_reproduce)
        lines.append("")

    # Actual result
    if bug.actual_result:
        lines.append("### Actual Result")
        lines.append(bug.actual_result)
        lines.append("")

    # Expected result
    if bug.expected_result:
        lines.append("### Expected Result")
        lines.append(bug.expected_result)
        lines.append("")

    # Attachments
    if bug.attachments:
        lines.append("### Attachments")
        for att in bug.attachments:
            lines.append(f"- {att}")
        lines.append("")

    # Linked items
    if bug.linked_item_id:
        lines.append("### Linked Items")
        lines.append(
            f"- {bug.linked_item_id} ({bug.linked_item_type}) \u2014 Failed"
        )
        lines.append("")

    # Comment
    if bug.comment:
        lines.append("### Comment")
        lines.append(bug.comment)
        lines.append("")

    return "\n".join(lines)


# ── Step-list normalisation ────────────────────────────────────────

_STEP_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s+|step\s*\d+\s*[:.\-]?\s*)",
                             re.IGNORECASE)


def normalize_steps_to_numbered_list(text: str) -> str:
    """Coerce any free-form steps blob into a clean ``1. … 2. …`` list.

    The QA Team Lead module hands us steps as either a single multi-line
    string with leading numbers (``"1. Open\\n2. Click"``), a bullet list,
    or a single sentence. ISTQB and Jira both expect a numbered list, so
    we normalise here once and trust the rest of the pipeline.

    Empty/whitespace-only input returns ``""`` so callers can decide
    whether to synthesise a fallback.
    """
    if not text or not text.strip():
        return ""

    # Split on hard newlines first; then on " 1. " / "; " etc. fallbacks
    # only if the whole blob is one line.
    raw_lines = [ln for ln in (text.splitlines()) if ln.strip()]
    if len(raw_lines) <= 1:
        # Try inline numbered split: "1. foo 2. bar 3. baz".
        single = raw_lines[0] if raw_lines else text.strip()
        inline = re.split(r"(?<!\d)\b(\d+)[.)]\s+", " " + single)
        # re.split with capturing group returns [pre, num, body, num, body, ...]
        if len(inline) >= 5:
            steps = []
            i = 1
            while i < len(inline) - 1:
                body = (inline[i + 1] or "").strip()
                if body:
                    steps.append(body)
                i += 2
            if steps:
                return "\n".join(f"{idx}. {s.rstrip('.')}" for idx, s in enumerate(steps, 1))
        # Otherwise treat the whole thing as a single step.
        body = _STEP_PREFIX_RE.sub("", single).strip().rstrip(".")
        return f"1. {body}" if body else ""

    cleaned = []
    for ln in raw_lines:
        body = _STEP_PREFIX_RE.sub("", ln).strip().rstrip(".")
        if body:
            cleaned.append(body)
    if not cleaned:
        return ""
    return "\n".join(f"{i}. {s}" for i, s in enumerate(cleaned, 1))
