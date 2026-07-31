"""Dual-format test cases — manual columns plus a derived BDD view.

Covers ``engine.gherkin``: the manual → Given/When/Then translation, the
parser, the structural linter, tag generation, and the format flag; plus
the DB round-trip, the ``.feature`` export and the render path.

Design under test: the manual columns stay the source of truth and the
Gherkin is derived at read time. Storing both would let them drift, with
the stale copy being what the runner executes — so ``TestCase.gherkin``
holds only text an operator hand-edited, and everything else comes from
:func:`engine.gherkin.ensure_gherkin`.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from engine import gherkin as gk
from engine.testcase_generator import TestCase


def _seed(client, cases: list) -> None:
    """Put test cases in the session so a GET can see them.

    ``_session_active_since`` is not decoration: app.py wipes every
    generated key when the session predates this server start, so a
    session seeded without it arrives at the route empty — which reads as
    "the export is broken" rather than "the fixture is".
    """
    from routes._shared import tc_to_dict, SERVER_START_TIME
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["test_cases_data"] = [tc_to_dict(c) for c in cases]


def _tc(**kw) -> TestCase:
    base = dict(
        id="SC1_001", section="Careers page", section_num=1,
        summary='Verify that User cannot submit the form with invalid data '
                'in the "Phone number" field',
        preconditions='The "Didn\'t find a suitable position?" form is opened '
                      'on the https://example.com/careers page',
        test_steps=(
            "1. Go to the site: https://example.com/careers\n"
            '2. Fill in the "Phone number" field with invalid data\n'
            '3. Mark the "I agree to the Cookie Policy" checkbox\n'
            "4. Click the [Find your role] button\n"
            "5. Pay attention to the result"
        ),
        test_data="Name: Test Testovko, Phone number: (32) 4512",
        expected_result=(
            "1. The entered data should not be accepted "
            '2. The "Phone number" field should be highlighted in red'
        ),
        category="Negative", priority="High",
    )
    base.update(kw)
    return TestCase(**base)


# ── Translation ──────────────────────────────────────────────────────

class TestScenarioFromTestCase:
    def test_preconditions_become_given(self):
        sc = gk.scenario_from_test_case(_tc())
        given = [s for s in sc.steps if s.keyword == "Given"]
        assert len(given) == 1
        assert given[0].text.startswith('the "Didn\'t find a suitable')

    def test_state_precondition_keeps_its_subject(self):
        # "The form is opened" must not become "I the form is opened".
        sc = gk.scenario_from_test_case(_tc())
        assert not any(s.text.startswith("I the") for s in sc.steps)

    def test_navigation_step_lands_in_the_given_block(self):
        sc = gk.scenario_from_test_case(_tc())
        # Given, then And for the navigation — before the first When.
        kinds = [s.keyword for s in sc.steps]
        first_when = kinds.index("When")
        nav = next(i for i, s in enumerate(sc.steps)
                   if "go to the site" in s.text)
        assert nav < first_when

    def test_action_steps_become_when_and_and(self):
        sc = gk.scenario_from_test_case(_tc())
        actions = [s for s in sc.steps
                   if s.keyword in ("When", "And")
                   and s.text.startswith("I ")
                   and "go to the site" not in s.text
                   and "test data" not in s.text]
        assert len(actions) == 3
        assert actions[0].keyword == "When"
        assert all(a.keyword == "And" for a in actions[1:])

    def test_imperative_step_becomes_first_person(self):
        sc = gk.scenario_from_test_case(_tc())
        assert any(s.text == 'I click the [Find your role] button'
                   for s in sc.steps)

    def test_numbered_expected_result_splits_into_then_and(self):
        sc = gk.scenario_from_test_case(_tc())
        thens = [s for s in sc.steps if s.keyword in ("Then",)]
        alls = [s for s in sc.steps if s.keyword in ("Then", "And")]
        assert len(thens) == 1
        assert any("highlighted in red" in s.text for s in alls)

    def test_observation_step_is_dropped_but_recorded(self):
        sc = gk.scenario_from_test_case(_tc())
        # A step definition cannot act on "Pay attention to …" — it is an
        # assertion in imperative clothing, already covered by Then.
        assert not any("pay attention" in s.text.lower() for s in sc.steps)
        assert any("observation step dropped" in n for n in sc.notes)

    def test_display_only_case_still_gets_a_when(self):
        # Gherkin needs a When for the runner to hang a trigger on.
        sc = gk.scenario_from_test_case(_tc(
            test_steps="1. Pay attention to the Footer",
            preconditions="", test_data="",
            expected_result="The Footer is displayed"))
        assert [s.keyword for s in sc.steps] == ["When", "Then"]

    def test_scenario_name_is_the_summary(self):
        sc = gk.scenario_from_test_case(_tc())
        assert sc.name == _tc().summary


class TestTestData:
    def test_field_value_pairs_become_a_data_table(self):
        sc = gk.scenario_from_test_case(_tc())
        step = next(s for s in sc.steps if s.table)
        assert step.table[0] == ["field", "value"]
        assert ["Phone number", "(32) 4512"] in step.table

    def test_prose_test_data_falls_back_to_a_doc_string(self):
        # Forcing prose into two columns would invent structure.
        sc = gk.scenario_from_test_case(_tc(
            test_data="Any account with more than one open order"))
        step = next(s for s in sc.steps if s.doc_string or s.table)
        assert step.doc_string
        assert not step.table

    def test_empty_test_data_adds_no_step(self):
        sc = gk.scenario_from_test_case(_tc(test_data=""))
        assert not any("test data" in s.text for s in sc.steps)


class TestTags:
    def test_case_id_keeps_its_case(self):
        # Cucumber tags are case-sensitive and the id is cited in bug
        # reports, so @TC-SC1_001 has to stay recognisable.
        assert "@TC-SC1_001" in gk.tags_for(_tc())

    def test_category_priority_and_type_are_lowercased(self):
        tags = gk.tags_for(_tc())
        assert "@negative" in tags and "@high" in tags
        assert "@functional" in tags

    def test_suite_becomes_a_tag_when_set(self):
        tc = _tc()
        tc.suite = "Regression"
        assert "@regression" in gk.tags_for(tc)

    def test_empty_attributes_contribute_nothing(self):
        tc = _tc(category="", priority="")
        tc.testing_type = ""
        assert gk.tags_for(tc) == ["@TC-SC1_001"]

    def test_whitespace_in_a_value_cannot_produce_two_tags(self):
        tc = _tc(category="Edge Case")
        tags = gk.tags_for(tc)
        assert "@edge-case" in tags
        assert all(" " not in t for t in tags)


# ── Rendering and parsing ────────────────────────────────────────────

class TestRender:
    def test_feature_groups_scenarios_by_section(self):
        features = gk.features_from_test_cases([
            _tc(id="A", section="Header"),
            _tc(id="B", section="Footer"),
            _tc(id="C", section="Header"),
        ])
        assert [f.name for f in features] == ["Header", "Footer"]
        assert len(features[0].scenarios) == 2

    def test_rendered_feature_is_valid(self):
        text = gk.feature_from_test_cases([_tc()]).render()
        assert text.startswith("Feature: Careers page")
        assert gk.lint(text) == []

    def test_notes_render_after_the_steps(self):
        text = gk.feature_from_test_cases([_tc()]).render()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        comment_ix = next(i for i, l in enumerate(lines)
                          if l.startswith("#"))
        last_step_ix = max(i for i, l in enumerate(lines)
                           if l.split(" ")[0] in gk.KEYWORDS)
        assert comment_ix > last_step_ix

    def test_filename_is_a_slug(self):
        assert gk.feature_filename("Careers page") == "careers-page.feature"
        assert gk.feature_filename("") == "test-cases.feature"
        assert gk.feature_filename("Grid / List view") == \
            "grid-list-view.feature"


class TestRoundTrip:
    def test_render_then_parse_preserves_the_structure(self):
        original = gk.feature_from_test_cases([_tc(), _tc(id="SC1_002")])
        parsed = gk.parse(original.render())
        assert parsed is not None
        assert parsed.name == original.name
        assert len(parsed.scenarios) == 2
        for a, b in zip(original.scenarios, parsed.scenarios):
            assert a.name == b.name
            assert a.tags == b.tags
            assert [(s.keyword, s.text) for s in a.steps] == \
                   [(s.keyword, s.text) for s in b.steps]

    def test_data_table_survives_the_round_trip(self):
        parsed = gk.parse(gk.feature_from_test_cases([_tc()]).render())
        tables = [s.table for s in parsed.scenarios[0].steps if s.table]
        assert tables and ["Phone number", "(32) 4512"] in tables[0]

    def test_doc_string_survives_the_round_trip(self):
        tc = _tc(test_data="Any account with more than one open order")
        parsed = gk.parse(gk.feature_from_test_cases([tc]).render())
        docs = [s.doc_string for s in parsed.scenarios[0].steps
                if s.doc_string]
        assert docs == ["Any account with more than one open order"]

    def test_comments_round_trip_as_notes(self):
        parsed = gk.parse(gk.feature_from_test_cases([_tc()]).render())
        assert any("observation step dropped" in n
                   for n in parsed.scenarios[0].notes)

    def test_parse_returns_none_on_empty_input(self):
        assert gk.parse("") is None
        assert gk.parse("   \n\n ") is None


# ── Linter ───────────────────────────────────────────────────────────

class TestLint:
    def test_valid_feature_has_no_findings(self):
        assert gk.lint("Feature: X\n\n  Scenario: Y\n"
                       "    When I click\n    Then it works") == []
        assert gk.is_valid("Feature: X\n\n  Scenario: Y\n"
                           "    When I click\n    Then it works")

    def test_missing_feature_line(self):
        assert any("no Feature:" in i
                   for i in gk.lint("Scenario: Y\n  When I click"))

    def test_missing_when_or_then_is_named(self):
        no_when = gk.lint("Feature: X\n\n  Scenario: Y\n    Then it works")
        assert any("no When step" in i for i in no_when)
        no_then = gk.lint("Feature: X\n\n  Scenario: Y\n    When I click")
        assert any("no Then step" in i for i in no_then)

    def test_scenario_opening_with_and_is_flagged(self):
        issues = gk.lint("Feature: X\n\n  Scenario: Y\n"
                         "    And I click\n    Then it works")
        assert any("opens with" in i for i in issues)

    def test_feature_with_no_scenario_is_flagged(self):
        assert any("no Scenario" in i for i in gk.lint("Feature: X"))

    def test_ragged_data_table_is_flagged(self):
        issues = gk.lint("Feature: X\n\n  Scenario: Y\n"
                         "    When I use data:\n"
                         "      | a | b |\n"
                         "      | c |\n"
                         "    Then it works")
        assert any("different column counts" in i for i in issues)

    @pytest.mark.parametrize("keyword", ["Scenario Outline:", "Background:",
                                         "Rule:", "Examples:"])
    def test_unsupported_constructs_are_reported_not_dropped(self, keyword):
        # Silently ignoring these would read as "the test did not run".
        text = f"Feature: X\n\n  {keyword} Y\n    When I click\n    Then ok"
        assert any("not supported" in i for i in gk.lint(text))

    def test_empty_text_is_a_finding(self):
        assert gk.lint("") == ["empty feature text"]


# ── Format flag ──────────────────────────────────────────────────────

class TestFormatFlag:
    @pytest.mark.parametrize("raw,want", [
        ("manual", "manual"), ("", "manual"), (None, "manual"),
        ("junk", "manual"), ("Manual", "manual"),
        ("gherkin", "gherkin"), ("BDD", "gherkin"),
        ("feature", "gherkin"), ("automation", "gherkin"),
    ])
    def test_coerce_format(self, raw, want):
        assert gk.coerce_format(raw) == want

    def test_default_is_manual(self):
        assert _tc().tc_format == "manual"
        assert gk.DEFAULT_FORMAT == "manual"

    def test_apply_format_stamps_only_the_flag(self):
        # gherkin stays empty so the column keeps its meaning: "an
        # operator hand-edited this".
        out = gk.apply_format([{"id": "A"}, {"id": "B"}], "bdd")
        assert [d["tc_format"] for d in out] == ["gherkin", "gherkin"]
        assert all("gherkin" not in d or not d["gherkin"] for d in out)

    def test_apply_format_does_not_mutate_the_input(self):
        src = [{"id": "A"}]
        gk.apply_format(src, "gherkin")
        assert "tc_format" not in src[0]

    def test_is_automation_targeted(self):
        assert not gk.is_automation_targeted(_tc())
        assert gk.is_automation_targeted(_tc(tc_format="gherkin"))


class TestEnsureGherkin:
    def test_derives_when_nothing_is_stored(self):
        text = gk.ensure_gherkin(_tc())
        assert "Scenario:" in text and "Then" in text

    def test_hand_edited_text_wins(self):
        tc = _tc(gherkin="Scenario: hand written\n  When I do\n  Then ok\n")
        assert gk.ensure_gherkin(tc) == tc.gherkin

    def test_derivation_follows_an_edit_to_the_manual_columns(self):
        # The point of deriving rather than storing: change the case and
        # the BDD view changes with it.
        before = gk.ensure_gherkin(_tc())
        after = gk.ensure_gherkin(_tc(
            test_steps="1. Go to the site: https://example.com/\n"
                       '2. Click the [Apply] button'))
        assert before != after
        assert "I click the [Apply] button" in after


# ── DB round-trip ────────────────────────────────────────────────────

class TestPersistence:
    def test_format_survives_a_save_load_cycle(self, tmp_path, monkeypatch):
        import engine.db as db
        monkeypatch.setenv("FLASK_DEBUG", "1")
        monkeypatch.setenv("DATABASE_URL",
                           f"sqlite:///{tmp_path / 'tfg.db'}")
        monkeypatch.setattr(db, "_engine", None, raising=False)
        db.init_db()
        pid = db.upsert_project("gherkin-round-trip")

        from routes._shared import tc_to_dict
        rows = [tc_to_dict(_tc(id="SC1_001", tc_format="gherkin")),
                tc_to_dict(_tc(id="SC1_002"))]
        rows[0]["gherkin"] = "Scenario: hand written\n  When I do\n"
        assert db.save_test_cases(pid, rows) == 2

        loaded = {d["id"]: d for d in db.load_test_cases(pid)}
        assert loaded["SC1_001"]["tc_format"] == "gherkin"
        assert loaded["SC1_001"]["gherkin"].startswith("Scenario: hand")
        assert loaded["SC1_002"]["tc_format"] == "manual"
        assert loaded["SC1_002"]["gherkin"] == ""

    def test_a_junk_format_value_is_coerced_on_write(self, tmp_path,
                                                    monkeypatch):
        import engine.db as db
        monkeypatch.setenv("FLASK_DEBUG", "1")
        monkeypatch.setenv("DATABASE_URL",
                           f"sqlite:///{tmp_path / 'tfg2.db'}")
        monkeypatch.setattr(db, "_engine", None, raising=False)
        db.init_db()
        pid = db.upsert_project("coerce")
        from routes._shared import tc_to_dict
        row = tc_to_dict(_tc())
        row["tc_format"] = "something-else"
        db.save_test_cases(pid, [row])
        assert db.load_test_cases(pid)[0]["tc_format"] == "manual"

    def test_tc_to_dict_carries_the_new_fields(self):
        from routes._shared import tc_to_dict
        d = tc_to_dict(_tc(tc_format="gherkin"))
        assert d["tc_format"] == "gherkin"
        assert d["gherkin"] == ""
        # Regression: these were dropped by tc_to_dict before PR-3, so a
        # generation round-trip through the session lost them.
        assert "suite" in d and "automation_steps_json" in d


# ── Export ───────────────────────────────────────────────────────────

class TestFeatureExport:
    def _zip(self, client):
        resp = client.get("/export/feature")
        return resp

    def test_manual_only_pack_refuses_with_an_explanation(self, client):
        _seed(client, [_tc()])
        resp = self._zip(client)
        assert resp.status_code == 409
        assert b"No automation-targeted" in resp.data

    def test_archive_carries_one_feature_per_section(self, client):
        _seed(client, [_tc(id="A", section="Header", tc_format="gherkin"),
                       _tc(id="B", section="Footer", tc_format="gherkin")])
        resp = self._zip(client)
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            names = set(zf.namelist())
            assert "features/header.feature" in names
            assert "features/footer.feature" in names
            assert "README.md" in names
            body = zf.read("features/header.feature").decode("utf-8")
            assert body.startswith("Feature: Header")
            assert gk.lint(body) == []

    def test_manual_cases_are_left_out_of_the_archive(self, client):
        _seed(client, [_tc(id="A", section="Header", tc_format="gherkin"),
                       _tc(id="B", section="Manual only")])
        with zipfile.ZipFile(io.BytesIO(self._zip(client).data)) as zf:
            assert not any("manual-only" in n for n in zf.namelist())

    def test_readme_names_the_derivation_contract(self, client):
        _seed(client, [_tc(tc_format="gherkin")])
        with zipfile.ZipFile(io.BytesIO(self._zip(client).data)) as zf:
            readme = zf.read("README.md").decode("utf-8")
        assert "source of truth" in readme


# ── Render path ──────────────────────────────────────────────────────

class TestRenderPath:
    def test_bdd_pane_appears_only_for_targeted_cases(self, client):
        _seed(client, [_tc(tc_format="gherkin")])
        body = client.get("/test-cases").get_data(as_text=True)
        assert "BDD view" in body
        assert "gherkin-block" in body

        _seed(client, [_tc()])
        body = client.get("/test-cases").get_data(as_text=True)
        assert "BDD view" not in body

    def test_format_knob_is_offered_on_test_cases(self, client):
        body = client.get("/test-cases").get_data(as_text=True)
        assert 'name="tc_format"' in body
        assert 'value="gherkin"' in body

    def test_checklist_offers_no_format_knob(self, client):
        # A checklist row is one observation with no steps to bind.
        body = client.get("/checklist").get_data(as_text=True)
        assert 'name="tc_format"' not in body

    def test_feature_export_button_only_shows_when_useful(self, client):
        from routes._shared import tc_to_dict
        with client.session_transaction() as sess:
            sess["test_cases_data"] = [tc_to_dict(_tc())]
        assert "/export/feature" not in \
            client.get("/test-cases").get_data(as_text=True)
        with client.session_transaction() as sess:
            sess["test_cases_data"] = [tc_to_dict(_tc(tc_format="gherkin"))]
        assert "/export/feature" in \
            client.get("/test-cases").get_data(as_text=True)
