"""No template may carry an inline event-handler attribute.

The app sends ``script-src 'self' 'nonce-…' https://unpkg.com`` with no
``'unsafe-inline'`` (``app.py:_apply_security_headers``). A nonce whitelists
a ``<script>`` *element*; it does not whitelist a handler *attribute*. So an
``onclick=`` in a template never runs, and the only trace is a console
message nobody reads — a click on the control behaves like a click on
nothing.

Thirty-one had accumulated across ``templates/`` by E11. The reported
symptom was one dead tab. The dangerous ones were silent: four
``onsubmit="return confirm(…)"`` guards on **destructive** actions — move
artefacts, new session, delete project, discard pack — which meant each of
those fired on a single click with no dialog, because the handler that would
have returned ``false`` never ran. A blocked confirmation does not fail
closed; it fails *open*.

``tests/test_inline_edit_component.py`` has asserted this property since
E4.2, but only against ``templates/_inline_edit.html`` — so a guard existed,
passed, and watched thirty-one violations accumulate in the files it did not
look at. This one globs every template.

Behaviour under a real click belongs in a browser and is verified there; what
CI can pin is that the attribute is absent and the delegated handler that
replaced it is actually loaded.
"""
from __future__ import annotations

import pathlib
import re

import pytest

TEMPLATES = pathlib.Path("templates")
BASE = TEMPLATES / "base.html"
HANDLERS = pathlib.Path("static/js/ui-handlers.js")

#: Any ``on<event>=`` attribute. Deliberately broad: the failure mode is
#: identical for every event name, so an allow-list of the ones we happen to
#: use today would let the next one through.
INLINE_HANDLER = re.compile(r"""\bon[a-z]{3,20}\s*=\s*["']""", re.IGNORECASE)

#: Attributes that read like handlers but are not.
NOT_HANDLERS = {"once", "only"}


def _templates() -> list[pathlib.Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_there_are_templates_to_check():
    """Guard the guard — a bad glob would make every case below vacuous."""
    found = _templates()
    assert len(found) >= 20, f"only {len(found)} templates found"


@pytest.mark.parametrize(
    "template", _templates(), ids=lambda p: p.name)
def test_no_inline_event_handler(template: pathlib.Path):
    # errors="replace": templates/index.html contains a stray NUL byte,
    # which is its own (harmless, unrelated) oddity. Decoding must not be
    # what decides whether this guard runs.
    text = template.read_text(encoding="utf-8", errors="replace")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in INLINE_HANDLER.finditer(line):
            attr = match.group(0).split("=")[0].strip().lower()
            if attr[2:] in NOT_HANDLERS:
                continue
            offenders.append(f"{template}:{lineno}: {line.strip()[:90]}")
    assert not offenders, (
        "inline handler attributes are blocked by the CSP and never run:\n"
        + "\n".join(offenders))


class TestTheReplacementIsWiredUp:
    """An absent attribute is only half the property.

    Removing ``onclick=`` without loading the delegated handler would swap a
    silently-dead control for a differently silently-dead control.
    """

    def test_the_delegated_handler_file_exists(self):
        assert HANDLERS.is_file(), f"{HANDLERS} is missing"

    def test_base_loads_it_with_a_nonce(self):
        base = BASE.read_text(encoding="utf-8")
        assert "js/ui-handlers.js" in base, (
            "ui-handlers.js is not loaded by base.html, so every data-*"
            " control it drives is inert")
        # Loaded from base rather than per-page on purpose: the data-confirm
        # guard protects destructive actions, and a page that forgot the
        # include would lose the confirmation without any sign.
        line = next(l for l in base.splitlines() if "js/ui-handlers.js" in l)
        assert "csp_nonce" in line, (
            "the script tag needs the nonce or the CSP blocks it too")

    def test_it_sets_no_handler_attributes_itself(self):
        js = HANDLERS.read_text(encoding="utf-8")
        # setAttribute('onclick', …) is the same hole with more steps.
        assert not re.search(r"setAttribute\(\s*['\"]on\w+", js)

    def test_it_uses_delegation(self):
        js = HANDLERS.read_text(encoding="utf-8")
        assert "addEventListener" in js
        # Delegated from the document, so markup rendered later is covered
        # without re-binding.
        assert re.search(r"document\.addEventListener", js)

    def test_no_eval_or_new_function(self):
        js = HANDLERS.read_text(encoding="utf-8")
        assert "eval(" not in js
        assert "new Function" not in js


class TestFilterChipsPointAtRealRows:
    """A filter whose scope selector matches nothing does nothing.

    ``data-filter-scope`` is a CSS selector naming the elements to show and
    hide, and ``data-filter-key`` names the ``data-*`` attribute to compare.
    Both are agreements with markup elsewhere in the same template, and
    getting either wrong produces chips that highlight correctly and filter
    nothing — the same silent failure the inline handlers had.
    """

    SCOPES = [
        ("test_cases.html", ".tc-card", ["category", "suite"]),
        ("checklist.html", ".cl-row", ["category"]),
    ]

    @pytest.mark.parametrize("template,scope,keys", SCOPES,
                             ids=[s[0] for s in SCOPES])
    def test_the_scope_and_keys_exist_in_the_template(
            self, template, scope, keys):
        text = (TEMPLATES / template).read_text(encoding="utf-8",
                                                errors="replace")
        chips = re.findall(r'data-filter-scope="([^"]+)"', text)
        assert chips, f"{template} declares no filter chips"
        assert set(chips) == {scope}, (
            f"{template} chips point at {set(chips)}, expected {scope!r}")

        # The scope has to name a class that some element actually carries.
        css_class = scope.lstrip(".")
        assert re.search(r'class="[^"]*\b' + re.escape(css_class) + r'\b',
                         text), (
            f"{template} has no element with class {css_class!r}, so the "
            "chips would filter an empty set")

        # And every key has to be a data attribute on those elements.
        for key in keys:
            assert f'data-{key}=' in text, (
                f"{template} rows carry no data-{key}, so filtering by "
                f"{key!r} compares against undefined and hides everything")
            assert f'data-filter-key="{key}"' in text


class TestDestructiveActionsStillAskFirst:
    """The four confirmations that were failing open.

    Named individually because each one guards a different irreversible
    action, and a sweep that converted three of four would look clean in a
    diff.
    """

    CASES = [
        ("index.html", "move_confirm"),
        ("index.html", "new_session_confirm"),
        ("index.html", "delete_confirm"),
        ("test_execution.html", "te_pack_clear_confirm"),
    ]

    @pytest.mark.parametrize("template,key", CASES,
                             ids=[f"{t}:{k}" for t, k in CASES])
    def test_the_guard_is_present_as_a_data_attribute(self, template, key):
        text = (TEMPLATES / template).read_text(encoding="utf-8",
                                                errors="replace")
        # The confirmation text still comes from i18n; only the mechanism
        # changed. Both must be on the same attribute.
        #
        # The window is `.{0,90}?` rather than "not a quote", because two of
        # these read the key through `{{ t.get('key', 'default') }}` — which
        # puts quotes *inside* the attribute value.
        pattern = re.compile(
            r"data-confirm\s*=\s*['\"].{0,90}?" + re.escape(key))
        assert pattern.search(text), (
            f"{template} lost its data-confirm for {key} — that action now "
            "runs on one click with no dialog")
