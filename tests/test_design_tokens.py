"""Regression guards for the design tokens and their accessibility contract.

The audit that introduced these tokens found the brand coral (#FF865C)
carrying white button text at 2.38:1, input borders at 1.24:1, focus rings
removed with ``outline: none`` and nothing put back, and 32 buttons sized
by hand with inline padding. Each guard below pins one of those shut:

(a) every text/fill pair built from the :root tokens meets WCAG AA
    (4.5:1), and non-text indicators — control borders, focus — meet 3:1;
(b) no ``.btn`` element sets padding or font-size inline (sizes come from
    .btn-sm / .btn-lg / .btn-xs);
(c) style.css never removes an outline without a :focus-visible rule for
    the same selector;
(d) templates carry no <style> blocks beyond a named allow-list.

Every checker is also run against a synthetic violation, so a guard that
silently stopped matching anything would fail rather than pass.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / "static" / "css" / "style.css"
TEMPLATES = ROOT / "templates"


# ── helpers ───────────────────────────────────────────────────────────

def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _root_tokens(css: str) -> dict[str, str]:
    """Custom properties from the top-level :root block(s), later wins."""
    css = _strip_comments(css)
    tokens: dict[str, str] = {}
    depth = 0
    i = 0
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        elif depth == 0 and css.startswith(":root", i):
            j = css.index("{", i)
            if css[i:j].strip() == ":root":
                k = css.index("}", j)
                for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css[j + 1:k]):
                    tokens[name] = value.strip()
                i = k
        i += 1
    return tokens


def _resolve(value: str, tokens: dict[str, str], depth: int = 0) -> str:
    if depth > 10:
        raise ValueError(f"var() cycle resolving {value}")
    m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^)]+))?\)", value.strip())
    if not m:
        return value.strip()
    name, fallback = m.group(1), m.group(2)
    if name in tokens:
        return _resolve(tokens[name], tokens, depth + 1)
    if fallback:
        return _resolve(fallback, tokens, depth + 1)
    raise KeyError(name)


def _hex(value: str) -> str:
    v = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", v):
        raise ValueError(f"not a hex colour: {value!r}")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return v.upper()


def _luminance(hex_colour: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    h = _hex(hex_colour)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tokens(css) -> dict[str, str]:
    return _root_tokens(css)


def colour(ref: str, tokens: dict[str, str]) -> str:
    """A literal hex, or a token name resolved through var() chains."""
    if ref.startswith("--"):
        return _resolve(f"var({ref})", tokens)
    return ref


# ── (a) contrast ──────────────────────────────────────────────────────

WHITE = "#FFFFFF"

#: (foreground, background, what it is). 4.5:1 — text.
TEXT_PAIRS = [
    (WHITE, "--accent", "white text on .btn-primary / active chip / lang toggle"),
    (WHITE, "--accent-hover", "white text on .btn-primary:hover"),
    (WHITE, "--primary", "white text on anything still using --primary"),
    (WHITE, "--success", "white text on .btn-success"),
    (WHITE, "--success-hover", "white text on .btn-success:hover"),
    (WHITE, "--danger", "white text on .btn-danger"),
    (WHITE, "--danger-hover", "white text on .btn-danger:hover"),
    ("--accent", WHITE, "links and accent text on white"),
    ("--accent", "--tf-bg", "links on the page background"),
    ("--accent", "--accent-soft", "accent text on the soft accent tint (chips, hover)"),
    ("--primary", WHITE, "anything coloured with --primary"),
    ("--text-subtle", WHITE, "hints and secondary text"),
    ("--text-subtle", "--tf-bg", "hints on the page background"),
    ("--text-light", WHITE, "legacy --text-light"),
    ("--text-muted", WHITE, "muted text"),
    ("--text-muted", "--tf-bg", "muted text and hints on the page background"),
    ("--warning", WHITE, "warning text"),
    ("--warning", "--warning-light", "warning text on its tint"),
    ("--badge-amber", "#FFFBEB", "amber badge"),
    ("--badge-orange", "#FFF7ED", "orange badge"),
    ("--badge-red", "#FEF2F2", "red badge"),
    ("--badge-emerald", "#ECFDF5", "emerald badge"),
    ("--badge-green", "#F0FDF4", "green badge"),
    ("--badge-grey", "#F3F4F6", "grey badge"),
    # Status families moved out of inline styles (block B).
    ("--text-strong", WHITE, "strong text on white"),
    ("--badge-grey", "--tf-bg", "slate secondary text on the page background"),
    ("--amber-text", "--warning-light", "amber banner and badge text"),
    ("--amber-text", "--amber-bg", "amber text at the light end of the banner"),
    ("--amber-text-strong", "--warning-light", "amber banner body text"),
    ("--info-text", "--tf-bg", "info callout title"),
    ("--info-text-strong", "--tf-bg", "info callout body and active-run hint"),
    ("--success-text", "--success-light", "running pill"),
    ("--violet-text", "--violet-bg", "violet item badge"),
    ("--indigo-text", "--indigo-bg", "done pill"),
    ("--danger-text", "--danger-light", "failed pill"),
]

#: 3:1 — WCAG 1.4.11 non-text contrast.
NON_TEXT_PAIRS = [
    ("--border-control", WHITE, "input / outline-button border on white"),
    ("--border-control", "--tf-bg", "input border on the page background"),
    ("--focus", WHITE, "focus ring on white"),
    ("--focus", "--tf-bg", "focus ring on the page background"),
    ("--focus-on-dark", "#1A1D24", "focus ring in the dark sidebar"),
]


@pytest.mark.parametrize("fg,bg,what", TEXT_PAIRS, ids=[p[2] for p in TEXT_PAIRS])
def test_text_pairs_meet_aa(tokens, fg, bg, what):
    ratio = contrast(colour(fg, tokens), colour(bg, tokens))
    assert ratio >= 4.5, f"{what}: {fg} on {bg} is {ratio:.2f}:1, needs 4.5:1"


@pytest.mark.parametrize("fg,bg,what", NON_TEXT_PAIRS, ids=[p[2] for p in NON_TEXT_PAIRS])
def test_indicators_meet_non_text_contrast(tokens, fg, bg, what):
    ratio = contrast(colour(fg, tokens), colour(bg, tokens))
    assert ratio >= 3.0, f"{what}: {fg} on {bg} is {ratio:.2f}:1, needs 3:1"


def test_there_is_exactly_one_root_block(css):
    """The legacy and brand layers each had a :root; two token sources is
    how --primary ended up pointing at a colour nobody had checked."""
    tops = re.findall(r"(?m)^:root\s*\{", _strip_comments(css))
    assert len(tops) == 1, f"{len(tops)} top-level :root blocks"


def test_primary_is_the_accessible_accent_not_coral(tokens):
    assert _hex(colour("--primary", tokens)) == _hex(colour("--accent", tokens))
    assert _hex(colour("--accent", tokens)) != "FF865C"


def test_the_contrast_maths_is_right():
    # Reference values from the WCAG definition.
    assert contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast("#FFFFFF", "#FF865C") == pytest.approx(2.38, abs=0.01)
    assert contrast("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.01)


def test_the_token_parser_sees_the_tokens(tokens):
    # If the parser silently returned {}, every pair above would KeyError —
    # but pin the shape anyway so a renamed token fails loudly here.
    for name in ("--accent", "--success", "--danger", "--text-subtle",
                 "--border-control", "--focus", "--space-1", "--space-6"):
        assert name in tokens, name


def test_spacing_scale(tokens):
    assert [tokens[f"--space-{i}"] for i in range(1, 7)] == [
        "4px", "8px", "12px", "16px", "24px", "32px"]


# ── (b) no inline sizing on buttons ───────────────────────────────────

_TAG = re.compile(r"<(\w+)\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
_CLASS = re.compile(r"\bclass\s*=\s*\"([^\"]*)\"", re.S)
_STYLE = re.compile(r"\bstyle\s*=\s*\"([^\"]*)\"", re.S)


_INPUT_BUTTON = re.compile(r"\btype\s*=\s*\"(?:submit|button|reset)\"", re.I)


def _is_button(tag: str, attrs: str) -> bool:
    """A <button>, an input that renders as one, or anything wearing .btn.

    The class alone was the test until 2026-10-14, so an unclassed
    <button style="padding:5px 12px…"> (estimation's Override) passed the
    check it was the example of."""
    tag = tag.lower()
    if tag == "button":
        return True
    if tag == "input" and _INPUT_BUTTON.search(attrs):
        return True
    c = _CLASS.search(attrs)
    return bool(c and "btn" in c.group(1).split())


def inline_sized_buttons(html: str) -> list[str]:
    found = []
    for m in _TAG.finditer(html):
        attrs = m.group(2)
        s = _STYLE.search(attrs)
        if not s or not _is_button(m.group(1), attrs):
            continue
        if re.search(r"\b(padding|font-size|height)\s*:", s.group(1)):
            line = html.count("\n", 0, m.start()) + 1
            found.append(f"line {line}: {' '.join(s.group(1).split())}")
    return found


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_button_is_sized_inline():
    offenders = {}
    for path in _templates():
        hits = inline_sized_buttons(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, (
        "use .btn-sm / .btn-lg / .btn-xs instead of inline padding or "
        f"font-size on a .btn: {offenders}")


def test_the_inline_sizing_check_catches_a_violation():
    bad = '<a href="#"\n   class="btn btn-primary"\n   style="font-size:0.9em;">Go</a>'
    assert inline_sized_buttons(bad)
    # …and does not flag a non-button or a button styled for layout only.
    assert not inline_sized_buttons('<span class="badge" style="padding:2px">x</span>')
    assert not inline_sized_buttons('<button class="btn" style="margin-top:8px">x</button>')
    # btn-sm is not "btn"
    assert not inline_sized_buttons('<i class="btn-icon" style="font-size:2em"></i>')
    # …but a button is a button with or without the class.
    assert inline_sized_buttons(
        '<button type="button" id="x"\n  style="padding:5px 12px;">Override</button>')
    assert inline_sized_buttons('<input type="submit" style="font-size:.85em">')
    assert not inline_sized_buttons('<input type="text" style="font-size:.85em">')


# ── (c) never remove an outline without putting one back ──────────────

def _rules(css: str):
    """(selector list, body) for every rule, @media bodies included."""
    css = _strip_comments(css)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = m.group(1).strip()
        if selector.startswith("@"):
            selector = selector.split("{")[-1].strip()
        out.append(([s.strip() for s in selector.split(",") if s.strip()], m.group(2)))
    return out


def outline_removals_without_replacement(css: str) -> list[str]:
    rules = _rules(css)
    with_ring = set()
    for selectors, body in rules:
        if re.search(r"\boutline\s*:\s*(?!none\b|0\b)[^;]+", body):
            with_ring.update(s for s in selectors if ":focus-visible" in s)
    global_ring = ":focus-visible" in with_ring
    bad = []
    for selectors, body in rules:
        if not re.search(r"\boutline\s*:\s*(none|0)\s*(;|$|!)", body):
            continue
        for sel in selectors:
            if ":focus-visible" in sel:
                bad.append(sel)          # removing the ring itself
                continue
            base = re.sub(r":focus(-within)?\b", "", sel).strip()
            if global_ring and ":focus" not in sel:
                # A non-focus rule (e.g. a reset) is still overridden by the
                # global :focus-visible ring, which has the same specificity
                # as the element's own :focus-visible state.
                continue
            if f"{base}:focus-visible" not in with_ring:
                bad.append(sel)
    return bad


def test_no_outline_is_removed_without_a_focus_visible_replacement(css):
    bad = outline_removals_without_replacement(css)
    assert not bad, f"`outline: none` with no :focus-visible replacement: {bad}"


def test_there_is_a_global_focus_ring(css):
    rules = _rules(css)
    assert any(":focus-visible" in sels and re.search(r"outline\s*:\s*2px solid var\(--focus\)", body)
               for sels, body in rules), "global :focus-visible ring missing"


def test_the_outline_check_catches_a_violation():
    bad = ".x:focus { outline: none; border-color: red; }"
    assert outline_removals_without_replacement(bad) == [".x:focus"]
    good = bad + "\n.x:focus-visible { outline: 2px solid blue; }"
    assert outline_removals_without_replacement(good) == []
    zero = ".y:focus{outline:0}"
    assert outline_removals_without_replacement(zero) == [".y:focus"]


# ── (d) no page-level <style> blocks ──────────────────────────────────

#: template -> why its <style> block has to stay in the page.
STYLE_BLOCK_ALLOWLIST = {
    "bug_reports.html": (
        "tests/test_bulk_bug_ops.py pins the .bulk-toolbar [hidden] rules in "
        "the rendered page body"),
    "test_cases.html": (
        "tests/test_recorder_ui.py pins the .ext-recorder-modal [hidden] rules "
        "in the rendered page body"),
}

_STYLE_BLOCK = re.compile(r"<style\b", re.I)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def style_blocks(html: str) -> int:
    return len(_STYLE_BLOCK.findall(_JINJA_COMMENT.sub("", html)))


def test_no_style_blocks_outside_the_allowlist():
    offenders = [p.name for p in _templates()
                 if style_blocks(p.read_text(encoding="utf-8"))
                 and p.name not in STYLE_BLOCK_ALLOWLIST]
    assert not offenders, (
        f"move these <style> blocks into static/css/style.css: {offenders}")


@pytest.mark.parametrize("name", sorted(STYLE_BLOCK_ALLOWLIST))
def test_allowlisted_style_blocks_stay_small_and_hidden_only(name):
    """An allow-list entry is for the [hidden]-pattern rules a test pins
    in the page, not a place to grow page styling again."""
    html = _JINJA_COMMENT.sub("", (TEMPLATES / name).read_text(encoding="utf-8"))
    blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.S | re.I)
    assert len(blocks) == 1, f"{name}: expected one allow-listed block, found {len(blocks)}"
    selectors = [s for sels, _ in _rules(blocks[0]) for s in sels]
    assert any("[hidden]" in s for s in selectors), f"{name}: allow-list is stale"
    assert len(selectors) <= 8, f"{name}: {len(selectors)} rules — move them to style.css"


def test_the_style_block_check_catches_a_violation():
    assert style_blocks("<div></div>\n<style>.a{}</style>") == 1
    assert style_blocks("{# a <style> mention in a comment #}") == 0


# ── (e) every custom property used is defined ─────────────────────────

_VAR_USE = re.compile(r"var\(\s*(--[\w-]+)")
_VAR_DEF = re.compile(r"(--[\w-]+)\s*:")


def _sources():
    return ([CSS_PATH] + _templates()
            + sorted((ROOT / "static" / "js").glob("*.js")))


def undefined_properties(texts: list[str]) -> set[str]:
    defined: set[str] = set()
    used: set[str] = set()
    for text in texts:
        defined |= set(_VAR_DEF.findall(text))
        used |= set(_VAR_USE.findall(text))
    return used - defined


def test_no_custom_property_is_used_without_a_definition():
    """``var(--tf-primary, #FF865C)`` read like the accessible primary and
    rendered the coral it was meant to retire, because --tf-primary was
    never defined; ``var(--color-text-muted, #888)`` rendered 3.5:1 text
    the same way (2026-10-14). A fallback is not a definition."""
    missing = undefined_properties(
        [p.read_text(encoding="utf-8") for p in _sources()])
    assert not missing, f"used but never defined: {sorted(missing)}"


def test_the_undefined_property_check_catches_a_violation():
    assert undefined_properties(
        [":root{--a:1px}", "x{color:var(--b, #888)}"]) == {"--b"}
    assert not undefined_properties([":root{--a:1px}", "x{gap:var(--a)}"])


# ── (f) no low-contrast text colour hard-coded in a page ──────────────

_STYLE_ATTR = re.compile(r"\bstyle\s*=\s*\"([^\"]*)\"", re.S)
_COLOUR_LITERAL = re.compile(
    r"(?<![-\w])color\s*:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b")


def _hex6(h: str) -> str:
    h = h.lstrip("#")
    return "#" + (h if len(h) == 6 else "".join(c * 2 for c in h))


def low_contrast_inline_colours(html: str,
                                backgrounds=(WHITE,)) -> list[str]:
    """Inline text colours under 4.5:1 on any of *backgrounds*, in a style
    attribute that does not also set its own background (a dark chip may
    use light text)."""
    found = []
    for m in _STYLE_ATTR.finditer(html):
        style = m.group(1)
        if "background" in style:
            continue
        for c in _COLOUR_LITERAL.finditer(style):
            ratio = min(contrast(_hex6(c.group(1)), bg) for bg in backgrounds)
            if ratio < 4.5:
                line = html.count("\n", 0, m.start()) + 1
                found.append(f"line {line}: {c.group(1)} is {ratio:.2f}:1")
    return found


def test_no_page_hard_codes_a_low_contrast_text_colour(tokens):
    """#64748b was typed into 26 places; it passes on white (4.76:1) and
    fails on the page background (4.48:1) — the one failure a live scan
    of twelve pages found. So both backgrounds are checked."""
    backgrounds = (WHITE, colour("--tf-bg", tokens))
    offenders = {}
    for path in _templates():
        hits = low_contrast_inline_colours(
            path.read_text(encoding="utf-8"), backgrounds)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, f"use var(--text-muted) or darker: {offenders}"


def test_the_inline_colour_check_catches_a_violation():
    assert low_contrast_inline_colours('<p style="color:#888;">x</p>')
    # Fine on white, not on the grey page background.
    assert not low_contrast_inline_colours('<p style="color:#64748b">x</p>')
    assert low_contrast_inline_colours('<p style="color:#64748b">x</p>',
                                       (WHITE, "#F7F8FA"))
    assert not low_contrast_inline_colours('<p style="color:#475569">x</p>')
    assert not low_contrast_inline_colours(
        '<pre style="background:#0f172a;color:#e2e8f0">x</pre>')


# ── (g) one gap between buttons ───────────────────────────────────────

#: Containers that lay buttons out in a row.
BUTTON_ROWS = (".btn-group", ".form-actions", ".dashboard-actions", ".te-tabs")


@pytest.mark.parametrize("selector", BUTTON_ROWS)
def test_button_rows_share_one_gap(css, selector):
    """The live pages spaced side-by-side buttons 6, 8, 12 or 14 px apart
    depending on which container held them (2026-10-14)."""
    gaps = [m.group(1).strip() for sels, body in _rules(css)
            if selector in sels
            for m in [re.search(r"(?<![-\w])gap\s*:\s*([^;]+)", body)] if m]
    assert gaps, f"{selector} sets no gap"
    assert all(g == "var(--gap-buttons)" for g in gaps), (selector, gaps)


def test_the_button_gap_is_on_the_spacing_scale(tokens):
    assert tokens["--gap-buttons"] in {
        f"var(--space-{i})" for i in range(1, 7)}


# ── (h) the sidebar model switcher is a compact control ───────────────

def _body(css: str, selector: str) -> str:
    return " ".join(body for sels, body in _rules(css) if selector in sels)


def test_the_model_switcher_is_compact_and_clear_of_its_status_line(css):
    """The switcher used the 43 px form select and sat flush on the
    "Now: claude-haiku-5-5" line, so its focus ring (2 px + 2 px offset)
    covered the text (owner screenshot, 2026-10-15). It takes the small
    control height, and the block spaces its children further apart than
    the ring reaches."""
    box = _body(css, ".sidebar-model")
    assert re.search(r"display\s*:\s*flex", box), box
    assert re.search(r"gap\s*:\s*var\(--space-2\)", box), box
    sel = _body(css, ".sidebar-model select")
    assert re.search(r"(?<![-\w])height\s*:\s*var\(--control-sm\)", sel), sel


# ── (i) inline styles only go down (block B, 2026-10-15) ──────────────
#
# 430 style="" attributes in 16 templates were moved into style.css one
# template at a time. Each template's count may only fall: a count above
# its ceiling fails, and so does one below it — lower the number here in
# the same commit, so the ceiling follows the work down and never leaves
# room to creep back. Templates not listed have a ceiling of 0.
#
# Dynamic values (a progress bar's width) stay inline by design; they are
# counted like the rest and are why a ceiling can end above zero.

INLINE_STYLE_CEILING = {
    "_project_picker.html": 11,
    "automation.html": 4,
    "bug_reports.html": 27,
    "checklist.html": 9,
    "estimation.html": 34,
    "guide.html": 8,
    "index.html": 34,
    "org_settings.html": 2,
    "review_session.html": 59,
    "test_cases.html": 68,
    "test_execution_live.html": 51,
    "test_execution_manual.html": 11,
    "test_execution_runs.html": 2,
    "guide/_sections_en.html": 2,
    "guide/_sections_ua.html": 2,
}

#: Script-side styling the same way: el.style.display / .color /
#: .background written from JS. Visibility goes through el.hidden (with
#: the global [hidden] rule), colours through classes.
JS_STYLE_CEILING = {
    "static/js/app.js": 1,
    "templates/checklist.html": 8,
    "templates/estimation.html": 6,
    "templates/guide.html": 2,
    "templates/review_session.html": 1,
    "templates/test_cases.html": 14,
    "templates/test_execution_live.html": 8,
}

_INLINE_STYLE = re.compile(r"""\sstyle\s*=\s*["']""", re.I)
_JS_STYLE = re.compile(r"\.style\.(?:display|color|background(?:Color)?)\s*=")


def inline_styles(html: str) -> int:
    return len(_INLINE_STYLE.findall(_JINJA_COMMENT.sub("", html)))


def js_style_writes(src: str) -> int:
    return len(_JS_STYLE.findall(src))


def _rel(path: pathlib.Path, base: pathlib.Path) -> str:
    return path.relative_to(base).as_posix()


def test_inline_styles_never_rise_and_the_ceiling_follows_them_down():
    over, stale = {}, {}
    for path in _templates():
        name = _rel(path, TEMPLATES)
        n = inline_styles(path.read_text(encoding="utf-8"))
        cap = INLINE_STYLE_CEILING.get(name, 0)
        if n > cap:
            over[name] = (n, cap)
        elif n < cap:
            stale[name] = (n, cap)
    assert not over, f"inline style=\"\" above the ceiling (count, cap): {over}"
    assert not stale, (
        f"fewer inline styles than the ceiling — lower it to the count: {stale}")


def test_js_style_writes_never_rise_and_the_ceiling_follows_them_down():
    files = sorted((ROOT / "static" / "js").rglob("*.js")) + _templates()
    over, stale = {}, {}
    for path in files:
        name = _rel(path, ROOT)
        n = js_style_writes(path.read_text(encoding="utf-8"))
        cap = JS_STYLE_CEILING.get(name, 0)
        if n > cap:
            over[name] = (n, cap)
        elif n < cap:
            stale[name] = (n, cap)
    assert not over, f".style.display/.color/.background above the ceiling: {over}"
    assert not stale, f"lower these JS ceilings to the count: {stale}"


def test_the_ratchets_count_what_they_claim():
    """The counters on a synthetic page, so a regex that stopped matching
    would fail here instead of passing every template at zero."""
    assert inline_styles('<p style="margin:0">a</p><b STYLE = \'x\'>') == 2
    assert inline_styles('{# <p style="x"> #}<p data-style="x">') == 0
    assert js_style_writes("el.style.display = 'none'; el.style.color='#000';"
                           " el.style.backgroundColor = x; el.hidden = true;") == 3
    # The scan reaches real files: the ceilings sum to what is on disk.
    assert sum(inline_styles(p.read_text(encoding="utf-8"))
               for p in _templates()) == sum(INLINE_STYLE_CEILING.values())


def test_test_execution_has_no_inline_styles_left():
    html = (TEMPLATES / "test_execution.html").read_text(encoding="utf-8")
    assert inline_styles(html) == 0
    js = (ROOT / "static" / "js" / "test-execution.js").read_text(encoding="utf-8")
    assert ".style." not in js


def test_the_hidden_attribute_always_hides(css):
    """JS toggles visibility with el.hidden now; a class that sets display
    would otherwise outrank the UA [hidden] rule and keep the element on
    screen (bitten three times — see the bulk-bar and recorder modals)."""
    # The selector is [hidden] alone — ".tf-hints [hidden]" and friends
    # are scoped rules and do not count.
    assert re.search(r"(?:^|\})\s*\[hidden\]\s*\{\s*display\s*:\s*none\s*!important",
                     _strip_comments(css)), "global [hidden] rule missing"
