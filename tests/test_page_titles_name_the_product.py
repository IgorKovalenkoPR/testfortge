"""Every page's browser title must name the product.

Found by walking the app, 2026-08-30. ``/bug-reports`` set
``<title>Bug Reports</title>`` — no product name at all — while
``/test-cases`` set ``TestForTge — Test Cases``. Three conventions were
in use at once across ``templates/``:

* ``TestForTge — <Page>``   (16 templates)
* ``<Page> — TestForTge``   (the org and auth pages)
* ``<Page>``                (bug_reports, test_execution,
                             test_execution_live, test_execution_runs)

The third is the defect. A tab reading only "Runs" is unidentifiable
among a browser's worth of tabs, and a bookmark saved from it names
nothing. This product is used with several tabs open by design — the
recorder opens the system under test in one and the review card in
another — and I mixed up two of my own tabs in this very session.

This test asserts the property over the whole route table rather than
over the four pages that happened to be wrong, because the next page
somebody adds will be the fifth. A parity gate that only knows the
names of today's offenders is blind to omissions — the enumeration has
to come from the app.
"""
from __future__ import annotations

import re

import pytest


TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)

PRODUCT = "TestForTge"

#: GET routes that render a full page and need no arguments. Built from
#: the URL map so a new page is covered the day it is added, minus the
#: things that are not pages: APIs, downloads, redirects and the
#: diagnostics endpoints.
_SKIP_PREFIXES = (
    "/api/", "/static/", "/healthz", "/readyz", "/debug/", "/export/",
    "/download", "/mcp",
    # Server-sent events. GET-ing it from the test client leaves a
    # half-consumed generator and Flask complains about a popped request
    # context — noise that has nothing to do with page titles.
    "/chat/stream",
)


def _page_routes(app) -> list[str]:
    out = []
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        if rule.arguments:                     # needs an id we don't have
            continue
        path = str(rule.rule)
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            continue
        out.append(path)
    return sorted(set(out))


def _titles(client, app) -> dict[str, str]:
    """``{path: title}`` for every page that renders one."""
    found = {}
    for path in _page_routes(app):
        try:
            resp = client.get(path)
        except Exception:                      # pragma: no cover
            continue
        if resp.status_code != 200:
            continue
        ctype = resp.headers.get("Content-Type", "")
        if "text/html" not in ctype:
            continue
        m = TITLE.search(resp.get_data(as_text=True))
        if m:
            found[path] = " ".join(m.group(1).split())
    return found


def test_the_sweep_actually_reached_the_pages(client, app):
    # Without this the assertions below pass on an empty dict, which is
    # how a coverage test quietly stops covering anything.
    titles = _titles(client, app)
    assert len(titles) >= 8, (
        f"only {len(titles)} page(s) answered with a title: "
        f"{sorted(titles)}")
    assert "/bug-reports" in titles, (
        "the page this test was written for was not reached")


def test_every_page_title_names_the_product(client, app):
    titles = _titles(client, app)
    nameless = {p: t for p, t in titles.items() if PRODUCT not in t}
    assert not nameless, (
        "these pages set a browser title that does not name the "
        f"product: {nameless}")


def test_no_page_title_is_only_the_product(client, app):
    # The other failure mode: a page that inherits base.html's bare
    # default is identifiable as the product and useless for telling
    # one page from another.
    titles = _titles(client, app)
    bare = {p: t for p, t in titles.items() if t.strip() == PRODUCT}
    assert not bare, f"these pages have no page name in the title: {bare}"
