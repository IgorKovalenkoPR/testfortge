# Brand alignment — TestFort Brand Guidelines 2025

Status: **phases 1–2 implemented 2026-10-16** (owner decisions below); phase 3 and the logo wait on the designer. Scope: `static/css/style.css` tokens and component rules; no behaviour changes.

## Decisions (owner, 2026-10-16)

| Q | Decision | Implemented as |
|---|---|---|
| Q1 Background | #E6EEF3 around the content, cards stay white | `--tf-bg: #E6EEF3`; `--tf-surface` unchanged; the four token darkenings of §2.4 shipped with it |
| Q2 Sidebar | Flat #202020 | `--tf-dark`; sidebar, its sticky footer and the mobile bar |
| Q3 Filled accents | Coral with #202020 text, one style everywhere | `--accent-fill` / `--accent-fill-hover` / `--on-accent-fill`: primary button, language toggle, skip link, step badges, active filter/metric chips, back-to-top, chat send and user bubble |
| Q6 Links | #B93C12 | `--accent` unchanged |
| Q4 Gradient | Decorative only (recommendation taken; the owner did not object) | `--tf-grad-blue` defined, used by nothing yet; `--tf-blue` → #557FF1, used only as a card edge |
| Q7 Body size | 14px body (recommendation taken) | `body` 14/24; h1 28/42, card h2 22/30, card h3 20/28 Medium; headings 600 |
| Q5 Logo | Waits on the designer's SVG | CSS wordmark unchanged |

Also: table headers and the chat header on `--tf-dark`, section rows and `.btn-export` (pill) on `--tf-dark-2`, focus ring `#2F5BD8`.
Deviation: the Google Fonts link still loads 700/800 — 42 rules use those weights and dropping the files would make the browser synthesise bold. Moving them to 600 is part of phase 3.
Guards: `tests/test_design_tokens.py` — coral/#202020 pairs, white on `--tf-dark`/`--tf-dark-2`, warning/success on the page background, the focus ring on `--tf-dark`, a pin that `.btn-primary` renders the coral fill with dark text (mutation-checked); the per-rule AA scan resolves the new token chains (mutation: white on coral fails it). axe gate green on every screen.
Hard constraint: WCAG 2.1 AA (task 8). Every ratio below was computed with the WCAG relative-luminance formula, the same `contrast()` used by `tests/test_design_tokens.py`. Nothing here is estimated.

Short version: we can adopt the brand palette, the coral pill button, the dark sidebar and the Poppins weights without weakening AA. Three things cannot be used as the guide shows them. Coral (#FF865C) cannot be text on light backgrounds. The blue gradient cannot carry text. Four grey and status tokens have to get darker when the page background moves to #E6EEF3.

---

## 1. Gap table

Backgrounds: **W** = white surface #FFFFFF, **BG** = brand page background #E6EEF3, **D** = brand dark #202020.

| Brand element | Current app | Proposed | Contrast (computed) | Visual impact |
|---|---|---|---|---|
| Light background #E6EEF3 (60%) | `--tf-bg` #F7F8FA | `--tf-bg: #E6EEF3` | White card on BG: 1.17:1 (decorative edge only) | High. A cool blue-grey canvas makes white cards stand out more. This is the largest single brand signal. |
| Dark background #202020 (30%) | Sidebar gradient #0F1115 → #1A1D24 | Flat `#202020` (new `--tf-dark`) | Sidebar text: #B9BCC5 8.58, #9CA0AB 6.23, #8A8F9C 5.04 on D. All pass. | Low. The sidebar gets slightly lighter and loses the gradient. |
| Dark #464646 | Not used (`.btn-export` is #111111) | `--tf-dark-2: #464646` for dark pill buttons and secondary dark surfaces | White on #464646 9.44:1 | Medium. Export/secondary-dark buttons match the guide. |
| Coral accent #FF865C | `--tf-coral` exists. Interactive `--accent` is #B93C12 | Coral **fill** with #202020 text for primary buttons. `--accent` #B93C12 stays for links and text | #202020 on coral **6.84:1**. White on coral 2.38:1 (fail) | High. Primary CTAs become the brand coral. |
| Coral as link colour | Links use `--accent` #B93C12 | Keep #B93C12 (see §2.2) | 5.65 on W, **4.81 on BG**, 4.83 on `--accent-soft` | None. |
| Blue gradient #557FF1 → #66C8F2 | `--tf-blue` #18AAFD (2.56:1 on W; not brand) | `--tf-grad-blue: linear-gradient(135deg,#557FF1,#66C8F2)`, decorative only. Retire #18AAFD | #557FF1: 3.69 W / 3.14 BG. #66C8F2: 1.89 W / 1.61 BG | Medium. Card accents, icon fills and progress decoration. |
| Focus ring | `--focus` #1D4ED8 (Tailwind blue) | `--focus: #2F5BD8` (the brand-blue hue 224°, darkened) | 5.82 W, 4.96 BG (needs 3:1) | Low. The ring becomes visibly on-brand. |
| Focus on dark | `--focus-on-dark` #FF865C on #1A1D24 (7.08) | Unchanged, on #202020 | **6.84:1** | None. |
| Typeface | Poppins 400–800 loaded; headings 700, wordmark 800/900 | Poppins 400/500/600 only; headings 600 | n/a | Slightly lighter headings. Two fewer font files to download. |

## 2. Brand vs WCAG — resolutions

### 2.1 Primary button: coral fill, dark text
- `.btn-primary` = `--tf-coral` #FF865C fill with **#202020** text: **6.84:1**, which passes AA for any text size. Hover is `--tf-coral-hover` #FF6A36 with #202020 at 5.72:1.
- White text on coral is **2.38:1** and is never allowed. The existing test `test_the_contrast_maths_is_right` already records this figure.
- Button edge: the coral fill against W is 2.38:1 and against BG is 2.03:1. That is acceptable under SC 1.4.11 because the text label identifies the button, not the boundary. Secondary buttons keep their `--border-control` outline.
- Dark alternative from the guide: a `#464646` pill with white text, **9.44:1**. Use it for `.btn-export` and for "secondary emphasis".
- Other places that put white text on an accent fill: `.lang-btn.active`, `.skip-link`, `.step-badge` and active chips. They can stay on `--accent` #B93C12 (5.65:1), or move to coral with #202020 text. Pick one for consistency (open question Q3).

### 2.2 Links and accent text: coral fails, keep the dark coral
- Brand coral as text fails everywhere: **2.38:1 on W, 2.03:1 on BG, 2.24:1 on the old BG**.
- I searched the coral hue (15.7°) for the lightest shade that passes. The first shade at 4.5:1 on BG is #C93400 (4.50 BG / 5.28 W). It is exactly on the threshold, more saturated and redder than the brand. Rejected.
- **Keep `--accent: #B93C12`**: **5.65:1 on W, 4.81:1 on BG**. It is the closest coral-family shade that leaves margin on both surfaces. `--accent-hover` #A8360F: 5.59:1 on BG.
- Links stay underlined inside running text, as they already are and as the guide shows them.
- Coral text is allowed only on dark surfaces: **6.84:1 on #202020**. On #464646 it is 3.96:1, so only large text (≥ 24px, or ≥ 18.66px bold) there.

### 2.3 Blue gradient: what it may be used for
| Use | Verdict | Why (computed) |
|---|---|---|
| Text in any gradient colour on W or BG | **No** | #66C8F2 is 1.89 on W and 1.61 on BG |
| White or dark text on top of the gradient | **No** | White: 3.69 → 1.89. #202020: 4.42 → 8.62. Both fail 4.5:1 somewhere along the gradient |
| Large headings in solid #557FF1 | Possible | 3.69 W / 3.14 BG (≥ 3:1 large text). Not recommended: it competes with coral |
| Non-text indicators that carry meaning (chart series, progress) | Only the **#557FF1 end** | 3.69 W / 3.14 BG (≥ 3:1). #66C8F2 fails |
| Decoration: icon second tone, card top-stripe, illustrations, empty states | **Yes** | Decoration has no contrast requirement |
| Interactive blue (focus ring) | Use `#2F5BD8` | Same hue as #557FF1, 4.96 BG |

### 2.4 Page background #E6EEF3: what breaks
The darker background lowers every grey token's contrast. Computed against the old background (#F7F8FA), the new one (#E6EEF3) and white:

| Token | Value | on W | on old BG | **on new BG** | Action |
|---|---|---|---|---|---|
| `--text-muted` (`--tf-mute`) | #5E6472 | 5.93 | 5.58 | **5.05** pass | none |
| `--text-subtle` / `--text-light` | #6B7280 | 4.83 | 4.55 | **4.12 FAIL** | → **#636A78** (5.44 W / 4.63 BG) |
| `--border-control` (3:1) | #8A909C | 3.21 | n/a | **2.73 FAIL** | → **#767C88** (4.19 W / 3.57 BG) |
| `--warning` (text) | #B45309 | 5.02 | n/a | **4.28 FAIL** | → **#A04A0A** (6.04 W / 5.14 BG) |
| `--success` (as text) | #0F7B5A | 5.25 | n/a | **4.47 FAIL** | → **#0E7352** (5.85 W / 4.98 BG). White on it 5.85 |
| `--danger` | #C62828 | 5.62 | n/a | 4.79 pass | none |
| `--accent` | #B93C12 | 5.65 | 5.32 | 4.81 pass | none |
| `--tf-ink` | #111111 | n/a | n/a | 16.09 | none |
| `--tf-line` #E5E7EB (card border) | n/a | n/a | n/a | 1.05 (invisible) | Cards sit on BG via shadow. Optional `--tf-line: #D5DFE6` |

Status badges sit on their own tints, so the background change does not affect them.

Hard-coded literals: a dry run of `low_contrast_inline_colours` against (W, #E6EEF3) finds **8** inline colours in templates that pass today and would fail. They are #dc2626 (4.11) ×5, #6b7280 (4.12) ×2 and #b45309 (4.28) ×1, in checklist, estimation, review_session and test_cases. The inline-style cleanup that is in progress should map them to tokens. style.css also has 6 literal `#6B7280` uses that need to become `var(--text-subtle)`.

## 3. Typography

The brand scale is a marketing scale: a 44px H1 and 20px body text. Proposed mapping for an app with dense data:

| App element | Now | Proposed (size/line-height, weight) | Brand step |
|---|---|---|---|
| `.page-header h1` | 32px / 1.15, 700 | **28/42, Semibold** | H3 |
| `.card h2` (section) | ~18.4px, 700 | **22/30, Semibold** | H4 |
| `.card h3` (sub-section) | default, 600 | **20/28, Medium** | H5 |
| Body / UI copy | 16px / 1.6 | **14/24 Regular** | P2 |
| Long-form prose (Guide, AI Ask answers, onboarding, empty states) | 14–16px | **16/24** (or P1 20/28 for hero empty states only) | Links / P1 |
| Links | inherit | inherit size, underlined in text | Links 16/24 |
| Buttons | 14px, 600 | unchanged: 14px Medium/Semibold | n/a |
| Data tables (`.data-table`, `.steps-table`, `.tc-detail-table`) | ~13.9px | **14/20** (deliberate deviation from P2's 24px leading) | P2 |
| Stat numbers | ~28.8px, 700 | 28/42 Semibold | H3 |

Where P1 (20/28) does **not** fit: test-case tables, the step tables, the run list, bug lists and filter bars. At 20px a 6-column test-case row wraps on a 1280px viewport, and row height grows about 40%. The tables are the product; P1 there would halve the number of rows a user can see. Use P1 only for single-message surfaces such as empty states, onboarding cards and the dashboard welcome card.

Weights: the guide names Regular, Medium and Semibold only. Drop 700 and 800 from the Google Fonts URL in `base.html` (a template edit, so phase 2). The wordmark's 800/900 weight goes away once the official logo SVG is used (Q5).

## 4. Iconography and illustrations

The current icons are Lucide (`data-lucide`), monochrome and stroke-based: about 15 in the sidebar plus about 60 in pages. The brand icons are filled, abstract and two-tone (coral plus the blue gradient). They mark the *start of a content block*. They are not for controls.

- **Low-cost option (about 1 day):** keep Lucide everywhere. Add a CSS class `.icon-brand` for block-start icons only: the card-title icon, the dashboard module tiles and the step headers. It renders the Lucide glyph inside a 32px rounded tile filled with `--tf-grad-blue`, with a coral dot or offset square behind it as a `::before` pseudo-element. Controls, the sidebar and table actions stay monochrome, so they remain readable and need no contrast work. This is decorative and needs no aria changes (`aria-hidden` icons stay hidden).
- **Full option (about 1–2 weeks with the designer):** a custom SVG sprite (`static/icons/brand.svg`) of about 20 two-tone abstract icons, one per module (Dashboard, Estimation, Checklist, Test Cases, Execution, Automation, Bug Reports, Guide, Team, Settings). Each uses `<linearGradient>` plus a coral fill. Use them for the module tiles, page headers and the sidebar. Use the brand's abstract illustrations for empty states, the 404/500 pages and the sign-in screen. Lucide stays for controls. The cost is in the drawing work, not in engineering.

## 5. Phased plan

### Phase 1: token values only (`:root` in style.css; no template or rule edits)
Change `--tf-bg` to #E6EEF3, `--text-subtle` to #636A78, `--border-control` to #767C88, `--warning` to #A04A0A, `--success` to #0E7352, `--focus` to #2F5BD8 and `--tf-line` to #D5DFE6 (optional). Add `--tf-dark` #202020, `--tf-dark-2` #464646 and `--tf-grad-blue`. Retire `--tf-blue` #18AAFD in favour of #557FF1 (it is only used as decoration).
Safe only **after** the inline-style cleanup has removed the 8 literals listed in §2.4.

Tests:
- `tests/test_design_tokens.py::TEXT_PAIRS`: the `("--text-subtle", "--tf-bg")`, `("--text-muted", "--tf-bg")` and `("--accent", "--tf-bg")` rows follow the token automatically. The `--text-subtle` row **fails at 4.12** if the background changes without the text-subtle change. That is the intended guard, so ship both together.
- `NON_TEXT_PAIRS`: the `("--border-control", "--tf-bg")` row fails at 2.73 without the border change. Add `("--warning", "--tf-bg")` and `("--success", "--tf-bg")` text pairs; neither is checked against the page background today.
- `test_no_page_hard_codes_a_low_contrast_text_colour` reads `--tf-bg`, so it retargets automatically and catches the 8 literals.
- `test_the_inline_colour_check_catches_a_violation` (lines 455–456) pins the literal `"#F7F8FA"`. Update it to `#E6EEF3`. It still passes, but it describes the wrong background.
- `test_the_token_parser_sees_the_tokens` and `test_no_custom_property_is_used_without_a_definition`: add the new tokens.
- `tests/test_e2e_a11y.py`: pins no colours. The axe run on the 14 screens (`TAGS` wcag2a/aa/21a/21aa) is the end-to-end check, so run the whole class after this phase.

### Phase 2: components (style.css rules, plus the font link in base.html)
- `.btn-primary`: background `var(--tf-coral)`, colour `#202020`, hover `--tf-coral-hover`. `.btn-export` becomes a #464646 pill. Decide the `.lang-btn.active`, `.skip-link`, `.step-badge` and chip set (Q3).
- Sidebar: flat `var(--tf-dark)`. The second rule block at about line 1386 overrides the first at line 134; remove the dead indigo gradient in the first block.
- Typography scale (§3), heading weight 600, table type 14/20, and the font weights in `base.html`.
- `.data-table th` currently uses white text on `--primary-dark` #A8360F (6.56:1). Consider `#202020` or `#464646` headers to match the guide's dark/coral balance.

Tests:
- `test_primary_is_the_accessible_accent_not_coral`: keep it, because links must not become coral. Add a separate assertion that `.btn-primary` uses `--tf-coral` with #202020.
- `TEXT_PAIRS` row `(WHITE, "--accent", "white text on .btn-primary …")`: rename it to reflect the remaining white-on-accent users. Add `("#202020", "--tf-coral")` and `("#202020", "--tf-coral-hover")` for the primary button, and `(WHITE, "--tf-dark-2")` for the dark pill.
- `NON_TEXT_PAIRS` row `("--focus-on-dark", "#1A1D24")` hard-codes the sidebar colour. Point it at `--tf-dark` (6.84:1).
- `test_there_is_a_global_focus_ring`, the button size scale (24/32/40/48), `--gap-buttons` and `test_no_button_is_sized_inline` are all unaffected.
- `tests/test_e2e_a11y.py`: run all 14 screens, especially `test_dashboard`, `test_test_cases` and `OTHER_PAGES` (where primary buttons are densest). Add a screenshot baseline if visual regression is wanted. axe does not judge brand fidelity.

### Phase 3: icons and illustrations
The low-cost `.icon-brand` tile first, then the custom sprite and illustrations. Tests: none pin icons today. Add a check that decorative brand SVGs carry `aria-hidden="true"` (or empty `alt`), and that `test_axe_finds_a_planted_violation` still runs. axe `image-alt` covers illustrations added as `<img>`.

## 6. Open questions for the owner and designer

1. **Background:** adopt #E6EEF3 app-wide, or only around the shell with content areas on white? The full canvas gives the strongest brand signal and costs the four token darkenings above.
2. **Sidebar:** flat #202020 as in the guide, or keep the current near-black gradient? The contrast is equivalent either way (coral focus 6.84 vs 7.08).
3. **White-on-accent elements** (active language toggle, skip link, step badge, active chips): convert to coral with #202020 text like the primary button, or keep dark-coral #B93C12 with white text? Two filled-accent styles side by side look inconsistent.
4. **Gradient:** confirm it is decorative only (icons, stripes, illustrations) and that no button, badge or text ever sits on it. Is solid #557FF1 acceptable as the second data-series colour in charts (3.69:1 on white)?
5. **Logo:** the sidebar wordmark is built from CSS text with a coral "sword-T". Can the designer supply the official logo SVG (light-on-dark variant)? The 20% safe zone on a 44px mark is about 9px, which the current padding already provides.
6. **Link colour:** the guide shows coral links. Is the designer comfortable with #B93C12 as the accessible stand-in, which is the closest coral shade that passes 4.5:1 on both surfaces?
7. **Body size:** confirm 14px (P2) as app body, with P1 (20px) limited to empty states and onboarding (§3).
