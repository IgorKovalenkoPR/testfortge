"""Platform search in a real browser (ADR 0003 §1.3, task T7) — beyond the
one walk ``tests/test_e2e_golden_paths.py::TestPlatformSearch`` takes.

That walk proves the parts join up. This file pins each behaviour a person
leans on, one test each, so a failure names the behaviour that broke:

* the header field as a keyboard combobox — "/" focuses, the grouped
  listbox opens, the arrows move ``aria-activedescendant``, Esc closes,
  Enter opens the results;
* what the results view *says* about the query: the "Translated:" line
  for a Ukrainian query, "Corrected:" for a typo, the relaxed-filter note,
  and the "How this was found" table of scores;
* the Ask panel over a stubbed ``/api/ask`` — the NDJSON contract
  (meta, text ×3, chips, done) arriving in the ``aria-live`` log, a
  ``[[TC-001]]`` citation becoming a link, a chip sending a follow-up that
  carries the history, Stop aborting a stream mid-answer, Reset clearing;
* the three refusal codes a person can actually hit (budget, rate_limited,
  no_key) rendered as words rather than codes;
* the contextual hints, opened from the "?" beside the page title;
* nothing scrolling sideways at 375 px.

**The stubs.** ``/api/ask`` is answered by ``page.route`` — the browser
never reaches the server for it, so there is no model, no key and no
network. The one exception is Stop: a ``page.route`` handler can only
fulfil a body whole, so "abort *mid*-stream" needs a server that has sent
part of an answer and not the rest. The live server runs in a thread of
this process, so ``engine.search.answer.ask_events`` is replaced there by a
generator that yields one text event and then waits on an Event the test
releases — the same seam ``tests/test_search_ask.py`` uses for the SDK.

**Conventions** are the golden paths' own and its fixtures are reused, not
copied: authenticated, CSRF on, every wait on a condition. Skipped when
Playwright or Chromium is missing, like its siblings.
"""
from __future__ import annotations

import json
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from tests.test_e2e_golden_paths import (  # noqa: E402,F401 — fixtures
    WAIT, Actor, _create_project, _project_id, actors, browser, live_server,
    search_on, team,
)

#: A phone. 375 × 812 is the narrowest viewport the restyle was checked at.
PHONE = {"width": 375, "height": 812}


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def shop(actors, team, search_on):
    """An admin, signed in, with the search golden project active and
    indexed. Returns the actor; the project id is on ``actor.project_id``."""
    from engine.search import indexer
    from tests._search_seed import seed_golden

    admin = actors(team["admin"]["email"]).sign_in()
    name = f"Search {team['tag']}"
    _create_project(admin, name)
    pid = _project_id(team["org"], name)
    seed_golden(pid)
    indexer.reindex_project(pid)
    admin.project_id = pid
    return admin


@pytest.fixture
def phone(browser, live_server, team, search_on):
    """The same admin as ``shop`` would be, in a 375 px context."""
    made = []

    def _phone(email: str) -> Actor:
        person = Actor(browser, live_server, email)
        person.context.close()
        # No ``is_mobile``: mobile emulation lets Chromium widen the layout
        # viewport to fit overflowing content, so innerWidth grows with the
        # page and "scrollWidth <= innerWidth" holds however wide it is —
        # this check passed over a 575 px dashboard until that was noticed.
        person.context = browser.new_context(viewport=PHONE)
        person.page = person.context.new_page()
        made.append(person)
        return person

    yield _phone
    for person in made:
        person.close()


def _ndjson(*events) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


#: The full T6 contract, with the answer split over three text events so
#: "streamed" means more than one chunk was stitched together.
ANSWER_STREAM = _ndjson(
    {"type": "meta", "mode": "hybrid", "cached": False, "grounded": True,
     "model_tier": "haiku",
     "items": [{"n": 1, "ref": "TC-001", "kind": "test_case",
                "title": "Login with valid credentials",
                "url": "/test-cases?focus=TC-001"}]},
    {"type": "text", "text": "Signing in with a valid password "},
    {"type": "text", "text": "is covered by [[TC-001]]"},
    {"type": "text", "text": " and by nothing else."},
    {"type": "chips", "chips": ["Which bugs mention login?"]},
    {"type": "done", "cached": False,
     "usage": {"input_tokens": 1, "output_tokens": 1},
     "citations": ["TC-001"]},
)


def _error_stream(code: str) -> str:
    return _ndjson(
        {"type": "meta", "mode": "hybrid", "cached": False, "grounded": True,
         "items": []},
        {"type": "error", "code": code, "message": "server text, not shown"},
        {"type": "chips", "chips": []},
        {"type": "done", "cached": False,
         "usage": {"input_tokens": 0, "output_tokens": 0}, "citations": []},
    )


class AskStub:
    """``page.route('**/api/ask')`` answering *body*; records each request."""

    def __init__(self, page, body: str):
        self.body = body
        self.requests: list[dict] = []
        page.route("**/api/ask", self._handle)

    def _handle(self, route):
        self.requests.append(route.request.post_data_json)
        route.fulfill(status=200, body=self.body,
                      headers={"Content-Type": "application/x-ndjson"})


def _open_ask(page):
    page.click("[data-ask-open]")
    page.wait_for_selector("#tf-ask:not([hidden])", timeout=WAIT)


def _ask(page, question: str):
    page.fill("#tf-ask-input", question)
    page.keyboard.press("Enter")


def _search(page, q: str):
    """Type *q* into the header field and press Enter with no option
    active — the results view, not an item."""
    page.locator("body").press("/")
    field = page.locator("#tf-search-input")
    field.fill(q)
    field.press("Enter")
    page.wait_for_selector("#tf-results:not([hidden]) .tf-results-count",
                           timeout=WAIT)


def _no_horizontal_overflow(page) -> None:
    sizes = page.evaluate(
        "() => ({scroll: document.documentElement.scrollWidth,"
        "        inner: window.innerWidth})")
    # Asserted, so the comparison below cannot become vacuous again.
    assert sizes["inner"] == PHONE["width"], sizes
    assert sizes["scroll"] <= sizes["inner"], (
        f"the page scrolls sideways: scrollWidth {sizes['scroll']} > "
        f"innerWidth {sizes['inner']}; widest: " + json.dumps(page.evaluate(
            """() => [...document.querySelectorAll('body *')]
                 .filter(e => {
                     if (e.getBoundingClientRect().right
                         <= window.innerWidth + 0.5) return false;
                     for (let a = e.parentElement; a && a !== document.body;
                          a = a.parentElement) {
                         if (getComputedStyle(a).overflowX !== 'visible')
                             return false;      // clipped by a scroller
                     }
                     return true;
                 })
                 .slice(0, 5)
                 .map(e => e.tagName + '.' + e.className + ' ' +
                           Math.round(e.getBoundingClientRect().right))"""
        )))


# ── 1. The combobox, by keyboard only ───────────────────────────────

class TestTheCombobox:
    def test_slash_arrows_escape_enter(self, shop):
        page = shop.page
        shop.go("/test-cases?lang=en")

        page.locator("body").press("/")
        field = page.locator("#tf-search-input")
        assert field.evaluate("el => el === document.activeElement"), \
            "'/' did not move focus to the search field"

        field.type("password", delay=20)
        page.wait_for_selector("#tf-search-listbox [role=option]",
                               timeout=WAIT)
        assert field.get_attribute("aria-expanded") == "true"
        groups = page.locator("#tf-search-listbox [role=group]")
        assert groups.count() >= 2, "the listbox is not grouped"
        # Every group is labelled by an element that exists.
        for i in range(groups.count()):
            g = groups.nth(i)
            label_id = g.get_attribute("aria-labelledby")
            if label_id:
                assert page.locator(f'[id="{label_id}"]').count() == 1, label_id
            else:
                assert g.get_attribute("aria-label"), "unlabelled group"

        assert field.get_attribute("aria-activedescendant") is None
        page.keyboard.press("ArrowDown")
        first = field.get_attribute("aria-activedescendant")
        assert first and page.locator(
            f'[id="{first}"][role=option][aria-selected=true]').count() == 1
        page.keyboard.press("ArrowDown")
        second = field.get_attribute("aria-activedescendant")
        assert second and second != first, \
            "ArrowDown did not move the active option"
        assert page.locator(f'[id="{first}"]').get_attribute(
            "aria-selected") == "false"

        page.keyboard.press("Escape")
        assert field.get_attribute("aria-expanded") == "false"
        assert field.get_attribute("aria-activedescendant") is None
        assert page.locator("#tf-search-dropdown").is_hidden()

        page.keyboard.press("Enter")
        page.wait_for_selector("#tf-results .tf-hit", timeout=WAIT)
        assert page.locator("#tf-results").is_visible()
        # Focus goes to the results heading, so a screen reader hears it.
        # Selector waits, not wait_for_function: a string predicate is
        # re-evaluated with eval() in the page, which the app's CSP refuses
        # whenever the first check is not already true — a timing flake.
        page.wait_for_selector("#tf-results-title:focus", timeout=WAIT)

        # Esc from the results view closes it and returns to the field.
        page.keyboard.press("Escape")
        assert page.locator("#tf-results").is_hidden()
        assert field.evaluate("el => el === document.activeElement")


# ── 2. What the results view says about the query ───────────────────

class TestTheResultsExplainThemselves:
    def test_a_ukrainian_query_says_what_it_was_translated_to(self, shop):
        shop.go("/test-cases?lang=en")
        _search(shop.page, "не працює вхід")
        note = shop.page.locator("#tf-results-notes .tf-note-translated")
        note.wait_for(timeout=WAIT)
        text = note.inner_text()
        assert text.startswith("Translated:"), text
        assert "вхід → login" in text, text

    def test_a_typo_says_what_it_was_corrected_to(self, shop):
        shop.go("/test-cases?lang=en")
        _search(shop.page, "paswword reset")
        note = shop.page.locator("#tf-results-notes .tf-note-corrected")
        note.wait_for(timeout=WAIT)
        text = note.inner_text()
        assert text.startswith("Corrected:"), text
        assert "paswword → password" in text, text
        # And the results are for the corrected word.
        refs = shop.page.locator("#tf-results-list .tf-hit-ref").all_inner_texts()
        assert {"TC-006", "TC-007"} & set(refs[:5]), refs

    def test_how_this_was_found_lists_the_scores(self, shop):
        page = shop.page
        shop.go("/test-cases?lang=en")
        _search(page, "password")
        toggle = page.locator("#tf-results-meta .tf-how-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.click()
        page.wait_for_selector("#tf-results-how .tf-how-table", timeout=WAIT)
        assert toggle.get_attribute("aria-expanded") == "true"

        headers = page.locator("#tf-results-how thead th").all_inner_texts()
        assert len(headers) == 4, headers
        rows = page.locator("#tf-results-how tbody tr")
        assert rows.count() >= 1
        hits = page.locator("#tf-results-list .tf-hit").count()
        assert rows.count() == min(hits, 10)
        # Each row: a ref, two rank cells and a numeric fused score.
        for i in range(rows.count()):
            cells = rows.nth(i).locator("td").all_inner_texts()
            assert len(cells) == 3, cells
            fused = cells[2].split(" ")[0]
            assert float(fused) > 0, cells
            assert any(c.startswith("#") for c in cells[:2]), cells

        # Esc folds the disclosure and gives focus back to its toggle.
        page.keyboard.press("Escape")
        assert page.locator("#tf-results-how").is_hidden()
        assert toggle.evaluate("el => el === document.activeElement")

    def test_a_filter_that_empties_the_list_is_relaxed_and_said_so(
            self, shop):
        """The UI has no "status" control of its own — the filter arrives
        from a refinement chip or a link. The request is rewritten on its
        way to the *real* server, so the note is the server's relaxing,
        rendered; nothing about the response is faked."""
        page = shop.page

        def _with_impossible_status(route):
            parts = urlsplit(route.request.url)
            query = dict(parse_qsl(parts.query))
            if "quick" not in query:
                query["status"] = "no-such-status"
            route.continue_(url=urlunsplit(parts._replace(
                query=urlencode(query))))

        page.route("**/api/search?*", _with_impossible_status)
        shop.go("/test-cases?lang=en")
        _search(page, "login")
        note = page.locator("#tf-results-notes .tf-note-relaxed")
        note.wait_for(timeout=WAIT)
        assert note.get_attribute("role") == "note"
        assert note.inner_text() == (
            "No results with status = no-such-status, "
            "so that filter was relaxed."), note.inner_text()
        assert page.locator("#tf-results-list .tf-hit").count() >= 1


# ── 3. The Ask panel ────────────────────────────────────────────────

class TestTheAskPanel:
    def test_a_streamed_answer_lands_in_the_live_log_with_a_link(self, shop):
        page = shop.page
        stub = AskStub(page, ANSWER_STREAM)
        shop.go("/test-cases?lang=en")
        _open_ask(page)
        assert page.locator("[data-ask-open]").get_attribute(
            "aria-expanded") == "true"
        assert page.locator("#tf-ask-input").evaluate(
            "el => el === document.activeElement")

        _ask(page, "Is a valid login covered?")
        answer = page.locator("#tf-ask .tf-ask-a").last
        page.wait_for_selector("#tf-ask .tf-ask-a[aria-busy=false]",
                               timeout=WAIT)

        # The three text events, joined, inside the live region.
        log = page.locator("#tf-ask-log")
        assert log.get_attribute("aria-live") == "polite"
        assert log.get_attribute("role") == "log"
        assert answer.evaluate(
            "el => !!el.closest('#tf-ask-log[aria-live]')")
        assert answer.inner_text() == (
            "Signing in with a valid password is covered by TC-001 "
            "and by nothing else."), answer.inner_text()
        assert "[[" not in answer.inner_text()

        cite = answer.locator("a.tf-cite")
        assert cite.count() == 1
        assert cite.inner_text() == "TC-001"
        assert cite.get_attribute("href") == "/test-cases?focus=TC-001"

        # The request: the question, scoped to the active project.
        assert len(stub.requests) == 1
        sent = stub.requests[0]
        assert sent["q"] == "Is a valid login covered?"
        assert sent["history"] == []
        assert sent["project_id"] == shop.project_id

        # Cited card + meta line.
        assert page.locator("#tf-ask .tf-ask-card a").first.get_attribute(
            "href") == "/test-cases?focus=TC-001"
        assert "1 sources" in page.locator(
            "#tf-ask .tf-ask-meta").last.inner_text()

    def test_a_chip_sends_a_follow_up_with_the_history(self, shop):
        page = shop.page
        stub = AskStub(page, ANSWER_STREAM)
        shop.go("/test-cases?lang=en")
        _open_ask(page)
        _ask(page, "Is a valid login covered?")
        chip = page.locator(
            "#tf-ask button.tf-chip >> text=Which bugs mention login?")
        chip.wait_for(timeout=WAIT)
        chip.click()

        page.locator("#tf-ask .tf-ask-a[aria-busy=false]").nth(1).wait_for(
            timeout=WAIT)
        assert page.locator("#tf-ask .tf-ask-turn").count() == 2
        assert len(stub.requests) == 2, stub.requests
        follow = stub.requests[1]
        assert follow["q"] == "Which bugs mention login?"
        assert len(follow["history"]) == 1
        assert follow["history"][0]["q"] == "Is a valid login covered?"
        assert "TC-001" in follow["history"][0]["a"]
        assert "Which bugs mention login?" in page.locator(
            "#tf-ask .tf-ask-q").last.inner_text()

    def test_stop_aborts_a_stream_mid_answer(self, shop, monkeypatch):
        """Server-side slow stub (see the module docstring for why)."""
        from engine.search import answer as answer_mod

        release = threading.Event()
        reached = threading.Event()

        def _slow(req):
            yield {"type": "meta", "mode": "hybrid", "cached": False,
                   "grounded": True, "items": []}
            yield {"type": "text", "text": "The first half arrived "}
            reached.set()
            release.wait(timeout=30)
            yield {"type": "text", "text": "AND THE SECOND HALF TOO."}
            yield {"type": "chips", "chips": []}
            yield {"type": "done", "cached": False, "citations": [],
                   "usage": {"input_tokens": 0, "output_tokens": 0}}

        monkeypatch.setattr(answer_mod, "ask_events", _slow)
        page = shop.page
        try:
            shop.go("/test-cases?lang=en")
            _open_ask(page)
            _ask(page, "Tell me slowly")
            answer = page.locator("#tf-ask .tf-ask-a").last
            page.wait_for_selector(
                "#tf-ask .tf-ask-a >> text=The first half arrived",
                timeout=WAIT)
            assert reached.is_set()
            stop = page.locator("#tf-ask [data-ask-stop]")
            assert stop.is_visible(), "no Stop button while streaming"
            assert page.locator("#tf-ask [data-ask-send]").is_hidden()
            stop.click()

            page.wait_for_selector("#tf-ask .tf-ask-note >> text=Stopped.",
                                   timeout=WAIT)
            assert answer.get_attribute("aria-busy") == "false"
            assert page.locator("#tf-ask [data-ask-send]").is_visible()
            assert stop.is_hidden()
        finally:
            release.set()
        # The rest of the stream never reaches the page.
        page.wait_for_timeout(300)
        assert "SECOND HALF" not in answer.inner_text()
        assert "The first half arrived" in answer.inner_text()

    def test_reset_clears_the_conversation(self, shop):
        page = shop.page
        stub = AskStub(page, ANSWER_STREAM)
        shop.go("/test-cases?lang=en")
        _open_ask(page)
        _ask(page, "Is a valid login covered?")
        page.wait_for_selector("#tf-ask .tf-ask-meta", timeout=WAIT)
        assert page.locator("#tf-ask [data-ask-empty]").is_hidden()

        page.click("#tf-ask [data-ask-reset]")
        assert page.locator("#tf-ask .tf-ask-turn").count() == 0
        assert page.locator("#tf-ask [data-ask-empty]").is_visible()

        # And the history went with it: the next question starts afresh.
        _ask(page, "A new question")
        page.wait_for_selector("#tf-ask .tf-ask-meta", timeout=WAIT)
        assert stub.requests[-1]["history"] == []

    @pytest.mark.parametrize("code,words", [
        ("budget", "AI allowance for this month is used up"),
        ("rate_limited", "Too many questions in a short time"),
        ("no_key", "AI answers are not configured for this team"),
    ])
    def test_a_refusal_is_said_in_words(self, shop, code, words):
        page = shop.page
        AskStub(page, _error_stream(code))
        shop.go("/test-cases?lang=en")
        _open_ask(page)
        _ask(page, "Anything at all?")
        alert = page.locator("#tf-ask .tf-ask-error[role=alert]")
        alert.wait_for(timeout=WAIT)
        text = alert.inner_text()
        assert words in text, text
        assert code not in text, f"the raw code {code!r} reached the page"
        assert "server text" not in text
        # A refusal does not pretend to have sources.
        assert page.locator("#tf-ask .tf-ask-meta").count() == 0


# ── 4. Hints ────────────────────────────────────────────────────────

class TestHints:
    """The page tips sit in a toggletip beside the title (owner ruling
    2026-10-16; the always-open card is gone). Closed on arrival, fetched
    on the first open, closed by Escape."""

    @pytest.mark.parametrize("path,page_key", [
        ("/test-cases?lang=en", "test_cases"),
        ("/bug-reports?lang=en", "bug_reports"),
    ])
    def test_hints_open_from_the_title_and_close_on_escape(self, shop, path,
                                                           page_key):
        page = shop.page
        shop.go(path)
        mount = page.locator(f'[data-hints][data-hints-page="{page_key}"]')
        assert mount.count() == 1, f"no hints mount on {path}"
        assert mount.locator("xpath=..").get_attribute("class") == "tf-title-row"
        toggle = mount.locator(".tf-tip-btn")
        note = mount.locator(".tf-tip-bubble")
        page.wait_for_load_state("load", timeout=WAIT)
        assert note.is_hidden(), "the tips opened on arrival"
        assert toggle.get_attribute("aria-expanded") == "false"

        toggle.click()
        mount.locator(".tf-hint").first.wait_for(timeout=WAIT)
        assert toggle.get_attribute("aria-expanded") == "true"
        assert mount.locator(".tf-hint-link").count() >= 1

        page.keyboard.press("Escape")
        assert note.is_hidden()
        assert toggle.get_attribute("aria-expanded") == "false"


# ── 5. A phone ──────────────────────────────────────────────────────

class TestNarrowViewport:
    def test_nothing_scrolls_sideways_at_375px(self, phone, team, search_on):
        from engine.search import indexer
        from tests._search_seed import seed_golden

        person = phone(team["admin"]["email"]).sign_in()
        name = f"Phone {team['tag']}"
        _create_project(person, name)
        pid = _project_id(team["org"], name)
        seed_golden(pid)
        indexer.reindex_project(pid)
        page = person.page

        person.go("/?lang=en")
        page.wait_for_selector("#tf-search-input", timeout=WAIT)
        # The active-project picker and the projects table are the two
        # things that widened this page; both must be on it.
        assert page.locator("#pp-select").count() == 1
        assert page.locator(".data-table").count() >= 1
        _no_horizontal_overflow(page)

        # And the check itself can fail: plant something too wide.
        page.evaluate("""() => { const d = document.createElement('div');
            d.id = 'tf-too-wide'; d.style.width = '600px';
            d.style.height = '1px';
            document.getElementById('main-content').appendChild(d); }""")
        with pytest.raises(AssertionError, match="tf-too-wide|scrolls"):
            _no_horizontal_overflow(page)
        page.evaluate("() => document.getElementById('tf-too-wide').remove()")

        person.go("/test-cases?lang=en")
        _search(page, "password")
        page.wait_for_selector("#tf-results .tf-hit", timeout=WAIT)
        page.click("#tf-results-meta .tf-how-toggle")
        page.wait_for_selector("#tf-results-how .tf-how-table", timeout=WAIT)
        _no_horizontal_overflow(page)

        AskStub(page, ANSWER_STREAM)
        _open_ask(page)
        _ask(page, "Is a valid login covered?")
        page.wait_for_selector("#tf-ask .tf-ask-meta", timeout=WAIT)
        _no_horizontal_overflow(page)
        # The panel itself fits the screen.
        box = page.locator("#tf-ask").bounding_box()
        assert box and box["x"] >= 0 and \
            box["x"] + box["width"] <= PHONE["width"] + 0.5, box
