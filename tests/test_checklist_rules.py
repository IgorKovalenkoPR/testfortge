"""Low-level checklist generation + the crawler evidence it needs.

Covers:
  * engine.checklist_rules — Header / Page Content / Footer sections,
    hierarchical numbering, form expansion, evidence gating, dedup.
  * engine.site_crawler — the page-region, menu-structure, contact and
    heading-level capture added for it, plus the gzip decode fix.

The shape under test is measured from the team's own reviewed deliverable
— the "Low-level checklist" sheet of Training Plan_Horban Yaroslavna.xlsx,
57 checks over Header (7) / Page Content (38) / Footer (12). See
``engine/qa_knowledge/style/checklist_style.yaml`` for the measurements.

Markup discipline: the fixtures below are shaped after real pages this was
developed against (testfort.com, qarea.com, python.org) rather than after
the parser. Synthetic fixtures have hidden live-page defects in this
codebase before — a header captured as 60 flat links, a honeypot field
promoted to a checklist row, four copies of one form expanded four times.
Each of those has a regression test here.
"""
from __future__ import annotations

import zlib

import pytest

from engine import checklist_rules as cr
from engine import glossary as g
from engine.site_crawler import (_decompress, _parse_page, _region_of,
                                 _social_network)


# ── Fixtures ─────────────────────────────────────────────────────────

MARKETING_PAGE = """
<html><head><title>Mobile App Testing Services</title></head><body>
<header class="site-header">
  <a href="/" class="logo"><img src="/logo.svg" alt="TestFort"></a>
  <nav><ul>
    <li><a href="/why-us">Why Us</a>
      <ul><li><a href="/testimonials">Testimonials</a></li>
          <li><a href="/pricing">Pricing</a></li></ul></li>
    <li><a href="/services">Services</a>
      <ul><li><a href="/services/manual">Manual Testing</a></li>
          <li><a href="/services/auto">Automation Testing</a></li></ul></li>
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
  <a href="https://www.youtube.com/@x">YouTube</a>
  <ul><li><a href="/why-us">Why us</a>
        <ul><li><a href="/about">About Us</a></li>
            <li><a href="/awards">Awards</a></li></ul></li></ul>
  <a href="/privacy-policy">Privacy Policy</a>
  <a href="/cookie-policy">Cookie Policy</a>
</footer></body></html>
"""


@pytest.fixture(scope="module")
def marketing_page() -> dict:
    from dataclasses import asdict
    return asdict(_parse_page(MARKETING_PAGE,
                              "https://example.com/mobile-app-testing",
                              "example.com"))


@pytest.fixture(scope="module")
def checklist(marketing_page) -> cr.LowLevelChecklist:
    return cr.build_checklist([marketing_page],
                              url="https://example.com/mobile-app-testing")


def _section(checklist, name):
    return next(s for s in checklist.sections if s.name == name)


def _blob(checklist) -> str:
    return " || ".join(c.objective for c in checklist.all_checks())


# ── Crawler: page regions ────────────────────────────────────────────

class TestRegionDetection:
    @pytest.mark.parametrize("tag,attrs,want", [
        ("header", {}, "header"),
        ("footer", {}, "footer"),
        ("div", {"role": "banner"}, "header"),
        ("div", {"role": "contentinfo"}, "footer"),
        ("div", {"class": "site-header"}, "header"),
        ("div", {"class": "site-footer"}, "footer"),
        ("div", {"id": "masthead"}, "header"),
        ("section", {"class": "global-footer"}, "footer"),
    ])
    def test_recognised_regions(self, tag, attrs, want):
        blob = " ".join(f"{k}={v}" for k, v in attrs.items())
        assert _region_of(tag, attrs, blob) == want

    @pytest.mark.parametrize("tag,attrs", [
        # A grid's column header is not the page Header. Same false-friend
        # family the terminology linter had to fence off.
        ("div", {"class": "column-header"}),
        ("div", {"class": "table-footer"}),
        ("div", {"class": "card-header"}),
        ("div", {"class": "header-row"}),
        ("div", {"class": "accordion-header"}),
        ("span", {"class": "site-header"}),   # not a container tag
        ("div", {"class": "content"}),
    ])
    def test_false_friends_are_not_regions(self, tag, attrs):
        blob = " ".join(f"{k}={v}" for k, v in attrs.items())
        assert _region_of(tag, attrs, blob) == ""

    def test_links_are_filed_by_region(self, marketing_page):
        hdr = [l["text"] for l in marketing_page["header_links"]]
        ftr = [l["text"] for l in marketing_page["footer_links"]]
        assert "Why Us" in hdr and "Case Studies" in hdr
        assert "Why us" in ftr and "About Us" in ftr
        assert "About Us" not in hdr

    def test_logo_is_detected_in_both_regions(self, marketing_page):
        assert marketing_page["has_header_logo"] is True
        assert marketing_page["has_footer_logo"] is True


class TestMenuStructure:
    def test_top_level_items_with_children_become_groups(self, marketing_page):
        groups = {g["label"]: g for g in marketing_page["nav_groups"]}
        assert set(groups) == {"Why Us", "Services", "Why us"}
        assert groups["Services"]["region"] == "header"
        assert groups["Why us"]["region"] == "footer"
        assert "Manual Testing" in groups["Services"]["children"]

    def test_plain_link_is_not_a_group(self, marketing_page):
        labels = {g["label"] for g in marketing_page["nav_groups"]}
        assert "Case Studies" not in labels

    def test_menu_children_are_flagged_in_menu(self, marketing_page):
        by_text = {l["text"]: l for l in marketing_page["header_links"]}
        # A mega-menu overflows the per-group children cap, so the flag —
        # not the children list — is what the checklist filters on.
        assert by_text["Manual Testing"]["in_menu"] is True
        assert by_text["Case Studies"]["in_menu"] is False


class TestContactAndSocial:
    def test_mailto_and_tel_are_split_out(self, marketing_page):
        assert marketing_page["email_links"] == ["contacts@example.com"]
        assert marketing_page["phone_links"] == ["+13103889334"]

    def test_social_networks_are_named(self, marketing_page):
        assert [s["network"] for s in marketing_page["social_links"]] == \
            ["LinkedIn", "Telegram", "YouTube"]

    def test_legal_links_are_collected(self, marketing_page):
        assert {l["text"] for l in marketing_page["legal_links"]} == \
            {"Privacy Policy", "Cookie Policy"}

    @pytest.mark.parametrize("href,want", [
        ("https://www.linkedin.com/company/x", "LinkedIn"),
        ("https://t.me/channel", "Telegram"),
        ("https://x.com/handle", "X (Twitter)"),
        ("https://play.google.com/store/apps/details?id=x", "Google Play"),
        # Host-matched, so a mention in a query string is not a profile.
        ("https://example.com/blog?ref=facebook.com", ""),
        ("https://example.com/about", ""),
    ])
    def test_social_network_resolution(self, href, want):
        assert _social_network(href.lower()) == want


class TestHeadingLevels:
    def test_levels_are_preserved(self, marketing_page):
        levels = [(h["level"], h["text"])
                  for h in marketing_page["heading_levels"]]
        assert (2, "Mobile Testing by Platform") in levels
        assert (3, "Manual testing") in levels

    def test_flat_headings_still_populated_for_old_consumers(self,
                                                            marketing_page):
        assert "Manual testing" in marketing_page["headings"]


class TestGzipDecode:
    """python.org served gzip regardless of Accept-Encoding.

    The body decoded to binary noise and produced a page record with zero
    headings, zero links and zero forms while reporting no error at all —
    so every downstream module silently under-covered that site.
    """

    def test_gzip_body_is_decoded(self):
        body = b"<html><h2>Hi</h2></html>"
        co = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
        gz = co.compress(body) + co.flush()
        out, err = _decompress(gz, "gzip")
        assert err == ""
        assert b"<h2>Hi</h2>" in out

    def test_identity_body_passes_through(self):
        assert _decompress(b"<html>", "") == (b"<html>", "")
        assert _decompress(b"<html>", "identity") == (b"<html>", "")

    def test_deflate_body_is_decoded(self):
        body = b"<html><h2>Hi</h2></html>"
        out, err = _decompress(zlib.compress(body), "deflate")
        assert err == "" and b"<h2>Hi</h2>" in out

    def test_truncated_stream_keeps_what_it_got(self):
        # The wire read is capped mid-document, so a partial inflate is
        # normal and must not be discarded: a prefix of the markup still
        # names some controls, an empty body names none.
        #
        # How MUCH survives is a property of zlib's window, not of us — a
        # highly repetitive payload yields only a few bytes from its first
        # half — so this asserts a non-empty prefix, not a proportion.
        body = (b"<html><h2>Heading</h2>"
                + b"".join(b"<p>item %d</p>" % i for i in range(2000)))
        co = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
        gz = co.compress(body) + co.flush()
        out, err = _decompress(gz[:len(gz) // 2], "gzip")
        assert err == ""
        assert out
        assert body.startswith(out)

    def test_unknown_encoding_is_an_error_not_garbage(self):
        out, err = _decompress(b"\x00\x01", "exotic")
        assert out == b"" and "unsupported" in err

    def test_undecodable_body_reports_rather_than_returning_noise(self):
        out, err = _decompress(b"not gzip at all", "gzip")
        assert out == b""
        assert err


# ── Numbering ────────────────────────────────────────────────────────

class TestNumbering:
    def test_hierarchy_matches_the_reference_form(self):
        cl = cr.LowLevelChecklist(sections=[
            cr.Section(name="Header", checks=[
                cr.Check("Verify that A", "Header"),
                cr.Check("Verify that B", "Header"),
            ]),
            cr.Section(name="Page Content", checks=[
                cr.Check("Verify that C", "Page Content"),
                cr.Check("Verify that C1", "Page Content", depth=3),
                cr.Check("Verify that C2", "Page Content", depth=3),
                cr.Check("Verify that D", "Page Content"),
            ]),
        ])
        cr.assign_numbers(cl)
        assert [s.number for s in cl.sections] == ["1", "2"]
        assert [c.number for c in cl.sections[0].checks] == ["1.1", "1.2"]
        assert [c.number for c in cl.sections[1].checks] == \
            ["2.1", "2.1.1", "2.1.2", "2.2"]

    def test_orphan_sub_check_is_promoted_not_dropped(self):
        cl = cr.LowLevelChecklist(sections=[
            cr.Section(name="Header", checks=[
                cr.Check("Verify that A", "Header", depth=3),
            ]),
        ])
        cr.assign_numbers(cl)
        check = cl.sections[0].checks[0]
        assert check.number == "1.1" and check.depth == 2

    def test_level_three_counter_resets_per_parent(self):
        cl = cr.LowLevelChecklist(sections=[
            cr.Section(name="S", checks=[
                cr.Check("Verify that A", "S"),
                cr.Check("Verify that A1", "S", depth=3),
                cr.Check("Verify that B", "S"),
                cr.Check("Verify that B1", "S", depth=3),
            ]),
        ])
        cr.assign_numbers(cl)
        assert [c.number for c in cl.sections[0].checks] == \
            ["1.1", "1.1.1", "1.2", "1.2.1"]


# ── Sections ─────────────────────────────────────────────────────────

class TestSectionShape:
    def test_the_three_reference_sections_are_produced_in_order(self,
                                                               checklist):
        assert [s.name for s in checklist.sections] == \
            ["Header", "Page Content", "Footer"]

    def test_surface_banner_names_the_page(self, checklist):
        assert checklist.surface == '"Mobile App Testing Services" page'

    def test_every_row_ships_unchecked(self, checklist):
        items = cr.to_checklist_items(checklist)
        assert items and all(i.status == "Unchecked" for i in items)

    def test_ids_keep_their_section_prefix(self, checklist):
        items = cr.to_checklist_items(checklist)
        by_section = {}
        for i in items:
            by_section.setdefault(i.section, set()).add(i.id.split("_")[0])
        assert by_section["Header"] == {"HDR"}
        assert by_section["Footer"] == {"FTR"}
        assert by_section["Page Content"] == {"CNT"}

    def test_item_num_and_depth_survive_the_conversion(self, checklist):
        items = cr.to_checklist_items(checklist)
        assert items[0].item_num == "1.1"
        assert any(i.depth == 3 and i.item_num.count(".") == 2
                   for i in items)


class TestHeaderSweep:
    def test_one_row_per_menu_not_per_sub_item(self, checklist):
        header = _section(checklist, "Header")
        menu_rows = [c for c in header.checks
                     if "sub-items are visible" in c.objective]
        assert len(menu_rows) == 2
        assert any('"Services" drop-down' in c.objective for c in menu_rows)
        # The regression this guards: filtering on the children lists let
        # mega-menu overflow through and produced a 16-row Header.
        assert not any("Manual Testing" in c.objective
                       for c in header.checks)

    def test_logo_row_is_present(self, checklist):
        header = _section(checklist, "Header")
        assert any("Homepage is opened after clicking the logo"
                   in c.objective for c in header.checks)

    def test_plain_top_level_link_gets_its_own_row(self, checklist):
        header = _section(checklist, "Header")
        assert any("Case Studies" in c.objective for c in header.checks)

    def test_cta_row_targets_the_contact_form(self, checklist):
        header = _section(checklist, "Header")
        assert any("[Contact us] button opens the Contact Form"
                   in c.objective for c in header.checks)

    def test_no_header_region_means_no_header_section(self):
        page = {"url": "https://x.test/", "title": "X",
                "headings": ["Only content"]}
        cl = cr.build_checklist([page])
        assert not any(s.name == "Header" for s in cl.sections)
        assert any("no Header region" in gap for gap in cl.gaps)


class TestFooterSweep:
    def test_contact_affordances_get_a_row_each(self, checklist):
        footer = _section(checklist, "Footer")
        blob = " ".join(c.objective for c in footer.checks)
        assert "opens the default mail client" in blob
        assert "opens the call application" in blob

    def test_social_networks_are_named_in_one_row(self, checklist):
        footer = _section(checklist, "Footer")
        rows = [c for c in footer.checks if "social media icon" in c.objective]
        assert len(rows) == 1
        assert "LinkedIn" in rows[0].objective
        assert "YouTube" in rows[0].objective

    def test_legal_links_get_a_row_each(self, checklist):
        footer = _section(checklist, "Footer")
        blob = " ".join(c.objective for c in footer.checks)
        assert '"Privacy Policy" link' in blob
        assert '"Cookie Policy" link' in blob

    def test_footer_menu_becomes_one_row(self, checklist):
        footer = _section(checklist, "Footer")
        assert any('"Why us" menu' in c.objective for c in footer.checks)

    def test_flat_fat_footer_is_reported_as_a_gap(self):
        page = {
            "url": "https://x.test/", "title": "X",
            "headings": ["Content"],
            "footer_links": [{"text": f"Link {i}", "href": f"/p{i}"}
                             for i in range(20)],
            "email_links": ["a@b.test"],
        }
        cl = cr.build_checklist([page])
        assert any("no menu structure" in gap for gap in cl.gaps)


class TestContentSweep:
    def test_h2_becomes_a_section_row_and_h3_nests_under_it(self, checklist):
        content = _section(checklist, "Page Content")
        parent = next(c for c in content.checks
                      if "Types of Mobile QA We Do" in c.objective)
        child = next(c for c in content.checks
                     if "Manual testing" in c.objective)
        assert parent.depth == 2 and child.depth == 3
        assert child.number.startswith(parent.number + ".")

    def test_h1_gets_the_heading_row(self, checklist):
        content = _section(checklist, "Page Content")
        assert '"Mobile Application Testing Services" heading' \
            in content.checks[0].objective

    def test_multiple_surfaces_get_one_section_each(self):
        pages = [
            {"url": "https://x.test/a", "title": "Alpha",
             "headings": ["One"], "has_header_logo": True},
            {"url": "https://x.test/b", "title": "Beta", "headings": ["Two"]},
        ]
        cl = cr.build_checklist(pages)
        names = [s.name for s in cl.sections]
        assert "Page Content — Alpha" in names
        assert "Page Content — Beta" in names

    def test_surfaces_beyond_the_cap_are_reported(self):
        pages = [{"url": f"https://x.test/{i}", "title": f"P{i}",
                  "headings": ["H"]}
                 for i in range(cr.MAX_CONTENT_SECTIONS + 3)]
        cl = cr.build_checklist(pages)
        assert any("further crawled surfaces" in gap for gap in cl.gaps)


class TestFormExpansion:
    def test_identical_placements_collapse_to_one_block(self, checklist):
        content = _section(checklist, "Page Content")
        blocks = [c for c in content.checks
                  if "is displayed with every field it declares"
                  in c.objective]
        # Two placements of one Contact Form 7 instance. Keying on the
        # action WITH its fragment let all of them through as separate
        # forms, which cost 15 rows each.
        assert len(blocks) == 1
        assert "Contact Form" in blocks[0].objective

    def test_repeated_placement_is_stated_not_hidden(self, checklist):
        assert "2 placements of the Contact Form" in _blob(checklist)

    def test_form_is_named_from_what_it_declares(self, checklist):
        assert "Form #" not in _blob(checklist)

    def test_per_field_rows_cover_valid_empty_and_format(self, checklist):
        blob = _blob(checklist)
        assert "Email field accepts valid data" in blob
        assert "Email field does not accept an empty value on submit" in blob
        assert "an invalid email format in the Email field" in blob

    def test_maxlength_produces_a_boundary_row(self, checklist):
        assert "longer than 400 characters" in _blob(checklist)

    def test_consent_checkbox_becomes_a_submit_guard_not_a_field_row(
            self, checklist):
        blob = _blob(checklist)
        assert "without marking the Consent checkbox" in blob
        assert "Consent field accepts valid data" not in blob

    def test_honeypot_field_never_reaches_the_sheet(self, checklist):
        # Contact Form 7 ships _wpcf7_ak_hp_textarea, an Akismet honeypot.
        # A row asking a tester to fill it in cannot be executed.
        blob = _blob(checklist).lower()
        assert "_wpcf7" not in blob
        assert "hp_textarea" not in blob

    def test_submit_and_success_rows_are_present(self, checklist):
        blob = _blob(checklist)
        assert "[Send] button is clickable" in blob
        assert "success message is displayed after submission" in blob

    def test_disambiguating_suffix_is_applied(self, checklist):
        assert "in the Contact section" in _blob(checklist)

    def test_no_required_attribute_means_no_empty_value_row(self):
        page = {
            "url": "https://x.test/", "title": "X", "headings": [],
            "forms": [{"action": "/go", "method": "post", "fields": [
                {"name": "nickname", "type": "text"},
            ]}],
        }
        cl = cr.build_checklist([page])
        blob = _blob(cl)
        assert "nickname field accepts valid data" in blob
        assert "does not accept an empty value" not in blob


class TestGridExpansion:
    def test_grid_rows_are_gated_on_declared_controls(self):
        page = {
            "url": "https://x.test/orders", "title": "Orders", "headings": [],
            "tables": [{"caption": "Customer orders",
                        "columns": ["Order ID", "Total"],
                        "sortable_columns": ["Order ID"],
                        "row_links": True, "select_all": True,
                        "row_count": 12}],
            "grid_controls": {"pagination": True, "filters": ["Status"]},
        }
        blob = _blob(cr.build_checklist([page]))
        assert '"Customer orders" grid is displayed with every column' in blob
        assert 'sorted by the "Order ID" column' in blob
        assert "after marking the header checkbox" in blob
        assert "pagination control" in blob
        assert '"Status" filter' in blob

    def test_a_bare_grid_gets_only_its_structural_row(self):
        page = {"url": "https://x.test/t", "title": "T", "headings": [],
                "tables": [{"caption": "", "columns": ["A", "B"],
                            "row_count": 3}]}
        blob = _blob(cr.build_checklist([page]))
        assert "data grid is displayed with every column" in blob
        assert "sorted by" not in blob
        assert "header checkbox" not in blob


# ── Wording compliance ──────────────────────────────────────────────

class TestWordingCompliance:
    def test_generated_checklist_passes_the_terminology_linter(self,
                                                              checklist):
        assert cr.lint_checklist(checklist) == []

    def test_every_objective_opens_with_verify(self, checklist):
        for check in checklist.all_checks():
            assert check.objective.startswith("Verify that"), check.objective

    def test_no_objective_ends_with_a_period(self, checklist):
        for check in checklist.all_checks():
            assert not check.objective.endswith("."), check.objective

    def test_page_regions_are_capitalised(self, checklist):
        blob = _blob(checklist)
        assert "the footer" not in blob
        assert "the homepage" not in blob

    def test_junk_labels_never_become_rows(self):
        page = {
            "url": "https://x.test/", "title": "X", "headings": [],
            "forms": [{"action": "/go", "fields": [
                {"name": "Δ", "type": "text"},
                {"name": "a", "type": "text"},
                {"name": "9f8e7d6c5b4a39281", "type": "text"},
                {"name": "email", "type": "email", "required": True},
            ]}],
        }
        blob = _blob(cr.build_checklist([page]))
        assert "Δ" not in blob
        assert "9f8e7d6c5b4a39281" not in blob
        assert "email field accepts valid data" in blob


class TestEmptyInput:
    def test_no_pages_reports_a_gap_rather_than_an_empty_sheet(self):
        cl = cr.build_checklist([])
        assert cl.total == 0
        assert any("No page was crawled" in gap for gap in cl.gaps)

    def test_non_dict_pages_are_ignored(self):
        cl = cr.build_checklist([None, "nonsense"])  # type: ignore[list-item]
        assert cl.total == 0


# ── Style asset ─────────────────────────────────────────────────────

class TestChecklistStyleAsset:
    def test_asset_ships_with_its_measurements(self):
        import os
        path = os.path.join(os.path.dirname(g.__file__), "qa_knowledge",
                            "style", "checklist_style.yaml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert text
        for key in ("scope_banner", "sections", "numbering",
                    "page_content_derivation", "form_derivation",
                    "objectives", "header_checks", "footer_checks",
                    "status_vocabulary", "anti_patterns"):
            assert key in text, key
        # The measured counts are what stop a maintainer treating the
        # shape as negotiable.
        assert "57 checks" in text
        assert "Training Plan_Horban Yaroslavna.xlsx" in text

    def test_asset_parses_as_yaml(self):
        import os
        import yaml
        path = os.path.join(os.path.dirname(g.__file__), "qa_knowledge",
                            "style", "checklist_style.yaml")
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        assert isinstance(doc, dict)
        assert doc["sections"]["canonical"] == \
            ["Header", "Page Content", "Footer"]
