"""Platform search UI (ADR 0003 §1.3, task T7).

What a rendered page carries, in each flag state:

* ``PLATFORM_SEARCH`` off → no search markup, no script, no hint mounts —
  the page is what it was before T7;
* on → the header combobox with its ARIA wiring, the results view, the
  dictionary block the script reads, a hint mount on each module page;
* ``PLATFORM_SEARCH_AI`` off (or undeclared, while T6 has not landed) → no
  AI block and no Ask panel; on → both, with their landmarks.

The behaviour in a browser (typing, the stream, the citations) is covered
by the Playwright scenario in ``tests/test_e2e_golden_paths.py``.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from engine.search import hints as _hints
from engine.search import ui as _ui

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
SEARCH_JS = ROOT / "static" / "js" / "search.js"

#: Module pages that carry a hint mount (owner item 6), by URL.
PAGES = {
    "/test-cases": "test_cases",
    "/checklist": "checklist",
    "/bug-reports": "bug_reports",
    "/test-execution": "executions",
    "/estimation": "estimation",
    "/automation": "automation",
}
#: Template → the page id it passes to the include.
TEMPLATE_PAGES = {
    "test_cases.html": "test_cases",
    "checklist.html": "checklist",
    "bug_reports.html": "bug_reports",
    "test_execution.html": "executions",
    "estimation.html": "estimation",
    "automation.html": "automation",
    "org_settings.html": "settings",
}


@pytest.fixture
def search_off(monkeypatch):
    monkeypatch.delenv("PLATFORM_SEARCH", raising=False)
    monkeypatch.delenv("PLATFORM_SEARCH_AI", raising=False)


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_SEARCH", "1")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.delenv("PLATFORM_SEARCH_AI", raising=False)
    monkeypatch.setattr(_ui, "ai_enabled", lambda: False)


@pytest.fixture
def ai_on(search_on, monkeypatch):
    monkeypatch.setattr(_ui, "ai_enabled", lambda: True)


def _page(client, path, lang="en") -> str:
    resp = client.get(f"{path}?lang={lang}")
    assert resp.status_code == 200, (path, resp.status_code)
    return resp.get_data(as_text=True)


def _tag(html: str, element_id: str) -> str:
    m = re.search(r"<[a-z]+\b[^>]*\bid=\"%s\"[^>]*>" % re.escape(element_id),
                  html, re.S)
    assert m, f"no element with id={element_id!r}"
    return m.group(0)


def _i18n_block(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="tf-search-i18n">'
                  r"(.*?)</script>", html, re.S)
    assert m, "the dictionary block the script reads is missing"
    return json.loads(m.group(1))


# ── Flag off: nothing ─────────────────────────────────────────────────

class TestFlagOff:
    @pytest.mark.parametrize("path", sorted(PAGES))
    def test_no_search_markup(self, client, search_off, path):
        html = _page(client, path)
        for needle in ('id="tf-search"', "js/search.js", "data-hints",
                       'role="combobox"', "tf-ask", "tf-search-i18n"):
            assert needle not in html, (path, needle)

    def test_flag_without_its_prerequisite_is_off(self, client, monkeypatch):
        monkeypatch.setenv("PLATFORM_SEARCH", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        assert 'id="tf-search"' not in _page(client, "/test-cases")
        assert _ui.search_ui_context() is None


# ── Flag on: field, results view, hints ───────────────────────────────

class TestFlagOn:
    def test_the_field_is_a_combobox_over_a_listbox(self, client, search_on):
        html = _page(client, "/test-cases")
        field = _tag(html, "tf-search-input")
        assert 'role="combobox"' in field
        assert 'aria-expanded="false"' in field
        assert 'aria-controls="tf-search-listbox"' in field
        assert 'aria-autocomplete="list"' in field
        assert re.search(r'<label class="sr-only" for="tf-search-input">',
                         html), "the field needs a label"
        listbox = _tag(html, "tf-search-listbox")
        assert 'role="listbox"' in listbox and "aria-label=" in listbox
        assert 'role="search"' in html
        assert 'data-search-url="/api/search"' in html

    def test_the_results_view_is_a_labelled_region(self, client, search_on):
        html = _page(client, "/test-cases")
        section = _tag(html, "tf-results")
        assert 'aria-labelledby="tf-results-title"' in section
        assert "hidden" in section
        assert 'id="tf-results-title"' in html
        assert 'aria-live="polite"' in _tag(html, "tf-results-meta")

    def test_the_script_is_loaded_with_the_nonce(self, client, search_on):
        html = _page(client, "/test-cases")
        m = re.search(r'<script src="[^"]*js/search\.js"[^>]*>', html)
        assert m and "nonce=" in m.group(0)

    def test_strings_come_from_the_dictionary(self, client, search_on):
        en = _i18n_block(_page(client, "/test-cases", "en"))
        ua = _i18n_block(_page(client, "/test-cases", "ua"))
        assert en["group_test_case"] == "Test cases"
        assert ua["group_test_case"] == "Тест-кейси"
        assert set(en) == set(ua)
        # Every value is a real translation, not a key echoed back.
        assert not [k for k, v in en.items() if not v or v.startswith(
            ("search_", "ask_"))]
        assert "{q}" in en["see_all"] and "{q}" in ua["see_all"]
        html_ua = _page(client, "/test-cases", "ua")
        assert 'placeholder="Шукати тест-кейси' in html_ua

    @pytest.mark.parametrize("path,page", sorted(PAGES.items()))
    def test_each_module_page_has_a_hint_mount(self, client, search_on,
                                               path, page):
        html = _page(client, path)
        assert f'data-hints-page="{page}"' in html
        assert 'data-hints-url="/api/hints"' in html
        toggle = re.search(r"<button[^>]*data-hints-toggle[^>]*>", html, re.S)
        assert toggle and 'aria-expanded="false"' in toggle.group(0)
        assert f'aria-controls="tf-hints-{page}"' in toggle.group(0)

    def test_nothing_on_the_sign_in_page(self, anon_client, search_on,
                                         monkeypatch):
        """Signed out, every search endpoint refuses — and the Ask form's
        submit button sat in front of the sign-in form's own."""
        monkeypatch.setenv("AUTH_ENABLED", "1")
        resp = anon_client.get("/auth/login?lang=en")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="tf-search"' not in html and "js/search.js" not in html

    def test_no_ai_without_the_ai_flag(self, client, search_on):
        html = _page(client, "/test-cases")
        assert 'data-ai="0"' in html
        for needle in ('id="tf-ask"', 'id="tf-ai"', "data-ask-open",
                       "data-ai-get"):
            assert needle not in html, needle


class TestAiOn:
    def test_the_ai_block_and_the_ask_panel_render(self, client, ai_on):
        html = _page(client, "/test-cases")
        assert 'data-ai="1"' in html
        panel = _tag(html, "tf-ask")
        assert 'role="dialog"' in panel
        assert 'aria-modal="false"' in panel        # no focus trap
        assert 'aria-labelledby="tf-ask-title"' in panel
        assert "hidden" in panel
        assert 'role="log"' in _tag(html, "tf-ask-log")
        assert 'aria-live="polite"' in _tag(html, "tf-ai-text")
        assert 'aria-labelledby="tf-ai-title"' in _tag(html, "tf-ai")
        opener = re.search(r"<button[^>]*data-ask-open[^>]*>", html, re.S)
        assert opener and 'aria-controls="tf-ask"' in opener.group(0)
        assert "data-ask-stop" in html and "data-ask-reset" in html

    def test_ask_strings_are_translated(self, client, ai_on):
        html = _page(client, "/test-cases", "ua")
        assert ">Запитати<" in html
        block = _i18n_block(html)
        assert block["err_budget"].startswith("Місячний ліміт")


# ── The flag read ─────────────────────────────────────────────────────

class TestFlagRead:
    def test_an_undeclared_ai_flag_reads_as_off(self, monkeypatch):
        from engine import features
        monkeypatch.setenv("PLATFORM_SEARCH_AI", "1")
        monkeypatch.delitem(features.FLAGS, "PLATFORM_SEARCH_AI",
                            raising=False)
        assert _ui.ai_enabled() is False

    def test_a_declared_ai_flag_is_honoured(self, monkeypatch):
        from engine import features
        monkeypatch.setitem(features.FLAGS, "PLATFORM_SEARCH_AI",
                            features.Flag("PLATFORM_SEARCH_AI", False, "E13",
                                          "test"))
        monkeypatch.setenv("PLATFORM_SEARCH", "1")
        monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
        monkeypatch.setenv("PLATFORM_SEARCH_AI", "1")
        assert _ui.ai_enabled() is True
        assert _ui.search_ui_context() == {"ai": True}
        monkeypatch.setenv("PLATFORM_SEARCH_AI", "0")
        assert _ui.search_ui_context() == {"ai": False}


# ── Wiring that a render cannot show ──────────────────────────────────

class TestWiring:
    @pytest.mark.parametrize("name,page", sorted(TEMPLATE_PAGES.items()))
    def test_templates_mount_hints_for_a_page_the_api_knows(self, name, page):
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        assert (f"{{% with hints_page='{page}' %}}"
                f"{{% include '_search_hints.html' %}}") in src
        assert _hints.canonical_page(page) == page

    def test_the_script_streams_ndjson_and_can_stop(self):
        js = SEARCH_JS.read_text(encoding="utf-8")
        assert "getReader()" in js and "TextDecoder" in js
        assert "new AbortController()" in js
        assert "'X-CSRFToken'" in js
        assert "quick=1" in js
        assert "DEBOUNCE_MS = 150" in js
        # localStorage only inside try/catch helpers.
        uses = [m.start() for m in re.finditer(r"localStorage", js)]
        assert len(uses) == 2
        for pos in uses:
            assert "try {" in js[max(0, pos - 40):pos]

    def test_snippets_are_the_only_html_the_script_injects(self):
        js = SEARCH_JS.read_text(encoding="utf-8")
        assert js.count(".innerHTML") == 1               # inside el()
        assert len(re.findall(r"\bhtml:\s", js)) == 1    # r.snippet only
        assert "html: r.snippet" in js
