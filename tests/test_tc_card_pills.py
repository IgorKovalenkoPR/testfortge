"""The pills on a test-case card say what they are (owner report, 2026-10-21).

On testforge.qarea.org every card carried two pills nobody could explain:

* a purple "—" — the Suite badge, rendered for editors with no suite set
  so the suite can be chosen in place, but labelled with nothing;
* an empty white pill — the user-story badge, rendered whether or not the
  case has a user story (generated site cases have none).

Now an empty suite reads "Suite", styled as a placeholder, and carries its
hint on the pill (hover and keyboard focus); the user-story badge exists
only when there is a story.
"""
from __future__ import annotations

import re
import secrets

import pytest

from engine import db

_RUN = secrets.token_hex(4)


def _case(external_id, suite="", story=""):
    return {"id": external_id, "section": "Website", "section_num": 1,
            "summary": f"Verify that {external_id} works", "preconditions": "",
            "test_steps": "1. Open\n2. Look", "test_data": "",
            "expected_result": "It works", "issues": "", "comment": "",
            "user_story_id": story, "category": "Positive",
            "priority": "High", "status": "Unchecked",
            "testing_type": "Functional", "suite": suite}


@pytest.fixture
def project(app, request):
    pid = db.upsert_project(name=f"Pills {request.node.name} {_RUN}"[:180])
    db.save_test_cases(pid, [_case("TC-001"),
                             _case("TC-002", suite="Smoke", story="US-007")])
    return pid


def _card(html: str, case_id: str) -> str:
    start = html.index(f'id="{case_id}" data-ie-row')
    end = html.index('<table class="tc-detail-table">', start)
    return html[start:end]


def _badges(card: str) -> list[str]:
    """Every badge span's inner text, tags stripped."""
    out = []
    for m in re.finditer(r'<span class="badge[^"]*"[^>]*>(.*?)</span>\s*'
                         r'(?=<span class="badge|</div>|\{%|$|<span)',
                         card, re.S):
        out.append(re.sub(r"<[^>]+>", "", m.group(1)).strip())
    return out


def _render(client, project, monkeypatch, editors: bool) -> str:
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1" if editors else "0")
    with client.session_transaction() as sess:
        sess["project_id"] = project
    resp = client.get("/test-cases")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


class TestEmptyPills:

    def test_editors_see_a_labelled_editable_suite_pill(self, client, project,
                                                        monkeypatch):
        card = _card(_render(client, project, monkeypatch, True), "TC-001")
        suite = re.search(r'<span class="badge badge-suite-\s*tc-badge-suite'
                          r' tf-hover-hint"\s+data-hint="([^"]+)">(.*?)'
                          r'<span class="sr-only">', card, re.S)
        assert suite, card
        hint, inner = suite.groups()
        assert "Smoke" in hint and "Click" in hint
        field = re.search(r'<span class="ie ie-empty"[^>]*'
                          r'data-ie-field="suite"[^>]*>([^<]*)</span>', inner)
        assert field and field.group(1).strip() == "Suite", inner
        assert 'data-ie-placeholder="Suite"' in inner
        assert "data-ie-readonly" not in inner or "—" not in inner
        assert ">—<" not in inner

    def test_no_user_story_no_badge(self, client, project, monkeypatch):
        for editors in (True, False):
            card = _card(_render(client, project, monkeypatch, editors),
                         "TC-001")
            # A *visible* empty pill; the folder badge is empty but
            # `hidden` until static/js/folders.js fills it.
            empty = [m.group(0) for m in re.finditer(
                r'<span class="badge[^"]*"[^>]*>\s*</span>', card)
                if " hidden" not in m.group(0)]
            assert empty == [], (editors, empty)
            assert "tc_user_story_hint" not in card
            assert "user story this case" not in card

    def test_viewers_see_no_suite_pill_for_a_case_in_no_suite(
            self, client, project, monkeypatch):
        card = _card(_render(client, project, monkeypatch, False), "TC-001")
        assert "tc-badge-suite" not in card

    @pytest.mark.parametrize("editors", [True, False])
    def test_filled_values_render_with_their_hints(self, client, project,
                                                   monkeypatch, editors):
        card = _card(_render(client, project, monkeypatch, editors), "TC-002")
        assert re.search(r'data-ie-field="suite"[^>]*>Smoke</span>', card)
        assert re.search(r'data-hint="The user story this case was written '
                         r'from\.">US-007</span>', card)
        hint = re.search(r'tc-badge-suite tf-hover-hint"\s+data-hint="([^"]+)"',
                         card).group(1)
        assert ("Click" in hint) == editors


def test_the_inline_editor_restores_the_placeholder_when_cleared():
    """A cleared field goes back to its placeholder, not to an empty pill —
    the JavaScript half of the same fix (static/js/inline-edit.js)."""
    from pathlib import Path
    js = (Path(__file__).parent.parent / "static" / "js" /
          "inline-edit.js").read_text(encoding="utf-8")
    assert "host.getAttribute('data-ie-placeholder')" in js
    assert "host.classList.toggle('ie-empty', !text)" in js
