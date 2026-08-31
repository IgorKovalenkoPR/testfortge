"""What the checklist could not cover has to be said out loud.

Two builders work out what they left out. ``checklist_rules`` appends a
gap for every sweep it skipped ("The markup declared no Footer region,
so the Footer sweep was omitted. Footer checks are usually 12 rows.");
``checklist_author`` appends what the agent could not evidence.
``_run_site_aware`` collects them and returns them under
``checklist_gaps``, above a comment that says exactly what they are for:

    # Surfaces the checklist could not evidence — an unstructured Footer,
    # sections beyond the cap. Flashed to the operator so a thin sheet
    # reads as a known limitation rather than as the whole product.

They were never flashed. ``checklist_gaps`` appeared exactly once in the
codebase — on the line that builds it. Nothing read it, at any of the
three call sites. The work of knowing was done and thrown away, and a
thin sheet read as the whole product, which is the sentence in the
comment describing what must not happen.

Its neighbour on the line above, ``crawl_errors``, is read and flashed
twice, in ``/test-cases`` and in ``/checklist``, and that is the shape
these follow.

Found by walking /checklist, 2026-08-31: an input naming eight features
produced 153 items and not one word about the requirement it had no
knowledge pack for. The operator has no way to tell "not applicable"
from "not covered".
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


GAP = ("The markup declared no Footer region, so the Footer sweep was "
       "omitted. Footer checks are usually 12 rows.")

#: Enough of ``_run_site_aware``'s contract for the wire-up under test.
#: A real crawl is not the subject here — the subject is what the route
#: does with what the crawl reported.
def _site_out(gaps: list[str]) -> dict:
    return {
        "tc_dicts": [],
        "cl_dicts": [{
            "id": "SA_FUNC_001",
            "section": "Site-aware — Functional",
            "objective": "Verify that the header logo links to the home page",
            "category": "Positive", "priority": "High",
            "status": "Unchecked",
        }],
        "profile": {}, "strategy": {"source": "rules"},
        "crawl_errors": [],
        "checklist_gaps": list(gaps),
    }


def _post_checklist_with_a_url(client):
    return client.post(
        "/checklist",
        data={"input_text": "https://example.test/ is the site under test."},
        follow_redirects=False)


def _banner(client) -> str:
    """The warning text as it reaches the operator.

    Read from the POST response, not from ``session["_flashes"]``. The
    usual rule for this codebase is the opposite — assert the flash, not
    the rendered page, because a landing page explaining the same
    situation can mask a route that says nothing. It does not apply
    here: this route renders in the same request rather than redirecting,
    so the template consumes the flash and the session is empty by the
    time a test can look. The first version of this test read the
    session and reported that nothing was flashed while the banner was
    on the page.
    """
    return _post_checklist_with_a_url(client).get_data(as_text=True)


class TestChecklistGaps:
    def test_a_gap_is_flashed(self, client):
        with patch("routes.generation._run_site_aware",
                   return_value=_site_out([GAP])):
            body = _banner(client)
        assert "Footer sweep was omitted" in body, (
            "the builder reported a gap and the operator was not told")

    def test_every_gap_is_flashed_not_just_the_first(self, client):
        gaps = [GAP, "Sections beyond the cap of 12 were not swept.",
                "Localisation was not covered — no knowledge pack matched."]
        with patch("routes.generation._run_site_aware",
                   return_value=_site_out(gaps)):
            body = _banner(client)
        missing = [g for g in gaps
                   if g.split("—")[0].strip()[:24] not in body]
        assert not missing, f"gaps dropped on the floor: {missing}"

    def test_no_gaps_means_no_banner(self, client):
        # A clean run must stay quiet. A warning that always fires is a
        # warning the operator learns to scroll past, which costs the
        # ones that matter.
        with patch("routes.generation._run_site_aware",
                   return_value=_site_out([])):
            body = _banner(client)
        assert "thinner than the site" not in body, (
            "a clean generation produced a limitation banner")

    def test_the_generated_items_still_arrive(self, client):
        # The fix must not be a banner that replaces the work. The
        # site-aware items are the reason the operator pressed the
        # button.
        with patch("routes.generation._run_site_aware",
                   return_value=_site_out([GAP])):
            _banner(client)
        body = client.get("/checklist").get_data(as_text=True)
        assert "header logo links to the home page" in body, (
            "the checklist lost the items it generated")

    def test_the_gap_text_is_not_swallowed_by_truncation(self, client):
        # `crawl_errors` is flashed as `"; ".join(errors[:3])`. Copying
        # that shape blindly would silently drop the fourth gap, and a
        # report of what was missed that itself misses things is worse
        # than none: it reads as complete.
        gaps = [f"Gap number {n} was not covered." for n in range(1, 6)]
        with patch("routes.generation._run_site_aware",
                   return_value=_site_out(gaps)):
            body = _banner(client)
        assert ("Gap number 5" in body or "more not listed" in body), (
            "the fifth gap left no trace at all — either say it or say "
            "how many were not said")
