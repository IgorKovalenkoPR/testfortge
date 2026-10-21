"""Every generation says which limits that run hit (2026-10-21).

The Guide's "Limits" sections (tests/test_guide_limits.py) say where
generation stops in general; this is the per-run half. Before it, the
crawl's 50-page budget, the checklist heading budget, the form budget, the
WBS folder budgets, the requirement-line cap and the evenness trim were
logged or silent, and the Additional Instructions cap only left a marker
in the model's prompt — so a thinner pack read as the whole product.

Gated through the real routes on the three recorded crawls
(``crawl_site`` replaced by the fixture, the browser off), sync and async,
test cases and checklist. A limit the fixtures do not reach (the 90 page
folders, the 8 forms) is lowered for its test — the crawl stays real.

The flashes are read where they are made (Flask's ``message_flashed``
signal), not from the rendered page: a page shows its flashes in the
same response that produced them, and a landing page can carry a flash
some other path raised.
"""
from __future__ import annotations

import gzip
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import message_flashed

from engine import db
from engine.i18n import get_lang

_FIX = Path(__file__).parent / "fixtures"
_SITES = {"qarea": "https://qarea.com/", "testfort": "https://testfort.com/",
          "dou": "https://dou.ua/"}
_RUN = secrets.token_hex(4)
EN = get_lang("en")


def _crawl(name):
    return json.load(gzip.open(_FIX / f"{name}_crawl_2026-10-20.json.gz",
                               "rt", encoding="utf-8"))


def _analysis(name):
    from engine.site_crawler import PageInfo, SiteAnalysis
    data = dict(_crawl(name))
    pages = [PageInfo(**p) for p in data.pop("pages")]
    return SiteAnalysis(pages=pages, **data)


@contextmanager
def _flashes(app):
    got: list[tuple[str, str]] = []

    def _record(sender, message, category, **_):
        got.append((category, message))

    message_flashed.connect(_record, app)
    try:
        yield got
    finally:
        message_flashed.disconnect(_record, app)


def _setup(client, monkeypatch, site=None):
    monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    if site:
        import engine.site_crawler as sc
        analysis = _analysis(site)
        monkeypatch.setattr(sc, "crawl_site", lambda url, *a, **k: analysis)
    pid = db.upsert_project(name=f"Limits {site or 'text'} "
                                 f"{secrets.token_hex(3)} {_RUN}")
    with client.session_transaction() as sess:
        sess["project_id"] = pid
        sess["lang"] = "en"
    return pid


def _sync(app, client, route, text, prompt=""):
    with _flashes(app) as got:
        resp = client.post(route, data={"input_text": text,
                                        "custom_prompt": prompt})
        assert resp.status_code == 200, resp.status_code
    return got


def _async(app, client, route, text, prompt=""):
    """The path the UI takes: run-async, poll status, land on the page."""
    with _flashes(app) as got:
        resp = client.post(f"{route}/run-async",
                           data={"input_text": text, "custom_prompt": prompt})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        job_id = resp.get_json()["job_id"]
        deadline = time.time() + 120
        while time.time() < deadline:
            status = client.get(f"{route}/status/{job_id}").get_json()
            if status.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        assert status.get("status") == "done", status
        client.get(route)
    return got


def _notes(got, key, **params):
    """The flashed notes rendering ``key``'s English text."""
    head = EN[key].split("%(")[0]
    return [m for c, m in got if c == "info" and m.startswith(head)]


def _note_keys(got) -> set[str]:
    keys = [k for k in EN if k.startswith("gen_limit_")
            and not k.startswith("gen_limit_see_guide")]
    return {k for k in keys if _notes(got, k)}


_TC = ("/test-cases", "tc")
_CL = ("/checklist", "cl")


# ── The crawl budget ────────────────────────────────────────────────

@pytest.mark.parametrize("route,kind", [_TC, _CL])
@pytest.mark.parametrize("site", ["qarea", "dou"])
def test_the_crawl_budget_names_what_was_left_unread(app, client,
                                                     monkeypatch, site,
                                                     route, kind):
    from engine.site_crawler import MAX_PAGES, unread_links
    _setup(client, monkeypatch, site)
    got = _sync(app, client, route, _SITES[site])
    read, left = unread_links(_analysis(site))
    assert left > 0
    text = EN["gen_limit_crawl"] % {"read": read, "total": read + left,
                                    "left": left, "limit": MAX_PAGES}
    guide = EN[f"gen_limit_see_guide_{kind}"]
    assert ("info", f"{text} {guide}") in got, got


def test_the_live_crawler_counts_the_links_it_left(monkeypatch):
    """The crawler records what the budget left as it stops — the fixture
    derivation is for crawls saved before it did."""
    import engine.site_crawler as sc
    monkeypatch.setattr(sc, "MAX_PAGES", 3)
    sc._CRAWL_CACHE.clear()
    links = "".join(f'<a href="/p{i}">Page {i}</a>' for i in range(1, 7))
    html = f"<html><head><title>T</title></head><body>{links}</body></html>"
    monkeypatch.setattr(sc, "_fetch_page", lambda url: (html, None))
    monkeypatch.setattr(sc, "_landed_url", lambda: "")
    analysis = sc.crawl_site("https://example.test/")
    assert len(analysis.pages) == 3
    assert analysis.unread_links == 4          # /p3../p6 of /p1../p6
    assert sc.unread_links(analysis) == (3, 4)
    sc._CRAWL_CACHE.clear()


# ── The checklist heading budget ────────────────────────────────────

@pytest.mark.parametrize("site", ["qarea", "testfort"])
def test_the_heading_budget_names_the_sections_it_trimmed(app, client,
                                                          monkeypatch, site):
    from engine import checklist_rules as clr
    from engine import wbs as W
    _setup(client, monkeypatch, site)
    got = _sync(app, client, "/checklist", _SITES[site])
    # Recomputed from the crawl the way the route feeds it: the entry page
    # and one page per template.
    pages = [p for p in _crawl(site)["pages"]]
    tree = W.build_wbs(pages)
    surfaces = W.surface_urls(tree, pages)
    pages = [p for i, p in enumerate(pages) if i == 0 or p["url"] in surfaces]
    cut = clr.build_checklist(pages).heading_cut
    assert cut["sections"] > cut["pages"] // 2, cut
    text = EN["gen_limit_headings"] % {"budget": clr.HEADING_BUDGET, **cut}
    assert ("info", f"{text} {EN['gen_limit_see_guide_cl']}") in got, got


def test_the_heading_budget_is_a_checklist_note_only(app, client,
                                                     monkeypatch):
    _setup(client, monkeypatch, "qarea")
    got = _sync(app, client, "/test-cases", _SITES["qarea"])
    assert not _notes(got, "gen_limit_headings"), got


# ── The form budget ─────────────────────────────────────────────────

@pytest.mark.parametrize("site", ["testfort", "dou"])
def test_the_form_budget_names_the_forms_it_skipped(app, client,
                                                    monkeypatch, site):
    from engine import tc_rules
    monkeypatch.setattr(tc_rules, "MAX_FORMS", 1)
    report: dict = {}
    tc_rules.enumerate_from_pages(_crawl(site)["pages"], report=report)
    assert report.get("forms_skipped"), report
    _setup(client, monkeypatch, site)
    got = _sync(app, client, "/test-cases", _SITES[site])
    text = EN["gen_limit_forms"] % {"n": report["forms_skipped"], "limit": 1}
    assert ("info", f"{text} {EN['gen_limit_see_guide_tc']}") in got, got
    # The checklist does not enumerate forms against this budget.
    got = _sync(app, client, "/checklist", _SITES[site])
    assert not _notes(got, "gen_limit_forms"), got


def test_forms_at_the_real_budget_say_nothing(app, client, monkeypatch):
    from engine import tc_rules
    report: dict = {}
    tc_rules.enumerate_from_pages(_crawl("qarea")["pages"], report=report)
    assert report == {}
    _setup(client, monkeypatch, "qarea")
    got = _sync(app, client, "/test-cases", _SITES["qarea"])
    assert not _notes(got, "gen_limit_forms"), got


# ── The WBS folder budgets ──────────────────────────────────────────

@pytest.mark.parametrize("budget,key,site,lowered", [
    ("MAX_PAGE_NODES", "gen_limit_wbs_pages", "qarea", 40),
    ("MAX_FOOTER_PAGES", "gen_limit_wbs_footer", "testfort", 5),
    ("MAX_OTHER_PAGES", "gen_limit_wbs_other", "dou", 0),
])
@pytest.mark.parametrize("route,kind", [_TC, _CL])
def test_a_structure_budget_names_the_pages_without_a_folder(
        app, client, monkeypatch, budget, key, site, lowered, route, kind):
    from engine import wbs as W
    assert not W.build_wbs(_crawl(site)["pages"]).cut     # real budgets hold
    monkeypatch.setattr(W, budget, lowered)
    cut = W.build_wbs(_crawl(site)["pages"]).cut
    assert len(cut) == 1 and next(iter(cut.values())), cut
    _setup(client, monkeypatch, site)
    got = _sync(app, client, route, _SITES[site])
    text = EN[key] % {"n": len(next(iter(cut.values()))), "limit": lowered}
    assert ("info", f"{text} {EN[f'gen_limit_see_guide_{kind}']}") in got, got


# ── The input caps ──────────────────────────────────────────────────

@pytest.mark.parametrize("route,kind", [_TC, _CL])
def test_a_long_instruction_is_cut_and_says_so(app, client, monkeypatch,
                                               route, kind):
    from engine.llm_safety import MAX_CUSTOM_PROMPT_CHARS
    _setup(client, monkeypatch)
    prompt = "Focus on the login form and its validation. " * 40
    got = _sync(app, client, route, "Login\nThe user signs in with e-mail.",
                prompt=prompt)
    text = EN["gen_limit_prompt"] % {"total": len(prompt.strip()),
                                     "limit": MAX_CUSTOM_PROMPT_CHARS}
    assert ("info", f"{text} {EN[f'gen_limit_see_guide_{kind}']}") in got, got


_MODEL_SAYS = {
    "engine.tc_author": json.dumps({"cases": [{
        "summary": "Verify that the home page opens",
        "steps": ["Open the home page", "Look at the page heading"],
        "expected_result": "The home page is displayed."}]}),
    "engine.checklist_author": json.dumps({"sections": [{
        "name": "Home", "checks": [
            {"objective": "Verify that the home page loads"}]}]}),
}


@pytest.mark.parametrize("route,kind,module", [
    ("/test-cases", "tc", "engine.tc_author"),
    ("/checklist", "cl", "engine.checklist_author"),
])
def test_the_ai_author_says_how_many_requirement_lines_it_read(
        app, client, monkeypatch, route, kind, module):
    """Only when the model's result is used — the rules path reads every
    line, and a failed call reads none."""
    import importlib
    from engine.llm_safety import MAX_REQUIREMENT_LINES
    author = importlib.import_module(module)
    monkeypatch.setenv("AUTOMATION_AI_MODE", "full")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-key")
    monkeypatch.setattr(author, "_call_llm",
                        lambda *a, **k: _MODEL_SAYS[module])
    _setup(client, monkeypatch, "dou")
    lines = [f"Requirement {i}: the page shows block {i}." for i in range(120)]
    got = _sync(app, client, route, "\n".join([_SITES["dou"], *lines]))
    # The URL line is a requirement line too.
    text = EN["gen_limit_requirements"] % {"total": 121,
                                           "limit": MAX_REQUIREMENT_LINES}
    guide = EN[f"gen_limit_see_guide_{kind}"]
    assert ("info", f"{text} {guide}") in got, got


@pytest.mark.parametrize("route,module", [
    ("/test-cases", "engine.tc_author"),
    ("/checklist", "engine.checklist_author"),
])
def test_a_failed_model_call_claims_no_reading(app, client, monkeypatch,
                                               route, module):
    import importlib
    author = importlib.import_module(module)
    asked = []

    def _down(*a, **k):
        asked.append(1)
        raise author.LLMUnavailable("test: no model")

    monkeypatch.setenv("AUTOMATION_AI_MODE", "full")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-key")
    monkeypatch.setattr(author, "_call_llm", _down)
    _setup(client, monkeypatch, "dou")
    lines = [f"Requirement {i}: the page shows block {i}." for i in range(120)]
    got = _sync(app, client, route, "\n".join([_SITES["dou"], *lines]))
    assert asked, "the model was never asked"
    assert not _notes(got, "gen_limit_requirements"), got


def test_requirement_lines_without_the_model_say_nothing(app, client,
                                                          monkeypatch):
    _setup(client, monkeypatch, "dou")
    lines = [f"Requirement {i}: the page shows block {i}." for i in range(120)]
    got = _sync(app, client, "/test-cases", "\n".join([_SITES["dou"], *lines]))
    assert not _notes(got, "gen_limit_requirements"), got


# ── The async path the UI takes ─────────────────────────────────────

@pytest.mark.parametrize("route,kind", [_TC, _CL])
def test_the_async_path_says_the_same(app, client, monkeypatch, route, kind):
    _setup(client, monkeypatch, "qarea")
    sync = _note_keys(_sync(app, client, route, _SITES["qarea"]))
    _setup(client, monkeypatch, "qarea")
    got = _async(app, client, route, _SITES["qarea"])
    assert "gen_limit_crawl" in sync
    assert _note_keys(got) == sync, got
    # Said once, not again by the next page load.
    with _flashes(app) as again:
        client.get(route)
    assert not _note_keys(again), again


def test_the_async_checklist_reports_its_gaps(app, client, monkeypatch):
    """The checklist drain flashed nothing at all before — not the crawl
    errors, not the gaps the sync path has reported since PR-2."""
    _setup(client, monkeypatch, "testfort")
    sync = [m for c, m in _sync(app, client, "/checklist",
                                _SITES["testfort"]) if c == "warning"]
    _setup(client, monkeypatch, "testfort")
    got = [m for c, m in _async(app, client, "/checklist",
                                _SITES["testfort"]) if c == "warning"]
    assert any(m.startswith(EN["checklist_gaps_partial"]) for m in sync)
    assert got == sync


# ── The evenness trim ───────────────────────────────────────────────

@pytest.mark.parametrize("site", ["qarea", "testfort", "dou"])
def test_the_evenness_trim_says_how_many_rows_it_dropped(app, client,
                                                         monkeypatch, site):
    from engine import wbs_baseline as bl
    reports = []
    real = bl.balance
    monkeypatch.setattr(bl, "balance",
                        lambda *a, **k: reports.append(real(*a, **k))
                        or reports[-1])
    _setup(client, monkeypatch, site)
    got = _sync(app, client, "/checklist", _SITES[site])
    (report,) = reports
    assert report["trimmed"] > 0, report
    text = EN["gen_limit_balance"] % {
        "band": int(bl.BAND), "median": f"{float(report['median']):g}",
        "cap": report["cap"], "n": report["trimmed"]}
    assert ("info", f"{text} {EN['gen_limit_see_guide_cl']}") in got, got


def test_an_untrimmed_pack_says_nothing_about_evenness(app, client,
                                                       monkeypatch):
    from engine import wbs_baseline as bl
    reports = []
    real = bl.balance
    monkeypatch.setattr(bl, "balance",
                        lambda *a, **k: reports.append(real(*a, **k))
                        or reports[-1])
    _setup(client, monkeypatch, "qarea")
    got = _sync(app, client, "/test-cases", _SITES["qarea"])
    (report,) = reports
    assert report["trimmed"] == 0, report
    assert not _notes(got, "gen_limit_balance"), got


# ── Nothing hit, nothing said ───────────────────────────────────────

@pytest.mark.parametrize("route", ["/test-cases", "/checklist"])
def test_a_run_that_hits_nothing_says_nothing(app, client, monkeypatch,
                                              route):
    _setup(client, monkeypatch)
    got = _sync(app, client, route,
                "Login\nThe user signs in with e-mail and password.\n"
                "A wrong password shows an error.",
                prompt="Cover the login page.")
    assert _note_keys(got) == set(), got
    assert not [m for c, m in got if c == "info" and "Guide" in m], got


# ── Rendering ───────────────────────────────────────────────────────

def test_notes_render_in_both_languages():
    from engine import generation_notes as gn
    notes = gn.prompt(1500) + [gn.note("gen_limit_forms", gn.TC, n=3,
                                       limit=8)]
    for lang in ("en", "ua"):
        t = get_lang(lang)
        tc = gn.render(notes, "tc", t)
        cl = gn.render(notes, "cl", t)
        assert len(tc) == 2 and len(cl) == 1, (lang, tc, cl)
        assert "1500" in tc[0] and "1000" in tc[0]
        assert all(m.endswith(t["gen_limit_see_guide_tc"]) for m in tc)
