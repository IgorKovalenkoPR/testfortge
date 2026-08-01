"""Deterministic (LLM-free) test-case enumeration.

This is the free-tier backbone: the Anthropic API has no free tier, and
the free tiers of other vendors either train on submitted content or
forbid serving EEA/UK users — neither acceptable for a QA vendor running
client requirements through the tool. So the rule engine, not the model,
has to carry generation.

The property under test throughout is **evidence discipline**: a case is
emitted only when the crawled markup justifies it. No ``required``
attribute, no required-field negative. No ``maxlength``, no boundary
case. That is what keeps the pack free of cases that fail for the wrong
reason — ``house_style.yaml`` → ``anti_patterns`` → "Inventing UI that the
artifacts do not evidence".
"""
from __future__ import annotations

import pytest

from engine import tc_rules
from engine.site_crawler import _PageParser
from engine.tc_author import Artifacts, lint_case


def _pages(html: str, url: str = "https://shop.test/order") -> list[dict]:
    p = _PageParser()
    p.feed(html)
    return [{"url": url, "title": p.title, "h1": p.h1, "forms": p.forms}]


ORDER_FORM = """
<html><head><title>Order page</title></head><body><h1>Customer order</h1>
<form method="post" action="/post">
  <label for="n">Customer name</label>
  <input type="text" id="n" name="n" required maxlength="60">
  <label for="t">Telephone</label>
  <input type="tel" id="t" name="t">
  <label for="e">E-mail</label>
  <input type="email" id="e" name="e" required>
  <label>Pizza size
    <select name="size"><option>Small</option><option>Large</option></select>
  </label>
  <label for="q">Quantity</label>
  <input type="number" id="q" name="q" required min="1" max="10">
  <label for="c">Comments</label>
  <textarea id="c" name="c" maxlength="500"></textarea>
  <button>Submit order</button>
</form></body></html>
"""


@pytest.fixture(scope="module")
def cases() -> list:
    return tc_rules.enumerate_from_pages(_pages(ORDER_FORM))


def _for(cases, label: str) -> list:
    return [c for c in cases if f'"{label}"' in c.summary]


# ── Evidence discipline ──────────────────────────────────────────────

class TestEvidenceGating:
    def test_required_negative_only_for_required_controls(self, cases):
        assert any("without the \"Customer name\" field filled" in c.summary
                   for c in cases)
        # Telephone is not required — no such case may exist for it.
        assert not any("without the \"Telephone\"" in c.summary
                       for c in cases)

    def test_boundary_cases_only_where_a_limit_is_declared(self, cases):
        # Customer name has maxlength=60; Telephone has none.
        assert any("maximum allowed length" in c.summary
                   for c in _for(cases, "Customer name"))
        assert not any("maximum" in c.summary
                       for c in _for(cases, "Telephone"))

    def test_range_cases_only_where_min_or_max_exists(self, cases):
        qty = _for(cases, "Quantity")
        assert any("minimum allowed value" in c.summary for c in qty)
        assert any("above the maximum" in c.summary for c in qty)
        # A plain text control must not acquire range cases.
        assert not any("minimum" in c.summary
                       for c in _for(cases, "Customer name"))

    def test_dropdown_option_cases_need_options(self, cases):
        size = _for(cases, "Pizza size")
        assert any("selects an existing value" in c.summary.lower()
                   for c in size)

    def test_unknown_evidence_tokens_never_fire(self, cases):
        """Odoo-specific pickers must not appear on a generic HTML form.

        Their `requires:` tokens (search_more_control,
        inline_create_control) cannot be satisfied by this inventory, so
        the cases are skipped rather than invented.
        """
        blob = " ".join(c.summary for c in cases)
        assert "Search More" not in blob
        assert "Create and Edit" not in blob

    @pytest.mark.parametrize("token,field,expected", [
        ("required", {"required": True}, True),
        ("required", {}, False),
        ("not_required", {}, True),
        ("not_required", {"required": True}, False),
        ("maxlength", {"maxlength": "10"}, True),
        ("maxlength", {}, False),
        ("range", {"min": "1"}, True),
        ("range", {"max": "9"}, True),
        ("range", {}, False),
        ("options", {"options": ["a"]}, True),
        ("options", {"options": []}, False),
        ("accept", {"accept": ".pdf"}, True),
        ("never_heard_of_it", {"required": True}, False),
        (None, {}, True),
    ])
    def test_token_semantics(self, token, field, expected):
        assert tc_rules._has_evidence(token, field, {"fields": []}) is expected

    def test_sibling_date_needs_two_date_controls(self):
        one = {"fields": [{"type": "date"}]}
        two = {"fields": [{"type": "date"}, {"type": "datetime-local"}]}
        assert tc_rules._has_evidence("sibling_date", {}, one) is False
        assert tc_rules._has_evidence("sibling_date", {}, two) is True


# ── Control-type mapping ─────────────────────────────────────────────

class TestControlTypes:
    @pytest.mark.parametrize("ftype,expected", [
        ("text", "text_input"), ("tel", "text_input"),
        ("password", "text_input"), ("email", "email_input"),
        ("number", "numeric_input"), ("range", "numeric_input"),
        ("date", "date_input"), ("datetime-local", "date_input"),
        ("select", "dropdown"), ("checkbox", "checkbox"),
        ("radio", "checkbox"), ("file", "file_upload"),
        ("textarea", "rich_text"),
    ])
    def test_primary_mapping(self, ftype, expected):
        assert tc_rules.control_type({"type": ftype}) == expected

    @pytest.mark.parametrize("ftype", ["hidden", "submit", "button", "reset"])
    def test_non_data_controls_are_skipped(self, ftype):
        assert tc_rules.control_type({"type": ftype}) is None
        assert tc_rules.control_types({"type": ftype}) == []

    def test_unknown_type_falls_back_to_text(self):
        # Matches the browser's own behaviour for an unknown type.
        assert tc_rules.control_type({"type": "color-picker-9000"}) \
            == "text_input"

    def test_textarea_draws_from_two_sets(self, cases):
        """A textarea has a maxlength like any text input AND needs the
        pasted-markup check, so it must not lose the boundary cases to a
        single-type mapping."""
        assert tc_rules.control_types({"type": "textarea"}) == \
            ["rich_text", "text_input"]
        comments = _for(cases, "Comments")
        assert any("markup inert" in c.summary for c in comments)
        assert any("maximum allowed length" in c.summary for c in comments)


# ── Naming ───────────────────────────────────────────────────────────

class TestNaming:
    def test_label_wins_over_placeholder_and_name(self):
        f = {"label": "Visible", "placeholder": "ph", "name": "nm"}
        assert tc_rules.field_label(f) == "Visible"

    def test_falls_back_through_placeholder_then_name_then_id(self):
        assert tc_rules.field_label({"placeholder": "ph", "name": "nm"}) == "ph"
        assert tc_rules.field_label({"name": "nm"}) == "nm"
        assert tc_rules.field_label({"id": "the-id"}) == "the-id"
        assert tc_rules.field_label({}) == "unnamed field"

    def test_section_is_the_surface_not_a_test_type(self, cases):
        # House style: sections are named after the UI surface.
        assert {c.section for c in cases} == {"Customer order"}

    def test_section_fallback_chain(self):
        """heading → h1 → title → humanised URL path → ordinal.

        The URL step was added after the real httpbin page (no <h1>, no
        <title>) produced the useless section name "Form #1".
        """
        assert tc_rules.surface_name({"h1": "H1 wins", "title": "t"},
                                     {"heading": "Heading wins"}, 1) \
            == "Heading wins"
        assert tc_rules.surface_name({"h1": "H1 wins", "title": "t"},
                                     {"heading": ""}, 1) == "H1 wins"
        assert tc_rules.surface_name({"url": "u", "h1": "", "title": "The title"},
                                     {"heading": ""}, 1) == "The title"
        assert tc_rules.surface_name({"url": "https://x.test/checkout/step-2"},
                                     {}, 1) == "Checkout step 2 form"

    def test_ordinal_only_when_nothing_names_the_surface(self):
        # Bare origin: no path to humanise, nothing on the page either.
        assert tc_rules.surface_name({"url": "https://x.test/"}, {}, 3) \
            == "Form #3"
        assert tc_rules.surface_name({}, {}, 2) == "Form #2"

    def test_first_step_is_a_breadcrumb_to_the_surface(self, cases):
        for c in cases:
            assert c.steps[0].startswith("Go to https://shop.test/order")


# ── House-style compliance ───────────────────────────────────────────

class TestHouseStyle:
    def test_every_case_passes_the_linter(self, cases):
        offenders = [(c.summary[:60], lint_case(c)) for c in cases
                     if lint_case(c)]
        assert not offenders, offenders

    def test_every_case_quotes_a_real_control_or_names_the_surface(self,
                                                                  cases):
        for c in cases:
            assert '"' in c.summary or "Customer order" in c.summary, c.summary

    def test_negatives_assert_the_feedback_half(self, cases):
        from engine.tc_author import asserts_feedback
        for c in cases:
            if c.category == "Negative":
                assert asserts_feedback(c.expected_result), c.summary

    def test_no_weak_modals_anywhere(self, cases):
        from engine.tc_author import has_weak_modal
        for c in cases:
            assert not has_weak_modal(c.expected_result)
            assert not has_weak_modal(c.summary)

    def test_both_polarities_are_produced(self, cases):
        cats = {c.category for c in cases}
        assert cats == {"Positive", "Negative"}

    def test_required_controls_get_high_priority(self, cases):
        for c in _for(cases, "Customer name"):
            assert c.priority == "High"
        for c in _for(cases, "Telephone"):
            assert c.priority == "Medium"

    def test_valid_value_data_is_not_the_boundary_value(self, cases):
        """A happy-path value must not be described as a 60-char string —
        that conflates the valid case with the boundary case."""
        valid = [c for c in _for(cases, "Customer name")
                 if "enters a valid value" in c.summary.lower()]
        assert valid
        assert "60-character" not in valid[0].test_data


# ── Form-level cases ─────────────────────────────────────────────────

class TestFormLevelCases:
    def test_happy_path_required_sweep_and_marker(self, cases):
        blob = " ".join(c.summary for c in cases)
        assert "with the required controls filled" in blob
        assert "with the required controls left empty" in blob
        assert "are marked before submission" in blob

    def test_required_sweep_is_skipped_when_nothing_is_required(self):
        html = ('<html><body><h1>Search</h1><form>'
                '<input type="text" name="q"><button>Go</button>'
                '</form></body></html>')
        cases = tc_rules.enumerate_from_pages(_pages(html))
        blob = " ".join(c.summary for c in cases)
        assert "with the required controls left empty" not in blob
        assert "with the required controls filled" in blob

    def test_submit_button_label_is_quoted_in_the_step(self, cases):
        submits = [s for c in cases for s in c.steps if "Submit order" in s]
        assert submits


# ── Robustness ───────────────────────────────────────────────────────

class TestRobustness:
    def test_no_forms_yields_nothing_rather_than_invention(self):
        html = "<html><body><h1>Just prose</h1><p>No form here.</p></body></html>"
        assert tc_rules.enumerate_from_pages(_pages(html)) == []
        assert tc_rules.enumerate_from_pages([]) == []

    def test_form_with_only_buttons_is_skipped(self):
        html = ('<html><body><form><button>Only me</button></form>'
                "</body></html>")
        assert tc_rules.enumerate_from_pages(_pages(html)) == []

    def test_output_is_reproducible(self):
        a = tc_rules.enumerate_from_pages(_pages(ORDER_FORM))
        b = tc_rules.enumerate_from_pages(_pages(ORDER_FORM))
        assert [c.to_dict() for c in a] == [c.to_dict() for c in b]

    def test_per_form_cap_is_enforced(self, monkeypatch):
        monkeypatch.setattr(tc_rules, "MAX_CASES_PER_FORM", 6)
        fields = "".join(
            f'<input type="text" name="f{i}" required maxlength="10">'
            for i in range(40))
        html = f'<html><body><h1>Big</h1><form>{fields}<button>S</button></form></body></html>'
        cases = tc_rules.enumerate_from_pages(_pages(html))
        assert len(cases) <= 6

    def test_form_cap_is_enforced(self, monkeypatch):
        monkeypatch.setattr(tc_rules, "MAX_FORMS", 2)
        one = '<form><input type="text" name="a" required><button>S</button></form>'
        html = f"<html><body><h1>Many</h1>{one * 6}</body></html>"
        cases = tc_rules.enumerate_from_pages(_pages(html))
        assert len({c.section for c in cases}) <= 2

    def test_unusable_rules_asset_degrades_quietly(self, monkeypatch):
        monkeypatch.setattr(tc_rules, "load_rules", lambda: {})
        assert tc_rules.enumerate_from_pages(_pages(ORDER_FORM)) == []

    def test_artifacts_wrapper_matches_the_pages_call(self):
        pages = _pages(ORDER_FORM)
        direct = tc_rules.enumerate_from_pages(pages)
        wrapped = tc_rules.enumerate_from_artifacts(Artifacts(pages=pages))
        assert [c.to_dict() for c in direct] == [c.to_dict() for c in wrapped]


# ── Integration with the generator ───────────────────────────────────

class TestGeneratorUsesTheRuleEngine:
    def test_no_api_key_still_produces_control_level_coverage(self,
                                                              monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from engine.testcase_generator import generate_from_artifacts

        tcs = generate_from_artifacts(Artifacts(pages=_pages(ORDER_FORM)))
        assert len(tcs) > 10, "the free-tier path must still enumerate"
        assert {t.section for t in tcs} == {"Customer order"}
        # Authored section numbers, so IDs cannot collide with the
        # strategy-category range.
        assert all(int(t.id[2:].split("_")[0]) >= 10 for t in tcs)
        assert {t.category for t in tcs} == {"Positive", "Negative"}

    def test_prompt_only_input_still_yields_nothing_without_a_key(self,
                                                                 monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from engine.testcase_generator import generate_from_artifacts
        # No inventory to enumerate and no model to reason: the legacy
        # knowledge-base stream owns this shape.
        assert generate_from_artifacts(
            Artifacts(custom_prompt="Write cases for the HR module")) == []

    def test_strategy_path_prefers_the_inventory_over_1to1_expansion(
            self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from engine import site_recon as _recon, test_strategy as _strat
        from engine.testcase_generator import generate_from_strategy

        profile = _recon.SiteProfile(url="https://shop.test", has_forms=True)
        strategy = _strat._rule_based_strategy(profile)
        tcs, cls = generate_from_strategy(
            profile, strategy,
            artifacts=Artifacts(pages=_pages(ORDER_FORM)))
        # Sections come from the surface, not from strategy categories.
        assert {t.section for t in tcs} == {"Customer order"}
        # The checklist still comes from the Medium/Low strategy checks.
        assert cls


# ── Real-world markup ────────────────────────────────────────────────

#: The actual httpbin.org/forms/post page — no <h1>, no <title>, labels
#: that wrap their control, colons in the visible text, and a radio
#: group. Ran against prod on 2026-07-31 and exposed three defects a
#: synthetic form had not: "Form #1" as a section name, labels quoted as
#: "Customer name:", and three identical cases for the one radio group.
HTTPBIN_FORM = """
<html><body><form method="post" action="/post">
 <p><label>Customer name: <input name="custname"></label></p>
 <p><label>Telephone: <input type="tel" name="custtel"></label></p>
 <p><label>E-mail address: <input type="email" name="custemail"></label></p>
 <fieldset><legend>Pizza Size</legend>
  <p><label><input type="radio" name="size" value="small"> Small</label></p>
  <p><label><input type="radio" name="size" value="medium"> Medium</label></p>
  <p><label><input type="radio" name="size" value="large"> Large</label></p>
 </fieldset>
 <p><label>Delivery time: <input type="time" min="11:00" max="21:00" name="delivery"></label></p>
 <p><label>Delivery instructions: <textarea name="comments"></textarea></label></p>
 <p><button>Submit order</button></p>
</form></body></html>
"""


@pytest.fixture(scope="module")
def httpbin_cases() -> list:
    return tc_rules.enumerate_from_pages(
        _pages(HTTPBIN_FORM, url="https://httpbin.org/forms/post"))


class TestRealWorldMarkup:
    def test_labels_lose_their_trailing_punctuation(self, httpbin_cases):
        blob = " ".join(c.summary for c in httpbin_cases)
        assert '"Customer name"' in blob
        assert '"Customer name:"' not in blob, (
            "a quoted colon reads as part of the control name")

    def test_radio_group_becomes_one_choice_control(self, httpbin_cases):
        size = [c for c in httpbin_cases if '"size"' in c.summary]
        assert size, "the radio group produced no cases"
        assert len(size) == len({c.summary for c in size}), (
            "three radio members must not yield three identical cases")
        assert all("drop-down" in c.summary for c in size), (
            "a radio group is a choice control, not N checkboxes")

    def test_radio_options_come_from_the_member_labels(self, httpbin_cases):
        pick = next(c for c in httpbin_cases
                    if "selects an existing value" in c.summary.lower())
        for opt in ("Small", "Medium", "Large"):
            assert opt in pick.test_data, pick.test_data

    def test_label_text_after_the_control_is_captured(self):
        """<label><input> Small</label> puts the name after the control."""
        p = _PageParser()
        p.feed('<form><label><input type="radio" name="s" value="v"> '
               "Visible</label></form>")
        assert p.forms[0]["fields"][0]["label"] == "Visible"

    def test_option_text_still_does_not_bleed_into_the_label(self):
        # The reason label collection was restricted in the first place.
        p = _PageParser()
        p.feed('<form><label>Size <select name="s">'
               "<option>Small</option><option>Large</option>"
               "</select></label></form>")
        assert p.forms[0]["fields"][0]["label"] == "Size"

    def test_section_falls_back_to_the_url_not_an_ordinal(self,
                                                         httpbin_cases):
        sections = {c.section for c in httpbin_cases}
        assert sections == {"Forms post"}, sections
        assert "Form #" not in " ".join(sections)

    def test_no_redundant_form_suffix(self):
        page = {"url": "https://x.test/forms/post"}
        assert tc_rules.surface_name(page, {}, 1) == "Forms post"
        page = {"url": "https://x.test/checkout"}
        assert tc_rules.surface_name(page, {}, 1) == "Checkout form"

    def test_time_input_with_min_max_gets_the_range_negative(self,
                                                            httpbin_cases):
        delivery = [c for c in httpbin_cases if '"Delivery time"' in c.summary]
        assert any("outside the allowed range" in c.summary
                   for c in delivery), (
            'min="11:00" max="21:00" justifies the out-of-range case')

    def test_still_house_style_clean_on_real_markup(self, httpbin_cases):
        offenders = [(c.summary[:60], lint_case(c)) for c in httpbin_cases
                     if lint_case(c)]
        assert not offenders, offenders


class TestRadioGrouping:
    def test_non_radio_fields_pass_through_untouched(self):
        fields = [{"type": "text", "name": "a"}, {"type": "email", "name": "b"}]
        assert tc_rules.group_radios(fields) == fields

    def test_groups_are_keyed_by_name(self):
        fields = [
            {"type": "radio", "name": "x", "label": "One"},
            {"type": "radio", "name": "x", "label": "Two"},
            {"type": "radio", "name": "y", "label": "Other"},
        ]
        out = tc_rules.group_radios(fields)
        assert len(out) == 2
        assert out[0]["options"] == ["One", "Two"]
        assert out[1]["options"] == ["Other"]

    def test_required_on_any_member_marks_the_group(self):
        fields = [
            {"type": "radio", "name": "x", "value": "a"},
            {"type": "radio", "name": "x", "value": "b", "required": True},
        ]
        assert tc_rules.group_radios(fields)[0]["required"] is True

    def test_group_is_named_after_the_field_not_a_member(self):
        fields = [{"type": "radio", "name": "size", "label": "Large"}]
        assert tc_rules.group_radios(fields)[0]["label"] == "size"

    def test_value_is_used_when_a_member_has_no_label(self):
        fields = [{"type": "radio", "name": "x", "value": "only"}]
        assert tc_rules.group_radios(fields)[0]["options"] == ["only"]
