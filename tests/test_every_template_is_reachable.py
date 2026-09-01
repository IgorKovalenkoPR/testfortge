"""A template no route renders is a page that does not exist.

Found by walking the app, 2026-08-31. Nine templates in ``templates/``
are not rendered by any route, not extended and not included:

    input_stories.html   recommendations.html  requirements.html
    setup.html           status_report.html    techniques.html
    test_metrics.html    tools.html            user_stories.html

Guessing their obvious URLs returns 404. Six of them are described in
``REQUIREMENTS.md`` and ``QA_FORGE_DESCRIPTION.md`` as features, which is
how they stayed invisible: the documentation agrees they exist and the
URL map does not.

They are not free. ``test_template_escaping`` lints them, a translator
asked to cover the product would translate them, and a reader deciding
whether a feature exists finds a file that looks like the answer.

This file does not delete them — which of the nine are wanted back is
not a test's call. It stops the set growing, which is the part nobody
would otherwise notice: a tenth dead template would be exactly as
invisible as these nine were. The allowlist carries a reason per entry
and is checked for staleness, the same shape
``tests/test_i18n_parity.py`` uses for ``DELIBERATELY_ENGLISH``.

Reachability, not existence, is the property. A template is reachable
when a route names it, or when a reachable template extends or includes
it — so partials stay reachable through their users, and a partial whose
last user disappears becomes visible here.
"""
from __future__ import annotations

import pathlib
import re

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"
CODE_DIRS = ("routes", "engine")
CODE_FILES = ("app.py",)

#: Templates with no route, kept for now, with why. Removing one from
#: ``templates/`` should remove it from here in the same commit — the
#: staleness test below says so.
UNREACHABLE = {
    "input_stories.html": "pre-E3 user-story entry page; no route",
    "recommendations.html": "pre-E3 recommendations page; no route",
    "requirements.html": "pre-E3 requirements page; no route",
    "setup.html": "pre-E3 project setup page, replaced by the picker",
    "status_report.html": "pre-E3 status report; no route",
    "techniques.html": "pre-E3 techniques reference; no route",
    # Its trend chart is the only in-repo consumer of /metrics/history,
    # which is live and gated and reads snapshots still written on every
    # completed run. Wiring this page back means the notes in
    # routes/dashboard.py, routes/_shared.py and engine/runner_worker.py
    # stop being true — they say the page is gone.
    "test_metrics.html": "superseded by the dashboard; /metrics is ops",
    "tools.html": "pre-E3 tools page; no route",
    "user_stories.html": "pre-E3 user-story list; no route",
}

_RENDER_RE = re.compile(r"""render_template\s*\(\s*["']([^"']+\.html)["']""")
_EXTENDS_RE = re.compile(r"""\{%-?\s*extends\s+["']([^"']+)["']""")
_INCLUDE_RE = re.compile(r"""\{%-?\s*include\s+["']([^"']+)["']""")
#: ``{% from "_inline_edit.html" import editable %}`` — a macro library is
#: pulled in this way, not with include, and the first version of this
#: file did not know that. It reported five live templates as dead:
#: _bulk_bar, _import_mapping and _inline_edit are imported by three
#: pages each. The scan was wrong, not the app.
_FROM_RE = re.compile(r"""\{%-?\s*from\s+["']([^"']+)["']""")
#: ``{% include "guide/_sections_" ~ lang ~ ".html" %}`` — a computed
#: name. Only the literal prefix is knowable statically, so every
#: template starting with it counts as reachable.
_INCLUDE_PREFIX_RE = re.compile(
    r"""\{%-?\s*include\s+["']([^"']+)["']\s*~""")


def _all_templates() -> set[str]:
    return {p.relative_to(TEMPLATE_DIR).as_posix()
            for p in TEMPLATE_DIR.rglob("*.html")}


def _code_text() -> str:
    parts = []
    for d in CODE_DIRS:
        for p in (REPO_ROOT / d).rglob("*.py"):
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    for f in CODE_FILES:
        parts.append((REPO_ROOT / f).read_text(encoding="utf-8",
                                               errors="replace"))
    return "\n".join(parts)


def _reachable() -> set[str]:
    """Templates a route renders, plus everything they pull in."""
    code = _code_text()
    frontier = set(_RENDER_RE.findall(code)) & _all_templates()
    seen: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        path = TEMPLATE_DIR / name
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        refs = (_EXTENDS_RE.findall(body) + _INCLUDE_RE.findall(body)
                + _FROM_RE.findall(body))
        for ref in refs:
            if ref in _all_templates():
                frontier.add(ref)
        for prefix in _INCLUDE_PREFIX_RE.findall(body):
            for candidate in _all_templates():
                if candidate.startswith(prefix):
                    frontier.add(candidate)
    return seen


def test_the_scan_found_the_app() -> None:
    """Guard the guard: a broken regex would make every claim below pass."""
    reachable = _reachable()
    assert len(reachable) >= 15, (
        f"only {len(reachable)} templates looked reachable — the scan is "
        f"broken, not the app: {sorted(reachable)}")
    for expected in ("base.html", "index.html", "test_cases.html",
                     "_project_picker.html",
                     # Reached by `{% from %}`, not include.
                     "_inline_edit.html", "_bulk_bar.html",
                     # Reached by a computed include name.
                     "guide/_sections_en.html", "guide/_sections_ua.html"):
        assert expected in reachable, (
            f"{expected} is rendered, extended or included and the scan "
            f"missed it")


def test_no_new_unreachable_template() -> None:
    unreachable = sorted(_all_templates() - _reachable())
    unexpected = [t for t in unreachable if t not in UNREACHABLE]
    assert not unexpected, (
        "these templates are not rendered by any route and are not "
        "extended or included by one that is. Either wire them up or add "
        f"them to UNREACHABLE with the reason: {unexpected}")


def test_the_allowlist_has_no_stale_entries() -> None:
    unreachable = _all_templates() - _reachable()
    gone = sorted(name for name in UNREACHABLE
                  if name not in _all_templates())
    revived = sorted(name for name in UNREACHABLE
                     if name in _all_templates() and name not in unreachable)
    assert not gone, (
        f"UNREACHABLE names templates that no longer exist: {gone}")
    assert not revived, (
        "these are reachable now and should come off the list: "
        f"{revived}")
