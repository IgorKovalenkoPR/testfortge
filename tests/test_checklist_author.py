"""The Low-Level Checklist Author agent.

Covers ``engine.checklist_author``: the cached system blocks, the evidence
gate, wording enforcement, numbering ownership, and every path back to the
deterministic enumeration.

Two properties carry most of the weight here:

* **The agent never decides what counts as evidence.** The prompt asks it
  not to invent controls; asking is not enforcement, so a quoted label no
  artefact contains is dropped after the call and the drop is reported. A
  row that fails because the control never existed is worse than a missing
  row — a gap is visible, a wrong failure is not.
* **Falling back is never a failure.** No key, a refused call, unparseable
  JSON, an empty result: all land on the enumeration, which already
  reproduces the reference shape. The agent is an improvement on a working
  path, not a dependency of one.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from urllib.parse import urlparse

import pytest

from engine import checklist_author as ca
from engine import checklist_rules as cr
from engine import glossary as gloss
from engine.site_crawler import _parse_page


PAGE = """<html><head><title>Mobile App Testing</title></head><body>
<header class="site-header">
  <a href="/" class="logo"><img src="/l.svg" alt="X"></a>
  <nav><ul><li><a href="/services">Services</a>
    <ul><li><a href="/s/manual">Manual Testing</a></li></ul></li></ul></nav>
  <a href="/contact" class="btn">Contact us</a>
</header>
<main><h1>Mobile Application Testing Services</h1>
<h2>Testing by Platform</h2>
<form action="/c" method="post"><label for="e">Email</label>
<input id="e" name="email" type="email" required>
<button type="submit">Send</button></form></main>
<footer class="site-footer"><a href="mailto:a@b.test">a@b.test</a>
<a href="/privacy-policy">Privacy Policy</a></footer></body></html>"""

URL = "https://example.com/mobile"


@pytest.fixture(scope="module")
def artifacts() -> ca.Artifacts:
    page = asdict(_parse_page(PAGE, URL, urlparse(URL).netloc))
    return ca.Artifacts(url=URL, pages=[page])


class _Block:
    def __init__(self, text): self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = None


def _payload(**over) -> str:
    base = {
        "rationale": "Walked the Header, the content section and the Footer.",
        "surface": '"Mobile Application Testing Services" page',
        "gaps": ["Pricing page was not crawled"],
        "sections": [
            {"name": "Header", "checks": [
                {"objective": "Verify that the Homepage is opened after "
                              "clicking the logo",
                 "depth": 2, "priority": "High"},
                {"objective": 'Verify that all sub-items are visible and '
                              'clickable from the "Services" drop-down menu',
                 "depth": 2},
            ]},
            {"name": "Page Content", "checks": [
                {"objective": 'Verify that the "Testing by Platform" section '
                              'is visible and matches the design',
                 "depth": 2},
                {"objective": "Verify that the Email field accepts valid "
                              "data", "depth": 3},
            ]},
        ],
    }
    base.update(over)
    return json.dumps(base)


@pytest.fixture()
def stub_llm(monkeypatch):
    def _install(text: str):
        monkeypatch.setattr(ca, "call_messages", lambda **kw: _Resp(text))
    return _install


# ── Prompt ───────────────────────────────────────────────────────────

class TestSystemBlocks:
    def test_three_cache_breakpoints_and_no_more(self):
        # The API allows four; the user prompt varies per call so it gets
        # none, and spending a breakpoint on a prefix that never hits is
        # the mistake this guards.
        blocks = ca._system_blocks()
        assert len(blocks) == 3
        assert all(b.get("cache_control") == {"type": "ephemeral"}
                   for b in blocks)

    def test_the_style_and_terminology_assets_reach_the_model(self):
        blob = "\n".join(b["text"] for b in ca._system_blocks())
        assert "checklist_style.yaml" in blob
        assert "57 checks" in blob                  # the measured shape
        assert "wording_rules.yaml" in blob
        assert "Remove dot at the end" in blob      # a reviewer quote
        assert "Hamburger button" in blob           # the glossary

    def test_the_head_forbids_test_case_shaped_rows(self):
        head = ca._system_blocks()[0]["text"]
        assert "NOT writing test cases" in head
        assert "Do NOT number" in head

    def test_the_prompt_hands_over_the_enumeration_as_a_floor(self,
                                                              artifacts):
        prompt = ca._build_user_prompt(artifacts)
        assert "Control inventory" in prompt
        assert "FLOOR" in prompt
        # The enumeration's own rows are in there to be improved on.
        assert "Verify that" in prompt


# ── Evidence gate ────────────────────────────────────────────────────

class TestEvidenceGate:
    def test_labels_are_collected_from_every_artefact_kind(self, artifacts):
        found = ca.evidenced_labels(artifacts)
        assert "services" in found
        assert "manual testing" in found            # a menu child
        assert "testing by platform" in found       # a heading
        assert "email" in found                     # a form field
        assert "privacy policy" in found            # a legal link

    def test_a_control_named_only_in_a_requirement_still_counts(self):
        arts = ca.Artifacts(requirements=["The Pricing Calculator must "
                                          "accept a seat count"])
        assert ca.unevidenced_quotes(
            'Verify that the "Pricing Calculator" is displayed',
            ca.evidenced_labels(arts)) == []

    def test_an_invented_label_is_reported(self, artifacts):
        missing = ca.unevidenced_quotes(
            'Verify that the "Pricing Calculator" widget is displayed',
            ca.evidenced_labels(artifacts))
        assert missing == ["Pricing Calculator"]

    def test_only_quoted_text_is_checked(self, artifacts):
        # Unquoted prose is description; a quoted label is a claim that
        # those exact words are on screen, which is the checkable part.
        assert ca.unevidenced_quotes(
            "Verify that the pricing calculator is displayed",
            ca.evidenced_labels(artifacts)) == []

    def test_the_gate_drops_the_row_and_says_so(self, artifacts, stub_llm):
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": 'Verify that the "Services" drop-down opens'},
            {"objective": 'Verify that the "Pricing Calculator" is displayed'},
        ]}]))
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        assert result.total == 1
        assert len(result.dropped) == 1
        assert "Pricing Calculator" in result.dropped[0]
        # Silently swallowing it would leave the operator thinking the
        # agent simply found less.
        assert any("dropped" in g for g in result.gaps)

    def test_the_drop_message_reads_correctly_for_one_row(self, artifacts,
                                                          stub_llm):
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": 'Verify that the "Services" drop-down opens'},
            {"objective": 'Verify that the "Nonexistent Thing" is displayed'},
        ]}]))
        gaps = ca.author_checklist(artifacts=artifacts,
                                   force_llm=True).gaps
        assert any("1 authored row was dropped" in g for g in gaps)


# ── Wording is enforced, not requested ───────────────────────────────

class TestWordingEnforcement:
    @pytest.mark.parametrize("raw,want", [
        ("the Footer is displayed", "Verify that the Footer is displayed"),
        ("verify the Homepage is opened.",
         "Verify the Homepage is opened"),
        ("Verify that the footer is displayed",
         "Verify that the Footer is displayed"),
    ])
    def test_opener_case_and_punctuation(self, raw, want):
        check, _ = ca.normalise_check(cr.Check(objective=raw, section="S"))
        assert check.objective == want

    def test_authored_rows_pass_the_terminology_linter(self, artifacts,
                                                       stub_llm):
        stub_llm(_payload())
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        assert cr.lint_checklist(result.checklist) == []

    def test_residual_findings_are_reported_not_hidden(self, artifacts,
                                                       stub_llm):
        # "correctly" grades instead of describing; normalisation cannot
        # fix that without inventing what the author meant.
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": "Verify that the logo is displayed correctly"},
        ]}]))
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        assert any("graded outcome" in f for f in result.lint_findings)

    def test_category_and_priority_are_coerced(self, artifacts, stub_llm):
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": 'Verify that the "Services" drop-down opens',
             "category": "negative", "priority": "urgent"},
        ]}]))
        check = ca.author_checklist(
            artifacts=artifacts, force_llm=True).checklist.all_checks()[0]
        assert check.category == "Negative"
        assert check.priority == "Medium"      # "urgent" is not a priority


# ── Numbering stays ours ─────────────────────────────────────────────

class TestNumbering:
    def test_numbers_are_applied_after_the_call(self, artifacts, stub_llm):
        stub_llm(_payload())
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        numbers = [c.number for c in result.checklist.all_checks()]
        assert numbers == ["1.1", "1.2", "2.1", "2.1.1"]

    @pytest.mark.parametrize("written", [
        '7.4 Verify that the "Services" drop-down opens',
        '2. Verify that the "Services" drop-down opens',
        '- Verify that the "Services" drop-down opens',
        '2.7.1 Verify that the "Services" drop-down opens',
    ])
    def test_a_number_the_model_wrote_is_stripped_not_embedded(
            self, artifacts, stub_llm, written):
        """Asserting the NUMBER alone passed while the text was mangled.

        Leaving the model's "7.4" in place duplicated the № column and
        defeated the opener fix, which prepended a second "Verify that" in
        front of the digits: "Verify that 7.4 Verify that the …".
        """
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": written},
        ]}]))
        check = ca.author_checklist(
            artifacts=artifacts, force_llm=True).checklist.all_checks()[0]
        assert check.number == "1.1"
        assert check.objective ==             'Verify that the "Services" drop-down opens'
        assert check.objective.count("Verify") == 1

    def test_depth_beyond_three_is_clamped(self, artifacts, stub_llm):
        # checklist_style.yaml: never past level 3, it stops being
        # scannable and the reference never needs it.
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": 'Verify that the "Services" drop-down opens'},
            {"objective": "Verify that the Email field accepts valid data",
             "depth": 9},
        ]}]))
        checks = ca.author_checklist(
            artifacts=artifacts, force_llm=True).checklist.all_checks()
        assert [c.number for c in checks] == ["1.1", "1.1.1"]


# ── Fallback ─────────────────────────────────────────────────────────

class TestFallback:
    def test_no_api_key_uses_the_enumeration(self, artifacts, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = ca.author_checklist(artifacts=artifacts)
        assert result.source == "deterministic"
        assert result.total > 0

    def test_kill_switch(self, artifacts, monkeypatch, stub_llm):
        stub_llm(_payload())
        monkeypatch.setenv("CL_AUTHOR_ENABLED", "0")
        assert ca.author_checklist(artifacts=artifacts,
                                   force_llm=True).source == "deterministic"

    def test_an_unreachable_llm_falls_back(self, artifacts, monkeypatch):
        def _boom(**kw):
            raise ca.LLMUnavailable("no credit")
        monkeypatch.setattr(ca, "call_messages", _boom)
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        assert result.source == "deterministic" and result.total > 0

    def test_unparseable_json_falls_back(self, artifacts, stub_llm):
        stub_llm("I'm afraid I can't do that")
        assert ca.author_checklist(artifacts=artifacts,
                                   force_llm=True).source == "deterministic"

    def test_an_empty_result_falls_back(self, artifacts, stub_llm):
        # An empty authored sheet is worse than an enumerated one: the
        # enumeration at least covers the surface.
        stub_llm(_payload(sections=[]))
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        assert result.source == "deterministic" and result.total > 0

    def test_a_result_gutted_by_the_evidence_gate_falls_back(self, artifacts,
                                                             stub_llm):
        stub_llm(_payload(sections=[{"name": "Header", "checks": [
            {"objective": 'Verify that the "Imaginary Widget" is displayed'},
        ]}]))
        assert ca.author_checklist(artifacts=artifacts,
                                   force_llm=True).source == "deterministic"

    def test_no_artefacts_at_all(self):
        result = ca.author_checklist(artifacts=ca.Artifacts())
        assert result.source == "deterministic"
        assert any("No page was crawled" in g for g in result.gaps)


# ── Handoff ──────────────────────────────────────────────────────────

class TestHandoff:
    def test_items_carry_number_depth_and_status(self, artifacts, stub_llm):
        stub_llm(_payload())
        items = ca.to_checklist_items(
            ca.author_checklist(artifacts=artifacts, force_llm=True))
        assert items[0].item_num == "1.1"
        assert all(i.status == "Unchecked" for i in items)
        assert any(i.depth == 3 for i in items)

    def test_the_shape_matches_the_deterministic_path(self, artifacts,
                                                      stub_llm):
        # Downstream — the exporter, the UI, the manual walk — must not be
        # able to tell which produced the sheet.
        stub_llm(_payload())
        authored = ca.to_checklist_items(
            ca.author_checklist(artifacts=artifacts, force_llm=True))
        enumerated = cr.to_checklist_items(
            cr.build_checklist(artifacts.pages, url=URL))
        assert {type(i) for i in authored} == {type(i) for i in enumerated}
        for item in authored:
            assert item.id and item.section and item.objective

    def test_the_rationale_and_gaps_survive(self, artifacts, stub_llm):
        stub_llm(_payload())
        result = ca.author_checklist(artifacts=artifacts, force_llm=True)
        assert "Walked the Header" in result.rationale
        assert any("Pricing page" in g for g in result.gaps)
