# TestForTge Recorder Extension

Browser extension that captures manual testing sessions and turns them
into TestForTge test cases. Pilot release (`v0.4.0`).

Since `v0.4.0` it is also an **MCP-driven active browser driver** — an
agent can navigate, read the page, click and fill in the operator's real
logged-in browser (see *Active browser control* below).

Since `v0.3.0` the recorder does **deep capture** via the Chrome
DevTools Protocol (`chrome.debugger`): alongside the clicks / fills /
navigation it already recorded, it now streams the SUT's **network
requests** (method, URL, status, failures), the page's real **console**
(logs, warnings, errors, uncaught exceptions), and periodic **DOM
snapshots** (page title + visible-text digest + interactive controls
with their locators). All of it lands on the review page, where failed
requests and console errors are flagged as bug-report candidates.

> **Heads-up — the yellow banner.** Deep capture attaches a debugger
> session to the tab being recorded, so Chrome shows a
> *"TestForTge Recorder started debugging this browser"* bar for the
> duration. This is expected (it's the same bar Playwright codegen and
> the DevTools Recorder raise) and disappears the moment you hit **Stop**.
> If the debugger can't attach (DevTools already open on the tab, a
> `chrome://` page, or you dismiss the bar), step capture keeps working
> and the review page notes that deep capture was unavailable.

This is the **real "Web Recorder"** UX:

1. You install the extension once.
2. Start a recording **either** from `/test-cases` in TestForTge
   (**🎬 Start session recording**) **or** straight from the
   extension's toolbar popup (type the site URL → **Start recording**).
3. A new tab opens at that URL with a **floating REC overlay**.
4. You walk through the scenario — kliki, форми, навігація.
5. You click **Stop** on the overlay (or in the popup).
6. The extension uploads the captured steps to TestForTge → the
   **review-session** page opens automatically → you tick which TCs
   to save and pick suite tags (Smoke / Regression / E2E).

### The toolbar popup

Clicking the extension icon shows:

- **While recording** — live step count + elapsed timer + **Stop**.
- **While idle** — a **Site to record** field, a **Start recording**
  button, and an **Open TestForTge** shortcut.

> **Why Start from the popup still opens a TestForTge tab:** the
> `/api/recorder-session/start` endpoint binds the recording to your
> *active project*, which it reads from the TestForTge session cookie.
> That cookie is `SameSite=Lax`, so a cross-site request from the
> extension can't carry it. Instead the popup opens
> `/test-cases#tfg-record=<url>`; the page (where the cookie + project
> are valid) pre-fills the recorder and launches it for you. One extra
> tab, zero manual clicks.

The popup remembers your TestForTge **instance** automatically after the
first recording (learned from the server's `finish_url`). You can also
set it manually via **change** next to "Instance".

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
| **Network requests** (deep capture) | `telemetry.network[]` — method, URL, status, mime, redirects, failures |
| **Console + uncaught exceptions** (deep capture) | `telemetry.console[]` — level, text, source |
| **DOM snapshots** (deep capture) | `telemetry.dom_snapshots[]` — title, text digest, interactive controls + locators |

Each click/fill carries up to 5 ranked locator alternates
(`data-testid > id > role > label > placeholder > alt > title > text >
css`) — same priority ladder PR-A's `engine/locator_registry.py`
uses, so the runner re-uses promotion across runs.

## What doesn't get captured (yet)

- **Network response bodies** — status + URL are captured; bodies are
  not (they can be large and require an extra CDP round-trip). Planned
  for a follow-up: fetch bodies lazily, capped, for failed / non-2xx
  XHR + fetch only.
- **Shadow DOM elements** — host element is captured, internals skipped.
- **iframe-nested events** — pilot recorder runs in the top frame only.
- **Drag-and-drop sequences** — only the start/end clicks.
- **File uploads** — file input value, but not file contents.
- **Custom keyboard shortcuts** beyond what triggers a click/change.
- **Arbitrary JS (`eval`)** — active control (below) is deliberately
  limited to structured verbs; there is no remote `eval`.

These are explicit pilot trade-offs, not bugs. Track in follow-up PRs.

## Active browser control (MCP-driven) — `v0.4.0+`

The extension can also act as a **remotely-driven executor**, so an agent
(via TestForTge's MCP server) can drive the operator's real,
already-logged-in browser — the same shape as the Claude in Chrome
extension, scoped to structured actions.

**How it works**

```
MCP client (agent)                 Flask                Extension (this)
  browser_control_start ─► mint session (DB)
      ◄─ open_url (token in #fragment)
  [operator opens open_url in Chrome] ──────────────►  register_control
                                                        start poll loop
  browser_navigate/read_page/    enqueue cmd (DB)
  click/fill/wait  ───────────►      │
                                POST /api/browser/poll ◄── poll (~1s)
                                     ──► command ───────►  execute:
                                                            navigate = tabs.update
                                                            read_page/click/fill
                                                              = content script
                                                            wait = timer
                                POST /api/browser/result ◄── result
      ◄─ tool returns result (DB)
```

**Turn it on**

1. Host env: `BROWSER_CONTROL_ENABLED=1` (separate from `RECORDER_ENABLED`
   — enabling the recorder does *not* expose the drive surface).
2. (Optional) `TFG_INSTANCE_URL=https://<your-instance>` on the **MCP
   server** process so the handoff URL carries the poll/result endpoints.
   When unset the extension falls back to the instance it learned from a
   prior recording.

**Flow**

1. Agent calls `browser_control_start(project_id, start_url)` → gets
   `{token, open_url}`.
2. Operator opens `open_url` in Chrome (extension installed). The token
   lives in the URL fragment, so the SUT never sees it. The toolbar shows
   a purple **🕹 Live control active** badge; the popup offers **Stop**.
3. Agent drives with `browser_navigate`, `browser_read_page` (returns
   `ref_N` handles), `browser_click(ref)`, `browser_fill(ref, text)`,
   `browser_wait(ms)`; `browser_control_status(token)` reports liveness.

**Safety model**

- Nothing is drivable until the operator explicitly opens the handoff URL
  — no silent takeover.
- Structured verbs only; **no arbitrary JS**.
- Each command is bound to the session token + project; a token can only
  drive the browser whose operator opened its handoff URL.
- Unlike deep capture, control is **debugger-free** — no yellow banner.
- Operator stops any time via the popup, or by closing the tab (the
  session then goes stale and expires).

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
| `debugger` | Deep capture — attach a CDP session to stream the SUT's network + console (v0.3.0+) |
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
