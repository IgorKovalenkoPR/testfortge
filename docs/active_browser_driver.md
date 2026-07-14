# Active Browser Driver — team guide

*How to let an AI agent drive a real, logged-in browser through the
TestForTge Recorder extension (PR-F Phase 2).*

The agent talks to TestForTge's **MCP server**; the MCP server talks to
the DB; the **extension** polls TestForTge and runs each command against
the tab the operator opened. Nothing happens until the operator opens
the handoff URL — no silent takeover, and there is **no arbitrary-JS**
command.

---

## 1. Turn it on (one-time)

### a. Host env flags

| Flag | Where | Why |
|---|---|---|
| `BROWSER_CONTROL_ENABLED=1` | **Flask** web service (Render) | Enables `/api/browser/poll` + `/api/browser/result`. Off by default — separate from `RECORDER_ENABLED`. |
| `TFG_INSTANCE_URL=https://<your-instance>` | **MCP server** process | Lets the handoff URL embed the poll/result endpoints. If unset, the extension falls back to the instance it learned from a prior recording. |
| `BROWSER_CMD_TIMEOUT_S=20` *(optional)* | MCP server | How long a `browser_*` tool waits for the extension before timing out. |

On Render these are set per-service in the dashboard (env-var section),
not in `render.yaml` — same as the recorder flag.

### b. Extension

Install the **TestForTge Recorder** extension (`v0.4.0+`):
`chrome://extensions` → *Developer mode* → *Load unpacked* → the
`extension/` folder. (Chrome Web Store publish pending.)

### c. MCP client

Point your MCP client (Claude Desktop / Claude Code / any MCP client) at
the TestForTge MCP server (stdio or the HTTP entrypoint with
`MCP_BEARER_TOKEN`). The `browser_*` tools appear automatically.

---

## 2. The MCP tools

| Tool | What it does |
|---|---|
| `browser_control_start(project_id, start_url)` | Mint a control session; returns `{token, open_url}`. Give `open_url` to the operator. |
| `browser_control_status(token)` | Is the browser attached? Returns `live` + `last_seen_seconds`. |
| `browser_navigate(token, url)` | Navigate the tab (http/https). Returns landed `{url, title}`. |
| `browser_read_page(token)` | Snapshot: `{url, title, elements:[{ref, role, name, text, tag}], text_digest}`. Each `ref` (`ref_1`…) is a handle. |
| `browser_click(token, ref)` | Click the element for `ref`. |
| `browser_fill(token, ref, text)` | Type into the input/textarea for `ref`. |
| `browser_wait(token, ms)` | Pause (0–30000 ms) to let a view settle. |
| `browser_control_stop(token)` | End the session + drop pending commands. |

`project_id` comes from `list_projects`.

---

## 3. Typical run

```
Agent:    browser_control_start("<pid>", "https://app.example.com/login")
          → { token: "abc…", open_url: "https://app.example.com/login#testfortge-control-token=abc…" }

Operator: opens open_url in Chrome (extension installed).
          Toolbar shows a purple  🕹 Live control active  badge.

Agent:    browser_control_status("abc…")        → live: true
          browser_read_page("abc…")             → elements:[{ref:"ref_4", role:"textbox", name:"Email"}, …]
          browser_fill("abc…", "ref_4", "qa@example.com")
          browser_fill("abc…", "ref_5", "••••••")
          browser_click("abc…", "ref_7")        # Sign in
          browser_wait("abc…", 1000)
          browser_read_page("abc…")             # fresh refs for the next page
          …
          browser_control_stop("abc…")
```

**Golden rule:** re-call `browser_read_page` after every navigation or
click that changes the page — `ref_N` handles are only valid for the
page they were read from (they reset on navigation).

---

## 4. Safety model

- **Consent-gated** — nothing is drivable until the operator opens the
  `open_url`. The control token lives in the URL *fragment*, so the site
  itself never receives it.
- **Structured verbs only** — navigate / read / click / fill / wait.
  There is deliberately **no `eval`** / arbitrary-JS command.
- **Scoped** — each command is bound to the session token + its project;
  a token can only drive the browser whose operator opened its handoff.
- **No debugger banner** — unlike deep-capture recording, driving uses
  `tabs.update` + content-script actions, so it raises no yellow bar.
- **Operator stop, any time** — popup *Stop live control*, or just close
  the tab (the session goes stale and expires).

---

## 5. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Tool returns `control_disabled` | `BROWSER_CONTROL_ENABLED=1` not set on the Flask host. |
| `browser_not_attached` | Operator hasn't opened `open_url` yet, or the tab was closed. Check `browser_control_status`. |
| `timeout` | Page load slow, or the extension stopped polling (tab closed / browser asleep). Re-check status; re-open `open_url` if needed. |
| `unknown_or_stopped_session` | Session sealed (`browser_control_stop`) or expired (default TTL 60 min). Start a new one. |
| `ref_not_found: ref_N` | The page changed since the last `read_page`. Re-read and use fresh refs. |
| Extension can't reach the instance | Set `TFG_INSTANCE_URL` on the MCP server, or do one recording first so the extension learns the instance. |

---

## 6. What it is *not* (yet)

- No arbitrary JS execution (by design).
- No network-response-body inspection during driving (that's a
  deep-capture recording follow-up).
- Top frame only — iframes/Shadow-DOM internals aren't addressable yet.

For the capture side (network / console / DOM telemetry during a manual
recording), see the extension `README.md` → *Deep capture*.
