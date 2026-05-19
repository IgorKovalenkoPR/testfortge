"""TestFortge — Mockup vision analyser.

Turns design assets (PNG / JPG / WebP / PDF / Figma URL) into a plain-
text feature list that the existing :func:`engine.qa_estimator.
features_from_text` can consume.

Architecture
------------
A real QA Manager looks at a mockup and lists what's testable: every
form, every dialog, every nav item, every state (loading / empty /
error). We replicate that by handing the image bytes to Claude (vision
model via the Anthropic SDK) with a prompt that asks for exactly that
output shape — markdown bullets, one feature per line, in the same
register the estimator already understands.

Three input paths share one analyse() entry point:

* **Files** — PNG / JPG / WebP / PDF uploads. PDFs are split into one
  PIL.Image per page (max 20 pages); each image is base64-encoded and
  passed to Claude as a vision content block.
* **Figma URL** — Public file/frame URL. With ``FIGMA_PAT`` env set
  we hit the Figma REST API and pull the file thumbnail; without it we
  fall back to the public ``s.figma.com`` Open Graph image. Both end
  up as a single PNG fed through the same vision pipeline.
* **Context** — Optional free-text the operator pastes alongside the
  files. Prepended to the LLM prompt so the model knows what domain
  it's looking at ("e-commerce checkout", "patient portal", …).

The output is a single newline-joined string. Failures are logged and
return an empty string so the calling route can flash a friendly
"couldn't analyse — try the text tab" message instead of a 500.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from engine.llm_client import LLMUnavailable, call_messages

_logger = logging.getLogger(__name__)

# Hard limits — vision calls are expensive and the model context is
# bounded. 20 images is a generous PDF + a few extra screens.
MAX_IMAGES = 20
MAX_BYTES_PER_IMAGE = 4 * 1024 * 1024  # 4 MB after re-encode
TARGET_LONG_EDGE = 1600                # px — Anthropic recommends ≤1568


@dataclass
class VisionResult:
    """Output of :func:`analyse`. ``text`` feeds straight into
    :func:`engine.qa_estimator.features_from_text`."""
    text: str = ""
    image_count: int = 0
    source_label: str = ""
    raw_response: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text and not self.error)


# ── Image preparation ────────────────────────────────────────────

def _resize_to_budget(im) -> bytes:
    """Re-encode a PIL Image as PNG with the long edge ≤ TARGET_LONG_EDGE.

    Anthropic's vision pipeline costs scale with image area and rejects
    >5 MB payloads. Resizing to 1600 px on the long edge gives a sweet
    spot: text in mockups stays readable, total bytes stay <2 MB on
    almost every input we've seen.
    """
    from PIL import Image
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    long_edge = max(im.size)
    if long_edge > TARGET_LONG_EDGE:
        scale = TARGET_LONG_EDGE / float(long_edge)
        new_size = (int(im.size[0] * scale), int(im.size[1] * scale))
        im = im.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    # PNG keeps mockup edges crisp; JPEG would soften UI text.
    im.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    # Defensive cap — if a giant image still busts the budget, fall
    # back to JPEG quality 80.
    if len(data) > MAX_BYTES_PER_IMAGE:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80, optimize=True)
        data = buf.getvalue()
    return data


def _load_image_bytes(file_path: str) -> list[bytes]:
    """Open a file path and return one or more PNG byte strings ready
    for vision input. PDFs are split per page; PNG/JPG/WebP are
    re-encoded. Unsupported formats return []."""
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    out: list[bytes] = []
    if ext == "pdf":
        # pdf2image + Poppler is the conventional path; if the host
        # doesn't have poppler installed, fall back to pypdf-rendered
        # blank-with-text frames so we at least get the embedded text.
        try:
            from pdf2image import convert_from_path
            pages = convert_from_path(file_path, dpi=150,
                                      first_page=1, last_page=MAX_IMAGES)
            for p in pages:
                out.append(_resize_to_budget(p))
        except Exception as exc:
            _logger.warning("pdf2image failed (%s); falling back to text-only PDF render",
                            exc)
            try:
                from PIL import Image, ImageDraw, ImageFont
                from pypdf import PdfReader
                reader = PdfReader(file_path)
                font = ImageFont.load_default()
                for i, page in enumerate(reader.pages[:MAX_IMAGES]):
                    txt = (page.extract_text() or "").strip()
                    if not txt:
                        continue
                    img = Image.new("RGB", (800, 1100), "white")
                    draw = ImageDraw.Draw(img)
                    draw.text((20, 20), txt[:5000], fill="black", font=font)
                    out.append(_resize_to_budget(img))
            except Exception as exc2:
                _logger.warning("pypdf fallback failed too: %s", exc2)
    elif ext in ("png", "jpg", "jpeg", "webp"):
        try:
            from PIL import Image
            with Image.open(file_path) as im:
                out.append(_resize_to_budget(im))
        except Exception as exc:
            _logger.warning("image load failed (%s): %s", file_path, exc)
    else:
        _logger.debug("unsupported mockup ext: %s", ext)
    return out


# ── Figma URL handling ──────────────────────────────────────────

_FIGMA_FILE_RE = re.compile(
    r"figma\.com/(?:file|design|proto)/([a-zA-Z0-9]+)(?:/[^?]*)?(?:\?node-id=([0-9-:%A-Za-z]+))?",
    re.I,
)


def _figma_image_bytes(url: str) -> list[bytes]:
    """Fetch a render of a Figma URL.

    Tries (in order):
    1. Figma REST API ``/v1/images/{file_key}`` if ``FIGMA_PAT`` is set
       — yields PNG renders of the first 5 frames of the file.
    2. Public Open Graph thumbnail at
       ``https://s.figma.com/...`` (always available for public files).

    Returns at most MAX_IMAGES PNG byte strings.
    """
    m = _FIGMA_FILE_RE.search(url or "")
    if not m:
        return []
    file_key = m.group(1)
    node_id = m.group(2) or ""
    pat = os.environ.get("FIGMA_PAT", "").strip()
    out: list[bytes] = []
    try:
        import requests
    except Exception as exc:
        _logger.warning("requests missing for figma fetch: %s", exc)
        return out
    # SSRF guard — every requests.get below routes operator-controlled
    # input (the pasted Figma URL, or img_url / og:image values pulled
    # from the response HTML). Without this an attacker could supply
    # an URL whose og:image points at http://127.0.0.1 and exfiltrate
    # internal images through our vision pipeline.
    from engine.security import require_safe_url, UnsafeUrlError
    # Path 1 — REST API with PAT.
    if pat:
        try:
            headers = {"X-Figma-Token": pat}
            # Get the file's children so we know what to render.
            files_url = f"https://api.figma.com/v1/files/{file_key}"
            require_safe_url(files_url)
            r = requests.get(
                files_url,
                headers=headers, timeout=8,
            )
            r.raise_for_status()
            doc = r.json().get("document") or {}
            ids: list[str] = []
            if node_id:
                ids = [node_id.replace("-", ":")]
            else:
                # Walk top-level frames.
                for page in (doc.get("children") or []):
                    for frame in (page.get("children") or [])[:5]:
                        if frame.get("id"):
                            ids.append(frame["id"])
                    if len(ids) >= 5:
                        break
            ids = ids[:MAX_IMAGES]
            if ids:
                images_url = f"https://api.figma.com/v1/images/{file_key}"
                require_safe_url(images_url)
                rr = requests.get(
                    images_url,
                    headers=headers,
                    params={"ids": ",".join(ids), "format": "png", "scale": 1},
                    timeout=10,
                )
                rr.raise_for_status()
                images = (rr.json().get("images") or {})
                for nid in ids:
                    img_url = images.get(nid)
                    if not img_url:
                        continue
                    try:
                        require_safe_url(img_url)
                    except UnsafeUrlError as _ssrf_exc:
                        _logger.warning("figma image %s blocked: %s",
                                          nid, _ssrf_exc)
                        continue
                    try:
                        ir = requests.get(img_url, timeout=10)
                        ir.raise_for_status()
                        from PIL import Image
                        with Image.open(io.BytesIO(ir.content)) as im:
                            out.append(_resize_to_budget(im))
                    except Exception as exc:
                        _logger.debug("figma image fetch failed (%s): %s",
                                      nid, exc)
        except UnsafeUrlError as exc:
            _logger.warning("figma REST fetch blocked: %s", exc)
        except Exception as exc:
            _logger.warning("figma REST fetch failed: %s", exc)
    # Path 2 — Public OG image. Always try as a fallback so we have at
    # least one frame to analyse.
    if not out:
        try:
            require_safe_url(url)
            page = requests.get(url, timeout=8,
                                 headers={"User-Agent": "TestForTge/1.0"})
            page.raise_for_status()
            html = page.text
            og = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.I,
            )
            if og:
                og_target = og.group(1)
                require_safe_url(og_target)
                ir = requests.get(og_target, timeout=8)
                ir.raise_for_status()
                from PIL import Image
                with Image.open(io.BytesIO(ir.content)) as im:
                    out.append(_resize_to_budget(im))
        except UnsafeUrlError as exc:
            _logger.warning("figma OG fetch blocked: %s", exc)
        except Exception as exc:
            _logger.warning("figma OG fetch failed: %s", exc)
    return out[:MAX_IMAGES]


# ── Vision prompt + Anthropic call ──────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior QA Manager auditing a UI design. "
    "Given screen mockups, your job is to enumerate every TESTABLE "
    "element a tester would need to cover: forms, fields, buttons, "
    "navigation, links, dialogs, error states, empty states, loading "
    "states, search, filters, and any business workflow inferable "
    "from the visuals. "
    "Be exhaustive but concise. Output ONLY a markdown bullet list "
    "(one feature per line, no headings, no commentary). Group "
    "related items into a single bullet only when they share the "
    "exact same testing strategy. Use the same phrasing register as "
    "an ISTQB test-case summary — start with a verb where possible: "
    "\"Submit checkout form\", \"Validate email format on registration\", "
    "\"Open mobile menu\", etc."
)


def _build_user_text(image_count: int, context: str) -> str:
    parts = [
        f"I'm attaching {image_count} mockup screen(s) of a product. "
        "List every testable feature visible across these screens "
        "as bullet points."
    ]
    if context and context.strip():
        parts.append("Domain context from the team:")
        parts.append(context.strip()[:600])
    return "\n\n".join(parts)


def _call_claude_vision(
    images: list[bytes], context: str
) -> tuple[str, str, str]:
    """Call Anthropic with image content blocks. Returns
    ``(feature_list_text, raw_response, error)`` — ``error`` is empty
    on success, populated when the LLM is unavailable or returned an
    unexpected error so the caller can surface it to the user.

    The actual SDK call routes through
    :func:`engine.llm_client.call_messages` which adds a 60 s
    per-attempt timeout and a 3-attempt exponential-backoff retry on
    transient errors. Terminal failures raise
    :class:`engine.llm_client.LLMUnavailable` which we convert into the
    ``error`` field of the return tuple.
    """
    if not images:
        return "", "", ""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        _logger.info("ANTHROPIC_API_KEY not set or empty; skipping vision "
                     "call. Set it in Render env vars to enable Mockups.")
        return "", "", "ANTHROPIC_API_KEY is not set on the server."
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    max_tokens = int(os.environ.get("ANTHROPIC_VISION_MAX_TOKENS", "2000"))
    content: list[dict] = []
    for img in images:
        b64 = base64.standard_b64encode(img).decode("ascii")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
        })
    content.append({"type": "text", "text": _build_user_text(len(images), context)})
    try:
        resp = call_messages(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except LLMUnavailable as exc:
        _logger.warning("Claude vision call unavailable: %s", exc)
        return "", "", f"Vision analysis is temporarily unavailable: {exc}"
    except Exception as exc:
        _logger.warning("Claude vision call failed: %s", exc)
        return "", "", f"Vision analysis failed: {exc}"
    out = ""
    for block in (resp.content or []):
        t = getattr(block, "text", "") or ""
        if t:
            out += t + "\n"
    return out.strip(), out.strip(), ""


def _normalise_to_bullets(raw: str) -> str:
    """Strip stray headings / numbering / extra blank lines from the
    LLM output so :func:`features_from_text` sees clean bullets.

    Tolerant: Claude usually obeys but occasionally emits a header
    line ("### Login") or numbered items; we down-convert both.

    Audit fix (2026-05-04): the previous implementation prefixed
    headings with "* " AFTER stripping leading "#"s, but the strip
    was order-dependent — `s.lstrip("# ")` consumed leading hash
    characters AND any spaces, then the next branch also re-wrapped
    in "* ". Result: "## Login Form" became "* ## Login Form" because
    the second branch saw "## Login Form" still starting with "#".
    Now we sanitise hashes FIRST in their own branch and short-circuit.
    """
    if not raw:
        return ""
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # 1) Strip ALL leading hashes + whitespace in one shot. A
        # heading like "### Login" becomes "Login"; "##Login" becomes
        # "Login" too. After this step `s` never starts with "#".
        s = re.sub(r"^#+\s*", "", s).strip()
        if not s:
            continue
        # 2) Strip leading numeric markers ("1. ", "2) ", ...).
        s = re.sub(r"^\d+[.)]\s+", "", s).strip()
        # 3) Strip an existing bullet glyph so we control the final
        # marker uniformly.
        s = re.sub(r"^[*\-•]\s+", "", s).strip()
        if not s:
            continue
        out.append("* " + s)
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return "\n".join(deduped)


# ── Public entry ────────────────────────────────────────────────

def analyse(*,
            file_paths: Iterable[str] = (),
            figma_url: str = "",
            context: str = "") -> VisionResult:
    """Run the vision pipeline against any combination of file paths
    and a Figma URL. Returns a :class:`VisionResult` whose ``text``
    field is ready to drop into the existing estimator.

    Order of operations:
      1. Decode every file into one or more PNG byte strings (PDFs
         become up to MAX_IMAGES pages).
      2. Append Figma frame renders if the URL is supplied.
      3. Truncate to MAX_IMAGES so the vision call stays under the
         model's context window and our cost ceiling.
      4. Call Claude with all images in one message.
      5. Normalise the response to a bullet list.
    """
    res = VisionResult()
    images: list[bytes] = []
    sources: list[str] = []

    for fp in list(file_paths or []):
        try:
            chunks = _load_image_bytes(fp)
        except Exception as exc:
            _logger.warning("mockup load failed for %s: %s", fp, exc)
            chunks = []
        if chunks:
            images.extend(chunks)
            sources.append(os.path.basename(fp))
        else:
            res.warnings.append(f"Could not extract images from {os.path.basename(fp)}")

    if figma_url and figma_url.strip():
        figma_imgs = _figma_image_bytes(figma_url)
        if figma_imgs:
            images.extend(figma_imgs)
            sources.append("Figma URL")
        else:
            res.warnings.append(
                "Could not fetch any frames from the Figma URL — for "
                "authenticated files set FIGMA_PAT in your environment."
            )

    if not images:
        res.error = "No mockup images could be loaded."
        return res

    if len(images) > MAX_IMAGES:
        res.warnings.append(
            f"Truncated to first {MAX_IMAGES} images "
            f"(you supplied {len(images)})."
        )
        images = images[:MAX_IMAGES]

    raw, raw_full, llm_error = _call_claude_vision(images, context)
    if llm_error:
        res.error = llm_error
        res.image_count = len(images)
        res.source_label = " + ".join(sources)
        return res
    if not raw:
        res.error = (
            "Vision analysis returned no output. "
            "Check ANTHROPIC_API_KEY is set on the server."
        )
        res.image_count = len(images)
        res.source_label = " + ".join(sources)
        return res

    res.text = _normalise_to_bullets(raw)
    res.raw_response = raw_full
    res.image_count = len(images)
    res.source_label = " + ".join(sources)
    return res


__all__ = ["VisionResult", "analyse",
           "MAX_IMAGES", "MAX_BYTES_PER_IMAGE", "TARGET_LONG_EDGE"]
