# BIMsmith Analytics — UI/UX Redesign Audit Tool

## What It Does

This tool automatically compares Figma mockups against the live BIMsmith Analytics site and generates a comprehensive HTML report with:

1. **Side-by-side screenshots** — Figma mockup (left) vs live site (right)
2. **Pixel-diff overlays** — differences highlighted with red rectangles & arrows
3. **SSIM score** — structural similarity metric (0–1, threshold 0.92)
4. **Accessibility audit** — WCAG 2.2 AA, Section 508, EN 301 549 via axe-core
5. **UI/UX heuristic checks** — broken elements, overflow, touch targets, focus styles, form labels, z-index chaos, horizontal scroll
6. **Console errors** — browser warnings/errors captured during navigation

## Standards Applied

| Standard | Scope |
|----------|-------|
| WCAG 2.2 Level AA | Color contrast, keyboard nav, ARIA, focus, labels |
| Section 508 | US federal accessibility requirements |
| EN 301 549 | EU digital accessibility standard |
| WCAG 2.5.8 | Touch target minimum 44×44px |
| WCAG 1.4.3 | Contrast ratio 4.5:1 (normal) / 3:1 (large text) |
| WCAG 2.4.7 | Visible focus indicators |
| Nielsen Norman 10 Heuristics | General usability evaluation |

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Playwright browser
playwright install chromium

# 3. Place Figma mockup PNGs in ./figma_mockups/
#    (already provided in the archive)
```

## Usage

```bash
# Basic run with defaults
python run_audit.py

# Full options
python run_audit.py \
  --site https://analytics-dev.bimsmith.com \
  --login man@user3.com \
  --password 'Qa123456+' \
  --mockups ./figma_mockups \
  --output ./audit_report
```

## Output

After running, open `./audit_report/audit_report.html` in a browser.

The report contains:
- Summary statistics (pages audited, SSIM scores, issue counts)
- Table of contents with pass/fail indicators
- Per-page sections with images + issue tables
- Console error log

## Customization

### Adding Pages to Audit

Edit the `PAGE_MAP` list in `run_audit.py` to add/modify page mappings:

```python
PAGE_MAP = [
    {
        "mockup": "YourMockup.png",      # filename in mockups dir
        "name": "Page Display Name",
        "route": "/your-route",           # site path after base URL
        "actions": [                      # optional browser actions
            {"type": "click", "selector": ".some-button"},
            {"type": "select_dropdown", "selector": "select.brand", "value": "BIMsmith"},
        ],
        "brand": "any"
    },
]
```

### Adjusting Thresholds

In `run_audit.py`:
- `SSIM_THRESHOLD = 0.92` — similarity score to pass/fail
- `MIN_CONTOUR_AREA = 200` — ignore pixel diff regions smaller than this
- `MIN_TOUCH_TARGET = 44` — WCAG touch target minimum in px

## Troubleshooting

- **Login fails**: Check that the selectors in the login section match your site's form
- **Pages not found**: Update `PAGE_MAP` routes to match actual site URLs
- **axe-core fails**: The CDN URL may be blocked; download axe.min.js locally
- **Images differ in size**: The tool auto-resizes; large differences reduce SSIM accuracy
