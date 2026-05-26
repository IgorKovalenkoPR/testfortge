"""PR-D′ regression — annotated screenshot helper.

Two layers of coverage:

1. **Pure-function unit tests** for
   :func:`engine.screenshot_annotator.annotate_screenshot` — degraded
   inputs (missing file, malformed bbox, zero-area rectangle) return
   ``None`` instead of raising; a healthy input writes a PNG that
   carries a red rectangle around the bbox + a red arrow pointing at
   it. Pixel-level assertions use a tiny synthetic input image so
   the test stays fast and deterministic.

2. **Integration test** against the LiveExecutor stub Playwright —
   when a heuristic emits a finding whose ``element`` selector
   resolves to a bounding box, ``_walk_one`` must replace
   ``finding["screenshot"]`` with the annotated path. When the
   selector mis-matches, the field stays empty so the PR-B fan-out
   falls back to the raw page shot.
"""

from __future__ import annotations

import os

import pytest

# Reuse the Playwright stubs from the scaffold suite (same pattern as
# tests/test_live_executor.py).
from tests.test_walkthrough_scaffold import (  # noqa: E402
    fake_pw,
    tmp_storage,
)


# ── Unit tests for the pure annotator function ────────────────────


class TestAnnotateScreenshotUnit:
    @pytest.fixture
    def raw_png(self, tmp_path):
        """Synthesise a tiny white PNG so the annotator has something
        to draw on. 200×120 keeps the test under a millisecond while
        leaving room for the rectangle + arrow."""
        from PIL import Image
        path = tmp_path / "page.png"
        Image.new("RGB", (200, 120), color="white").save(path)
        return str(path)

    def test_writes_png_at_output_path_for_valid_bbox(self, raw_png, tmp_path):
        from engine.screenshot_annotator import annotate_screenshot
        out = str(tmp_path / "annotated.png")
        result = annotate_screenshot(
            raw_path=raw_png,
            bbox={"x": 30, "y": 40, "width": 80, "height": 30},
            output_path=out,
        )
        assert result == out, (
            "successful annotation must return the output path"
        )
        assert os.path.isfile(out), (
            "annotated PNG must be written to the requested location"
        )

    def test_annotated_image_contains_red_pixels(self, raw_png, tmp_path):
        """The annotator draws a red rectangle and a red arrow on the
        otherwise-white synthetic page. ``(255, 0, 0)`` pixels MUST
        appear in the output — if they don't, the overlay didn't fire
        and the operator gets the same useless raw shot."""
        from PIL import Image
        from engine.screenshot_annotator import annotate_screenshot
        out = str(tmp_path / "annotated.png")
        annotate_screenshot(
            raw_path=raw_png,
            bbox={"x": 50, "y": 30, "width": 60, "height": 40},
            output_path=out,
        )
        img = Image.open(out).convert("RGB")
        # Count red pixels — the rectangle alone contributes
        # >= 2*(60+40+8) px (4-px stroke around an 8-px-padded bbox),
        # plus the arrow shaft and head. 200 is a conservative floor.
        red_count = sum(1 for px in img.getdata() if px == (255, 0, 0))
        assert red_count > 200, (
            f"expected >200 red pixels (rectangle + arrow), got {red_count}"
        )

    def test_returns_none_when_bbox_is_falsy(self, raw_png, tmp_path):
        from engine.screenshot_annotator import annotate_screenshot
        out = str(tmp_path / "annotated.png")
        assert annotate_screenshot(raw_png, None, out) is None
        assert annotate_screenshot(raw_png, {}, out) is None
        assert not os.path.isfile(out), (
            "no output PNG should be written when annotation aborts"
        )

    def test_returns_none_when_bbox_keys_missing(self, raw_png, tmp_path):
        from engine.screenshot_annotator import annotate_screenshot
        out = str(tmp_path / "annotated.png")
        # Missing "height" — Playwright sometimes returns partial
        # dicts when the element is hidden / detached.
        assert annotate_screenshot(
            raw_png, {"x": 1, "y": 1, "width": 1}, out,
        ) is None

    def test_returns_none_when_bbox_zero_or_negative(self, raw_png, tmp_path):
        from engine.screenshot_annotator import annotate_screenshot
        out = str(tmp_path / "annotated.png")
        # Zero-width — element has been display:none'd or its dom
        # node was removed between locator() and bounding_box().
        assert annotate_screenshot(
            raw_png, {"x": 10, "y": 10, "width": 0, "height": 30}, out,
        ) is None
        # Negative width — should never happen but guard anyway.
        assert annotate_screenshot(
            raw_png, {"x": 10, "y": 10, "width": -5, "height": 30}, out,
        ) is None

    def test_returns_none_when_raw_path_missing(self, tmp_path):
        from engine.screenshot_annotator import annotate_screenshot
        assert annotate_screenshot(
            raw_path=str(tmp_path / "does_not_exist.png"),
            bbox={"x": 1, "y": 1, "width": 10, "height": 10},
            output_path=str(tmp_path / "out.png"),
        ) is None

    def test_bbox_outside_image_returns_none(self, raw_png, tmp_path):
        """If the element is fully clipped out of the screenshot's
        viewport (e.g. scrolled below the fold on a non-full-page
        shot), the clamped rectangle has zero area and annotation
        bails — the operator is better served by the raw page shot
        with PR-B fan-out picking up the slack."""
        from engine.screenshot_annotator import annotate_screenshot
        out = str(tmp_path / "annotated.png")
        # raw_png is 200×120; bbox at y=500 is entirely below.
        assert annotate_screenshot(
            raw_png, {"x": 50, "y": 500, "width": 40, "height": 40}, out,
        ) is None

    def test_creates_parent_directory_for_output(self, raw_png, tmp_path):
        from engine.screenshot_annotator import annotate_screenshot
        # Nested path under tmp_path that doesn't exist yet.
        out = str(tmp_path / "nested" / "subdir" / "annotated.png")
        result = annotate_screenshot(
            raw_path=raw_png,
            bbox={"x": 30, "y": 40, "width": 80, "height": 30},
            output_path=out,
        )
        assert result == out
        assert os.path.isfile(out)


class TestDeriveAnnotatedPath:
    def test_path_carries_finding_idx_and_annotated_suffix(self):
        from engine.screenshot_annotator import derive_annotated_path
        out = derive_annotated_path(
            "/tmp/run-id/LIVE-PAGE-001/page.png", finding_idx=3,
        )
        # Same directory, ``_finding03_annotated.png`` suffix so
        # bug-attachment listing can grep for ``_annotated.png`` to
        # prefer this over the raw page shot.
        assert out.endswith("_finding03_annotated.png"), out
        assert os.path.dirname(out).endswith("LIVE-PAGE-001"), out

    def test_zero_padded_idx_avoids_alpha_sort_surprises(self):
        from engine.screenshot_annotator import derive_annotated_path
        # 02-format means 1-9 sort before 10+ in lexicographic
        # listings (gallery thumbnail order).
        out_1 = derive_annotated_path("/a/page.png", finding_idx=1)
        out_10 = derive_annotated_path("/a/page.png", finding_idx=10)
        assert "finding01_" in out_1
        assert "finding10_" in out_10
        assert out_1 < out_10, (
            "zero-padded index must lex-sort numerically"
        )


# ── Integration with LiveExecutor._walk_one ───────────────────────


class TestWalkOneAnnotation:
    """End-to-end wire-up against the LiveExecutor stub Playwright.

    The scaffold's ``_FakeLocator`` and ``_FakePage.screenshot`` write
    fake-PNG bytes (intentional — Pillow is not in their test budget),
    so we stub :func:`engine.screenshot_annotator.annotate_screenshot`
    and ``_FakeLocator.bounding_box`` rather than driving the real
    Pillow draw path. Pillow itself is covered by
    :class:`TestAnnotateScreenshotUnit` above; the contract this
    fixture pins is that ``_walk_one``:

    * calls ``page.locator(element).first.bounding_box(...)`` when
      the finding names a selector,
    * passes the resulting bbox to the annotator,
    * writes the annotator's return value into ``finding["screenshot"]``
      so the bug factory persists the annotated PNG.
    """

    def test_finding_with_resolvable_selector_gets_annotated_shot(
        self, fake_pw, tmp_storage, monkeypatch
    ):
        fake_pw()
        from engine import live_executor as le
        from engine import screenshot_annotator
        from tests.test_walkthrough_scaffold import _FakeLocator

        # Teach the scaffold's locator to answer ``bounding_box`` —
        # the real Playwright Locator has this; the scaffold left it
        # out because heuristic tests never needed it.
        monkeypatch.setattr(
            _FakeLocator, "bounding_box",
            lambda self, **_kw: {
                "x": 30, "y": 40, "width": 80, "height": 30,
            },
            raising=False,
        )
        # Stub the annotator to return a predictable path so we can
        # assert ``_walk_one`` wired the bbox → annotator → finding
        # without depending on real Pillow draw output.
        stub_annot_called = {"calls": []}

        def _stub_annot(*, raw_path, bbox, output_path):
            stub_annot_called["calls"].append({
                "raw_path": raw_path,
                "bbox": dict(bbox),
                "output_path": output_path,
            })
            return output_path

        monkeypatch.setattr(
            screenshot_annotator, "annotate_screenshot", _stub_annot,
        )

        # Stub a heuristic so it emits a finding that names a CSS
        # selector. Without a selector the annotator path is skipped
        # (covered separately below).
        def _fake_scan(page, url, tc_id, *, note):
            note(
                "Major", "Images", "broken_image",
                f"Broken image on {url}",
                url=url, tc_id=tc_id, element="img.broken-hero",
            )

        monkeypatch.setattr(le, "scan_broken_images", _fake_scan)
        ex = le.LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=1,
        )
        ex.run(start_urls=["https://example.com/"])

        # Annotator must have been called with the bounding box the
        # locator returned.
        assert stub_annot_called["calls"], (
            "annotator must be invoked when a finding has a selector"
        )
        call = stub_annot_called["calls"][0]
        assert call["bbox"] == {
            "x": 30, "y": 40, "width": 80, "height": 30,
        }, call

        broken = [f for f in ex.findings
                   if f["defect_class"] == "broken_image"]
        assert broken
        shot = broken[0]["screenshot"]
        # The annotator's return value (the ``_annotated.png`` path)
        # must end up on the finding so the bug factory persists it.
        assert shot.endswith("_annotated.png"), (
            f"expected annotated screenshot path on finding, got {shot!r}"
        )

    def test_finding_without_selector_falls_back_to_page_shot(
        self, fake_pw, tmp_storage, monkeypatch
    ):
        """When the heuristic emits a finding with no ``element``
        selector — e.g. a console-error finding — there is nothing to
        draw a box around. PR-B's fan-out must still hand the raw page
        screenshot to the bug factory."""
        fake_pw()
        from engine import live_executor as le
        from engine import screenshot_annotator

        # Annotator must NOT be invoked when there's no selector.
        called = {"n": 0}

        def _stub_annot(**_kw):
            called["n"] += 1
            return None

        monkeypatch.setattr(
            screenshot_annotator, "annotate_screenshot", _stub_annot,
        )

        def _fake_scan(page, url, tc_id, *, note):
            note(
                "Major", "JS", "page_error",
                "Uncaught TypeError",
                url=url, tc_id=tc_id,
                # element="" — no selector to annotate.
            )

        monkeypatch.setattr(le, "scan_broken_images", _fake_scan)
        ex = le.LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=1,
        )
        ex.run(start_urls=["https://example.com/"])

        assert called["n"] == 0, (
            "annotator must not be called for unselectored findings"
        )
        page_err = [f for f in ex.findings
                     if f["defect_class"] == "page_error"]
        assert page_err
        shot = page_err[0]["screenshot"]
        assert shot, "PR-B fan-out must still supply a raw page shot"
        assert not shot.endswith("_annotated.png"), (
            f"unselectored finding must inherit raw page.png, "
            f"got {shot!r}"
        )

    def test_annotator_failure_falls_through_to_page_shot(
        self, fake_pw, tmp_storage, monkeypatch
    ):
        """If the annotator returns ``None`` (bbox malformed, Pillow
        missing, file unreadable), the finding must still receive the
        raw page shot via PR-B fan-out — losing the box-overlay is a
        cosmetic regression, but losing the attachment entirely would
        regress all the way to the original "No attachments captured"
        banner."""
        fake_pw()
        from engine import live_executor as le
        from engine import screenshot_annotator
        from tests.test_walkthrough_scaffold import _FakeLocator

        monkeypatch.setattr(
            _FakeLocator, "bounding_box",
            lambda self, **_kw: {
                "x": 30, "y": 40, "width": 80, "height": 30,
            },
            raising=False,
        )
        # Annotator returns None — simulates Pillow open failure.
        monkeypatch.setattr(
            screenshot_annotator, "annotate_screenshot",
            lambda **_kw: None,
        )

        def _fake_scan(page, url, tc_id, *, note):
            note(
                "Major", "Images", "broken_image",
                "Broken on page",
                url=url, tc_id=tc_id, element="img.hero",
            )

        monkeypatch.setattr(le, "scan_broken_images", _fake_scan)
        ex = le.LiveExecutor(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=1,
        )
        ex.run(start_urls=["https://example.com/"])

        broken = [f for f in ex.findings
                   if f["defect_class"] == "broken_image"]
        assert broken
        shot = broken[0]["screenshot"]
        # Falls back to raw page.png — NOT annotated, but not empty.
        assert shot, "PR-B fan-out must still supply a raw shot"
        assert not shot.endswith("_annotated.png"), shot
