"""The phone-width navigation drawer (UI review, T8 polish).

At 375px the sidebar used to fill the entire first screen: the ≤768px rule
turned it into a strip but kept the desktop ``height: 100vh``. It is now an
off-canvas drawer behind a hamburger in a ``.mobile-bar``. These tests pin
the contract a screen-reader and keyboard user depends on — the markup
(rendered, not grepped from the template), the CSS that hides and shows it,
and the JS that wires Esc and focus return.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

_TOGGLE = re.compile(r"<button\b[^>]*\bdata-nav-toggle\b[^>]*>", re.S)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag)
    return m.group(1) if m else None


def _toggle(html: str) -> str:
    found = _TOGGLE.findall(html)
    assert len(found) == 1, f"expected one nav toggle, found {len(found)}"
    return found[0]


def test_toggle_markup_is_rendered(client):
    html = client.get("/").get_data(as_text=True)
    tag = _toggle(html)
    assert _attr(tag, "type") == "button"
    assert _attr(tag, "aria-controls") == "sidebar-nav"
    assert _attr(tag, "aria-expanded") == "false"
    assert (_attr(tag, "aria-label") or "").strip(), "needs an accessible name"
    # Sized by the .btn scale, never inline (tests/test_design_tokens.py).
    classes = (_attr(tag, "class") or "").split()
    assert "btn" in classes and "btn-sm" in classes
    assert _attr(tag, "style") is None
    # It controls the element that exists.
    assert re.search(r'<nav\b[^>]*\bid="sidebar-nav"', html)
    # Inside a .mobile-bar that precedes the nav.
    assert html.index('class="mobile-bar"') < html.index('id="sidebar-nav"')


def test_toggle_name_is_translated(client):
    en = _attr(_toggle(client.get("/?lang=en").get_data(as_text=True)),
               "aria-label")
    ua = _attr(_toggle(client.get("/?lang=ua").get_data(as_text=True)),
               "aria-label")
    assert en == "Menu"
    assert ua == "Меню"


def _media_block(css: str, query: str, needle: str) -> str:
    """The body of the @media *query* block that contains *needle*."""
    for m in re.finditer(re.escape(f"@media ({query})") + r"\s*\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        body = css[m.end():i - 1]
        if needle in body:
            return body
    raise AssertionError(f"no @media ({query}) block containing {needle!r}")


def test_css_hides_the_bar_on_desktop_and_the_drawer_on_phones():
    assert re.search(r"(?m)^\.mobile-bar\s*\{\s*display:\s*none;", CSS)
    bar = _media_block(CSS, "max-width: 768px", ".mobile-bar {")
    assert "display: flex" in bar
    drawer = _media_block(CSS, "max-width: 768px", ".sidebar.is-open")
    closed = drawer.split(".sidebar.is-open", 1)[0]
    assert "translateX(-100%)" in closed
    # Closed = out of the tab order too, not only off-screen.
    assert "visibility: hidden" in closed
    # The old strip layout (which kept height: 100vh) is gone.
    assert "flex-direction: row" not in drawer


def test_js_wires_escape_focus_return_and_aria_expanded():
    start = APP_JS.index("Phone-width navigation drawer")
    block = APP_JS[start:start + 3000]
    assert "'[data-nav-toggle]'" in block
    assert "aria-expanded" in block
    assert "'Escape'" in block
    assert "toggle.focus()" in block
