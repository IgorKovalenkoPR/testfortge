"""End-to-end: one page in, a QA deliverable out.

Every PR in this series was tested in isolation. This walks the whole
chain the way a user does, because the interesting failures live in the
joins, not in the modules:

    markup
      → crawl              (regions, menus, contacts, heading levels)
      → low-level checklist (Header / Page Content / Footer, numbered)
      → test cases          (house style, glossary-clean)
      → BDD view            (derived, not stored)
      → automation bundle   (a runnable TS project + honest coverage)
      → Allure ingest       (a run comes back)
      → bug areas           (six attributes, filled)
      → manual walk         (verdicts, resumable)

The fixture markup is shaped after the page the reference deliverable was
written against — a marketing site with a mega-menu, repeated contact
forms, a honeypot and a fat footer — because those are the shapes that
broke each module during development.

``TFG_LIVE_E2E=1`` additionally runs the crawl against a real site. Off by
default: CI should not depend on a third party being up, and a network
failure there would read as our regression.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import asdict
from urllib.parse import urlparse

import pytest

from engine import allure_ingest as ai
from engine import automation_codegen as cg
from engine import bug_areas as ba
from engine import checklist_rules as clr
from engine import gherkin as gk
from engine import glossary as gloss
from engine import manual_run as mr
from engine import site_findings as sf
from engine.site_crawler import _parse_page
from engine.site_tester import CheckResult


PAGE = """
<html lang="en"><head><title>Mobile App Testing Services</title>
<meta name="description" content="We test mobile apps."></head><body>
<header class="site-header">
  <a href="/" class="logo"><img src="/logo.svg" alt="TestFort"></a>
  <nav><ul>
    <li><a href="/why-us">Why Us</a>
      <ul><li><a href="/testimonials">Testimonials</a></li>
          <li><a href="/pricing">Pricing</a></li></ul></li>
    <li><a href="/services">Services</a>
      <ul><li><a href="/services/manual">Manual Testing</a></li>
          <li><a href="/services/auto">Automation Testing</a></li>
          <li><a href="/services/api">API Testing</a></li></ul></li>
    <li><a href="/cases">Case Studies</a></li>
  </ul></nav>
  <a href="/contact-us" class="btn">Contact us</a>
</header>
<main>
  <h1>Mobile Application Testing Services</h1>
  <h2>Mobile Testing by Platform</h2>
  <h2>Types of Mobile QA We Do</h2>
    <h3>Manual testing</h3>
    <h3>Test automation</h3>
  <h2>What our clients say about us</h2>
  <form action="/wp/contact#wpcf7-f42-o1" method="post">
    <label for="n">Name</label><input id="n" name="name" required maxlength="80">
    <label for="e">Email</label><input id="e" name="email" type="email" required>
    <label for="d">Project Description</label>
    <textarea id="d" name="descr" required maxlength="400"></textarea>
    <input type="checkbox" name="consent" required>
    <label for="consent">I agree to the Privacy Policy</label>
    <input type="text" name="_wpcf7_ak_hp_textarea" maxlength="100">
    <button type="submit">Send</button>
  </form>
  <form action="/wp/contact#wpcf7-f42-o2" method="post">
    <label for="n2">Name</label><input id="n2" name="name" required maxlength="80">
    <label for="e2">Email</label><input id="e2" name="email" type="email" required>
    <label for="d2">Project Description</label>
    <textarea id="d2" name="descr" required maxlength="400"></textarea>
    <input type="checkbox" name="consent" required>
    <input type="text" name="_wpcf7_ak_hp_textarea" maxlength="100">
    <button type="submit">Send</button>
  </form>
</main>
<footer class="site-footer">
  <a href="/" class="footer-logo"><img src="/logo.svg" alt="TestFort"></a>
  <a href="mailto:contacts@example.com">contacts@example.com</a>
  <a href="tel:+13103889334">+1 310 388 9334</a>
  <a href="https://www.linkedin.com/company/x">LinkedIn</a>
  <a href="https://t.me/x">Telegram</a>
  <ul><li><a href="/why-us">Why us</a>
        <ul><li><a href="/about">About Us</a></li>
            <li><a href="/awards">Awards</a></li></ul></li></ul>
  <a href="/privacy-policy">Privacy Policy</a>
  <a href="/cookie-policy">Cookie Policy</a>
</footer></body></html>
"""

URL = "https://example.com/mobile-app-testing"


@pytest.fixture(scope="module")
def page() -> dict:
    return asdict(_parse_page(PAGE, URL, urlparse(URL).netloc))


@pytest.fixture(scope="module")
def checklist(page) -> clr.LowLevelChecklist:
    return clr.build_checklist([page], url=URL)


# ── 1. Crawl → checklist ─────────────────────────────────────────────

class TestCrawlToChecklist:
    def test_the_three_reference_sections_come_out(self, checklist):
        assert [s.name for s in checklist.sections] == \
            ["Header", "Page Content", "Footer"]

    def test_the_hierarchy_survives(self, checklist):
        nums = [c.number for c in checklist.all_checks()]
        assert "1.1" in nums
        assert any(n.count(".") == 2 for n in nums), "no level-3 rows"

    def test_the_size_is_in_the_reference_range(self, checklist):
        # The reference deliverable is 57 checks for one page. An order of
        # magnitude either side means the generator lost its shape.
        assert 25 <= checklist.total <= 120, checklist.total

    def test_every_row_passes_the_terminology_linter(self, checklist):
        assert clr.lint_checklist(checklist) == []

    def test_the_honeypot_never_reaches_a_deliverable(self, checklist):
        blob = " ".join(c.objective for c in checklist.all_checks()).lower()
        assert "wpcf7" not in blob and "hp_textarea" not in blob

    def test_repeated_forms_collapse(self, checklist):
        blocks = [c for c in checklist.all_checks()
                  if "is displayed with every field it declares"
                  in c.objective]
        assert len(blocks) == 1


# ── 2. Checklist → the module's own dataclass ────────────────────────

class TestChecklistHandoff:
    def test_items_carry_number_depth_and_status(self, checklist):
        items = clr.to_checklist_items(checklist)
        assert items
        assert items[0].item_num == "1.1"
        assert all(i.status == "Unchecked" for i in items)
        assert any(i.depth == 3 for i in items)


# ── 3. Test cases → BDD → bundle ─────────────────────────────────────

@pytest.fixture(scope="module")
def cases():
    from engine.testcase_generator import TestCase
    common = dict(section_num=1, category="Negative", priority="High",
                  tc_format="gherkin")
    return [
        TestCase(id="SC1_001", section="Contact form",
                 summary='Verify that User cannot submit the Contact Form '
                         'without marking the Consent checkbox',
                 preconditions=f"The Contact Form is opened on the {URL} page",
                 test_steps=(f"1. Go to the site: {URL}\n"
                             '2. Fill in the "Name" field with valid data\n'
                             '3. Fill in the "Email" field with valid data\n'
                             '4. Click the [Send] button\n'
                             "5. Pay attention to the result"),
                 test_data="Name: Test Testovko, Email: qa@example.com",
                 expected_result="An error message is displayed",
                 **common),
        TestCase(id="SC1_002", section="Header",
                 summary="Verify that the Homepage is opened after clicking "
                         "the logo",
                 preconditions="", test_data="",
                 test_steps=(f"1. Go to the site: {URL}\n"
                             '2. Click on the "Logo" link'),
                 expected_result="The Homepage is opened",
                 **{**common, "category": "Positive"}),
    ]


class TestCasesToBundle:
    def test_cases_are_house_style_clean(self, cases):
        for tc in cases:
            assert gloss.lint_text(tc.summary, kind="title") == [], tc.id
            assert gloss.lint_steps(mr.split_steps(tc.test_steps)) == [], tc.id

    def test_the_bdd_view_is_derived_not_stored(self, cases):
        tc = cases[0]
        assert tc.gherkin == ""
        assert "Scenario:" in gk.ensure_gherkin(tc)

    def test_the_feature_file_is_valid(self, cases):
        text = gk.feature_from_test_cases(cases).render()
        assert gk.lint(text) == []

    def test_the_bundle_is_runnable_shaped(self, cases):
        data = cg.bundle_zip(cases, base_url="https://example.com",
                             project_name="E2E")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            assert {"package.json", "playwright.config.ts",
                    "steps/actions.ts", "steps/assertions.ts",
                    "MANUAL-ASSERTIONS.md"} <= names
            assert any(n.startswith("features/") for n in names)
            pkg = json.loads(zf.read("package.json"))
            assert "allure-playwright" in pkg["devDependencies"]

    def test_coverage_is_reported_before_anything_runs(self, cases):
        cov = cg.coverage_report(cases)
        assert cov.scenarios == 2
        # These cases use only house verbs, so nothing should be stranded.
        assert cov.unbound_actions == []
        assert cov.bound_pct >= 90, cov.to_dict()


# ── 4. A run comes back ──────────────────────────────────────────────

class TestRunIngest:
    def test_allure_results_map_back_to_the_cases(self, cases):
        docs = []
        for tc, status in zip(cases, ("failed", "skipped")):
            docs.append({
                "name": tc.summary, "status": status,
                "start": 0, "stop": 1500,
                "labels": [{"name": "tag", "value": f"TC-{tc.id}"},
                           {"name": "suite", "value": tc.section}],
                "statusDetails": {"message": "expected visible"},
                "steps": [],
            })
        summary = ai.summarise(docs)
        assert ai.statuses_by_case(summary) == {
            "SC1_001": "failed", "SC1_002": "skipped"}
        # One executed, none passed — and the skip is in neither bucket.
        assert summary.executed == 1
        assert summary.pass_rate == 0.0
        assert summary.skipped == 1


# ── 5. Bugs land in the right area ───────────────────────────────────

class TestBugAreas:
    def test_a_sweep_fills_all_six_attributes(self):
        results = {
            "load_time": CheckResult("Failed", "Page loaded in 7412ms"),
            "https": CheckResult("Failed", "Site is NOT served over HTTPS"),
            "images_have_alt": CheckResult("Failed", "12 images have no alt"),
            "favicon": CheckResult("Failed", "No favicon declared"),
            "cta_buttons": CheckResult("Failed", "No call-to-action found"),
            "nav_links_work": CheckResult("Failed", "2 links return 404"),
        }
        found = sf.findings_from_results(results, base_url=URL)
        counts = ba.counts_by_area(found)
        assert all(counts[a] >= 1 for a in ba.AREAS), counts

    def test_a_critical_is_never_low_priority(self):
        found = sf.findings_from_results(
            {"https": CheckResult("Failed", "NOT served over HTTPS")},
            base_url=URL)
        assert found[0]["severity"] == "Critical"
        assert found[0]["priority"] not in ("Low", "Lowest")


# ── 6. The manual walk over the generated pack ───────────────────────

class TestManualWalkOverGeneratedWork:
    def test_the_generated_pack_is_walkable(self, cases, checklist):
        items = clr.to_checklist_items(checklist)
        queue = mr.build_queue(cases, items)
        assert len(queue) == len(cases) + len(items)

        # Every test-case row arrives with the steps a tester needs; every
        # checklist row is a single observation with none.
        tc_rows = [q for q in queue if q.kind == "test_case"]
        cl_rows = [q for q in queue if q.kind == "checklist"]
        assert all(q.steps for q in tc_rows)
        assert all(q.expected_result for q in tc_rows)
        assert all(not q.steps for q in cl_rows)

    def test_progress_treats_a_skip_honestly(self, cases):
        queue = mr.build_queue(cases, [])
        progress = mr.compute_progress(queue, [
            {"case_external_id": "SC1_001", "status": "Passed"},
            {"case_external_id": "SC1_002", "status": "Skipped"},
        ])
        assert progress.finished
        assert progress.executed == 1 and progress.pass_rate == 100.0


# ── 7. Through the app ───────────────────────────────────────────────

class TestThroughTheApp:
    """The same artefacts, reached the way a user reaches them."""

    @pytest.fixture()
    def seeded(self, client, cases, checklist, request):
        from engine import db as _db
        from routes._shared import cl_to_dict, tc_to_dict, SERVER_START_TIME
        pid = _db.upsert_project(f"e2e-{request.node.name}")
        _db.save_test_cases(pid, [tc_to_dict(c) for c in cases])
        _db.save_checklist(
            pid, [cl_to_dict(i) for i in clr.to_checklist_items(checklist)])
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
            sess["project_setup"] = {"project_name": "e2e"}
            sess.pop("test_cases_data", None)
            sess.pop("checklist_data", None)
        return pid

    def test_checklist_page_renders_the_hierarchy(self, client, seeded):
        body = client.get("/checklist").get_data(as_text=True)
        assert 'class="cl-num">1.1<' in body
        assert "cl-row-sub" in body

    def test_test_cases_page_shows_the_bdd_view(self, client, seeded):
        body = client.get("/test-cases").get_data(as_text=True)
        assert "BDD view" in body
        assert "gherkin-block" in body

    def test_feature_export_downloads(self, client, seeded):
        resp = client.get("/export/feature")
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert any(n.endswith(".feature") for n in zf.namelist())

    def test_automation_page_reports_coverage(self, client, seeded):
        body = client.get("/automation").get_data(as_text=True)
        assert "steps bound" in body
        assert "never reports green" in body

    def test_automation_bundle_downloads(self, client, seeded):
        resp = client.get("/automation/bundle.zip")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"

    def test_a_manual_walk_runs_to_completion(self, client, seeded):
        import re as _re
        from engine import db as _db
        resp = client.post("/test-execution/manual/start",
                           data={"run_mode": "manual"})
        run_id = int(_re.search(r"/manual/(\d+)",
                                resp.headers["Location"]).group(1))
        page_body = client.get(
            f"/test-execution/manual/{run_id}").get_data(as_text=True)
        assert "Manual run" in page_body

        run = _db.get_execution_run(run_id)
        queue = (run["env_payload"] or {}).get("manual_queue") or []
        assert len(queue) > 10, "the walk should cover the whole pack"

        for entry in queue:
            client.post(f"/test-execution/manual/{run_id}/verdict",
                        data={"external_id": entry["external_id"],
                              "verdict": "Passed"})
        client.post(f"/test-execution/manual/{run_id}/finish")
        run = _db.get_execution_run(run_id)
        assert run["status"] == "completed"
        assert run["stats"]["pass_rate"] == 100.0

    def test_bug_reports_offers_every_area(self, client, seeded):
        # The filter toolbars only render once the project has bugs — an
        # empty project has nothing to filter. Seed one.
        from engine import db as _db
        _db.save_bug(seeded, {"title": "Slow homepage",
                              "defect_class": "slow_page_load"},
                     source="execution")
        body = client.get("/bug-reports").get_data(as_text=True)
        for area in ba.AREAS:
            assert area.replace("&", "&amp;") in body

    def test_the_bug_sheet_exports(self, client, seeded):
        from engine import db as _db
        _db.save_bug(seeded, {"title": "Slow homepage",
                              "defect_class": "slow_page_load"},
                     source="execution")
        body = client.get("/export-bug-reports.csv").get_data(as_text=True)
        assert body.splitlines()[0].startswith("Bug ID,Summary,Area,")
        assert "Performance" in body


# ── 8. Against a real site, opt-in ───────────────────────────────────

@pytest.mark.skipif(os.environ.get("TFG_LIVE_E2E") != "1",
                    reason="set TFG_LIVE_E2E=1 to crawl a real site")
class TestLiveSite:
    """Off by default: CI must not depend on a third party being up.

    A network failure here would read as our regression, which is exactly
    the kind of false signal that gets a suite ignored.
    """

    def test_a_real_page_yields_a_usable_checklist(self):
        from engine.site_crawler import _fetch_page
        url = os.environ.get(
            "TFG_LIVE_E2E_URL",
            "https://testfort.com/mobile-application-testing")
        html, err = _fetch_page(url)
        assert not err and html, err
        # The gzip fix: a page that decoded to noise reported zero of
        # everything while raising nothing.
        assert len(html) > 20_000, f"suspiciously small body: {len(html)}"

        record = asdict(_parse_page(html, url, urlparse(url).netloc))
        assert record["headings"], "no headings — did the body decode?"

        built = clr.build_checklist([record], url=url)
        assert built.total >= 20
        assert clr.lint_checklist(built) == []
        assert [s.name for s in built.sections][0] == "Header"
