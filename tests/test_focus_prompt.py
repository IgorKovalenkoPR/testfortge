"""Regression for the operator complaint that 'focus on forms на
https://testfort.com' was being ignored by the generator. Two
problems lived in the previous build:

  * `_FOCUS_RE`'s capture class excluded `:` so any URL inside the
    focus phrase prevented the whole regex from matching.
  * The generator never used the parsed focus phrase to filter the
    final TC list, so even when the parser DID extract tokens they
    had no effect.

Both are now fixed; this suite locks down the new behaviour."""
import pytest


class TestFocusPromptParsing:
    def test_simple_focus(self):
        from engine.testcase_generator import _parse_tc_prompt
        d = _parse_tc_prompt("focus on forms")
        assert d.get("focus_tokens") == ["forms"]

    def test_focus_with_url_strips_domain(self):
        """'focus on forms на https://testfort.com' previously failed
        to match. Now it matches AND drops the domain so 'testfort'
        doesn't sneak into the keyword list."""
        from engine.testcase_generator import _parse_tc_prompt
        d = _parse_tc_prompt("focus on forms на https://testfort.com")
        assert "forms" in (d.get("focus_tokens") or [])
        assert "testfort" not in (d.get("focus_tokens") or [])

    def test_ukrainian_only(self):
        from engine.testcase_generator import _parse_tc_prompt
        d = _parse_tc_prompt("тільки на login flow")
        toks = d.get("focus_tokens") or []
        assert "login" in toks
        assert "flow" in toks


class TestFocusFilterNarrowsResult:
    def test_focus_drops_unrelated_sections(self):
        from engine.testcase_generator import generate_test_cases
        raw = [{
            "id": "RAW-1",
            "text": "User logs in with email/password, can search products, "
                    "and can submit a contact form.",
        }]
        all_tc = generate_test_cases([], "", raw_requirements=raw)
        focused = generate_test_cases([], "focus on forms",
                                       raw_requirements=raw)
        assert len(focused) <= len(all_tc), "focus should narrow"
        sections = sorted({tc.section for tc in focused})
        assert sections == ["Forms"], (
            f"expected only Forms section, got {sections!r}")

    def test_focus_falls_back_when_nothing_matches(self):
        """If the focus token matches nothing, the filter must NOT
        empty the list — operator still gets the unfiltered set so
        they don't think generation broke."""
        from engine.testcase_generator import generate_test_cases
        raw = [{"id": "RAW-1",
                "text": "User logs in with email and password."}]
        focused = generate_test_cases(
            [], "focus on widgets-that-dont-exist",
            raw_requirements=raw,
        )
        assert focused, "fallback to full set must keep results"
