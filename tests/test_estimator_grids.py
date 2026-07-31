"""The effort estimate has to see grids, not just forms and buttons.

Before this, `features_from_site_analysis` priced a page from its form,
button and nav counts. A list surface has few of all three and is worth
12-20 cases in the reference corpus, so an admin app built out of grids
came out costed like a brochure — the estimate under-quoted precisely
the work that dominates the suite.

The budget is not a second density formula. It is counted by
`tc_rules.count_grid_cases`, which walks the same `coverage_rules.yaml`
checks the generator writes from, so the number in the estimate is the
number of cases the pack will actually contain. The test that matters
most here is the one asserting those two agree.
"""
from __future__ import annotations

import pytest

from engine import qa_estimator as est
from engine import tc_rules
from engine.site_crawler import SiteAnalysis, _detect_features, _parse_page
from engine.tc_author import Artifacts


ADMIN_GRID = """
<html><head><title>Orders</title></head><body><h1>Orders</h1>
<form role="search"><input type="search" name="q" placeholder="Search orders"></form>
<select name="status_filter"><option>All</option><option>Paid</option></select>
<select name="bulk-action-selector-top">
  <option>-- Bulk actions --</option><option>Delete selected</option>
  <option>Export to CSV</option></select>
<a href="/orders/new">Add order</a>
<table><caption>Customer orders</caption>
  <thead><tr><th><input type="checkbox"></th>
    <th aria-sort="none"><a href="?s=id">Order ID</a></th>
    <th class="sortable"><a href="?s=c">Customer</a></th>
    <th>Status</th></tr></thead>
  <tbody><tr><td><input type="checkbox"></td>
    <td><a href="/orders/1">#1</a></td><td>Ann</td><td>Paid</td></tr></tbody>
</table>
<nav aria-label="pagination"><a rel="next" href="?p=2">Next</a></nav>
</body></html>
"""

#: Byte-for-byte the same page with the grid demoted to page furniture.
#: Any budget difference between the two is attributable to the grid and
#: nothing else.
LAYOUT_ONLY = ADMIN_GRID.replace("<table>", '<table role="presentation">')


def _analysis(html: str, url: str = "https://shop.test/orders") -> SiteAnalysis:
    analysis = SiteAnalysis(base_url="https://shop.test", domain="shop.test")
    analysis.pages = [_parse_page(html, url, "shop.test")]
    analysis.page_count = 1
    _detect_features(analysis, html)
    return analysis


def _total(analysis) -> int:
    return sum(f.test_cases
               for f in est.features_from_site_analysis(analysis))


# ── Crawler-side signal ──────────────────────────────────────────────

class TestGridFeatureFlag:
    def test_a_grid_sets_the_flag_and_the_feature(self):
        analysis = _analysis(ADMIN_GRID)
        assert analysis.has_grid is True
        assert analysis.grid_count == 1
        assert "grids" in analysis.features_detected

    def test_a_layout_table_sets_neither(self):
        analysis = _analysis(LAYOUT_ONLY)
        assert analysis.has_grid is False
        assert analysis.grid_count == 0
        assert "grids" not in analysis.features_detected

    def test_the_existing_feature_flags_are_unchanged(self):
        analysis = _analysis(ADMIN_GRID)
        assert analysis.features_detected[0] == "web_general"
        assert "search" in analysis.features_detected

    def test_grids_are_noted_but_do_not_re_type_the_site(self):
        """A pricing table on a marketing page is a real grid.

        Letting it vote for "dashboard" would inflate that whole site's
        budget through the per-page density formula, so the grid is
        reported in the notes and charged for on its own page only.
        """
        analysis = _analysis(ADMIN_GRID)
        assert analysis.site_type == _analysis(LAYOUT_ONLY).site_type
        assert any("data grid" in n for n in analysis.architecture_notes)


# ── Estimator-side budget ────────────────────────────────────────────

class TestGridBudget:
    def test_a_grid_raises_the_estimate(self):
        with_grid = _total(_analysis(ADMIN_GRID))
        without = _total(_analysis(LAYOUT_ONLY))
        assert with_grid > without, (with_grid, without)

    def test_the_budget_equals_what_the_generator_will_emit(self):
        """The number quoted and the number delivered must be the same.

        This is the whole reason the estimator calls into `tc_rules`
        instead of carrying its own density formula: add a check to
        `coverage_rules.yaml` and both move together, or this fails.
        """
        page = _analysis(ADMIN_GRID).pages[0]
        priced, _ = est._grid_tc(page)

        generated = tc_rules.enumerate_from_artifacts(Artifacts(pages=[{
            "url": page.url, "h1": page.h1, "title": page.title,
            "forms": [], "tables": page.tables,
            "grid_controls": page.grid_controls,
        }]))
        assert priced == len(generated), (priced, len(generated))

    def test_the_page_row_names_the_grid_it_charged_for(self):
        # A number with no stated reason is not reviewable by the lead
        # who signs the estimate off.
        features = est.features_from_site_analysis(_analysis(ADMIN_GRID))
        row = [f for f in features if f.name == "Orders"][0]
        assert "1 grid" in row.comment
        assert "list-surface cases" in row.comment

    def test_the_global_grid_row_appears_only_with_a_grid(self):
        names = [f.name for f in
                 est.features_from_site_analysis(_analysis(ADMIN_GRID))]
        assert any("grids" in n for n in names)
        names = [f.name for f in
                 est.features_from_site_analysis(_analysis(LAYOUT_ONLY))]
        assert not any("grids" in n for n in names)

    @pytest.mark.parametrize("site_type", [
        "wordpress", "static", "landing", "spa",
        "ecommerce", "dashboard", "app", "generic",
    ])
    def test_every_site_type_prices_the_grids_feature(self, site_type):
        # Without an entry the global budget silently falls back to 6,
        # which would make the site type look irrelevant to grid cost.
        analysis = _analysis(ADMIN_GRID)
        analysis.site_type = site_type
        row = [f for f in est.features_from_site_analysis(analysis)
               if f.name == "Global — grids"]
        assert row and row[0].test_cases > 0

    def test_the_grid_budget_is_not_swallowed_by_the_density_cap(self):
        """The per-page cap bounds form/button density, not surfaces.

        Folding the grid under the same ceiling is how the original
        under-estimate happened: the cap is 7 on a content page, and a
        list surface alone is worth more than that.
        """
        analysis = _analysis(ADMIN_GRID)
        analysis.site_type = "static"        # cap = 7
        row = [f for f in est.features_from_site_analysis(analysis)
               if f.name == "Orders"][0]
        assert row.test_cases > 7


# ── Degrading safely ─────────────────────────────────────────────────

class TestRobustness:
    def test_a_page_without_the_grid_attributes_costs_nothing_extra(self):
        """Crawls cached before grid support have no `tables` attribute."""
        class LegacyPage:
            url = "https://shop.test/x"
            title = "X"
            h1 = "X"
            forms: list = []
            buttons: list = []
            nav_links: list = []

        assert est._grid_tc(LegacyPage()) == (0, "")

    def test_an_empty_grid_list_costs_nothing(self):
        class Page:
            tables: list = []
            grid_controls: dict = {}

        assert est._grid_tc(Page()) == (0, "")

    def test_only_the_first_few_grids_on_a_page_are_priced(self, caplog):
        class Page:
            grid_controls: dict = {}
            tables = [{"columns": ["A", "B"], "row_count": 2}] * 9

        with caplog.at_level("INFO", logger="engine.qa_estimator"):
            total, note = est._grid_tc(Page())
        assert f"{est.MAX_PRICED_GRIDS_PER_PAGE} grids" in note
        assert "priced 3" in caplog.text
        assert total > 0

    def test_the_per_page_ceiling_is_enforced_and_logged(self, monkeypatch,
                                                          caplog):
        monkeypatch.setattr(est, "MAX_GRID_TC_PER_PAGE", 4)

        class Page:
            grid_controls: dict = {}
            tables = [{"columns": ["A", "B"], "row_count": 2}] * 3

        with caplog.at_level("INFO", logger="engine.qa_estimator"):
            total, _ = est._grid_tc(Page())
        assert total == 4
        assert "kept 4 (cap 4)" in caplog.text


# ── The counter itself ───────────────────────────────────────────────

class TestCountGridCases:
    def test_it_counts_what_enumerate_grid_writes(self):
        page = _analysis(ADMIN_GRID).pages[0]
        table, controls = page.tables[0], page.grid_controls
        written = tc_rules.enumerate_grid(
            {"url": page.url}, table, controls, "Customer orders",
            tc_rules.grid_checks())
        assert tc_rules.count_grid_cases(table, controls) == len(written)

    def test_a_bare_grid_counts_the_two_cases_it_justifies(self):
        assert tc_rules.count_grid_cases(
            {"columns": ["A", "B"], "row_count": 1}, {}) == 2

    def test_an_empty_grid_still_earns_its_header_check(self):
        assert tc_rules.count_grid_cases(
            {"columns": ["A", "B"], "row_count": 0}, {}) == 1

    def test_it_is_capped_like_the_generator(self, monkeypatch):
        monkeypatch.setattr(tc_rules, "MAX_CASES_PER_GRID", 3)
        page = _analysis(ADMIN_GRID).pages[0]
        assert tc_rules.count_grid_cases(
            page.tables[0], page.grid_controls) == 3

    def test_a_non_grid_costs_nothing(self):
        assert tc_rules.count_grid_cases({}) == 0
        assert tc_rules.count_grid_cases("not a dict") == 0
