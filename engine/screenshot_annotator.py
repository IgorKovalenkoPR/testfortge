"""PR-D′ — annotated screenshots for walkthrough findings.

Walkthrough heuristics emit findings that name a CSS selector for
the offending element (e.g. ``img.broken-hero``, ``button#submit``).
Before this module the bug-report attachment was the raw page
screenshot, which left the operator hunting through the page to
find what was actually broken. The QA style guide requires a visual
marker — a red box around the element + a red arrow pointing at it
from the page edge — to make the defect location unambiguous.

Design notes
------------
* **Pillow-only.** ``requirements.txt`` already pins ``Pillow==11.1.0``
  for other parts of the pipeline (report rendering, thumbnail
  generation). Reusing it avoids a new dependency. ImageDraw covers
  rectangle + line + polygon (for the arrowhead) — that is all we
  need.
* **Safe by construction.** Every public entry point traps Pillow /
  filesystem exceptions and returns ``None`` so the caller can fall
  back to the unannotated screenshot rather than aborting the run.
  The walkthrough is the only consumer today and we never want a
  cosmetic overlay to drop a finding from the bug list.
* **Arrow geometry — opposite corner heuristic.** Pick the page
  corner *farthest* from the bounding-box centre as the arrow
  origin. That guarantees a visually clear arrow even when the
  element is near a page edge (a corner-anchored arrow always has
  >= 0.5 × viewport length of clear track).
* **No font dependency.** We avoid ``ImageDraw.text`` so we don't
  have to ship a TTF or rely on the host having one — the red box
  + arrow alone communicate the defect location and the rest of the
  evidence lives in the bug-report body.

Public API:

    annotate_screenshot(raw_path, bbox, output_path) -> str | None

Returns ``output_path`` on success, ``None`` on any failure.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Mapping

_logger = logging.getLogger(__name__)

# Stroke width for the red rectangle + arrow shaft. 4 px reads as
# "intentional QA marker" on 1280×800 and stays visible on the
# 240-px-wide gallery thumbnail the live UI renders.
_STROKE = 4

# Padding around the bounding box. Tight bboxes (single-line text)
# look cramped without this; 4 px is the smallest gap that still
# reads as a "border" rather than "overlay on top of the element".
_BBOX_PAD = 4

# Arrowhead geometry — 18 px from tip to base, 25° flare. Tuned by
# eye on the broken-image screenshots from the ART project export.
_ARROWHEAD_LEN = 18
_ARROWHEAD_FLARE_RAD = math.radians(25)


def annotate_screenshot(
    raw_path: str,
    bbox: Mapping[str, float],
    output_path: str,
) -> str | None:
    """Draw a red rectangle around ``bbox`` and a red arrow pointing
    at it on top of ``raw_path``; save to ``output_path``; return the
    output path. Returns ``None`` on any failure so the caller can
    fall back to the unannotated screenshot.

    Args:
        raw_path:    Filesystem path to the source PNG (the page-level
                     screenshot ``LiveExecutor._screenshot`` produced
                     for this page).
        bbox:        Playwright bounding-box dict —
                     ``{"x", "y", "width", "height"}`` in viewport
                     coordinates. Anything else (``None``, missing
                     keys, non-numeric values) causes a graceful
                     ``None`` return.
        output_path: Destination PNG path. Parent directory is created
                     if missing.
    """
    # Defensive: bail before we even try to open the file if the
    # bounding box is malformed. Pillow exceptions are noisy and
    # would otherwise leak into the run log every time a selector
    # mis-matched.
    if not bbox:
        return None
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        w = float(bbox["width"])
        h = float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if not os.path.isfile(raw_path):
        return None

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _logger.debug("annotate_screenshot: Pillow not importable")
        return None

    try:
        img = Image.open(raw_path).convert("RGB")
    except Exception as exc:  # IOError, UnidentifiedImageError, etc.
        _logger.debug("annotate_screenshot: open(%s) failed: %s",
                      raw_path, exc)
        return None

    width, height = img.size

    # Clamp the rectangle to the image so we don't draw off-canvas
    # (Playwright returns viewport coords; the screenshot may be
    # full-page and taller than the viewport in some configs).
    pad = _BBOX_PAD
    left = max(0, int(x - pad))
    top = max(0, int(y - pad))
    right = min(width, int(x + w + pad))
    bottom = min(height, int(y + h + pad))
    if right <= left or bottom <= top:
        # Zero-area rectangle — the element is fully clipped out of
        # the screenshot's viewport. Annotation would be pointless.
        return None

    draw = ImageDraw.Draw(img)
    draw.rectangle((left, top, right, bottom), outline="red",
                   width=_STROKE)

    # Pick the arrow origin in the page corner *farthest* from the
    # bbox centre. That maximises the visible arrow length and avoids
    # the awkward case where the element is in the top-left and we'd
    # draw a 5-px arrow from the top-left corner that nobody can see.
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    # Choose corner: top-left / top-right / bottom-left / bottom-right.
    # Origin is the corner FURTHEST from the centroid.
    corners = {
        "tl": (40, 40),
        "tr": (width - 40, 40),
        "bl": (40, height - 40),
        "br": (width - 40, height - 40),
    }
    origin_label = max(
        corners.keys(),
        key=lambda k: (corners[k][0] - cx) ** 2 + (corners[k][1] - cy) ** 2,
    )
    ox, oy = corners[origin_label]

    # Tip lands on the closest rectangle edge midpoint to the origin
    # — looks tidier than aiming at a corner. We compare the four
    # edge midpoints by squared distance to the origin.
    edge_midpoints = {
        "top":    ((left + right) / 2.0, top),
        "bottom": ((left + right) / 2.0, bottom),
        "left":   (left, (top + bottom) / 2.0),
        "right":  (right, (top + bottom) / 2.0),
    }
    tx, ty = min(
        edge_midpoints.values(),
        key=lambda p: (p[0] - ox) ** 2 + (p[1] - oy) ** 2,
    )

    # Arrow shaft.
    draw.line([(ox, oy), (tx, ty)], fill="red", width=_STROKE)

    # Arrowhead — two short legs swept back from the tip.
    angle = math.atan2(ty - oy, tx - ox)
    head_a = (
        tx - _ARROWHEAD_LEN * math.cos(angle - _ARROWHEAD_FLARE_RAD),
        ty - _ARROWHEAD_LEN * math.sin(angle - _ARROWHEAD_FLARE_RAD),
    )
    head_b = (
        tx - _ARROWHEAD_LEN * math.cos(angle + _ARROWHEAD_FLARE_RAD),
        ty - _ARROWHEAD_LEN * math.sin(angle + _ARROWHEAD_FLARE_RAD),
    )
    draw.polygon([head_a, (tx, ty), head_b], fill="red")

    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        img.save(output_path, "PNG")
    except Exception as exc:
        _logger.debug("annotate_screenshot: save(%s) failed: %s",
                      output_path, exc)
        return None

    return output_path


def derive_annotated_path(raw_path: str, finding_idx: int) -> str:
    """Compose the destination path for an annotated screenshot.

    Convention: ``<raw_dir>/<raw_stem>_finding<NN>_annotated.png`` —
    keeps the annotated PNG alongside the raw page shot so a single
    automation-asset listing surfaces both, and the ``_annotated``
    suffix lets the bug-attachment renderer pick the annotated one
    preferentially.
    """
    raw_dir = os.path.dirname(raw_path)
    stem, _ext = os.path.splitext(os.path.basename(raw_path))
    return os.path.join(
        raw_dir, f"{stem}_finding{finding_idx:02d}_annotated.png"
    )
