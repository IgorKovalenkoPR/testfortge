"""Toggletips — the "?" beside a control (owner ruling 2026-10-16).

The owner asked for hints over the important elements, shown on hover;
the first build was a per-page card instead. The component is
templates/_toggletip.html; its behaviour (hover, focus, tap, Escape) is
driven in a browser by tests/test_e2e_toggletips.py. These are the
server-side halves: what the macro renders, and where it may be put.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"

_CALL = re.compile(r"\{\{\s*tip\(")
_ID = re.compile(r"\{\{\s*tip\([^\n]*,\s*'([a-z0-9-]+)'\)\s*\}\}")


def _strip_comments(src: str) -> str:
    return re.sub(r"\{#.*?#\}", "", src, flags=re.S)


def tips_inside(src: str, tag: str) -> list[str]:
    """Each <tag>…</tag> (label, h1–h6) whose content calls tip()."""
    src = _strip_comments(src)
    pat = re.compile(rf"<({tag})\b[^>]*>((?:(?!</\1>).)*?)</\1>", re.S | re.I)
    return [m.group(0)[:120] for m in pat.finditer(src) if _CALL.search(m.group(2))]


def _users() -> dict[str, str]:
    out = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        src = p.read_text(encoding="utf-8")
        if _CALL.search(_strip_comments(src)) and p.name != "_toggletip.html":
            out[str(p.relative_to(TEMPLATES))] = src
    return out


class TestWhereATipMayGo:
    def test_some_templates_use_it(self):
        assert {"test_execution.html", "_project_picker.html",
                "_search_panel.html", "base.html",
                # stage 2
                "index.html", "estimation.html", "_input_block.html",
                "test_cases.html", "checklist.html", "test_execution_runs.html",
                "test_execution_live.html", "automation.html",
                "bug_reports.html", "org_settings.html"} <= set(_users())

    @pytest.mark.parametrize("tag", ["label", "h1|h2|h3|h4|h5|h6"])
    def test_never_inside_a_label_or_a_heading(self, tag):
        """A button inside a <label> joins the accessible name of the input
        it labels ("Base URL Help: Base URL"), and inside a heading it joins
        the heading's. The "?" goes beside them (.tf-label-row,
        .tf-title-row)."""
        bad = {name: hits for name, src in _users().items()
               if (hits := tips_inside(src, tag))}
        assert not bad, bad

    def test_the_scan_would_notice(self):
        assert tips_inside('<label for="x">Base URL {{ tip(a, b, \'x\') }}</label>', "label")
        assert tips_inside("<h3 class=\"card-title\">Run\n {{ tip(a, b, 'r') }}\n</h3>",
                           "h1|h2|h3|h4|h5|h6")
        assert not tips_inside("<span class=\"tf-label-row\"><label>A</label>"
                               "{{ tip(a, b, 'x') }}</span>", "label")

    def test_every_user_imports_the_macro(self):
        missing = [name for name, src in _users().items()
                   if "import tip with context" not in src]
        assert not missing, missing

    def test_ids_are_unique_within_a_template(self):
        """Two notes with one id would open each other's button."""
        dupes = {}
        for name, src in _users().items():
            ids = _ID.findall(_strip_comments(src))
            seen = {i for i in ids if ids.count(i) > 1}
            if seen:
                dupes[name] = sorted(seen)
        assert not dupes, dupes

    def test_the_old_always_open_copy_is_gone_from_test_execution(self):
        """Recommendation B: the secondary explanations moved into notes.
        The Base URL warning stays on screen on purpose."""
        src = (TEMPLATES / "test_execution.html").read_text(encoding="utf-8")
        for key in ("te_run_mode_hint", "te_assignee_hint", "te_record_video_hint",
                    "te_env_hint", "te_wt_max_pages_hint", "te_suite_filter_hint"):
            assert f"t.get('{key}'" not in src, key
        assert "te_base_url_hint_v2" in src

    @pytest.mark.parametrize("name,gone", [
        ("estimation.html", ("<small>{{ t.get('est_compat_rate_hint'",
                             "<small>{{ t.get('est_team_size_hint'")),
        ("_input_block.html", ("<p class=\"hint\">{{ t.get('tc_format_hint'",)),
        ("index.html", ("<p class=\"hint\">{{ t.get('move_artefacts_hint'",)),
        ("org_settings.html", ("<p class=\"text-muted\">{{ t.aim_intro }}</p>",
                               "<p class=\"text-muted\">{{ t.ms_policy_intro }}</p>")),
    ])
    def test_stage_two_moved_the_secondary_copy_into_notes(self, name, gone):
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        for needle in gone:
            assert needle not in src, (name, needle)


class TestWhatTheMacroRenders:
    @pytest.fixture
    def render(self, app):
        def go(lang="en"):
            from engine.i18n import en, ua
            t = (ua if lang == "ua" else en).TRANSLATIONS
            tpl = app.jinja_env.from_string(
                "{% from '_toggletip.html' import tip with context %}"
                "{{ tip('Note <b>text</b>', 'Base <i>URL</i>', 'x1') }}")
            with app.test_request_context():
                return tpl.render(t=_Dict(t))
        return go

    def test_a_closed_disclosure_named_after_what_it_explains(self, render):
        html = render()
        btn = re.search(r"<button[^>]*>", html).group(0)
        assert 'type="button"' in btn
        assert 'aria-expanded="false"' in btn
        assert 'aria-controls="tip-x1"' in btn
        assert 'aria-label="Help: Base URL"' in btn
        note = re.search(r'<span class="tf-tip-bubble" id="tip-x1" hidden>(.*?)</span>', html)
        assert note, html
        # Autoescaped: the note is text, never markup.
        assert note.group(1) == "Note &lt;b&gt;text&lt;/b&gt;"
        assert 'role="tooltip"' not in html
        assert "data-tip" in html

    def test_the_label_is_localised(self, render):
        assert 'aria-label="Підказка: Base URL"' in render("ua")


class _Dict(dict):
    def __getattr__(self, k):
        return self[k]

    def get(self, k, default=None):  # noqa: D401 — mirrors the i18n object
        return dict.get(self, k, default)
