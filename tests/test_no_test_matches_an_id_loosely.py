"""``f"#{run_id}" in page`` is not an assertion about the page.

Third occurrence, so it gets a rule. An empty page in this product already
contains, measured on `/test-execution/runs`:

    #0        from the CSS colour ``#0f172a``
    #64748    from the CSS colour ``#64748b``
    #34       from ``&#34;``  — the quote entity in the chat greeting
    #39       from ``&#39;``  — the apostrophe entity

A fresh scratch database hands out single-digit ids, and ``"#3" in "#34"``
and ``"#6" in "#64748b"`` are both true. So the substring form fails in
**both** directions:

* ``in page`` passes when the page lists nothing;
* ``not in page`` fails when the page is correct.

The second is what happened: ``tests/test_runs_page_picker.py`` flaked under
``-n auto`` on the run whose id landed on 3 or 6, and passed on a rerun. A
flake nobody chases is a test nobody believes.

``tests/test_execute_assignment.py::_rows`` solved this before and its
docstring names ``&#39;`` — "the test passed for years only because the full
suite had created enough runs to push the ids past two digits". Three other
call sites had not caught up: two of them mine, written after reading that
very note.

So the rule is derived rather than remembered: no test may interpolate a
value straight into a bare ``"#…"`` f-string. The alternatives are one line
— read the table cell (``<td>#(\\d+)</td>``) or include the text that
follows the id in the message under test (``#12 (tc_driven)``).

**AST, not a regex over the text.** The first version of this file searched
lines and flagged the three docstrings — including this one — that explain
why the shape is wrong. A docstring is a ``Constant``; the shape being
policed is a ``JoinedStr``, and nothing describing it in prose can be one.
"""
from __future__ import annotations

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent
SELF = pathlib.Path(__file__).name


def _is_bare_hash_fstring(node: ast.AST) -> bool:
    """``f"#{x}"`` and nothing else — no text after the interpolation.

    ``f"#{x} (mode)"`` and ``f"#{x}</td>"`` are the fixes, so they must
    not match: the trailing text is what makes them unambiguous.
    """
    if not isinstance(node, ast.JoinedStr) or len(node.values) != 2:
        return False
    head, tail = node.values
    return (isinstance(head, ast.Constant)
            and head.value == "#"
            and isinstance(tail, ast.FormattedValue))


def _offenders():
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == SELF:
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if _is_bare_hash_fstring(node):
                found.append(f"{path.name}:{node.lineno}")
    return found


class TestThePredicate:
    """One per shape, because a predicate that stopped matching would let
    the rule below pass on everything — which is the same failure the rule
    is about."""

    @staticmethod
    def _first_fstring(code: str):
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.JoinedStr):
                return node
        return None

    def test_it_catches_the_bare_shape(self):
        assert _is_bare_hash_fstring(
            self._first_fstring('x = f"#{run_id}"'))

    def test_it_ignores_a_trailing_word(self):
        assert not _is_bare_hash_fstring(
            self._first_fstring('x = f"#{blocker} (tc_driven)"'))

    def test_it_ignores_a_trailing_tag(self):
        assert not _is_bare_hash_fstring(
            self._first_fstring('x = f"#{run_id}</td>"'))

    def test_it_ignores_a_url(self):
        assert not _is_bare_hash_fstring(
            self._first_fstring('x = f"/runs/{run_id}"'))

    def test_it_ignores_a_plain_string(self):
        assert not _is_bare_hash_fstring(ast.parse('x = "#{run_id}"').body[0])


def test_no_test_interpolates_a_value_into_a_bare_hash():
    offenders = _offenders()
    assert not offenders, (
        'these compare a value to a page as f"#{...}", which matches the '
        "&#34; / &#39; entities in the chat greeting and the CSS colours "
        "#0f172a / #64748b — so it can pass on a page that lists nothing "
        "and fail on a page that is right. Read the table cell, or "
        "include the text that follows the id: " + ", ".join(offenders))


def test_the_scan_reads_the_suite():
    """A scan that finds no files passes every assertion about what it
    finds — the lesson from the control-byte gate, which skipped the whole
    tree on its first run."""
    files = list(TESTS.glob("test_*.py"))
    assert len(files) > 100, f"only {len(files)} test files found"
    assert any(p.name == "test_execute_assignment.py" for p in files)
