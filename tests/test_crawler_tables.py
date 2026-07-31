"""The crawler must recognise data grids — and only data grids.

Why this matters: ``coverage_rules.yaml`` carries a whole ``list_surface``
section (sorting, paging, filters, bulk actions, empty state) that could
not be generated at all, because the parser did not see tables. Teaching
it to see them is only half the job. The other half is *not* seeing them
where they are not: old markup builds page shells out of ``<table>``, and
a suite full of sort/pagination cases against a layout table is precisely
the anti-pattern ``house_style.yaml`` names — "Inventing UI that the
artifacts do not evidence".

So every positive assertion here has a negative twin: the grid is
recognised, the layout table is refused; the pager is found, a bare
"Next" button in a wizard is not.
"""
from __future__ import annotations

import pytest

from engine.site_crawler import _PageParser


ORDERS_GRID = """
<html><head><title>Orders</title></head><body>
<h1>Orders</h1>
<form role="search"><input type="search" name="q" placeholder="Search orders"></form>
<label for="st">Status</label>
<select id="st" name="status_filter">
  <option>All</option><option>Paid</option><option>Refunded</option>
</select>
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
    <tr><td><input type="checkbox"></td><td><a href="/orders/3">#3</a></td>
        <td>Cid</td><td>New</td></tr>
  </tbody>
</table>
<nav aria-label="pagination">
  <a href="?p=1">1</a><a href="?p=2">2</a><a rel="next" href="?p=2">Next</a>
</nav>
</body></html>
"""


def _parse(html: str) -> _PageParser:
    p = _PageParser()
    p.feed(html)
    return p


@pytest.fixture(scope="module")
def grid() -> dict:
    p = _parse(ORDERS_GRID)
    assert len(p.tables) == 1, p.tables
    return p.tables[0]


@pytest.fixture(scope="module")
def controls() -> dict:
    return _parse(ORDERS_GRID).grid_controls


# ── The grid itself ──────────────────────────────────────────────────

class TestGridShape:
    def test_caption_names_the_surface(self, grid):
        assert grid["caption"] == "Customer orders"

    def test_column_headers_are_captured_in_order(self, grid):
        # The checkbox <th> has no text, so it contributes no column.
        assert grid["columns"] == ["Order ID", "Customer", "Status"]

    def test_only_body_rows_are_counted(self, grid):
        # The header row must not inflate the count — an empty-state case
        # turns on "zero data rows", not "zero <tr>".
        assert grid["row_count"] == 3

    def test_row_checkboxes_and_select_all_are_distinguished(self, grid):
        assert grid["has_checkboxes"] is True
        assert grid["select_all"] is True

    def test_row_links_are_detected(self, grid):
        assert grid["row_links"] is True

    def test_sortable_columns_need_a_sort_signal(self, grid):
        # "Order ID" carries aria-sort, "Customer" a sort class + header
        # link; "Status" is plain text and must stay out.
        assert grid["sortable_columns"] == ["Order ID", "Customer"]

    def test_private_parser_state_does_not_leak(self, grid):
        assert not [k for k in grid if k.startswith("_")]


class TestSortEvidence:
    def test_aria_sort_alone_is_enough(self):
        p = _parse('<table><tr><th aria-sort="none">A</th>'
                   "<th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
        assert p.tables[0]["sortable_columns"] == ["A"]

    def test_a_clickable_header_counts(self):
        p = _parse("<table><tr><th><button>A</button></th><th>B</th></tr>"
                   "<tr><td>1</td><td>2</td></tr></table>")
        assert p.tables[0]["sortable_columns"] == ["A"]

    def test_a_plain_header_does_not(self):
        p = _parse("<table><tr><th>A</th><th>B</th></tr>"
                   "<tr><td>1</td><td>2</td></tr></table>")
        assert p.tables[0]["sortable_columns"] == []

    def test_a_sortable_table_marks_all_of_its_columns(self):
        # Grid libraries flag the table, not each header — Wikipedia's
        # class="wikitable sortable", DataTables, bootstrap-table.
        p = _parse('<table class="wikitable sortable">'
                   "<tr><th>Country</th><th>Population</th></tr>"
                   "<tr><td>UA</td><td>1</td></tr></table>")
        assert p.tables[0]["sortable_columns"] == ["Country", "Population"]

    def test_an_unsortable_header_opts_out_of_a_sortable_table(self):
        p = _parse('<table class="wikitable sortable">'
                   '<tr><th>Country</th><th class="unsortable">Notes</th>'
                   "</tr><tr><td>UA</td><td>x</td></tr></table>")
        assert p.tables[0]["sortable_columns"] == ["Country"]

    def test_a_link_in_a_header_is_not_a_row_link(self):
        p = _parse("<table><tr><th><a href='?sort=a'>A</a></th><th>B</th>"
                   "</tr><tr><td>1</td><td>2</td></tr></table>")
        assert p.tables[0]["row_links"] is False


# ── Layout tables must NOT become grids ──────────────────────────────

class TestLayoutTablesAreRefused:
    def test_a_table_with_no_headers_is_not_a_grid(self):
        p = _parse("<table><tr><td>menu</td><td>content</td></tr></table>")
        assert p.tables == []

    def test_role_presentation_is_refused_even_with_headers(self):
        p = _parse('<table role="presentation"><tr><th>A</th><th>B</th></tr>'
                   "<tr><td>1</td><td>2</td></tr></table>")
        assert p.tables == []

    def test_role_none_is_refused(self):
        p = _parse('<table role="none"><tr><th>A</th><th>B</th></tr>'
                   "<tr><td>1</td><td>2</td></tr></table>")
        assert p.tables == []

    def test_a_single_header_cell_is_not_tabular_enough(self):
        # The classic sidebar: <th>Menu</th> above a stack of links.
        p = _parse("<table><tr><th>Menu</th></tr><tr><td>"
                   "<a href='/a'>A</a></td></tr></table>")
        assert p.tables == []

    def test_an_explicit_role_rescues_a_one_column_grid(self):
        # role="grid" is the author asserting tabular semantics, which is
        # evidence a bare <table> does not supply.
        p = _parse('<table role="grid"><tr><th>Name</th></tr>'
                   "<tr><td>Ann</td></tr></table>")
        assert len(p.tables) == 1

    def test_a_grid_nested_in_a_layout_table_still_counts(self):
        p = _parse('<table role="presentation"><tr><td>'
                   "<table><tr><th>A</th><th>B</th></tr>"
                   "<tr><td>1</td><td>2</td></tr></table>"
                   "</td></tr></table>")
        assert len(p.tables) == 1
        assert p.tables[0]["columns"] == ["A", "B"]

    def test_an_empty_grid_is_still_a_grid(self):
        # Zero rows is a state of a real grid, not a disqualification —
        # the row-dependent cases gate on row_count separately.
        p = _parse("<table><thead><tr><th>A</th><th>B</th></tr></thead>"
                   "<tbody></tbody></table>")
        assert len(p.tables) == 1
        assert p.tables[0]["row_count"] == 0


# ── Real-world markup ────────────────────────────────────────────────

class TestRealWorldMarkup:
    """Both defects below came from parsing live pages on 2026-07-31.

    A synthetic fixture passed everything; the real pages did not. Same
    lesson the httpbin form taught the form parser.
    """

    def test_a_row_header_is_not_a_column(self):
        """pypi.org/project/requests — the file-hash table.

        ``<th scope="row">SHA256</th><td>…</td>`` made SHA256 / MD5 /
        BLAKE2b-256 look like three extra columns, so the grid claimed
        five columns where it renders two — and would have fanned sort
        cases out over columns that do not exist.
        """
        p = _parse("<table><caption>Hashes</caption>"
                   "<thead><tr><th>Algorithm</th><th>Hash digest</th></tr>"
                   "</thead><tbody>"
                   '<tr><th scope="row">SHA256</th><td>abc</td></tr>'
                   '<tr><th scope="row">MD5</th><td>def</td></tr>'
                   "</tbody></table>")
        assert p.tables[0]["columns"] == ["Algorithm", "Hash digest"]
        assert p.tables[0]["row_count"] == 2

    def test_a_row_header_without_scope_is_still_not_a_column(self):
        # The buffer catches it from the row's shape alone: a row that
        # also holds <td> is a data row, whatever its <th> declares.
        p = _parse("<table><tr><th>Algorithm</th><th>Digest</th></tr>"
                   "<tr><th>SHA256</th><td>abc</td></tr></table>")
        assert p.tables[0]["columns"] == ["Algorithm", "Digest"]

    def test_a_repeated_footer_header_row_does_not_double_the_columns(self):
        """datatables.net — <tfoot> repeats the header row verbatim.

        Left alone it doubled every column, and with it every sort case.
        """
        head = "<tr><th>Name</th><th>Office</th></tr>"
        p = _parse(f"<table><thead>{head}</thead>"
                   "<tbody><tr><td>Ann</td><td>Kyiv</td></tr></tbody>"
                   f"<tfoot>{head}</tfoot></table>")
        assert p.tables[0]["columns"] == ["Name", "Office"]
        assert p.tables[0]["row_count"] == 1

    def test_js_attached_sorting_yields_no_sort_evidence(self):
        """A DataTables grid declares no sortability in its markup.

        Emitting sort cases anyway would be a guess. No signal, no case —
        the same rule that keeps "Search More" off a generic form.
        """
        p = _parse('<table id="example" class="table table-striped">'
                   "<thead><tr><th>Name</th><th>Age</th></tr></thead>"
                   "<tbody><tr><td>Ann</td><td>30</td></tr></tbody></table>")
        assert p.tables[0]["sortable_columns"] == []


# ── Controls around the grid ─────────────────────────────────────────

class TestGridControls:
    def test_pagination_container_and_its_labels(self, controls):
        assert controls["pagination"] is True
        assert controls["pagination_labels"] == ["1", "2", "Next"]

    def test_search_control(self, controls):
        assert controls["search"] is True
        assert controls["search_labels"] == ["Search orders"]

    def test_filter_control(self, controls):
        assert controls["filters"] == ["status filter"]

    def test_bulk_actions_come_from_the_picker_options(self, controls):
        # The picker's own "-- Bulk actions --" prompt names the control,
        # not an action, so it must not become a case.
        assert controls["bulk_actions"] == ["Delete selected",
                                            "Export to CSV"]

    def test_create_control(self, controls):
        assert controls["create_controls"] == ["Add order"]

    def test_rel_next_alone_evidences_a_pager(self):
        p = _parse('<table><tr><th>A</th><th>B</th></tr>'
                   '<tr><td>1</td><td>2</td></tr></table>'
                   '<a rel="next" href="/p/2">More</a>')
        assert p.grid_controls["pagination"] is True

    def test_a_bare_next_button_is_not_a_pager(self):
        # A wizard's "Next" is not a pager. Without a pagination
        # container, rel, or aria-label there is no evidence of paging.
        p = _parse("<table><tr><th>A</th><th>B</th></tr>"
                   "<tr><td>1</td><td>2</td></tr></table>"
                   "<button>Next</button>")
        assert p.grid_controls["pagination"] is False

    def test_a_select_the_markup_does_not_call_a_filter_is_not_one(self):
        p = _parse('<select name="country"><option>UA</option></select>')
        assert p.grid_controls["filters"] == []

    def test_add_to_cart_is_not_a_create_control(self):
        p = _parse("<button>Add to cart</button><a href='/x'>Add to bag</a>")
        assert p.grid_controls["create_controls"] == []

    def test_grid_controls_are_dropped_on_a_page_with_no_grid(self):
        # A pager on a page with no table evidences nothing list_surface
        # can use, and would otherwise be inherited by the next surface.
        from engine.site_crawler import _parse_page
        page = _parse_page(
            '<html><body><nav class="pagination"><a href="/2">Next</a></nav>'
            "</body></html>", "https://x.test/", "x.test")
        assert page.tables == []
        assert page.grid_controls == {}

    def test_page_info_carries_the_grid_through(self):
        from engine.site_crawler import _parse_page
        page = _parse_page(ORDERS_GRID, "https://shop.test/orders",
                           "shop.test")
        assert len(page.tables) == 1
        assert page.grid_controls["pagination"] is True


# ── Caps and existing behaviour ──────────────────────────────────────

class TestLimitsAndCompatibility:
    def test_table_count_is_capped(self):
        from engine.site_crawler import MAX_TABLES_PER_PAGE
        one = ("<table><tr><th>A</th><th>B</th></tr>"
               "<tr><td>1</td><td>2</td></tr></table>")
        p = _parse(one * (MAX_TABLES_PER_PAGE + 5))
        assert len(p.tables) == MAX_TABLES_PER_PAGE

    def test_column_count_is_capped(self):
        from engine.site_crawler import MAX_GRID_COLUMNS
        heads = "".join(f"<th>c{i}</th>" for i in range(MAX_GRID_COLUMNS + 10))
        p = _parse(f"<table><tr>{heads}</tr><tr><td>x</td></tr></table>")
        assert len(p.tables[0]["columns"]) == MAX_GRID_COLUMNS

    def test_form_parsing_is_untouched_by_the_table_work(self, ):
        p = _parse(ORDERS_GRID)
        # The role="search" form still yields its control, and the two
        # loose <select>s stay out of it (they are not inside a <form>).
        assert len(p.forms) == 1
        names = [f["name"] for f in p.forms[0]["fields"]]
        assert names == ["q"]

    def test_a_select_inside_a_form_still_collects_its_options(self):
        p = _parse('<form><select name="bulk_action">'
                   "<option>Delete</option><option>Export</option>"
                   "</select></form>")
        # The same <select> feeds both sinks: the form field inventory
        # and the bulk-action list.
        assert p.forms[0]["fields"][0]["options"] == ["Delete", "Export"]
        assert p.grid_controls["bulk_actions"] == ["Delete", "Export"]

    def test_table_text_does_not_bleed_into_a_label(self):
        p = _parse("<form><label>Name <input name='n'></label></form>"
                   "<table><tr><th>Col</th><th>Col2</th></tr>"
                   "<tr><td>v</td><td>w</td></tr></table>")
        assert p.forms[0]["fields"][0]["label"] == "Name"
