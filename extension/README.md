# TestForTge Recorder Extension

Browser extension that captures manual testing sessions and turns them
into TestForTge test cases. Pilot release (`v0.1.0`).

This is the **real "Web Recorder"** UX:

1. You install the extension once.
2. On `/test-cases` in TestForTge you click **🎬 Start session
   recording**, enter a Start URL.
3. A new tab opens at that URL with a **floating REC overlay**.
4. You walk through the scenario — kliki, форми, навігація.
5. You click **Stop** on the overlay.
6. The extension uploads the captured steps to TestForTge → the
   **review-session** page opens automatically → you tick which TCs
   to save and pick suite tags (Smoke / Regression / E2E).

## Install — Chrome (Dev Mode, pilot)

The pilot release is **not on Chrome Web Store yet** (publishing
pending). Until then, use Developer mode unpacked install:

1. **Get the files.** Clone the TestForTge repo or download a zip;
   either way you need the `extension/` directory.

   ```bash
   git clone https://github.com/IgorKovalenkoPR/testfortge.git
   ```

2. **Open `chrome://extensions/`** in Chrome.

3. **Toggle "Developer mode"** in the top-right corner.

4. **Click "Load unpacked"** and select the `extension/` folder from
   the cloned repo.

5. The **TestForTge Recorder** icon (orange square + REC dot) appears
   in your Chrome toolbar. You may need to pin it via the puzzle-piece
   menu.

## Use it

**Prerequisites on TestForTge host:** `RECORDER_ENABLED=1` env var set
(env-gated pilot — without it the trigger button is invisible).

1. Open TestForTge → switch to your project → go to **Test Cases**.
2. Above the TC list, click **🎬 Start session recording**.
3. In the modal, paste the **Start URL** of the site you want to
   record (e.g. `https://your-sut.example.com/`).
4. Click **Launch**. A new tab opens at that URL. The
   floating REC overlay appears in the top-right corner.
5. Walk through your scenario normally — every click, fill, change,
   and navigation is captured. The overlay shows the step count and
   timer.
6. When done, click **Stop** on the overlay. The extension uploads
   the steps to TestForTge and opens the review tab automatically.
7. On the review page, tick **Save** for each proposed TC, pick a
   **Suite tag** (Smoke / Regression / E2E), and click **Save N TCs**.
8. The new TCs appear in `/test-cases` with the suite badge. Re-run
   them later via Test Execution.

## What gets captured

| Event | Captured as |
|---|---|
| Click on a button / link / element | `action: "click"` + ranked locator chain |
| Filling an input or textarea | `action: "fill"` + value |
| Changing a `<select>` | `action: "select"` + selected value |
| Form submit | Synthetic boundary marker for the LLM segmenter |
| `history.pushState` / hash-change navigation | `action: "goto"` |

Each click/fill carries up to 5 ranked locator alternates
(`data-testid > id > role > label > placeholder > alt > title > text >
css`) — same priority ladder PR-A's `engine/locator_registry.py`
uses, so the runner re-uses promotion across runs.

## What doesn't get captured (yet)

- **Shadow DOM elements** — host element is captured, internals skipped.
- **iframe-nested events** — pilot recorder runs in the top frame only.
- **Drag-and-drop sequences** — only the start/end clicks.
- **File uploads** — file input value, but not file contents.
- **Custom keyboard shortcuts** beyond what triggers a click/change.

These are explicit pilot trade-offs, not bugs. Track in follow-up PRs.

## How it works (architecture)

```
TestForTge UI                       Browser tab (any SUT)
  /test-cases                            │
    │  click 🎬 Start                    │
    ▼                                    │
  POST /api/recorder-session/start       │
    ──► returns {token, finish_url}      │
    │                                    │
    │  open new tab to SUT?#token=...    │
    └──────────────────────────────────► │
                                         ▼
                              content.js reads #token
                                         │
                              register_session ──► background.js
                                         │
                              mount REC overlay
                                         │
                              listen click/fill/change/submit
                                         │
                              for each event:
                                derive locator chain (PR-A ladder)
                                append_step ──► background.js buffer
                                         │
                              ◄── user clicks Stop on overlay
                                         │
                              stop_recording ──► background.js
                                         │
                              POST {token, steps} to finish_url
                                         │
                              TestForTge: segment + classify
                                ──► create SessionDraft
                                ──► returns {review_url}
                                         │
                              chrome.tabs.create({url: review_url})
                                         ▼
                                  /test-cases/review-session/<draft_token>
                                         │
                                  operator ticks Save + picks suite
                                         │
                                  TCs land in /test-cases
```

## Permissions explained

| Permission | Why |
|---|---|
| `storage` | Persist recording state between service-worker restarts |
| `tabs` | Open the review tab after Stop |
| `scripting` | Inject the overlay into the SUT tab |
| `host_permissions: <all_urls>` | Record any site the tester opens |

The extension does **not** read TestForTge cookies, sniff form values
across origins, or send anything anywhere except the `finish_url` the
TestForTge backend issued during `/start`. Token is single-use and
expires when consumed.

## Uninstall

`chrome://extensions/` → toggle off, or click **Remove**.

## Reporting issues

Open an issue on
[github.com/IgorKovalenkoPR/testfortge](https://github.com/IgorKovalenkoPR/testfortge)
with `[Recorder Extension]` in the title.
