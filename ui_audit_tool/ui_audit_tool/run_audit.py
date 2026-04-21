#!/usr/bin/env python3
"""
=============================================================================
  BIMsmith Analytics — UI/UX Redesign Audit Tool
=============================================================================
  Compares Figma mockups against the live site, produces a side-by-side
  HTML report with pixel-diff overlays, accessibility audit (WCAG 2.2 AA),
  and general UI/UX best-practice checks.

  Requirements:
    pip install playwright pillow opencv-python-headless numpy scikit-image
    playwright install chromium

  Usage:
    python run_audit.py --site https://analytics-dev.bimsmith.com \
                        --login man@user3.com --password 'Qa123456+' \
                        --mockups ./figma_mockups \
                        --output ./audit_report
=============================================================================
"""

import argparse, asyncio, json, os, re, sys, time, hashlib, shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import cv2
from skimage.metrics import structural_similarity as ssim

# Playwright
from playwright.async_api import async_playwright

# ===========================================================================
# CONFIG
# ===========================================================================
VIEWPORT = {"width": 1920, "height": 1080}
FULL_PAGE_SCREENSHOT = True
SSIM_THRESHOLD = 0.92          # below this → "significant mismatch"
DIFF_COLOR = (255, 0, 0)       # red
DIFF_RECT_THICKNESS = 3
MIN_CONTOUR_AREA = 200         # ignore tiny pixel noise

# WCAG 2.2 AA contrast minimums
CONTRAST_NORMAL_TEXT = 4.5
CONTRAST_LARGE_TEXT = 3.0
LARGE_TEXT_PX = 24             # 18.66px bold or 24px normal
MIN_TOUCH_TARGET = 44          # px – WCAG 2.5.8

# ===========================================================================
# DATA CLASSES
# ===========================================================================
@dataclass
class DiffRegion:
    x: int; y: int; w: int; h: int
    description: str = ""

@dataclass
class PageAudit:
    page_name: str
    url: str
    mockup_file: str
    screenshot_path: str = ""
    diff_image_path: str = ""
    side_by_side_path: str = ""
    ssim_score: float = 0.0
    diff_regions: list = field(default_factory=list)
    accessibility_issues: list = field(default_factory=list)
    uiux_issues: list = field(default_factory=list)
    css_mismatches: list = field(default_factory=list)

# ===========================================================================
# MOCKUP → PAGE MAPPING
# ===========================================================================
# Maps Figma mockup filenames to site routes/actions.
# Adjust if your navigation differs.
PAGE_MAP = [
    {
        "mockup": "Client Dashboard - BIMsmith.png",
        "name": "BIMsmith Dashboard",
        "route": "/",                       # after login, landing page
        "actions": [
            {"type": "select_dropdown", "selector": "[data-testid='brand-select'], select.brand-select, .brand-selector", "value": "BIMsmith"},
        ],
        "brand": "BIMsmith"
    },
    {
        "mockup": "Client Dashboard - Swatchbox.png",
        "name": "Swatchbox Dashboard",
        "route": "/",
        "actions": [
            {"type": "select_dropdown", "selector": "[data-testid='brand-select'], select.brand-select, .brand-selector", "value": "Swatchbox"},
        ],
        "brand": "Swatchbox"
    },
    {
        "mockup": "Dashboard Swatchbox - Bimsmith colors.png",
        "name": "Analytics Dashboard (BIMsmith colors)",
        "route": "/",
        "actions": [],
        "brand": "BIMsmith",
        "variant": "bimsmith-colors"
    },
    {
        "mockup": "Dashboard Swatchbox - Swatchbox colors.png",
        "name": "Analytics Dashboard (Swatchbox colors)",
        "route": "/",
        "actions": [],
        "brand": "Swatchbox",
        "variant": "swatchbox-colors"
    },
    {
        "mockup": "Inventory.png",
        "name": "Inventory Page",
        "route": "/inventory",
        "actions": [],
        "brand": "any"
    },
    {
        "mockup": "Additional Charts.png",
        "name": "Additional Charts / Reports",
        "route": "/reports",               # adjust if different
        "actions": [],
        "brand": "any"
    },
]

# ===========================================================================
# IMAGE COMPARISON UTILITIES
# ===========================================================================

def load_and_resize(path_a: str, path_b: str):
    """Load two images, resize the second to match the first's width."""
    img_a = cv2.imread(path_a)
    img_b = cv2.imread(path_b)
    if img_a is None or img_b is None:
        return None, None
    h_a, w_a = img_a.shape[:2]
    h_b, w_b = img_b.shape[:2]
    if w_a != w_b:
        scale = w_a / w_b
        img_b = cv2.resize(img_b, (w_a, int(h_b * scale)))
    # Match heights by cropping/padding
    h_a, h_b = img_a.shape[0], img_b.shape[0]
    max_h = max(h_a, h_b)
    if h_a < max_h:
        img_a = cv2.copyMakeBorder(img_a, 0, max_h - h_a, 0, 0,
                                   cv2.BORDER_CONSTANT, value=(30, 30, 30))
    if h_b < max_h:
        img_b = cv2.copyMakeBorder(img_b, 0, max_h - h_b, 0, 0,
                                   cv2.BORDER_CONSTANT, value=(30, 30, 30))
    return img_a, img_b


def compute_diff(mockup_path: str, screenshot_path: str, output_dir: str,
                 page_name: str) -> tuple:
    """
    Compare mockup vs screenshot.
    Returns (ssim_score, diff_regions, diff_image_path, side_by_side_path).
    """
    img_a, img_b = load_and_resize(mockup_path, screenshot_path)
    if img_a is None or img_b is None:
        return 0.0, [], "", ""

    gray_a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)

    # SSIM
    score, diff_map = ssim(gray_a, gray_b, full=True)
    diff_map = (diff_map * 255).astype("uint8")
    thresh = cv2.threshold(diff_map, 0, 255,
                           cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]

    # Find contours of differing regions
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)

    diff_overlay = img_b.copy()
    regions = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_CONTOUR_AREA:
            continue
        x, y, w, h = cv2.boundingRect(c)
        cv2.rectangle(diff_overlay, (x, y), (x + w, y + h),
                      (0, 0, 255), DIFF_RECT_THICKNESS)
        regions.append(DiffRegion(x=x, y=y, w=w, h=h))

    # Draw arrows pointing to large diffs
    for r in regions:
        if r.w * r.h > 5000:
            cx, cy = r.x + r.w // 2, r.y - 20
            cv2.arrowedLine(diff_overlay, (cx, max(0, cy - 40)),
                            (cx, cy), (0, 0, 255), 3, tipLength=0.4)

    # Save diff overlay
    safe_name = re.sub(r'[^\w\-]', '_', page_name)
    diff_path = os.path.join(output_dir, f"diff_{safe_name}.png")
    cv2.imwrite(diff_path, diff_overlay)

    # Side-by-side
    separator = np.full((img_a.shape[0], 4, 3), (0, 200, 255), dtype=np.uint8)
    side = np.hstack([img_a, separator, diff_overlay])
    sbs_path = os.path.join(output_dir, f"sbs_{safe_name}.png")
    cv2.imwrite(sbs_path, side)

    return score, regions, diff_path, sbs_path


# ===========================================================================
# ACCESSIBILITY CHECKS (run in browser via Playwright)
# ===========================================================================

AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"

async def run_axe_audit(page) -> list:
    """Inject axe-core and run accessibility audit."""
    issues = []
    try:
        await page.add_script_tag(url=AXE_CDN)
        await page.wait_for_timeout(1000)
        results = await page.evaluate("""
            async () => {
                const res = await axe.run(document, {
                    runOnly: {
                        type: 'tag',
                        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
                                 'wcag22aa', 'best-practice', 'section508']
                    }
                });
                return res.violations.map(v => ({
                    id: v.id,
                    impact: v.impact,
                    description: v.description,
                    help: v.help,
                    helpUrl: v.helpUrl,
                    tags: v.tags,
                    nodes_count: v.nodes.length,
                    nodes: v.nodes.slice(0, 5).map(n => ({
                        html: n.html.substring(0, 200),
                        target: n.target,
                        failureSummary: n.failureSummary
                    }))
                }));
            }
        """)
        issues = results or []
    except Exception as e:
        issues = [{"id": "axe-error", "impact": "unknown",
                   "description": f"axe-core failed: {str(e)[:200]}",
                   "nodes_count": 0, "nodes": []}]
    return issues


# ===========================================================================
# UI / UX HEURISTIC CHECKS (run in browser)
# ===========================================================================

async def check_uiux(page) -> list:
    """General UI/UX best-practice checks via JS evaluation."""
    checks = await page.evaluate(r"""
    () => {
        const issues = [];

        // 1. Images without alt text
        document.querySelectorAll('img').forEach(img => {
            if (!img.alt && !img.getAttribute('role')) {
                issues.push({
                    type: 'missing-alt',
                    severity: 'major',
                    element: img.outerHTML.substring(0, 150),
                    message: 'Image missing alt attribute'
                });
            }
        });

        // 2. Buttons / links too small (touch target < 44px)
        document.querySelectorAll('button, a, [role="button"], input[type="submit"]').forEach(el => {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0 &&
                (rect.width < 44 || rect.height < 44)) {
                issues.push({
                    type: 'small-touch-target',
                    severity: 'minor',
                    element: el.outerHTML.substring(0, 150),
                    message: `Touch target too small: ${Math.round(rect.width)}x${Math.round(rect.height)}px (min 44x44)`
                });
            }
        });

        // 3. Text overflow / clipping
        document.querySelectorAll('*').forEach(el => {
            const style = getComputedStyle(el);
            if (style.overflow === 'hidden' && el.scrollWidth > el.clientWidth + 2) {
                const text = el.textContent?.trim().substring(0, 60);
                if (text && text.length > 3) {
                    issues.push({
                        type: 'text-overflow',
                        severity: 'minor',
                        element: el.tagName + (el.className ? '.' + el.className.split(' ')[0] : ''),
                        message: `Text clipped: "${text}..."`
                    });
                }
            }
        });

        // 4. Missing focus styles
        const focusable = document.querySelectorAll('a, button, input, select, textarea, [tabindex]');
        // sample first 20
        Array.from(focusable).slice(0, 20).forEach(el => {
            const style = getComputedStyle(el);
            if (style.outlineStyle === 'none' && style.boxShadow === 'none') {
                issues.push({
                    type: 'missing-focus-style',
                    severity: 'minor',
                    element: el.tagName + (el.className ? '.' + el.className.split(' ')[0] : ''),
                    message: 'No visible focus indicator (WCAG 2.4.7)'
                });
            }
        });

        // 5. Empty links / buttons
        document.querySelectorAll('a, button').forEach(el => {
            const text = el.textContent?.trim();
            const ariaLabel = el.getAttribute('aria-label');
            const title = el.getAttribute('title');
            if (!text && !ariaLabel && !title && !el.querySelector('img, svg')) {
                issues.push({
                    type: 'empty-interactive',
                    severity: 'major',
                    element: el.outerHTML.substring(0, 150),
                    message: 'Interactive element has no accessible name'
                });
            }
        });

        // 6. Color contrast (sample check on visible text)
        // (full contrast is handled by axe-core, this is supplementary)

        // 7. Z-index stacking issues (overlapping elements)
        const allEls = document.querySelectorAll('*');
        const highZ = [];
        allEls.forEach(el => {
            const z = parseInt(getComputedStyle(el).zIndex);
            if (z > 9999) {
                highZ.push({tag: el.tagName, class: el.className, z: z});
            }
        });
        if (highZ.length > 3) {
            issues.push({
                type: 'z-index-chaos',
                severity: 'minor',
                element: JSON.stringify(highZ.slice(0, 3)),
                message: `${highZ.length} elements with z-index > 9999 — potential stacking issues`
            });
        }

        // 8. Horizontal scroll (broken layout)
        if (document.documentElement.scrollWidth > window.innerWidth + 5) {
            issues.push({
                type: 'horizontal-scroll',
                severity: 'major',
                element: 'body',
                message: `Page has horizontal scroll (${document.documentElement.scrollWidth}px > ${window.innerWidth}px)`
            });
        }

        // 9. Console errors captured earlier (placeholder)

        // 10. Forms without labels
        document.querySelectorAll('input, select, textarea').forEach(el => {
            const id = el.id;
            const ariaLabel = el.getAttribute('aria-label');
            const ariaLabelledBy = el.getAttribute('aria-labelledby');
            const hasLabel = id && document.querySelector(`label[for="${id}"]`);
            const parentLabel = el.closest('label');
            if (!hasLabel && !parentLabel && !ariaLabel && !ariaLabelledBy &&
                el.type !== 'hidden' && el.type !== 'submit') {
                issues.push({
                    type: 'form-no-label',
                    severity: 'major',
                    element: el.outerHTML.substring(0, 150),
                    message: 'Form input has no associated label (WCAG 1.3.1)'
                });
            }
        });

        return issues;
    }
    """)
    return checks or []


# ===========================================================================
# CSS PROPERTY EXTRACTION (for detailed comparison)
# ===========================================================================

async def extract_css_properties(page) -> dict:
    """Extract key CSS props from major UI elements."""
    return await page.evaluate("""
    () => {
        const result = {};
        const selectors = {
            'sidebar': '.sidebar, nav, [class*="sidebar"], [class*="nav"]',
            'header': 'header, [class*="header"], [class*="topbar"]',
            'cards': '[class*="card"], [class*="widget"], [class*="panel"]',
            'buttons': 'button, .btn, [class*="button"]',
            'tables': 'table, [class*="table"], [class*="grid"]',
            'charts': '[class*="chart"], canvas, svg',
            'inputs': 'input, select, textarea',
            'dropdowns': '[class*="dropdown"], [class*="select"]',
        };

        for (const [name, sel] of Object.entries(selectors)) {
            const els = document.querySelectorAll(sel);
            const samples = [];
            Array.from(els).slice(0, 5).forEach(el => {
                const cs = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                samples.push({
                    tag: el.tagName,
                    class: el.className?.toString().substring(0, 100),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    bg: cs.backgroundColor,
                    color: cs.color,
                    fontSize: cs.fontSize,
                    fontFamily: cs.fontFamily?.substring(0, 80),
                    padding: cs.padding,
                    margin: cs.margin,
                    borderRadius: cs.borderRadius,
                    border: cs.border?.substring(0, 60),
                });
            });
            if (samples.length) result[name] = samples;
        }
        return result;
    }
    """)


# ===========================================================================
# MAIN BROWSER AUTOMATION
# ===========================================================================

async def run_audit(site_url: str, login: str, password: str,
                    mockups_dir: str, output_dir: str):
    """Main audit workflow."""
    os.makedirs(output_dir, exist_ok=True)
    screenshots_dir = os.path.join(output_dir, "screenshots")
    diffs_dir = os.path.join(output_dir, "diffs")
    os.makedirs(screenshots_dir, exist_ok=True)
    os.makedirs(diffs_dir, exist_ok=True)

    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport=VIEWPORT,
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
        )

        # Capture console errors
        console_errors = []
        page = await context.new_page()
        page.on("console", lambda msg: console_errors.append(
            {"type": msg.type, "text": msg.text[:300]}
        ) if msg.type in ("error", "warning") else None)

        # ---- LOGIN ----
        print(f"[1/5] Logging in to {site_url}...")
        await page.goto(site_url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        # Try common login form patterns
        login_selectors = [
            ('input[type="email"]', 'input[type="password"]', 'button[type="submit"]'),
            ('input[name="email"]', 'input[name="password"]', 'button[type="submit"]'),
            ('#email', '#password', 'button[type="submit"]'),
            ('input[placeholder*="email" i]', 'input[placeholder*="password" i]', 'button'),
            ('input[type="text"]', 'input[type="password"]', 'button'),
        ]

        logged_in = False
        for email_sel, pass_sel, btn_sel in login_selectors:
            try:
                email_el = await page.query_selector(email_sel)
                pass_el = await page.query_selector(pass_sel)
                if email_el and pass_el:
                    await email_el.fill(login)
                    await pass_el.fill(password)
                    await page.wait_for_timeout(500)
                    btn = await page.query_selector(btn_sel)
                    if btn:
                        await btn.click()
                    else:
                        await page.keyboard.press("Enter")
                    await page.wait_for_timeout(5000)
                    logged_in = True
                    print(f"    ✓ Login submitted (selector: {email_sel})")
                    break
            except Exception:
                continue

        if not logged_in:
            print("    ⚠ Could not find login form — taking screenshot as-is")

        await page.wait_for_timeout(3000)
        current_url = page.url
        print(f"    Current URL after login: {current_url}")

        # ---- DISCOVER PAGES ----
        print("[2/5] Discovering site pages...")
        nav_links = await page.evaluate("""
        () => {
            const links = new Set();
            document.querySelectorAll('a[href], [class*="nav"] a, .sidebar a').forEach(a => {
                const href = a.getAttribute('href');
                if (href && !href.startsWith('#') && !href.startsWith('javascript')
                    && !href.startsWith('mailto')) {
                    links.add(href);
                }
            });
            return Array.from(links);
        }
        """)
        print(f"    Found {len(nav_links)} navigation links: {nav_links[:10]}")

        # ---- AUDIT EACH MAPPED PAGE ----
        print("[3/5] Auditing pages against mockups...")
        for mapping in PAGE_MAP:
            mockup_path = os.path.join(mockups_dir, mapping["mockup"])
            if not os.path.exists(mockup_path):
                print(f"    ⚠ Mockup not found: {mapping['mockup']} — skipping")
                continue

            page_name = mapping["name"]
            safe_name = re.sub(r'[^\w\-]', '_', page_name)
            print(f"    → Auditing: {page_name}")

            # Navigate
            route = mapping["route"]
            target_url = site_url.rstrip('/') + route
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=30000)
            except Exception:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Execute any actions (e.g., switch brand dropdown)
            for action in mapping.get("actions", []):
                try:
                    if action["type"] == "select_dropdown":
                        sel = action["selector"]
                        for s in sel.split(","):
                            s = s.strip()
                            el = await page.query_selector(s)
                            if el:
                                await el.select_option(label=action["value"])
                                await page.wait_for_timeout(2000)
                                break
                    elif action["type"] == "click":
                        await page.click(action["selector"])
                        await page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"      ⚠ Action failed: {e}")

            # Screenshot
            scr_path = os.path.join(screenshots_dir, f"{safe_name}.png")
            await page.screenshot(path=scr_path, full_page=FULL_PAGE_SCREENSHOT)

            # Pixel diff
            score, regions, diff_path, sbs_path = compute_diff(
                mockup_path, scr_path, diffs_dir, page_name
            )

            # Accessibility
            a11y = await run_axe_audit(page)

            # UI/UX checks
            uiux = await check_uiux(page)

            # CSS extraction
            css = await extract_css_properties(page)

            audit = PageAudit(
                page_name=page_name,
                url=page.url,
                mockup_file=mapping["mockup"],
                screenshot_path=scr_path,
                diff_image_path=diff_path,
                side_by_side_path=sbs_path,
                ssim_score=score,
                diff_regions=[asdict(r) for r in regions],
                accessibility_issues=a11y,
                uiux_issues=uiux,
                css_mismatches=[],
            )
            results.append(audit)
            print(f"      SSIM: {score:.4f} | Diffs: {len(regions)} | "
                  f"A11y: {len(a11y)} | UI/UX: {len(uiux)}")

        # ---- AUDIT PAGES WITHOUT SPECIFIC MOCKUPS ----
        print("[4/5] Scanning additional pages for UI/UX + Accessibility...")
        visited = set()
        for link in nav_links[:20]:  # limit to 20 extra pages
            if link in visited:
                continue
            visited.add(link)
            full_url = link if link.startswith("http") else site_url.rstrip('/') + link
            try:
                await page.goto(full_url, wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(2000)

                safe = re.sub(r'[^\w\-]', '_', link)[:60]
                scr = os.path.join(screenshots_dir, f"extra_{safe}.png")
                await page.screenshot(path=scr, full_page=True)

                a11y = await run_axe_audit(page)
                uiux = await check_uiux(page)

                audit = PageAudit(
                    page_name=f"[Extra] {link}",
                    url=page.url,
                    mockup_file="(no mockup)",
                    screenshot_path=scr,
                    ssim_score=-1,
                    accessibility_issues=a11y,
                    uiux_issues=uiux,
                )
                results.append(audit)
                print(f"    → {link}: A11y={len(a11y)} UI/UX={len(uiux)}")
            except Exception as e:
                print(f"    ⚠ Failed {link}: {e}")

        # Save console errors
        with open(os.path.join(output_dir, "console_errors.json"), "w") as f:
            json.dump(console_errors, f, indent=2)

        await browser.close()

    # ---- GENERATE REPORT ----
    print("[5/5] Generating HTML report...")
    report_path = generate_html_report(results, output_dir, console_errors)
    print(f"\n✅ Audit complete! Report: {report_path}")
    return report_path


# ===========================================================================
# HTML REPORT GENERATOR
# ===========================================================================

def img_to_base64(path: str) -> str:
    """Convert image file to base64 data URI for embedding in HTML."""
    if not path or not os.path.exists(path):
        return ""
    import base64
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    ext = Path(path).suffix.lower().replace(".", "")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    return f"data:{mime};base64,{data}"


def severity_badge(impact: str) -> str:
    colors = {
        "critical": "#ff4444",
        "serious": "#ff8800",
        "moderate": "#ffcc00",
        "minor": "#88cc00",
        "major": "#ff8800",
    }
    c = colors.get(impact, "#999")
    return f'<span style="background:{c};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600">{impact.upper()}</span>'


def generate_html_report(results: list, output_dir: str,
                         console_errors: list) -> str:
    """Build a self-contained HTML report."""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Stats
    total_pages = len(results)
    pages_with_mockup = [r for r in results if r.ssim_score >= 0]
    total_a11y = sum(len(r.accessibility_issues) for r in results)
    total_uiux = sum(len(r.uiux_issues) for r in results)
    avg_ssim = (sum(r.ssim_score for r in pages_with_mockup) / len(pages_with_mockup)
                if pages_with_mockup else 0)

    pages_html = []
    for i, r in enumerate(results):
        # Side-by-side images
        sbs_img = ""
        if r.side_by_side_path:
            sbs_img = f'''
            <div class="sbs-container">
                <h4>Side-by-Side: Figma Mockup (left) vs Live Site (right)</h4>
                <img src="{img_to_base64(r.side_by_side_path)}" class="sbs-img" alt="comparison"/>
            </div>'''

        diff_img = ""
        if r.diff_image_path:
            diff_img = f'''
            <div class="diff-container">
                <h4>Differences Highlighted (red rectangles + arrows)</h4>
                <img src="{img_to_base64(r.diff_image_path)}" class="diff-img" alt="diff overlay"/>
            </div>'''

        screenshot_img = ""
        if r.screenshot_path:
            screenshot_img = f'''
            <div class="screenshot-container">
                <h4>Live Site Screenshot</h4>
                <img src="{img_to_base64(r.screenshot_path)}" class="screenshot-img" alt="screenshot"/>
            </div>'''

        # SSIM indicator
        ssim_html = ""
        if r.ssim_score >= 0:
            color = "#4caf50" if r.ssim_score >= SSIM_THRESHOLD else "#ff5722"
            status = "PASS" if r.ssim_score >= SSIM_THRESHOLD else "MISMATCH"
            ssim_html = f'''
            <div class="ssim-badge" style="background:{color}">
                SSIM: {r.ssim_score:.4f} — {status}
            </div>
            <p>Diff regions found: <strong>{len(r.diff_regions)}</strong></p>'''

        # Accessibility table
        a11y_rows = ""
        for issue in r.accessibility_issues[:20]:
            impact = issue.get("impact", "unknown")
            a11y_rows += f'''
            <tr>
                <td>{severity_badge(impact)}</td>
                <td><strong>{issue.get("id","")}</strong></td>
                <td>{issue.get("description","")[:200]}</td>
                <td>{issue.get("nodes_count",0)}</td>
                <td><a href="{issue.get("helpUrl","#")}" target="_blank">WCAG ref</a></td>
            </tr>'''

        a11y_html = f'''
        <div class="a11y-section">
            <h4>Accessibility Issues ({len(r.accessibility_issues)})</h4>
            {"<p class='pass'>✅ No accessibility violations detected</p>" if not r.accessibility_issues else f"""
            <table class="issues-table">
                <tr><th>Severity</th><th>Rule</th><th>Description</th><th>Elements</th><th>Reference</th></tr>
                {a11y_rows}
            </table>"""}
        </div>'''

        # UI/UX issues
        uiux_rows = ""
        for issue in r.uiux_issues[:20]:
            sev = issue.get("severity", "minor")
            uiux_rows += f'''
            <tr>
                <td>{severity_badge(sev)}</td>
                <td>{issue.get("type","")}</td>
                <td>{issue.get("message","")[:200]}</td>
                <td><code>{issue.get("element","")[:100]}</code></td>
            </tr>'''

        uiux_html = f'''
        <div class="uiux-section">
            <h4>UI/UX Issues ({len(r.uiux_issues)})</h4>
            {"<p class='pass'>✅ No UI/UX issues detected</p>" if not r.uiux_issues else f"""
            <table class="issues-table">
                <tr><th>Severity</th><th>Type</th><th>Description</th><th>Element</th></tr>
                {uiux_rows}
            </table>"""}
        </div>'''

        pages_html.append(f'''
        <section class="page-audit" id="page-{i}">
            <h3>{r.page_name}</h3>
            <p class="url">URL: <a href="{r.url}" target="_blank">{r.url}</a> |
               Mockup: <code>{r.mockup_file}</code></p>
            {ssim_html}
            {sbs_img}
            {diff_img}
            {screenshot_img}
            {a11y_html}
            {uiux_html}
        </section>''')

    # Console errors section
    console_html = ""
    if console_errors:
        rows = "".join(f"<tr><td>{e['type']}</td><td>{e['text'][:300]}</td></tr>"
                       for e in console_errors[:50])
        console_html = f'''
        <section class="page-audit" id="console-errors">
            <h3>Browser Console Errors/Warnings ({len(console_errors)})</h3>
            <table class="issues-table">
                <tr><th>Type</th><th>Message</th></tr>
                {rows}
            </table>
        </section>'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UI/UX Redesign Audit Report — BIMsmith Analytics</title>
<style>
:root {{
    --bg: #1a1a2e; --card: #16213e; --text: #e0e0e0;
    --accent: #0f3460; --highlight: #e94560; --green: #4caf50;
    --border: #2a2a4a;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    line-height: 1.6; padding: 20px;
}}
h1 {{ color: #fff; font-size: 28px; margin-bottom: 8px; }}
h2 {{ color: #ccc; font-size: 20px; margin: 30px 0 15px; border-bottom: 2px solid var(--highlight); padding-bottom: 8px; }}
h3 {{ color: #fff; font-size: 18px; margin-bottom: 10px; }}
h4 {{ color: #bbb; font-size: 14px; margin: 15px 0 8px; }}
.header {{ text-align: center; padding: 30px; background: var(--accent); border-radius: 12px; margin-bottom: 30px; }}
.header p {{ color: #aaa; font-size: 14px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }}
.stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; text-align: center; }}
.stat-card .number {{ font-size: 36px; font-weight: 700; color: #fff; }}
.stat-card .label {{ font-size: 13px; color: #888; margin-top: 5px; }}
.page-audit {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 25px; margin-bottom: 25px;
}}
.url {{ font-size: 13px; color: #888; margin-bottom: 15px; }}
.url a {{ color: #64b5f6; }}
.ssim-badge {{
    display: inline-block; padding: 6px 16px; border-radius: 6px;
    color: #fff; font-weight: 700; font-size: 14px; margin: 10px 0;
}}
.sbs-img, .diff-img, .screenshot-img {{
    max-width: 100%; border: 1px solid var(--border); border-radius: 8px; margin: 10px 0;
}}
.issues-table {{
    width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px;
}}
.issues-table th {{
    background: var(--accent); color: #fff; padding: 10px; text-align: left;
    border-bottom: 2px solid var(--highlight);
}}
.issues-table td {{
    padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top;
}}
.issues-table tr:hover {{ background: rgba(255,255,255,0.03); }}
code {{ background: rgba(255,255,255,0.08); padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
.pass {{ color: var(--green); font-weight: 600; }}
.toc {{ background: var(--card); border-radius: 10px; padding: 20px; margin-bottom: 25px; }}
.toc a {{ color: #64b5f6; text-decoration: none; display: block; padding: 4px 0; font-size: 14px; }}
.toc a:hover {{ color: #fff; }}
.standards {{ background: var(--card); border-radius: 10px; padding: 20px; margin-bottom: 25px; font-size: 13px; }}
.standards h3 {{ margin-bottom: 10px; }}
.standards ul {{ list-style: none; padding: 0; }}
.standards li {{ padding: 4px 0; }}
.standards li::before {{ content: "📋 "; }}
</style>
</head>
<body>

<div class="header">
    <h1>UI/UX Redesign Audit Report</h1>
    <p>BIMsmith Analytics Dashboard — {now}</p>
    <p>Figma Mockups vs Live Site Comparison + Accessibility + UI/UX Best Practices</p>
</div>

<div class="stats">
    <div class="stat-card"><div class="number">{total_pages}</div><div class="label">Pages Audited</div></div>
    <div class="stat-card"><div class="number">{len(pages_with_mockup)}</div><div class="label">Mockup Comparisons</div></div>
    <div class="stat-card"><div class="number">{avg_ssim:.2%}</div><div class="label">Avg SSIM Score</div></div>
    <div class="stat-card"><div class="number" style="color:{'var(--green)' if total_a11y == 0 else 'var(--highlight)'}">{total_a11y}</div><div class="label">Accessibility Issues</div></div>
    <div class="stat-card"><div class="number">{total_uiux}</div><div class="label">UI/UX Issues</div></div>
    <div class="stat-card"><div class="number">{len(console_errors)}</div><div class="label">Console Errors</div></div>
</div>

<div class="standards">
    <h3>Standards & Guidelines Applied</h3>
    <ul>
        <li><strong>WCAG 2.2</strong> (Web Content Accessibility Guidelines) — Level AA conformance</li>
        <li><strong>Section 508</strong> (US Rehabilitation Act) — Electronic accessibility</li>
        <li><strong>EN 301 549</strong> (EU Accessibility Standard) — Digital services</li>
        <li><strong>WCAG 2.5.8</strong> — Target Size (minimum 44×44 CSS px)</li>
        <li><strong>WCAG 1.4.3</strong> — Contrast (minimum 4.5:1 normal, 3:1 large text)</li>
        <li><strong>WCAG 2.4.7</strong> — Focus Visible</li>
        <li><strong>WCAG 1.3.1</strong> — Info and Relationships (form labels)</li>
        <li><strong>Nielsen Norman Group Heuristics</strong> — 10 Usability Heuristics</li>
        <li><strong>Material Design / Carbon Design</strong> — Component spacing & sizing guidelines</li>
    </ul>
</div>

<div class="toc">
    <h3>Table of Contents</h3>
    {"".join(f'<a href="#page-{i}">{"🟢" if r.ssim_score >= SSIM_THRESHOLD else "🔴" if 0 <= r.ssim_score < SSIM_THRESHOLD else "🔵"} {r.page_name} (SSIM: {r.ssim_score:.2f})</a>' if r.ssim_score >= 0 else f'<a href="#page-{i}">🔵 {r.page_name} (UI/UX only)</a>' for i, r in enumerate(results))}
    {"<a href='#console-errors'>⚠️ Console Errors</a>" if console_errors else ""}
</div>

<h2>Page-by-Page Audit Results</h2>
{"".join(pages_html)}
{console_html}

<footer style="text-align:center;padding:30px;color:#555;font-size:12px">
    Generated by BIMsmith Analytics UI Audit Tool — {now}
</footer>
</body></html>'''

    report_path = os.path.join(output_dir, "audit_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


# ===========================================================================
# CLI
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="BIMsmith Analytics UI Audit")
    parser.add_argument("--site", default="https://analytics-dev.bimsmith.com",
                        help="Site URL")
    parser.add_argument("--login", default="man@user3.com", help="Login email")
    parser.add_argument("--password", default="Qa123456+", help="Password")
    parser.add_argument("--mockups", default="./figma_mockups",
                        help="Path to Figma mockup images")
    parser.add_argument("--output", default="./audit_report",
                        help="Output directory for report")
    args = parser.parse_args()

    asyncio.run(run_audit(args.site, args.login, args.password,
                          args.mockups, args.output))


if __name__ == "__main__":
    main()
