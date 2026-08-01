"""An HTML entity written inside a Jinja expression renders literally.

Why this file exists: `templates/automation.html` shipped a default
string reading `this project&#39;s BDD test cases`. The entity was written
to avoid terminating the single-quoted Jinja literal around it — but
autoescape then escaped the `&` on the way out, so the page delivered
`&amp;#39;` and the browser drew `this project&#39;s` on screen, in the
first paragraph of the module.

Nothing caught it. The template compiled, every route test passed, and
the string only misbehaves once rendered — it was found by looking at the
deployed page. The fix is to quote the literal with `"` and type a real
apostrophe.

The rule is narrow on purpose: an entity in plain markup is correct and
common (`&amp;&amp;` inside a `<code>` block, `&nbsp;`), so only
occurrences **inside a `{{ ... }}` expression** are wrong.
"""
from __future__ import annotations

import pathlib
import re

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"

# Non-greedy so adjacent expressions on one line stay separate.
_EXPRESSION_RE = re.compile(r"\{\{.*?\}\}", re.S)
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def _templates() -> list[pathlib.Path]:
    return sorted(TEMPLATE_DIR.rglob("*.html"))


def test_there_are_templates_to_check() -> None:
    """Guard the guard: a bad glob would make this file silently vacuous."""
    assert len(_templates()) > 10


@pytest.mark.parametrize(
    "template", _templates(), ids=lambda p: p.name
)
def test_no_html_entity_inside_a_jinja_expression(
    template: pathlib.Path,
) -> None:
    source = template.read_text(encoding="utf-8")
    offenders = []
    for expression in _EXPRESSION_RE.finditer(source):
        for entity in _ENTITY_RE.finditer(expression.group(0)):
            line = source[: expression.start() + entity.start()].count("\n") + 1
            offenders.append(f"{template.name}:{line} — {entity.group(0)}")

    assert not offenders, (
        "HTML entity inside a Jinja expression; autoescape will escape the "
        "'&' again and the browser will draw the entity as text. Use a "
        'double-quoted literal and the real character instead:\n  '
        + "\n  ".join(offenders)
    )
