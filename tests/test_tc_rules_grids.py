"""Deterministic enumeration of the ``list_surface`` coverage model.

Grids are where the reference corpus says a list surface earns 12-20
cases — sorting, paging, filtering, selection, bulk actions, empty state.
None of that could be generated before, because the crawler did not parse
tables, so ``coverage_rules.yaml`` carried the model and nothing read it.

The property under test is the same evidence discipline the field-level
engine runs on, moved to the grid: **no signal in the markup, no case**.
No pager in the page, no pagination case. No column the markup calls
sortable, no sort case. Group By, Advanced Search, favourite filters, the
column picker and the record counter have no HTML signature at all, so on
a crawled site they must never appear — the same way "Search More" and
"Create and Edit" stay off a generic form.
"""
from __future__ import annotations

import pytest

from engine import tc_rules
from engine.site_crawler import _PageParser
from engine.tc_author import Artifacts, lint_case


ORDERS_GRID = """
<html><head><title>Orders</title></head><body>
<h1>Orders</h1>
<form role="search"><input type="search" name="q" placeholder="Search orders"></form>
<select name="status_filter"><option>All</option><option>Paid</option></select>
<select name="bulk-action-selector-top">
  <option value="">-- Bulk actions --</option>
  <option value="del">Delete selected</option>
  <option value="exp">Export to CSV</option>
</select>
<a class="button" href="/orders/new">Add order</a>
<table class="orders">
  <caption>Customer orders</caption>
  <thead><tr>
    <th><input type="checkbox" id="all"></th>
    <th aria-sort="ascending"><a href="?sort=id">Order ID</a></th>
    <th class="sortable-col"><a href="?sort=cust">Customer</a></th>
    <th>Status</th>
  </tr></thead>
  <tbody>
    <tr><td><input type="checkbox"></td><td><a href="/orders/1">#1</a></td>
        <td>Ann</td><td>Paid</td></tr>
    <tr><td><input type="checkbox"></td><td><a href="/orders/2">#2</a></td>
        <td>Bo</td><td>New</td></tr>
  </tbody>
</table>
<nav aria-label="pagination">
  <a href="?p=1">1</a><a rel="next" href="?p=2">Next</a>
</nav>
</body></html>
"""

#: The honest floor: headers and rows, nothing around them. Everything a
#: pager / filter / checkbox would unlock must be absent from its pack.
BARE_GRID = """
<html><body><h1>Report</h1>
<table>
  <thead><tr><th>Month</th><th>Total</th></tr></thead>
  <tbody><tr><td>May</td><td>10</td></tr></tbody>
</table>
</body></html>
"""


def _pages(html: str, url: str = "https://shop.test/orders") -> list[dict]:
    p = _PageParser()
    p.feed(html)
    return [{"url": url, "title": p.title, "h1": p.h1, "forms": p.forms,
             "tables": p.tables, "grid_controls": p.grid_controls}]


@pytest.fixture(scope="module")
def cases() -> list:
    return tc_rules.enumerate_from_pages(_pages(ORDERS_GRID))


@pytest.fixture(scope="module")
def grid_cases(cases) -> list:
    return [c for c in cases if c.section == "Customer orders"]


@pytest.fixture(scope="module")
def bare_cases() -> list:
    return tc_rules.enumerate_from_pages(
        _pages(BARE_GRID, url="https://shop.test/report"))


def _blob(cases) -> str:
    return " ".join(c.summary for c in cases)


# ── Evidence discipline ──────────────────────────────────────────────

class TestEvidenceGating:
    def test_a_rich_grid_yields_the_corpus_volume(self, grid_cases):
        # The coverage model's own sizing note: "a grid surface yields
        # 12-20" cases.
        assert 12 <= len(grid_cases) <= 20, len(grid_cases)

    def test_sorting_only_for_columns_the_markup_calls_sortable(self,
                                                                grid_cases):
        blob = _blob(grid_cases)
        assert 'by the "Order ID" column in ascending order' in blob
        assert 'by the "Customer" column in descending order' in blob
        # "Status" is a plain header — no sort signal, no sort case.
        assert '"Status" column' not in blob

    def test_pagination_needs_a_pager(self, grid_cases, bare_cases):
        assert "next page" in _blob(grid_cases)
        assert "next page" not in _blob(bare_cases)

    def test_bulk_actions_are_named_from_the_picker(self, grid_cases):
        blob = _blob(grid_cases)
        assert '"Delete selected" bulk action' in blob
        assert '"Export to CSV" bulk action' in blob

    def test_selection_cases_need_checkboxes(self, grid_cases, bare_cases):
        assert "row checkboxes" in _blob(grid_cases)
        assert "header checkbox" in _blob(grid_cases)
        assert "checkbox" not in _blob(bare_cases)

    def test_row_link_cases_need_a_link_in_a_cell(self, grid_cases,
                                                  bare_cases):
        assert "clicking its row link" in _blob(grid_cases)
        assert "row link" not in _blob(bare_cases)

    def test_search_and_empty_state_need_a_search_control(self, grid_cases,
                                                          bare_cases):
        assert "empty state" in _blob(grid_cases)
        assert "empty state" not in _blob(bare_cases)

    def test_a_bare_grid_still_gets_its_structural_cases(self, bare_cases):
        # Headers and one row are enough to justify exactly two checks —
        # and the pack must not be padded past them.
        assert len(bare_cases) == 2
        blob = _blob(bare_cases)
        assert "every column header it declares" in blob
        assert "populated in every column" in blob

    def test_odoo_only_checks_never_fire_on_crawled_markup(self, cases):
        blob = _blob(cases)
        for absent in ("Group By", "Advanced Search", "favourite",
                       "field selector", "record counter"):
            assert absent not in blob, absent

    @pytest.mark.parametrize("token,table,controls,expected", [
        ("columns", {"columns": ["a", "b"]}, {}, True),
        ("columns", {"columns": ["a"]}, {}, False),
        ("rows", {"row_count": 1}, {}, True),
        ("rows", {"row_count": 0}, {}, False),
        ("row_links", {"row_links": True}, {}, True),
        ("row_links", {}, {}, False),
        ("checkboxes", {"has_checkboxes": True}, {}, True),
        ("select_all", {"select_all": True}, {}, True),
        ("sortable", {"sortable_columns": ["a"]}, {}, True),
        ("sortable", {"sortable_columns": []}, {}, False),
        ("pagination", {}, {"pagination": True}, True),
        ("pagination", {}, {"pagination": False}, False),
        ("search", {}, {"search": True}, True),
        ("filters", {}, {"filters": ["x"]}, True),
        ("filters", {}, {"filters": []}, False),
        ("bulk_actions", {}, {"bulk_actions": ["x"]}, True),
        ("create_control", {}, {"create_controls": ["New"]}, True),
        # Every token the coverage model carries for a surface HTML
        # cannot express resolves False, so its check is skipped.
        ("group_by_control", {"columns": ["a", "b"]}, {}, False),
        ("advanced_search_control", {}, {"search": True}, False),
        ("save_filter_control", {}, {"filters": ["x"]}, False),
        ("column_picker_control", {"columns": ["a", "b"]}, {}, False),
        ("record_counter", {"row_count": 9}, {}, False),
        ("never_heard_of_it", {"columns": ["a", "b"]}, {}, False),
        (None, {}, {}, True),
    ])
    def test_token_semantics(self, token, table, controls, expected):
        assert tc_rules._has_grid_evidence(token, table, controls) is expected

    def test_the_yaml_declares_a_token_for_every_check(self):
        checks = tc_rules.load_rules()["list_surface"]["checks"]
        untokened = [c["id"] for c in checks if not c.get("requires")]
        assert not untokened, (
            f"{untokened} would fire on any page that has a table at all")

    def test_every_token_the_yaml_uses_is_documented(self):
        surface = tc_rules.load_rules()["list_surface"]
        used = {c.get("requires") for c in surface["checks"]}
        assert used <= set(surface["evidence"]), (
            used - set(surface["evidence"]))


# ── House style ──────────────────────────────────────────────────────

class TestHouseStyle:
    def test_every_grid_case_passes_the_linter(self, cases):
        offenders = [(c.summary[:70], lint_case(c)) for c in cases
                     if lint_case(c)]
        assert not offenders, offenders

    def test_every_summary_names_the_grid(self, grid_cases):
        for c in grid_cases:
            assert '"Customer orders"' in c.summary, c.summary

    def test_no_case_carries_an_unfilled_placeholder(self, cases):
        for c in cases:
            for text in (c.summary, c.expected_result, *c.steps):
                assert "{" not in text and "}" not in text, text

    def test_first_step_is_a_breadcrumb_to_the_grid(self, grid_cases):
        for c in grid_cases:
            assert c.steps[0] == ("Go to https://shop.test/orders -> "
                                  "Customer orders")

    def test_negatives_assert_the_user_visible_feedback(self, cases):
        from engine.tc_author import asserts_feedback
        negatives = [c for c in cases if c.category == "Negative"]
        assert negatives
        for c in negatives:
            assert asserts_feedback(c.expected_result), c.summary

    def test_both_polarities_are_produced(self, grid_cases):
        assert {c.category for c in grid_cases} == {"Positive", "Negative"}

    def test_no_title_carries_two_assertions(self, grid_cases):
        """House style splits "next and previous" into two cases.

        One Failed row has to name one broken thing; a title joining two
        assertions cannot say which half broke.
        """
        blob = _blob(grid_cases)
        assert "next and previous" not in blob
        assert "ascending and descending" not in blob
        assert "Add and remove columns" not in blob

    def test_fan_out_values_reach_the_test_data_column(self, grid_cases):
        sort = [c for c in grid_cases if "ascending order" in c.summary][0]
        assert sort.test_data == 'Column: "Order ID"'
        reach = [c for c in grid_cases
                 if "every column header it declares" in c.summary][0]
        assert '"Order ID", "Customer", "Status"' in reach.test_data


# ── Section naming ───────────────────────────────────────────────────

class TestGridSectionName:
    def test_a_caption_names_the_surface_outright(self):
        assert tc_rules.grid_section_name(
            {"h1": "Orders"}, {"caption": "Customer orders"}, 1) \
            == "Customer orders"

    def test_a_page_heading_takes_a_grid_suffix(self):
        # The heading names the page, not the grid; on a page holding
        # both a form and a grid the two sections must differ.
        assert tc_rules.grid_section_name({"h1": "Orders"}, {}, 1) \
            == "Orders grid"

    def test_a_heading_that_already_says_list_is_left_alone(self):
        for heading in ("Order list", "Product index", "Results table"):
            assert tc_rules.grid_section_name({"h1": heading}, {}, 1) \
                == heading

    def test_the_url_is_the_last_signal_before_an_ordinal(self):
        assert tc_rules.grid_section_name(
            {"url": "https://x.test/admin/orders"}, {}, 1) \
            == "Admin orders grid"
        assert tc_rules.grid_section_name({"url": "https://x.test/"}, {}, 4) \
            == "Grid #4"
        assert tc_rules.grid_section_name({}, {}, 2) == "Grid #2"

    def test_the_grid_and_the_form_on_one_page_are_separate_sections(self,
                                                                     cases):
        assert {c.section for c in cases} == {"Customer orders", "Orders"}

    def test_captionless_siblings_are_told_apart_by_an_ordinal(self):
        """w3schools/html/html_tables.asp ships two captionless grids.

        Both fell back to the page heading, so their summaries came out
        word-for-word identical and a reader could not tell which grid a
        Failed row belonged to.
        """
        page = {"h1": "Tutorials"}
        assert tc_rules.grid_section_names(page, [{}, {}]) == [
            "Tutorials grid #1", "Tutorials grid #2"]

    def test_a_captioned_grid_keeps_its_own_name_beside_a_sibling(self):
        page = {"h1": "Tutorials"}
        assert tc_rules.grid_section_names(
            page, [{"caption": "Customer orders"}, {}]) == [
            "Customer orders", "Tutorials grid"]

    def test_a_lone_grid_takes_no_ordinal(self):
        assert tc_rules.grid_section_names({"h1": "Orders"}, [{}]) == \
            ["Orders grid"]


# ── Robustness ───────────────────────────────────────────────────────

class TestRobustness:
    def test_a_layout_table_produces_nothing(self):
        html = ("<html><body><h1>Home</h1><table><tr><td>menu</td>"
                "<td>content</td></tr></table></body></html>")
        assert tc_rules.enumerate_from_pages(_pages(html)) == []

    def test_no_tables_and_no_forms_yields_nothing(self):
        html = "<html><body><h1>Prose</h1><p>Nothing here.</p></body></html>"
        assert tc_rules.enumerate_from_pages(_pages(html)) == []

    def test_output_is_reproducible(self):
        a = tc_rules.enumerate_from_pages(_pages(ORDERS_GRID))
        b = tc_rules.enumerate_from_pages(_pages(ORDERS_GRID))
        assert [c.to_dict() for c in a] == [c.to_dict() for c in b]

    def test_artifacts_wrapper_carries_the_grid_through(self):
        pages = _pages(ORDERS_GRID)
        direct = tc_rules.enumerate_from_pages(pages)
        wrapped = tc_rules.enumerate_from_artifacts(Artifacts(pages=pages))
        assert [c.to_dict() for c in direct] == [c.to_dict() for c in wrapped]

    def test_a_page_dict_without_the_new_keys_still_works(self):
        # Callers built before grids existed hand over url/title/h1/forms
        # only; they must keep working, just without grid coverage.
        p = _PageParser()
        p.feed(ORDERS_GRID)
        legacy = [{"url": "https://shop.test/orders", "h1": p.h1,
                   "forms": p.forms}]
        assert tc_rules.enumerate_from_pages(legacy)

    def test_malformed_grid_entries_are_skipped(self):
        pages = _pages(ORDERS_GRID)
        pages[0]["tables"] = ["not a dict", None] + pages[0]["tables"]
        assert tc_rules.enumerate_from_pages(pages)


class TestCapsAreLoudNotSilent:
    def test_fan_out_is_capped_and_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(tc_rules, "MAX_FAN_OUT", 2)
        heads = "".join(f'<th aria-sort="none">c{i}</th>' for i in range(9))
        html = (f"<html><body><h1>Wide</h1><table><thead><tr>{heads}</tr>"
                "</thead><tbody><tr><td>x</td><td>y</td></tr></tbody>"
                "</table></body></html>")
        with caplog.at_level("INFO", logger="engine.tc_rules"):
            cases = tc_rules.enumerate_from_pages(_pages(html))
        sorts = [c for c in cases if "ascending order" in c.summary]
        assert len(sorts) == 2
        assert "fans out over 9 sortable_columns" in caplog.text

    def test_per_grid_cap_is_enforced_and_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(tc_rules, "MAX_CASES_PER_GRID", 5)
        with caplog.at_level("INFO", logger="engine.tc_rules"):
            cases = tc_rules.enumerate_from_pages(_pages(ORDERS_GRID))
        grid = [c for c in cases if c.section == "Customer orders"]
        assert len(grid) == 5
        assert "kept 5 (cap 5)" in caplog.text

    def test_grid_count_cap_is_enforced_and_logged(self, monkeypatch,
                                                   caplog):
        monkeypatch.setattr(tc_rules, "MAX_GRIDS", 2)
        one = ("<table><caption>C</caption><tr><th>A</th><th>B</th></tr>"
               "<tr><td>1</td><td>2</td></tr></table>")
        html = f"<html><body><h1>Many</h1>{one * 5}</body></html>"
        with caplog.at_level("INFO", logger="engine.tc_rules"):
            cases = tc_rules.enumerate_from_pages(_pages(html))
        # Two grids × the two checks a bare grid justifies.
        assert len(cases) == 4
        assert "stopped after 2 grids" in caplog.text

    def test_a_check_with_evidence_but_no_phrasing_warns(self, caplog):
        rule = {"id": "half_written", "category": "Positive",
                "title": "Verify that the {grid} grid does a thing"}
        with caplog.at_level("WARNING", logger="engine.tc_rules"):
            out = tc_rules.enumerate_grid(
                {"url": "https://x.test/g"},
                {"columns": ["A", "B"], "row_count": 1}, {}, "G", [rule])
        assert out == []
        assert "no step template" in caplog.text

    def test_an_unknown_fan_out_list_warns_and_emits_nothing(self, caplog):
        rule = {"id": "bad_fan", "fan_out": "made_up_list",
                "title": "Verify that x", "step": "y", "expected": "z"}
        with caplog.at_level("WARNING", logger="engine.tc_rules"):
            out = tc_rules.enumerate_grid(
                {"url": "https://x.test/g"}, {"columns": ["A", "B"]}, {},
                "G", [rule])
        assert out == []
        assert "unknown fan_out list" in caplog.text
