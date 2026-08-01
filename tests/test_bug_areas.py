"""Bug reports — the six quality attributes, and detectors that fill them.

Covers ``engine.bug_areas`` (the taxonomy), ``engine.site_findings`` (the
detector that turns failed site checks into bugs), the DB round-trip of the
new bug-sheet columns, the ``/bug-reports`` area filter and the CSV export.

The gap this closes: ``site_tester`` ran 53 checks and reported Failed on
every one that failed, but only the handful an item's summary happened to
match ever became a bug. Everything else was computed and discarded, which
is why the product could not produce a Performance or a Security bug no
matter how broken the site was — not for lack of detection, but because
detection never reached the Bug Reports module.
"""
from __future__ import annotations

import inspect
import re

import pytest

from engine import bug_areas as ba
from engine import db as _db
from engine import site_findings as sf
from engine.site_tester import CheckResult, SiteTestRunner


# ── Taxonomy completeness ────────────────────────────────────────────

class TestMappingIsExhaustive:
    def test_every_site_check_has_an_area(self):
        """An unmapped check silently defaults to Functional.

        That is the quiet failure this guards: a Performance or Security
        finding filed as Functional does not disappear, it gets triaged by
        the wrong person and lands in the wrong column of the report. A
        default is fine for a value we have never seen; it is not fine for
        one we ship.
        """
        src = inspect.getsource(SiteTestRunner.run_all_checks)
        keys = set(re.findall(r'"([a-z_0-9]+)":\s+self\.check_', src))
        assert keys, "could not read the check registry"
        assert not (keys - set(ba.CHECK_AREA)), \
            sorted(keys - set(ba.CHECK_AREA))

    def test_no_mapping_points_at_a_check_that_does_not_exist(self):
        src = inspect.getsource(SiteTestRunner.run_all_checks)
        keys = set(re.findall(r'"([a-z_0-9]+)":\s+self\.check_', src))
        assert not (set(ba.CHECK_AREA) - keys), \
            sorted(set(ba.CHECK_AREA) - keys)

    def test_every_defect_class_has_an_area(self):
        from engine.bug_template import CLASS_SEVERITY
        unmapped = (set(CLASS_SEVERITY) - set(ba.CLASS_AREA)
                    - set(ba.NEW_CLASS_SEVERITY))
        assert not unmapped, sorted(unmapped)

    def test_every_new_defect_class_has_a_severity(self):
        from engine.bug_template import CLASS_SEVERITY
        for _check, (_area, cls) in ba.CHECK_AREA.items():
            assert cls in CLASS_SEVERITY, cls

    def test_all_six_areas_are_reachable_from_a_check(self):
        # If an area had no check mapped to it, that column of the report
        # could never fill from a sweep.
        reachable = {area for area, _cls in ba.CHECK_AREA.values()}
        assert reachable == set(ba.AREAS)


class TestAreaResolution:
    @pytest.mark.parametrize("cls,area", [
        ("click_timeout", ba.FUNCTIONAL),
        ("broken_image", ba.UI_UX),
        ("hamburger_dead", ba.USABILITY),
        ("axe_serious", ba.ACCESSIBILITY),
        ("cta_tiny_tap_target", ba.ACCESSIBILITY),
        ("social_no_noopener", ba.SECURITY),
        ("slow_page_load", ba.PERFORMANCE),
        ("no_https", ba.SECURITY),
    ])
    def test_area_for_class(self, cls, area):
        assert ba.area_for_class(cls) == area

    def test_unknown_class_falls_back(self):
        assert ba.area_for_class("never-heard-of-it") == ba.DEFAULT_AREA

    @pytest.mark.parametrize("raw,want", [
        ("Security", ba.SECURITY), ("security", ba.SECURITY),
        ("a11y", ba.ACCESSIBILITY), ("UI", ba.UI_UX), ("ux", ba.UI_UX),
        ("UI & UX", ba.UI_UX), ("UI and UX", ba.UI_UX),
        ("perf", ba.PERFORMANCE), ("Performance", ba.PERFORMANCE),
    ])
    def test_coerce_area(self, raw, want):
        assert ba.coerce_area(raw) == want

    def test_coerce_refuses_a_value_it_does_not_know(self):
        # Empty, not a guess — the caller decides what to do about it.
        assert ba.coerce_area("Chaos") == ""
        assert ba.coerce_area(None) == ""

    def test_an_operators_triage_is_never_overwritten(self):
        # Triage is a judgement. Re-deriving it on every read would
        # silently undo it.
        assert ba.resolve_area(
            {"bug_area": "Usability", "defect_class": "axe_serious"}) \
            == ba.USABILITY

    def test_resolves_from_extra_and_from_labels(self):
        assert ba.resolve_area(
            {"extra": {"defect_class": "slow_page_load"}}) == ba.PERFORMANCE
        assert ba.resolve_area(
            {"extra": {"labels": ["defect:mixed_content"]}}) == ba.SECURITY

    def test_counts_include_empty_areas(self):
        # A chip that vanished when its bucket was empty would read as
        # "this product has no accessibility defects", when it means
        # "nobody has looked".
        counts = ba.counts_by_area([{"defect_class": "axe_serious"}])
        assert set(counts) == set(ba.AREAS)
        assert counts[ba.ACCESSIBILITY] == 1
        assert counts[ba.SECURITY] == 0


# ── Detector ─────────────────────────────────────────────────────────

def _results(**kw) -> dict:
    return {k: CheckResult(*v) for k, v in kw.items()}


class TestSiteFindings:
    def test_a_failed_check_becomes_a_bug(self):
        f = sf.finding_from_check(
            "load_time",
            CheckResult("Failed", "Page loaded in 7412ms — exceeds 3000ms"),
            base_url="https://x.test")
        assert f is not None
        assert f["bug_area"] == ba.PERFORMANCE
        assert "7412ms" in f["actual_result"]
        assert f["expected_result"].startswith("The Homepage loads")
        assert "1. Go to the site: https://x.test" in f["steps_to_reproduce"]

    def test_a_passing_check_produces_nothing(self):
        assert sf.finding_from_check(
            "load_time", CheckResult("Passed", "1200ms")) is None

    def test_our_own_error_is_not_filed_against_the_product(self):
        # "Check error: …" is this tool falling over. Filing it would put
        # our stack trace on the client's defect list.
        assert sf.finding_from_check(
            "form_labels",
            CheckResult("Failed", "Check error: NoneType has no attribute")
        ) is None

    def test_unreachable_site_is_not_a_product_defect(self):
        assert sf.finding_from_check(
            "page_loads", CheckResult("Failed", "Could not reach host")
        ) is None

    def test_a_sweep_can_fill_all_six_areas(self):
        # The whole point: before this, four of the six were unreachable.
        results = _results(
            load_time=("Failed", "Page loaded in 7412ms"),
            https=("Failed", "Site is NOT served over HTTPS"),
            images_have_alt=("Failed", "12 of 40 images have no alt"),
            favicon=("Failed", "No favicon declared"),
            cta_buttons=("Failed", "No call-to-action control found"),
            nav_links_work=("Failed", "2 navigation links return HTTP 404"),
        )
        found = sf.findings_from_results(results, base_url="https://x.test")
        counts = sf.summarise_by_area(found)
        assert all(counts[a] >= 1 for a in ba.AREAS), counts

    def test_findings_are_ordered_most_severe_first(self):
        found = sf.findings_from_results(_results(
            favicon=("Failed", "No favicon declared"),
            https=("Failed", "Site is NOT served over HTTPS"),
        ), base_url="https://x.test")
        assert found[0]["severity"] == "Critical"

    def test_a_critical_is_never_filed_at_low_priority(self):
        # "Critical / Low" reads as "serious, ignore it". Site-wide
        # defects affect every page, so the floor is raised rather than
        # the severity lowered.
        for key, msg in (("https", "Site is NOT served over HTTPS"),
                         ("password_masked", "Password renders in clear")):
            f = sf.finding_from_check(key, CheckResult("Failed", msg),
                                      base_url="https://x.test")
            if f["severity"] == "Critical":
                assert f["priority"] not in ("Low", "Lowest"), f

    def test_the_title_names_the_defect_not_the_check(self):
        # "load_time check failed" is a fact about our tooling.
        f = sf.finding_from_check(
            "load_time", CheckResult("Failed", "Page loaded in 7412ms"),
            base_url="https://x.test")
        assert "check failed" not in f["title"]
        assert "7412ms" in f["title"]

    def test_labels_carry_the_class_area_and_source(self):
        f = sf.finding_from_check(
            "mixed_content", CheckResult("Failed", "HTTP image on HTTPS page"),
            base_url="https://x.test")
        assert "defect:mixed_content" in f["labels"]
        assert "area:security" in f["labels"]
        assert "source:site_sweep" in f["labels"]

    def test_the_cap_is_reported_rather_than_swallowed(self):
        results = _results(**{
            f"check_{i}": ("Failed", f"failure {i}") for i in range(3)})
        # Unknown keys map to Functional/unknown but still produce
        # findings; the point here is that the shortfall is countable.
        kept = sf.findings_from_results(results, limit=2)
        assert len(kept) == 2
        assert sf.dropped_count(results, kept) == 1


# ── Execution wiring ─────────────────────────────────────────────────

class TestExecutionSweep:
    def test_unmatched_failed_checks_become_bugs(self, monkeypatch):
        from engine import qa_testers

        class _FakeRunner:
            def __init__(self, url):
                pass

            def run_all_checks(self):
                return _results(
                    https=("Failed", "Site is NOT served over HTTPS"),
                    load_time=("Failed", "Page loaded in 9000ms"),
                )

            def match_item(self, summary):
                return None

        monkeypatch.setattr("engine.site_tester.SiteTestRunner", _FakeRunner)
        out = qa_testers.execute_items(
            items=[{"id": "SC1_001", "summary": "Verify that the logo is "
                                                "displayed", "section": "H"}],
            item_type="test_case", tester_id="auto",
            environment="Chrome/Windows", testing_types=["Functional"],
            site_url="https://x.test", site_sweep=True)
        areas = {b.get("bug_area") for b in out["bugs"]}
        assert ba.SECURITY in areas
        assert ba.PERFORMANCE in areas

    def test_the_sweep_is_off_unless_asked_for(self, monkeypatch):
        """A run must not quietly gain findings nobody requested.

        Wiring this on by default added 22 bugs to a walkthrough run that
        had filed 2 of its own — the flood that makes a bug list stop
        being read.
        """
        from engine import qa_testers

        class _FakeRunner:
            def __init__(self, url):
                pass

            def run_all_checks(self):
                return _results(https=("Failed", "NOT served over HTTPS"))

            def match_item(self, summary):
                return None

        monkeypatch.setattr("engine.site_tester.SiteTestRunner", _FakeRunner)
        out = qa_testers.execute_items(
            items=[{"id": "SC1_001", "summary": "Verify that the logo is "
                                                "displayed", "section": "H"}],
            item_type="test_case", tester_id="auto",
            environment="Chrome/Windows", testing_types=["Functional"],
            site_url="https://x.test")
        assert not [b for b in out["bugs"]
                    if "source:site_sweep" in (b.get("labels") or [])]

    def test_a_matched_check_is_not_swept_twice(self, monkeypatch):
        from engine import qa_testers

        class _FakeRunner:
            def __init__(self, url):
                pass

            def run_all_checks(self):
                return _results(https=("Failed", "NOT served over HTTPS"))

            def match_item(self, summary):
                return "https"

        monkeypatch.setattr("engine.site_tester.SiteTestRunner", _FakeRunner)
        out = qa_testers.execute_items(
            items=[{"id": "SC1_001", "summary": "Verify HTTPS",
                    "section": "Security"}],
            item_type="test_case", tester_id="auto",
            environment="Chrome/Windows", testing_types=["Security"],
            site_url="https://x.test", site_sweep=True)
        swept = [b for b in out["bugs"]
                 if "source:site_sweep" in (b.get("labels") or [])]
        assert swept == []

    def test_a_failing_item_carries_its_testing_type_as_the_area(self):
        from engine.qa_testers import _area_from_item
        assert _area_from_item({"testing_type": "Performance"}) == \
            ba.PERFORMANCE
        assert _area_from_item({}, ["Security"]) == ba.SECURITY
        assert _area_from_item({}, []) == ba.DEFAULT_AREA


# ── Persistence ──────────────────────────────────────────────────────

class TestBugSheetColumns:
    def test_the_reference_columns_round_trip(self):
        pid = _db.upsert_project("bug-sheet-round-trip")
        _db.save_bug(pid, {
            "title": "Slow homepage",
            "preconditions": "User is on the Homepage",
            "attachment": "https://drive.example/evidence",
            "assignee": "Dev A",
            "defect_class": "slow_page_load",
        }, source="execution")
        row = _db.list_bugs(pid)[0]
        assert row["preconditions"] == "User is on the Homepage"
        assert row["attachment"] == "https://drive.example/evidence"
        assert row["assignee"] == "Dev A"
        assert row["bug_area"] == ba.PERFORMANCE

    def test_area_is_derived_when_the_caller_did_not_decide(self):
        pid = _db.upsert_project("bug-area-derived")
        _db.save_bug(pid, {"title": "A11y", "defect_class": "axe_serious"})
        assert _db.list_bugs(pid)[0]["bug_area"] == ba.ACCESSIBILITY

    def test_an_explicit_area_survives_the_write(self):
        pid = _db.upsert_project("bug-area-explicit")
        _db.save_bug(pid, {"title": "Triaged", "bug_area": "Usability",
                           "defect_class": "axe_serious"})
        assert _db.list_bugs(pid)[0]["bug_area"] == ba.USABILITY

    def test_a_pre_migration_bug_defaults_to_functional(self):
        pid = _db.upsert_project("bug-area-default")
        _db.save_bug(pid, {"title": "No metadata at all"})
        assert _db.list_bugs(pid)[0]["bug_area"] == ba.FUNCTIONAL


# ── UI ───────────────────────────────────────────────────────────────

@pytest.fixture()
def seeded(client, request):
    from routes._shared import SERVER_START_TIME
    pid = _db.upsert_project(f"bug-areas-{request.node.name}")
    # The dev SQLite file outlives a pytest run, and upsert_project keys on
    # the name — so a re-run would double every count this asserts on.
    _db.delete_bugs_for_project(pid)
    for title, cls in (("Slow homepage", "slow_page_load"),
                       ("No HTTPS", "no_https"),
                       ("Missing alt", "missing_alt_text"),
                       ("Broken image", "broken_image"),
                       ("Dead hamburger", "hamburger_dead"),
                       ("404 link", "malformed_link")):
        _db.save_bug(pid, {"title": title, "defect_class": cls,
                           "severity": "Major", "status": "Open"},
                     source="execution")
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_id"] = pid
        sess["project_setup"] = {"project_name": "areas"}
    return pid


class TestAreaFilterUI:
    def test_every_area_gets_a_chip_with_its_count(self, client, seeded):
        body = client.get("/bug-reports").get_data(as_text=True)
        for area in ba.AREAS:
            escaped = area.replace("&", "&amp;")
            m = re.search(re.escape(escaped) +
                          r'\s*<span class="badge">(\d+)</span>', body)
            assert m, f"no chip for {area}"
            assert m.group(1) == "1", area

    def test_filtering_narrows_the_list(self, client, seeded):
        body = client.get("/bug-reports?area=Security").get_data(as_text=True)
        assert "No HTTPS" in body
        assert "Slow homepage" not in body

    def test_chips_keep_their_true_counts_while_filtered(self, client,
                                                         seeded):
        # A chip whose count changed to match the filtered view would tell
        # the operator nothing.
        body = client.get("/bug-reports?area=Security").get_data(as_text=True)
        assert re.search(r'Performance\s*<span class="badge">1</span>', body)

    def test_an_unknown_area_is_ignored_rather_than_emptying_the_list(
            self, client, seeded):
        body = client.get("/bug-reports?area=Chaos").get_data(as_text=True)
        assert "No HTTPS" in body and "Slow homepage" in body

    def test_the_area_filter_composes_with_the_source_filter(self, client,
                                                             seeded):
        resp = client.get("/bug-reports?area=Security&source=manual_tc")
        assert resp.status_code == 200


class TestCsvExport:
    def test_columns_match_the_reference_sheet(self, client, seeded):
        body = client.get("/export-bug-reports.csv").get_data(as_text=True)
        header = body.splitlines()[0]
        # The "Bugs" tab of Training Plan_Horban Yaroslavna.xlsx, plus
        # Area — the reference has no column for the six attributes.
        assert header == (
            "Bug ID,Summary,Area,Status,Priority,Severity,Reporter,Date,"
            "Environment,Preconditions,Steps to reproduce,Actual result,"
            "Expected result,Attachment,Note,Assignee")

    def test_every_bug_is_a_row(self, client, seeded):
        body = client.get("/export-bug-reports.csv").get_data(as_text=True)
        assert len(body.strip().splitlines()) == 7   # header + 6

    def test_area_reaches_the_sheet(self, client, seeded):
        body = client.get("/export-bug-reports.csv").get_data(as_text=True)
        assert "Performance" in body and "Accessibility" in body

    def test_a_formula_cell_is_neutralised(self, client, request):
        # Excel executes a cell starting with = + - @ on open.
        from routes._shared import SERVER_START_TIME
        pid = _db.upsert_project(f"bug-csv-injection-{request.node.name}")
        _db.save_bug(pid, {"title": "=cmd|'/c calc'!A1",
                           "defect_class": "broken_image"})
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
            sess["project_setup"] = {"project_name": "x"}
        body = client.get("/export-bug-reports.csv").get_data(as_text=True)
        assert "\n=cmd" not in body
        assert ",=cmd" not in body

    def test_empty_project_says_so(self, client, request):
        from routes._shared import SERVER_START_TIME
        pid = _db.upsert_project(f"bug-csv-empty-{request.node.name}")
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
            sess["project_setup"] = {"project_name": "x"}
        resp = client.get("/export-bug-reports.csv", follow_redirects=True)
        assert b"No bug reports to export" in resp.data
