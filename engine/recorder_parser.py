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
from typing import List, Tuple

from engine.automation_qa import AutomationStep
from engine.locator_registry import (LocatorCandidate, candidates_to_targets,
                                      rank_candidates)


# Codegen action methods on a locator → internal action label.
#
# ``uncheck`` used to map onto ``check``, with a comment saying it
# carried value="false" "so the runner's single check-handling branch
# covers both". The branch reads no value, so it did not: a recorded
# uncheck replayed as a check, performing the opposite action and
# reporting success. A plan written in a comment is not a plan the code
# is following.
_ACTION_MAP = {
    "click": "click",
    "fill": "fill",
    "press": "press",
    "select_option": "select",
    "check": "check",
    "uncheck": "uncheck",
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

    # PR-C — codegen 1.40+ assertion toolbar emits
    #   ``await expect(page.get_by_role(...)).to_be_visible()``
    #   ``await expect(page).to_have_url("https://...")``
    #   ``await expect(page.get_by_text("X")).to_contain_text("X")``
    # The hot-key the plan asked for IS codegen's native "Assert" UI;
    # we ride on it instead of injecting our own overlay. Pattern: the
    # outermost call is a method on ``expect(...)`` — the inner call
    # carries the locator argument we need to render. Anything we don't
    # recognise falls through to the action dispatch below.
    if _is_expect_assertion(call):
        a_step = _build_assertion_step(call)
        if a_step is not None:
            steps.append(a_step)
        return

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
        candidates = _candidates_from_chain(call.func.value)
        primary, alternates = candidates_to_targets(candidates)
        # ``_render_locator`` is the source of truth for ``target`` —
        # it always produces a non-empty selector when the chain
        # bottoms out at ``page``. If candidate derivation produced a
        # different primary (rare: e.g. testid sorting wins over a
        # text leaf), trust the dedicated renderer to keep
        # PR-B's test golden contracts stable and use candidates
        # purely as additional alternates.
        if primary and primary != target:
            alternates = [primary] + [a for a in alternates if a != target]
        # Drop the primary if it slipped into alternates via dedup.
        alternates = [a for a in alternates if a and a != target]
        label = _label_from_chain(call.func.value)
        value = _first_string_arg(call)
        steps.append(AutomationStep(
            action=_ACTION_MAP[method],
            target=target,
            value=value,
            raw=ast.unparse(call),
            target_alternates=alternates,
            locator_label=label,
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


# ── PR-A: candidate enumeration (multi-locator fallback) ─────────

def _collect_chain(value_node: ast.AST) -> List[Tuple[str, ast.Call]]:
    """Walk a locator chain bottom→top, return [(method, call_node), ...]
    in outer-first order — i.e. the leaf the action was called on lands
    LAST. Mirrors :func:`_render_locator`'s traversal but keeps both the
    method name and the AST node around so candidate enumeration can
    pull arg / kwarg metadata. Returns ``[]`` when the chain doesn't
    root at ``page``."""
    parts: List[Tuple[str, ast.Call]] = []
    cur = value_node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        method = cur.func.attr
        if method in _LOCATOR_FACTORIES:
            parts.append((method, cur))
        cur = cur.func.value
    if not (isinstance(cur, ast.Name) and cur.id == "page"):
        return []
    return list(reversed(parts))


def _candidates_from_chain(value_node: ast.AST) -> List[LocatorCandidate]:
    """Derive every selector strategy reachable from a single codegen
    chain — without a Playwright runtime probe.

    The recorded primary ``page.get_by_role("button", name="Sign in")``
    yields:
        * ``role=button[name="Sign in"]`` (score 70, full)
        * ``role=button`` (score 70, role-only — relaxed)
        * ``text=Sign in`` (score 40, derived from the role-name)

    A chained ``page.locator("#bar").get_by_text("Hi")`` yields:
        * ``#bar >> text=Hi`` (score 40, full chain)
        * ``text=Hi`` (score 40, leaf-only)
        * ``#bar`` (score 90 if it starts with ``#``, the ``id`` strategy)

    Order doesn't need to be perfect here — the registry's
    :func:`engine.locator_registry.rank_candidates` re-sorts by score
    and dedups by value, so callers always see a clean ranked list.
    """
    parts = _collect_chain(value_node)
    if not parts:
        return []
    cands: List[LocatorCandidate] = []

    # Full chain (joined with " >> ") — what the runner uses today.
    joined = " >> ".join(_locator_part(m, c) for m, c in parts).strip()
    if joined:
        leaf_method, _leaf_call = parts[-1]
        leaf_strategy = _strategy_for_method(leaf_method, joined)
        # Boost the joined chain so it ALWAYS wins over a relaxed
        # variant of the same leaf — testers expect the recorded
        # exact match to be tried first when the recording is fresh.
        cands.append(LocatorCandidate(strategy=leaf_strategy,
                                       value=joined, score=999))

    # Leaf alone (when the chain has more than one segment) + role/text
    # relaxations off the leaf.
    leaf_method, leaf_call = parts[-1]
    leaf_value = _locator_part(leaf_method, leaf_call)
    if leaf_value and len(parts) > 1:
        cands.append(LocatorCandidate(
            strategy=_strategy_for_method(leaf_method, leaf_value),
            value=leaf_value,
        ))

    # role+name → role-only AND text=name fallbacks.
    if leaf_method == "get_by_role":
        name_kw = _kw_string(leaf_call, "name")
        role_arg = _first_string_arg(leaf_call)
        if name_kw and role_arg:
            cands.append(LocatorCandidate(
                strategy="role", value=f"role={role_arg}"))
            cands.append(LocatorCandidate(
                strategy="text", value=f"text={name_kw}"))

    # Each ancestor segment is also a usable selector on its own
    # (e.g. ``#bar`` from ``#bar >> text=Hi``). They generally won't
    # outrank the joined chain because their score is the strategy
    # baseline, but they give the runner *something* to try when the
    # leaf drifted and the chain no longer resolves.
    for method, call in parts[:-1]:
        seg = _locator_part(method, call)
        if seg:
            cands.append(LocatorCandidate(
                strategy=_strategy_for_method(method, seg), value=seg))

    return rank_candidates(cands)


def _strategy_for_method(method: str, value: str) -> str:
    """Map a codegen factory method back to a locator_registry strategy.

    ``locator(...)`` is the catch-all bucket — the parser doesn't know
    whether the literal selector inside is CSS, XPath, or a Playwright
    pseudo (``role=...``), so we sniff the value's prefix. Everything
    else maps straight from the factory name.
    """
    if method == "get_by_test_id":
        return "testid"
    if method == "get_by_role":
        return "role"
    if method == "get_by_label":
        return "label"
    if method == "get_by_placeholder":
        return "placeholder"
    if method == "get_by_text":
        return "text"
    if method == "get_by_alt_text":
        return "alt"
    if method == "get_by_title":
        return "title"
    # method == "locator" → sniff the raw selector.
    v = value.lstrip()
    if v.startswith("#") or v.startswith("id="):
        return "id"
    if v.startswith("//") or v.startswith("xpath="):
        return "xpath"
    return "css"


def _label_from_chain(value_node: ast.AST) -> str:
    """Stable identifier for the registry: same recorded element across
    different runs → same label. We use the leaf segment only, because
    the leaf is what actually identifies the control — the chain ancestor
    is a scope hint, not the element's identity. A role+name leaf yields
    ``"role=button:Sign in"``; a testid leaf yields ``"testid=submit"``.

    Returns ``""`` when the chain is empty or doesn't root at ``page``;
    callers (and the runner) treat an empty label as "skip registry
    tracking entirely" — matches what text-authored TCs do too.
    """
    parts = _collect_chain(value_node)
    if not parts:
        return ""
    leaf_method, leaf_call = parts[-1]
    arg0 = _first_string_arg(leaf_call)
    name_kw = _kw_string(leaf_call, "name")
    if leaf_method == "get_by_role" and arg0:
        if name_kw:
            return f"role={arg0}:{name_kw}"
        return f"role={arg0}"
    if leaf_method == "get_by_test_id" and arg0:
        return f"testid={arg0}"
    if leaf_method == "get_by_label" and arg0:
        return f"label={arg0}"
    if leaf_method == "get_by_placeholder" and arg0:
        return f"placeholder={arg0}"
    if leaf_method == "get_by_text" and arg0:
        return f"text={arg0}"
    if leaf_method == "get_by_alt_text" and arg0:
        return f"alt={arg0}"
    if leaf_method == "get_by_title" and arg0:
        return f"title={arg0}"
    if leaf_method == "locator" and arg0:
        return f"locator={arg0}"
    return ""


# ── PR-C: assertion capture (codegen toolbar → AutomationStep) ──

# Map a codegen ``expect(...).<matcher>()`` call to our assertion_type.
# ``to_have_url`` / ``to_have_url`` accept a string or a regex — the
# parser keeps the raw string verbatim and the runner glob-matches it.
_TEXT_MATCHERS = {"to_contain_text", "to_have_text"}
_VISIBLE_MATCHERS = {"to_be_visible", "to_be_attached", "to_be_in_viewport"}
_URL_MATCHERS = {"to_have_url"}


def _is_expect_assertion(call: ast.Call) -> bool:
    """True when ``call`` is the outer ``.to_*()`` of an expect-chain.

    Recognises both ``expect(...).to_be_visible()`` and the negated
    ``expect(...).not_.to_be_visible()`` form — negation is deferred
    (no runtime support yet), but we still record it as an assertion
    so the editor can show it instead of silently dropping the step.
    """
    if not isinstance(call.func, ast.Attribute):
        return False
    matcher = call.func.attr
    if matcher not in (_TEXT_MATCHERS | _VISIBLE_MATCHERS | _URL_MATCHERS):
        return False
    # Walk down through .not_ / Attribute chains to find expect(...).
    cur = call.func.value
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if not (isinstance(cur, ast.Call)
            and isinstance(cur.func, ast.Name)
            and cur.func.id == "expect"):
        return False
    return True


def _build_assertion_step(call: ast.Call) -> AutomationStep | None:
    """Turn an ``expect(LOCATOR).to_*()`` call into an AutomationStep.

    Returns ``None`` when the locator chain inside ``expect`` doesn't
    root at ``page`` (defensive against scaffolding the parser doesn't
    own). ``to_have_url`` is special-cased because its inner argument is
    ``page`` rather than a locator factory chain — we read the URL
    pattern off the matcher's first positional arg.
    """
    matcher = call.func.attr  # type: ignore[attr-defined]

    # Find the ``expect(LOCATOR)`` call by walking attribute chain.
    cur = call.func.value  # type: ignore[attr-defined]
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if not (isinstance(cur, ast.Call)
            and isinstance(cur.func, ast.Name)
            and cur.func.id == "expect"
            and cur.args):
        return None
    inner = cur.args[0]
    raw_src = ast.unparse(call)

    if matcher in _URL_MATCHERS:
        # expect(page).to_have_url("...") — pattern is the first arg.
        pattern = _first_string_arg(call)
        return AutomationStep(
            action="expect_url",
            target=pattern,
            value=pattern,
            raw=raw_src,
            kind="assertion",
            assertion_type="url",
        )

    # For visible / text the inner expression is a locator chain.
    if isinstance(inner, ast.Call):
        target = _render_locator(inner)
        if not target:
            return None
        cands = _candidates_from_chain(inner)
        primary, alternates = candidates_to_targets(cands)
        if primary and primary != target:
            alternates = [primary] + [a for a in alternates if a != target]
        alternates = [a for a in alternates if a and a != target]
        label = _label_from_chain(inner)
    else:
        target = ""
        alternates = []
        label = ""

    if matcher in _VISIBLE_MATCHERS:
        return AutomationStep(
            action="expect_visible",
            target=target,
            raw=raw_src,
            target_alternates=alternates,
            locator_label=label,
            kind="assertion",
            assertion_type="visible",
        )

    # _TEXT_MATCHERS — value carries the expected substring.
    expected = _first_string_arg(call)
    return AutomationStep(
        action="expect_text",
        target=target,
        value=expected,
        raw=raw_src,
        target_alternates=alternates,
        locator_label=label,
        kind="assertion",
        assertion_type="text",
    )
