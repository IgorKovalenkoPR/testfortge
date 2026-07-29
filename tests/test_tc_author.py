"""Test Case Author agent + house-style enforcement.

Covers:
  * engine.tc_author — style assets, control inventory, lint/normalise,
    declarative-voice rewriting, deterministic expansion, and the LLM
    path with a stubbed ``call_messages``.
  * engine.qa_team_lead — the corpus-derived review rules (declarative
    expected result, negative feedback assertion, generic-step ban).
  * engine.testcase_generator.generate_from_strategy — authored path
    versus deterministic fallback.

The house style under test is measured from a real 4,808-case QA-team
deliverable (the Odoo Test Plan); see
``engine/qa_knowledge/style/house_style.yaml``.
"""
from __future__ import annotations

import json

import pytest

from engine import site_recon as _recon
from engine import tc_author
from engine import test_strategy as _strat
from engine.testcase_generator import generate_from_strategy


# ── Style assets ─────────────────────────────────────────────────────

class TestStyleAssets:
    def test_house_style_loads_and_carries_the_key_rules(self):
        text = tc_author.house_style_text()
        assert text, "house_style.yaml must ship with the package"
        assert "summary_grammar" in text
        assert "expected_result" in text
        assert "anti_patterns" in text
        # The measured evidence is what stops the model treating a house
        # convention as negotiable — it must survive into the prompt.
        assert "4,808" in text

    def test_coverage_rules_load_and_carry_the_pairing_rules(self):
        text = tc_author.coverage_rules_text()
        assert text
        for key in ("list_surface", "create_form", "state_machine",
                    "permissions", "error_messages", "must_pair"):
            assert key in text, key

    def test_assets_are_cached(self):
        # Same object identity via lru_cache — the prompt is rebuilt on
        # every generation, so re-reading from disk each time would be
        # pure waste.
        assert tc_author.house_style_text() is tc_author.house_style_text()

    def test_system_blocks_mark_every_static_part_cacheable(self):
        blocks = tc_author._system_blocks("ISTQB chunk")
        assert len(blocks) >= 3
        assert all(b.get("cache_control") == {"type": "ephemeral"}
                   for b in blocks)


# ── Control inventory ────────────────────────────────────────────────

class TestControlInventory:
    def test_renders_labels_types_and_constraints(self):
        pages = [{
            "url": "https://x.test/signup",
            "title": "Sign up",
            "h1": "Create your account",
            "headings": ["Account", "Billing"],
            "nav_links": ["Home", "Pricing"],
            "buttons": ["Create account", "Cancel"],
            "forms": [{
                "method": "post",
                "action": "/signup",
                "fields": [
                    {"name": "email", "type": "email", "required": True,
                     "maxlength": 254},
                    {"label": "Country", "type": "select",
                     "options": ["Ukraine", "Poland"]},
                ],
            }],
        }]
        out = tc_author.build_control_inventory(pages)
        assert "https://x.test/signup" in out
        assert '"Create account"' in out
        assert '"email" (type=email, required, maxlength=254)' in out
        assert "Ukraine / Poland" in out
        assert "form #1 (POST" in out

    def test_empty_pages_render_nothing(self):
        assert tc_author.build_control_inventory([]) == ""

    def test_page_cap_is_reported_not_silent(self):
        pages = [{"url": f"https://x.test/{i}"} for i in range(15)]
        out = tc_author.build_control_inventory(pages, max_pages=5)
        assert "+10 further surfaces" in out


# ── Declarative-voice rewriting ──────────────────────────────────────

class TestDeclarativeVoice:
    @pytest.mark.parametrize("src,want", [
        ("User should be authenticated", "User is authenticated"),
        ("Results should be displayed", "Results are displayed"),
        ("Form should not be submitted", "Form is not submitted"),
        ("The record must be created", "The record is created"),
        ("The banner shall be visible", "The banner is visible"),
        ("The server should reject the payload",
         "The server rejects the payload"),
        ("No errors should occur", "No errors occur"),
        ("Images should have alt text", "Images have alt text"),
        ("The message should identify the field",
         "The message identifies the field"),
        ("The row should not persist", "The row does not persist"),
    ])
    def test_rewrites(self, src, want):
        assert tc_author.normalise_expected_result(src) == want

    def test_declarative_text_is_left_alone(self):
        src = ("User cannot create Job Position without required field "
               "filling. A warning is displayed.")
        assert tc_author.normalise_expected_result(src) == src

    def test_multi_sentence_agreement_is_per_clause(self):
        src = ("The record should be saved. Validation errors should not "
               "be displayed.")
        out = tc_author.normalise_expected_result(src)
        assert out == ("The record is saved. Validation errors are not "
                       "displayed.")

    def test_no_weak_modal_survives_the_rewrite(self):
        for src in ("X should be Y", "X must occur", "X shall not be Z",
                    "X is expected to be Y", "X ought to be Y"):
            assert not tc_author.has_weak_modal(
                tc_author.normalise_expected_result(src)), src

    def test_third_person_singular_orthography(self):
        f = tc_author._third_person_singular
        assert f("match") == "matches"
        assert f("pass") == "passes"
        assert f("carry") == "carries"
        assert f("stay") == "stays"
        assert f("go") == "goes"
        assert f("have") == "has"
        assert f("round-trip") == "round-trips"


# ── Lint / normalise ─────────────────────────────────────────────────

def _case(**kw) -> tc_author.AuthoredCase:
    base = dict(
        summary='Verify that User can save the record via the "Save" button',
        preconditions="Record is created",
        steps=["Go to HR module -> Employees grid",
               'Click on the "Save" button'],
        test_data="",
        expected_result='User can save the record via the "Save" button.',
        category="Positive",
        priority="High",
        section="Employees grid",
    )
    base.update(kw)
    return tc_author.AuthoredCase(**base)


class TestLint:
    def test_compliant_case_has_no_findings(self):
        assert tc_author.lint_case(_case()) == []

    def test_flags_missing_verify_that_opener(self):
        findings = tc_author.lint_case(_case(summary="Check the save button"))
        assert any("Verify that" in f for f in findings)

    def test_accepts_the_error_message_title_grammar(self):
        # The corpus's dedicated error-message sweep uses
        # "<Surface>: <attempted action>" instead of "Verify that ...".
        case = _case(
            summary="Employee Create page: Trying to proceed with all the "
                    "required fields empty",
            category="Negative",
            expected_result="All the empty required fields are highlighted, "
                            "and the error message names them.",
        )
        assert tc_author.lint_case(case) == []

    def test_flags_generic_placeholder_steps(self):
        case = _case(steps=["Navigate to the matching page",
                            "Perform the action described in the objective",
                            "Observe the result"])
        findings = tc_author.lint_case(case)
        assert sum("generic placeholder" in f for f in findings) == 3

    def test_flags_weak_modal_in_expected_result(self):
        findings = tc_author.lint_case(
            _case(expected_result="The record should be saved"))
        assert any("weak modal" in f for f in findings)

    def test_flags_negative_case_without_feedback_assertion(self):
        case = _case(category="Negative",
                     expected_result="The record is not created.")
        # "is not created" IS a feedback token (nothing persisted), so
        # this one passes; the bare refusal below does not.
        assert tc_author.lint_case(case) == []
        bare = _case(category="Negative",
                     expected_result="User cannot save the record.")
        assert any("feedback" in f for f in tc_author.lint_case(bare))

    def test_flags_single_step_case(self):
        findings = tc_author.lint_case(_case(steps=["Open the page"]))
        assert any("fewer than 2 steps" in f for f in findings)


class TestNormalise:
    def test_fixes_opener_modal_and_step_numbering(self):
        case = _case(
            summary="check that the record should be saved",
            steps=["1. Go to the grid", "2) Click Save"],
            expected_result="The record should be saved",
        )
        fixed, residual = tc_author.normalise_case(case)
        assert fixed.summary.startswith("Verify that ")
        assert not tc_author.has_weak_modal(fixed.summary)
        assert fixed.steps == ["Go to the grid", "Click Save"]
        assert fixed.expected_result == "The record is saved"
        assert residual == []

    def test_drops_generic_steps(self):
        case = _case(steps=["Go to HR module -> Employees grid",
                            "Perform the action",
                            'Click on the "Save" button'])
        fixed, _ = tc_author.normalise_case(case)
        assert fixed.steps == ["Go to HR module -> Employees grid",
                               'Click on the "Save" button']

    def test_appends_the_missing_negative_feedback_half(self):
        case = _case(category="Negative",
                     expected_result="User cannot save the record")
        fixed, residual = tc_author.normalise_case(case)
        assert tc_author.asserts_feedback(fixed.expected_result)
        assert residual == []

    def test_folds_legacy_categories_onto_polarity(self):
        for legacy in ("Edge Case", "Security", "Boundary"):
            fixed, _ = tc_author.normalise_case(_case(
                category=legacy,
                expected_result="The value is rejected and a warning is "
                                "displayed."))
            assert fixed.category == "Negative", legacy


# ── Deterministic expansion ──────────────────────────────────────────

class TestExpandCheck:
    def test_builds_a_real_body_not_a_placeholder(self):
        check = _strat.CheckSpec(
            objective="Verify successful login routes to the dashboard",
            rationale="Baseline auth path",
            priority="High",
            url_pattern="/login*",
            istqb_technique="Use Case",
        )
        profile = _recon.SiteProfile(url="https://x.test", site_type="saas")
        case = tc_author.expand_check(check, profile=profile,
                                      section="Functional")
        assert case.summary.startswith("Verify that")
        # Step 1 is navigation and names the real host + area.
        assert case.steps[0] == "Go to https://x.test -> /login"
        assert len(case.steps) >= 3
        assert not any(tc_author.is_generic_step(s) for s in case.steps)
        assert not tc_author.has_weak_modal(case.expected_result)
        assert "Use Case" in case.preconditions
        assert case.category == "Positive"
        assert case.priority == "High"

    def test_negative_objective_gets_the_refusal_body(self):
        check = _strat.CheckSpec(
            objective="Verify login fails with an error for wrong credentials",
            priority="High", url_pattern="*/login*",
        )
        case = tc_author.expand_check(check)
        assert case.category == "Negative"
        assert tc_author.asserts_feedback(case.expected_result)
        assert "Nothing is persisted" in case.expected_result
        # The refusal path checks persistence too.
        assert any("was not persisted" in s for s in case.steps)

    def test_test_data_is_derived_from_the_objective(self):
        case = tc_author.expand_check(_strat.CheckSpec(
            objective="Verify the email field rejects malformed addresses"))
        assert "tester@example.com" in case.test_data

        case = tc_author.expand_check(_strat.CheckSpec(
            objective="Verify field boundaries via BVA on max length"))
        assert "Max+1" in case.test_data

    def test_two_expansions_of_the_same_check_are_identical(self):
        check = _strat.CheckSpec(objective="Verify the grid paginates",
                                 priority="Medium")
        a = tc_author.expand_check(check)
        b = tc_author.expand_check(check)
        assert a.to_dict() == b.to_dict()

    def test_navigation_step_degrades_gracefully(self):
        assert tc_author.navigation_step("", site_url="https://x.test") == \
            "Open https://x.test in the browser"
        assert tc_author.navigation_step("/cart/*") == "Go to /cart/"
        assert tc_author.navigation_step("*") == \
            "Open the application under test in the browser"


class TestInferCategory:
    @pytest.mark.parametrize("objective,want", [
        ("Verify that User can create a record", "Positive"),
        ("Verify that User cannot create a record", "Negative"),
        ("Verify that User can`t merge Opportunities with Won stage",
         "Negative"),
        ('Verify the "Launch" button is not displayed while active',
         "Negative"),
        ("Verify the form rejects an invalid email", "Negative"),
        ("Verify saving without required fields is blocked", "Negative"),
        ("Verify the grid renders 20 rows per page", "Positive"),
        # Regression guards: an earlier looser pattern matched bare
        # "without", "error" and status codes, which turned these three
        # positive capability checks into refusal cases with a
        # nonsensical "confirm nothing was persisted" body.
        ("Verify all internal navigation links resolve without 404/5xx",
         "Positive"),
        ("Verify no JavaScript errors are logged in the browser console",
         "Positive"),
        ("Verify the export completes without a timeout", "Positive"),
    ])
    def test_polarity(self, objective, want):
        assert tc_author.infer_category(objective) == want


# ── author_test_cases: fallback + LLM path ───────────────────────────

def _strategy() -> _strat.TestStrategy:
    return _strat.TestStrategy(
        site_url="https://x.test",
        matrix={
            "Functional": [
                _strat.CheckSpec(objective="Verify that User can log in",
                                 priority="High"),
                _strat.CheckSpec(objective="Verify that the grid filters",
                                 priority="Medium"),
            ],
            "Accessibility": [
                _strat.CheckSpec(objective="Verify keyboard reachability",
                                 priority="High"),
            ],
        },
        source="rule_based",
    )


class TestAuthorFallback:
    def test_no_api_key_yields_one_case_per_check(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = tc_author.author_test_cases(strategy=_strategy())
        assert result.source == "deterministic"
        assert len(result.cases) == 3
        assert {c.section for c in result.cases} == {"Functional",
                                                     "Accessibility"}
        for case in result.cases:
            assert tc_author.lint_case(case) == []

    def test_no_strategy_and_no_key_yields_nothing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = tc_author.author_test_cases()
        assert result.cases == []
        assert result.source == "deterministic"

    def test_llm_failure_falls_back(self, monkeypatch):
        from engine import llm_client

        def _boom(**kwargs):
            raise llm_client.LLMUnavailable("no key")

        monkeypatch.setattr(tc_author, "call_messages", _boom)
        result = tc_author.author_test_cases(strategy=_strategy(),
                                             force_llm=True)
        assert result.source == "deterministic"
        assert len(result.cases) == 3

    def test_unparseable_response_falls_back(self, monkeypatch):
        monkeypatch.setattr(tc_author, "call_messages",
                            lambda **kw: _FakeResp("not json at all"))
        result = tc_author.author_test_cases(strategy=_strategy(),
                                             force_llm=True)
        assert result.source == "deterministic"


class _FakeBlock:
    def __init__(self, text): self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.usage = None


_LLM_PAYLOAD = {
    "rationale": "Walked the job-positions grid and the create form.",
    "gaps": ["Payment surfaces were not crawled"],
    "cases": [
        {
            "section": "Job Positions grid",
            "summary": 'Verify that User can filter Job Positions using '
                       'the "Internal" filter',
            "preconditions": "Job Positions are created",
            "steps": ["1. Go to HR module -> Job Positions grid",
                      '2. Click on the "Internal" filter button'],
            "test_data": "",
            "expected_result": 'User can filter Job Positions using the '
                               '"Internal" filter. Only internal positions '
                               'remain in the grid.',
            "category": "Positive",
            "priority": "Medium",
            "url_pattern": "/hr/job-positions*",
        },
        {
            "section": "Job Position creation",
            "summary": "Verify that User cannot create Job Position "
                       "without the required fields filling",
            "preconditions": "",
            "steps": ["Go to HR module -> Job Positions grid",
                      'Click on the "Create" button',
                      "Leave the required fields empty",
                      'Click on the "Save" button'],
            "test_data": "",
            # Deliberately hedged and missing the feedback half so the
            # normaliser has something to fix.
            "expected_result": "The Job Position should not be created",
            "category": "Negative",
            "priority": "High",
        },
        {
            # Unexecutable — one step. Must be dropped, not shipped.
            "section": "Job Positions grid",
            "summary": "Verify that the grid loads",
            "steps": ["Open the grid"],
            "expected_result": "The grid loads.",
            "category": "Positive",
        },
    ],
}


class TestAuthorLLMPath:
    def test_parses_normalises_and_drops_unexecutable_cases(self, monkeypatch):
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return _FakeResp(json.dumps(_LLM_PAYLOAD))

        monkeypatch.setattr(tc_author, "call_messages", _fake)
        artifacts = tc_author.Artifacts(
            url="https://odoo.test",
            custom_prompt="Focus on the HR module",
            requirements=["HR module must let a recruiter open a position"],
            pages=[{"url": "https://odoo.test/hr",
                    "buttons": ["Create", "Internal"]}],
        )
        result = tc_author.author_test_cases(
            profile=_recon.SiteProfile(url="https://odoo.test"),
            strategy=_strategy(), artifacts=artifacts, force_llm=True)

        assert result.source == "llm"
        # Third case is unexecutable and must be dropped with a finding.
        assert len(result.cases) == 2
        assert any("unexecutable" in f for f in result.lint_findings)
        assert result.gaps == ["Payment surfaces were not crawled"]

        # Step numbering supplied by the model is stripped — the
        # exporter owns numbering, otherwise re-export double-numbers.
        first = result.cases[0]
        assert first.steps[0] == "Go to HR module -> Job Positions grid"
        assert first.url_pattern == "/hr/job-positions*"

        # The hedged negative case is normalised on both counts.
        neg = result.cases[1]
        assert not tc_author.has_weak_modal(neg.expected_result)
        assert tc_author.asserts_feedback(neg.expected_result)
        assert neg.category == "Negative"

        # Everything the agent shipped is house-style compliant.
        for case in result.cases:
            assert tc_author.lint_case(case) == []

    def test_prompt_carries_the_control_inventory_and_artifacts(
            self, monkeypatch):
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return _FakeResp(json.dumps(_LLM_PAYLOAD))

        monkeypatch.setattr(tc_author, "call_messages", _fake)
        tc_author.author_test_cases(
            strategy=_strategy(),
            artifacts=tc_author.Artifacts(
                url="https://odoo.test",
                custom_prompt="Only smoke",
                requirements=["A recruiter opens a position"],
                attachments=[{"name": "spec.pdf",
                              "excerpt": "The Save button persists."}],
                pages=[{"url": "https://odoo.test/hr",
                        "buttons": ["Create"],
                        "forms": [{"fields": [{"name": "job_name",
                                               "type": "text",
                                               "required": True}]}]}],
            ),
            force_llm=True)

        prompt = captured["messages"][0]["content"]
        assert "https://odoo.test/hr" in prompt
        assert '"Create"' in prompt
        assert '"job_name" (type=text, required)' in prompt
        assert "A recruiter opens a position" in prompt
        assert "The Save button persists." in prompt
        assert "Only smoke" in prompt
        # The strategy checks come through as objectives to expand, not
        # as finished cases.
        assert "Verify that User can log in" in prompt
        assert "objectives to expand" in prompt.lower() \
            or "OBJECTIVE, not" in prompt

    def test_call_fits_inside_an_achievable_timeout(self, monkeypatch):
        """The SDK call must carry its own timeout, not llm_client's 60 s.

        An 8k-token authoring response needs ~2 minutes at Sonnet's output
        rate. On the 60 s default the call times out, burns all three
        retries, and silently falls back to the deterministic expansion —
        i.e. the agent never runs in production while every unit test
        (which stubs the SDK) still passes.
        """
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return _FakeResp(json.dumps(_LLM_PAYLOAD))

        monkeypatch.setattr(tc_author, "call_messages", _fake)
        tc_author.author_test_cases(strategy=_strategy(), force_llm=True)

        from engine.llm_client import DEFAULT_TIMEOUT
        assert "timeout" in captured, \
            "author must pass an explicit timeout to call_messages"
        assert captured["timeout"] > DEFAULT_TIMEOUT
        # Output tokens must be emittable inside that window. 40 tok/s is
        # a deliberately pessimistic floor for Sonnet.
        assert captured["max_tokens"] / 40 < captured["timeout"]

    def test_kill_switch_forces_the_deterministic_path(self, monkeypatch):
        monkeypatch.setenv("TC_AUTHOR_ENABLED", "0")
        monkeypatch.setattr(
            tc_author, "call_messages",
            lambda **kw: pytest.fail("LLM must not be called when disabled"))
        result = tc_author.author_test_cases(strategy=_strategy(),
                                             force_llm=True)
        assert result.source == "deterministic"
        assert len(result.cases) == 3

    @pytest.mark.parametrize("value,enabled", [
        ("1", True), ("", True), ("true", True), ("on", True),
        ("0", False), ("false", False), ("no", False), ("off", False),
        ("OFF", False),
    ])
    def test_kill_switch_parsing(self, monkeypatch, value, enabled):
        monkeypatch.setenv("TC_AUTHOR_ENABLED", value)
        assert tc_author._author_enabled() is enabled

    def test_empty_case_list_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            tc_author, "call_messages",
            lambda **kw: _FakeResp(json.dumps({"cases": []})))
        result = tc_author.author_test_cases(strategy=_strategy(),
                                             force_llm=True)
        assert result.source == "deterministic"


# ── generate_from_strategy integration ───────────────────────────────

class TestGenerateFromStrategyAuthored:
    def test_deterministic_path_keeps_the_priority_routing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        profile = _recon.SiteProfile(url="https://x.test")
        tcs, cls = generate_from_strategy(profile, _strategy())
        # High → TC, Medium/Low → checklist, unchanged contract.
        assert len(tcs) == 2
        assert len(cls) == 1
        assert all(tc.id.startswith("SA") for tc in tcs)
        # And every body is now real, not the old shared placeholder.
        for tc in tcs:
            steps = tc.test_steps.splitlines()
            assert len(steps) >= 3
            assert "Perform the action described in the objective" \
                not in tc.test_steps
            assert not tc_author.has_weak_modal(tc.expected_result)
        # Two TCs no longer share one body.
        assert tcs[0].test_steps != tcs[1].test_steps

    def test_category_column_now_carries_polarity(self, monkeypatch):
        # The pre-authoring path wrote the *test type* ("Functional",
        # "Accessibility") into TestCase.category, which the /test-cases
        # stat chips count as Positive/Negative — so every site-aware
        # case counted as neither.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        tcs, _ = generate_from_strategy(
            _recon.SiteProfile(url="https://x.test"), _strategy())
        assert {tc.category for tc in tcs} <= {"Positive", "Negative"}

    def test_authored_path_replaces_the_tc_stream(self, monkeypatch):
        monkeypatch.setattr(
            tc_author, "call_messages",
            lambda **kw: _FakeResp(json.dumps(_LLM_PAYLOAD)))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        profile = _recon.SiteProfile(url="https://odoo.test")
        tcs, cls = generate_from_strategy(
            profile, _strategy(),
            artifacts=tc_author.Artifacts(url="https://odoo.test"))

        # Sections come from the author (UI surfaces), not from the
        # strategy's category names.
        sections = {tc.section for tc in tcs}
        assert sections == {"Job Positions grid", "Job Position creation"}
        # Authored section numbers start above the reserved category
        # range so IDs never collide with the deterministic stream.
        assert all(int(tc.id[2:].split("_")[0]) >= 10 for tc in tcs)
        # Checklist still comes from the Medium/Low strategy checks.
        assert len(cls) == 1

    def test_artifacts_only_path_needs_no_url_or_strategy(self, monkeypatch):
        # Prompt-only / attachment-only input must still reach the author.
        from engine.testcase_generator import generate_from_artifacts

        monkeypatch.setattr(
            tc_author, "call_messages",
            lambda **kw: _FakeResp(json.dumps(_LLM_PAYLOAD)))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        tcs = generate_from_artifacts(tc_author.Artifacts(
            custom_prompt="Write cases for the HR module",
            requirements=["A recruiter can open a job position"],
        ))
        assert len(tcs) == 2
        assert {tc.section for tc in tcs} == {"Job Positions grid",
                                              "Job Position creation"}
        for tc in tcs:
            assert tc.test_steps.startswith("1. ")
            assert not tc_author.has_weak_modal(tc.expected_result)

    def test_artifacts_only_path_yields_nothing_without_llm(self, monkeypatch):
        from engine.testcase_generator import generate_from_artifacts
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # No strategy matrix means nothing for the deterministic fallback
        # to expand; the legacy knowledge-base path owns this input shape.
        assert generate_from_artifacts(
            tc_author.Artifacts(custom_prompt="anything")) == []
        assert generate_from_artifacts(None) == []

    def test_authored_ids_are_stable_across_regenerates(self, monkeypatch):
        from engine.testcase_generator import generate_from_artifacts
        monkeypatch.setattr(
            tc_author, "call_messages",
            lambda **kw: _FakeResp(json.dumps(_LLM_PAYLOAD)))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        arts = tc_author.Artifacts(custom_prompt="Write cases")
        first = [tc.id for tc in generate_from_artifacts(arts)]
        second = [tc.id for tc in generate_from_artifacts(arts)]
        assert first == second
        # Distinct sections get distinct section numbers.
        assert len({i.split("_")[0] for i in first}) == 2

    def test_authoring_exception_falls_back_to_deterministic(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        def _boom(**kwargs):
            raise RuntimeError("author blew up")

        monkeypatch.setattr(tc_author, "author_test_cases", _boom)
        tcs, cls = generate_from_strategy(
            _recon.SiteProfile(url="https://x.test"), _strategy(),
            artifacts=tc_author.Artifacts(url="https://x.test"))
        assert len(tcs) == 2 and len(cls) == 1


# ── QA Team Lead review rules ────────────────────────────────────────

class _TC:
    """Minimal duck-typed test case for the reviewer."""

    def __init__(self, **kw):
        self.id = kw.get("id", "SC1_001")
        self.summary = kw.get("summary", "Verify that User can save")
        self.preconditions = kw.get("preconditions", "")
        self.test_steps = kw.get("test_steps", "1. Go to the grid\n"
                                               "2. Click Save")
        self.test_data = kw.get("test_data", "")
        self.expected_result = kw.get("expected_result", "The record is saved")
        self.category = kw.get("category", "Positive")


class TestTeamLeadHouseStyle:
    def test_hedged_expected_result_is_rewritten_not_enforced(self):
        from engine.qa_team_lead import review_test_cases
        tc = _TC(expected_result="The record should be saved")
        fixed, report = review_test_cases([tc])
        assert fixed[0].expected_result == "The record is saved"
        finding = next(f for f in report.findings
                       if f.category == "Expected Result Voice")
        assert finding.auto_fixed

    def test_declarative_expected_result_is_left_alone(self):
        from engine.qa_team_lead import review_test_cases
        tc = _TC(expected_result="The required fields are highlighted")
        fixed, report = review_test_cases([tc])
        assert fixed[0].expected_result == "The required fields are highlighted"
        assert not [f for f in report.findings
                    if f.category == "Expected Result Voice"]

    def test_negative_case_gains_the_feedback_assertion(self):
        from engine.qa_team_lead import review_test_cases
        tc = _TC(category="Negative",
                 summary="Verify that User cannot save the record",
                 expected_result="User cannot save the record")
        fixed, report = review_test_cases([tc])
        assert tc_author.asserts_feedback(fixed[0].expected_result)
        assert any(f.category == "Missing Feedback Assertion"
                   for f in report.findings)

    def test_generic_steps_are_flagged_and_stripped(self):
        from engine.qa_team_lead import review_test_cases
        tc = _TC(test_steps="1. Go to HR module -> Employees grid\n"
                            "2. Perform the action\n"
                            "3. Click on the \"Save\" button")
        fixed, report = review_test_cases([tc])
        assert "Perform the action" not in fixed[0].test_steps
        assert fixed[0].test_steps == ('1. Go to HR module -> Employees grid\n'
                                       '2. Click on the "Save" button')
        assert any(f.category == "Generic Step" for f in report.findings)

    def test_generic_step_kept_when_stripping_would_leave_one_step(self):
        from engine.qa_team_lead import review_test_cases
        tc = _TC(test_steps="1. Go to the grid\n2. Perform the action")
        fixed, report = review_test_cases([tc])
        # Flagged, but not stripped — a one-step case is worse.
        assert "Perform the action" in fixed[0].test_steps
        finding = next(f for f in report.findings
                       if f.category == "Generic Step")
        assert not finding.auto_fixed

    def test_single_step_case_is_flagged(self):
        from engine.qa_team_lead import review_test_cases
        _, report = review_test_cases([_TC(test_steps="1. Open the page")])
        assert any(f.category == "Insufficient Steps" for f in report.findings)

    def test_hedged_summary_is_rewritten(self):
        from engine.qa_team_lead import review_test_cases
        tc = _TC(summary="Verify that the record should be saved")
        fixed, report = review_test_cases([tc])
        assert not tc_author.has_weak_modal(fixed[0].summary)
        assert any(f.category == "Summary Voice" for f in report.findings)


# ── Knowledge-base content complies with its own standard ────────────

class TestKnowledgeBaseCompliance:
    def test_no_shipped_test_case_template_hedges(self):
        from engine.qa_knowledge_loader import LOADER
        offenders: list[str] = []
        for area in LOADER.areas():
            for tpl in LOADER.get_test_cases(area) or []:
                if tc_author.has_weak_modal(tpl.expected_result or ""):
                    offenders.append(f"{area}: {tpl.summary[:60]}")
        assert not offenders, (
            "shipped templates must assert, not hedge: "
            + "; ".join(offenders))


# ── Route-level wiring ───────────────────────────────────────────────

class TestTestCasesRoute:
    """POST /test-cases must actually surface the authored pack.

    Every other test in this file stubs the SDK and calls the engine
    directly, so all of them would still pass if the route never reached
    the author agent. This one closes that gap.
    """

    def test_prompt_only_input_renders_authored_cases(self, client,
                                                      monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(
            tc_author, "call_messages",
            lambda **kw: _FakeResp(json.dumps(_LLM_PAYLOAD)))

        resp = client.post("/test-cases", data={
            "input_text": "HR module lets a recruiter manage job positions.\n"
                          "A recruiter can create and delete a position.",
            "custom_prompt": "Write test cases for the HR module",
        }, follow_redirects=True)

        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert ('Verify that User can filter Job Positions using the '
                '&#34;Internal&#34; filter' in body
                or "Verify that User can filter Job Positions" in body)
        assert ("Verify that User cannot create Job Position without the "
                "required fields filling" in body)
        # The hedged expected result was normalised before render.
        assert "should not be created" not in body
