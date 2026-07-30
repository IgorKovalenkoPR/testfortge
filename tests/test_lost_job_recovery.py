"""Recovery when a generation job id stops resolving.

Reported twice on prod. Second report: "The generation job was lost —
retrying directly." after Elapsed 1m 49s.

Mechanism, established from evidence rather than guessed:
  * JobQueue is an in-memory dict inside the gunicorn worker. Pruning
    only drops DONE/FAILED jobs older than 30 min, so a job running at
    109 s cannot be evicted — the process holding it must have died.
  * /healthz reported uptime 211 s right after the report, i.e. the
    instance had just restarted.
  * render.yaml already dropped 2 workers → 1 because "Playwright
    Chromium needs ~250 MB and 2 simultaneous workers OOM the 512 MB
    instance". Generation still launched Chromium *in-process*, so a
    single worker plus the crawl plus the LLM client stayed over budget.

Two fixes are covered here:
  1. TESTFORTGE_BROWSER_ENABLED gates the in-process Playwright pass, so
     the free plan stops OOM-killing its own worker.
  2. /api/pack-info lets the client tell "finished then restarted" (work
     is saved, reload shows it) from "died mid-run" (nothing saved, retry
     is the only option). Both produced the same 404 before, so the UI
     told users to redo work that was already on disk.
"""
from __future__ import annotations

import uuid

import pytest

from engine import db as _db
from engine import qa_persona


TC_ROWS = [
    {"id": "SC1_001", "section": "Grid", "section_num": 1,
     "summary": "Verify that User can open the record",
     "preconditions": "", "test_steps": "1. Go to the grid\n2. Click a row",
     "test_data": "", "expected_result": "The record opens.",
     "category": "Positive", "priority": "High", "status": "Unchecked"},
]


# ── The OOM driver ───────────────────────────────────────────────────

class TestBrowserPassSwitch:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("TESTFORTGE_BROWSER_ENABLED", raising=False)
        assert qa_persona.browser_pass_enabled() is True

    @pytest.mark.parametrize("value,expected", [
        ("0", False), ("false", False), ("no", False), ("off", False),
        ("OFF", False), ("1", True), ("true", True), ("", True),
    ])
    def test_parsing(self, monkeypatch, value, expected):
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", value)
        assert qa_persona.browser_pass_enabled() is expected

    def test_disabled_skips_playwright_entirely(self, monkeypatch):
        """The whole point: no Chromium in the web worker."""
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")

        def _must_not_run(*a, **kw):  # pragma: no cover
            pytest.fail("browser_tester.get_or_run must not be called "
                        "when the browser pass is disabled")

        import engine.browser_tester as bt
        monkeypatch.setattr(bt, "get_or_run", _must_not_run)

        # Keep the crawl cheap and offline.
        import engine.site_crawler as sc
        monkeypatch.setattr(sc, "crawl_site",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("offline")))

        result = qa_persona.analyze_input(
            [{"text": "Check https://example.test/login"}])
        assert result.browser_findings == []

    def test_skip_is_reported_not_silent(self, monkeypatch):
        # A quieter suite is worse than an honest one: the operator has
        # to know performance/console findings are missing by design.
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
        import engine.site_crawler as sc
        monkeypatch.setattr(sc, "crawl_site",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("offline")))
        result = qa_persona.analyze_input(
            [{"text": "Check https://example.test/login"}])
        assert any("browser pass skipped" in e.lower()
                   for e in result.crawl_errors), result.crawl_errors

    def test_crawl_still_runs_when_the_browser_pass_is_off(self,
                                                           monkeypatch):
        """The control inventory must survive — the author agent needs it."""
        monkeypatch.setenv("TESTFORTGE_BROWSER_ENABLED", "0")
        called = {}

        class _Page:
            url = "https://example.test/"
            title = "Home"
            h1 = "Welcome"
            headings = ["Features"]
            nav_links = ["Login"]
            buttons = ["Sign in"]
            forms = [{"fields": [{"name": "email", "type": "email"}]}]
            has_video = False
            images_count = 1
            links_internal = []
            error = None

        class _Analysis:
            pages = [_Page()]
            features_detected = ["auth"]
            site_type = "saas"
            crawl_errors = []
            has_auth = True
            has_search = False
            has_forms = True
            has_payment = False
            nav_items = ["Login"]

        import engine.site_crawler as sc

        def _crawl(url, *a, **kw):
            called["url"] = url
            return _Analysis()

        monkeypatch.setattr(sc, "crawl_site", _crawl)
        result = qa_persona.analyze_input(
            [{"text": "Check https://example.test/login"}])
        assert called.get("url"), "the requests-based crawl must still run"
        assert result.site_pages, "control inventory must be populated"


# ── Telling "saved" from "lost" ──────────────────────────────────────

class TestPackInfoEndpoint:
    def test_reports_the_saved_row_count(self, client):
        pid = _db.upsert_project(f"LostJob_{uuid.uuid4().hex[:8]}")
        _db.save_test_cases(pid, TC_ROWS)
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        body = client.get("/api/pack-info?kind=tc").get_json()
        assert body["count"] == 1
        assert body["project"] == pid

    def test_zero_when_nothing_was_saved(self, client):
        pid = _db.upsert_project(f"LostJob_{uuid.uuid4().hex[:8]}")
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        assert client.get("/api/pack-info?kind=tc").get_json()["count"] == 0

    def test_zero_without_a_project(self, client):
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
        body = client.get("/api/pack-info?kind=tc").get_json()
        assert body["count"] == 0 and body["project"] is None

    def test_checklist_kind(self, client):
        pid = _db.upsert_project(f"LostJob_{uuid.uuid4().hex[:8]}")
        _db.save_checklist(pid, [{
            "id": "FUNC_001", "section": "Functional",
            "objective": "Verify that the grid loads", "comments": "",
            "category": "Positive", "priority": "High",
            "status": "Unchecked"}])
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        assert client.get("/api/pack-info?kind=cl").get_json()["count"] == 1

    def test_db_outage_answers_zero_rather_than_500(self, client,
                                                   monkeypatch):
        pid = _db.upsert_project(f"LostJob_{uuid.uuid4().hex[:8]}")
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        def _boom(_pid):
            raise RuntimeError("database is suspended")

        monkeypatch.setattr(_db, "load_test_cases", _boom)
        resp = client.get("/api/pack-info?kind=tc")
        # The client falls back to "retry" advice; a 500 here would break
        # the recovery path entirely.
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 0


class TestLostJobClientWiring:
    @pytest.fixture
    def script(self, client):
        return client.get("/test-cases").get_data(as_text=True)

    def test_probes_before_advising_a_retry(self, script):
        assert "handleJobLost" in script
        assert "/api/pack-info?kind=tc" in script

    def test_offers_the_saved_pack_when_rows_exist(self, script):
        assert "showSavedPack" in script
        assert "tc_gen_show_saved" in script or "Show saved test cases" in script

    def test_still_offers_retry_when_nothing_was_saved(self, script):
        assert "showLostAndRetry" in script

    def test_drops_the_message_that_promised_a_retry_it_never_did(self,
                                                                 script):
        # The old copy said "retrying directly" while the code had
        # already stopped retrying and was waiting on a button.
        assert "The generation job was lost — retrying directly." not in script
