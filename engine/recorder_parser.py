"""Parse Playwright codegen --target python-async output into AutomationStep[].

Codegen emits one expression statement per recorded action:

    await page.goto("https://app.example.com/login")
    await page.get_by_label("Email").fill("user@example.com")
    await page.get_by_role("button", name="Sign in").click()
    await page.locator("#sidebar").get_by_text("Settings").click()

The parser walks the AST in source order, ignores browser-lifecycle calls
(launch / new_context / new_page / context.close / browser.close), and reduces
each recorded action to one :class:`engine.automation_qa.AutomationStep`. The
locator chain is reconstructed into a single Playwright selector string so the
runner can hand it straight to ``page.locator(target)``.

Pure function, no Playwright runtime required — safe to import in tests.
"""
from __future__ import annotations

import ast
from typing import List

from engine.automation_qa import AutomationStep


# Codegen action methods on a locator → internal action label.
# uncheck is rare; map it to ``check`` with value="false" so the runner's
# single check-handling branch covers both.
_ACTION_MAP = {
    "click": "click",
    "fill": "fill",
    "press": "press",
    "select_option": "select",
    "check": "check",
    "uncheck": "check",
}

# Navigation methods called directly on ``page``.
_NAV_METHODS = {"goto"}

# Locator factory methods on ``page`` (and chainable on locators).
_LOCATOR_FACTORIES = {
    "locator",
    "get_by_role",
    "get_by_label",
    "get_by_placeholder",
    "get_by_text",
    "get_by_test_id",
    "get_by_alt_text",
    "get_by_title",
}


def parse_codegen_output(src: str) -> List[AutomationStep]:
    """Parse codegen Python source into ordered AutomationStep[].

    Silently returns ``[]`` on a SyntaxError so an in-progress recording
    that crashed mid-write doesn't blow up the CLI — callers can decide
    whether an empty result is an error condition.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    steps: List[AutomationStep] = []
    _walk(tree, steps)
    return steps


def _walk(node: ast.AST, steps: List[AutomationStep]) -> None:
    """Walk children in source order, recursing into function / with / try
    bodies. Each Expr we encounter is offered to :func:`_process_expr`."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.With, ast.AsyncWith,
                              ast.If, ast.Try, ast.Module)):
            _walk(child, steps)
        elif isinstance(child, ast.Expr):
            _process_expr(child, steps)


def _process_expr(expr: ast.Expr, steps: List[AutomationStep]) -> None:
    call = expr.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return
    if not isinstance(call.func, ast.Attribute):
        return
    method = call.func.attr

    if method in _NAV_METHODS and _is_page_root(call.func.value):
        url = _first_string_arg(call)
        if url:
            steps.append(AutomationStep(
                action="goto",
                target=url,
                raw=ast.unparse(call),
            ))
        return

    if method in _ACTION_MAP:
        target = _render_locator(call.func.value)
        if not target:
            return
        value = _first_string_arg(call)
        if method == "uncheck":
            value = "false"
        steps.append(AutomationStep(
            action=_ACTION_MAP[method],
            target=target,
            value=value,
            raw=ast.unparse(call),
        ))


def _is_page_root(value_node: ast.AST) -> bool:
    """True if the locator chain bottoms out at the bare ``page`` name."""
    cur = value_node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        cur = cur.func.value
    return isinstance(cur, ast.Name) and cur.id == "page"


def _first_string_arg(call: ast.Call) -> str:
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return ""


def _kw_string(call: ast.Call, key: str) -> str:
    for kw in call.keywords:
        if (kw.arg == key
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)):
            return kw.value.value
    return ""


def _render_locator(value_node: ast.AST) -> str:
    """Convert a locator chain AST back to a single Playwright selector.

    Examples::

        page.get_by_role("button", name="Login")  → 'role=button[name="Login"]'
        page.get_by_test_id("submit")             → 'data-testid=submit'
        page.get_by_label("Email")                → 'label=Email'
        page.get_by_placeholder("Search")         → 'placeholder=Search'
        page.get_by_text("Welcome")               → 'text=Welcome'
        page.locator("css=.foo")                  → 'css=.foo'
        page.locator("#bar").get_by_text("Hi")    → '#bar >> text=Hi'

    Walks outermost-in (which is right-to-left in source order), then
    reverses so the rendered selector matches what a tester would type.
    Returns "" if the chain does not bottom out at ``page`` — defensive
    against scaffolding lines that hold no recorded action.
    """
    parts: List[str] = []
    cur = value_node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        method = cur.func.attr
        if method in _LOCATOR_FACTORIES:
            parts.append(_locator_part(method, cur))
        cur = cur.func.value
    if not (isinstance(cur, ast.Name) and cur.id == "page"):
        return ""
    return " >> ".join(reversed(parts))


def _locator_part(method: str, call: ast.Call) -> str:
    arg0 = _first_string_arg(call)
    name_kw = _kw_string(call, "name")
    if method == "locator":
        return arg0
    if method == "get_by_test_id":
        return f"data-testid={arg0}"
    if method == "get_by_role":
        if name_kw:
            return f'role={arg0}[name="{name_kw}"]'
        return f"role={arg0}"
    if method == "get_by_text":
        return f"text={arg0}"
    if method == "get_by_label":
        return f"label={arg0}"
    if method == "get_by_placeholder":
        return f"placeholder={arg0}"
    if method == "get_by_alt_text":
        return f"alt={arg0}"
    if method == "get_by_title":
        return f"title={arg0}"
    return arg0
