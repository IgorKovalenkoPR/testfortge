# TestFortge MCP Server (v1.6 — read + writes + Tedgie chat)

Local stdio MCP server that exposes TestFortge data — projects, test
cases, bug reports, execution runs, walkthrough findings — to MCP-aware
LLM clients (Claude Desktop, Claude Code, etc.). The v1.5 surface added
two write tools so an agent can file bugs and kick off runs without a
human at the Flask UI; v1.6 adds one chat tool that proxies to Tedgie's
QA persona.

The server reuses `engine.db` for all DB access, so it sees whatever
DB the main Flask app would see (local SQLite by default, Postgres if
`DATABASE_URL` is set). It does **not** start a web server; it speaks
JSON-RPC on stdin/stdout and the client spawns it as a subprocess.

## Install

```powershell
# from the repo root
python -m pip install -r mcp_server/requirements.txt
```

Adds only `mcp>=1.2.0` (FastMCP). Not added to the top-level
`requirements.txt` so the Render web-app image stays lean.

## Read tools

| Tool | Arguments | Returns |
|---|---|---|
| `list_projects` | — | every project, sorted by `updated_at` desc, with `test_cases_count`, `checklist_count`, `bug_count` |
| `list_test_cases` | `project_id`, optional `status`, `trigger` | TCs with full fields incl. `url_pattern` + `trigger` (Sprint 5 walkthrough binding) |
| `list_bug_reports` | optional `project_id`, `severity`, `status`, `source` | bugs newest first; `source` ∈ {tedgie, execution, manual, import} |
| `list_execution_runs` | `project_id`, optional `limit` (≤500) | recent runs with status, timestamps, `env_payload`, `stats` |
| `get_execution_run` | `run_id` | one run plus its `case_results` (per-TC outcomes + screenshots) |
| `walkthrough_findings_stats` | optional `paths[]` | per-defect-class summary; auto-globs `automation_runs/*.result.json` if `paths` omitted |

## Write tools (v1.5)

| Tool | Arguments | Returns |
|---|---|---|
| `create_bug_report` | `title` (required), optional `severity`/`priority`/`status`/`environment`/`steps_to_reproduce`/`actual_result`/`expected_result`/`project_id`/`source`/`related_case_id`/`run_id`/`reporter`/`extra` | new bug's `db_id` + an echo of the persisted fields |
| `trigger_test_execution` | `project_id` (required), optional `base_url`/`test_case_ids`/`env_types`/`mode`/`headless`/`walkthrough_config` | `config_id` + worker PID + the config + log paths |

`trigger_test_execution` writes the same config JSON shape the Flask
`/test-execution` route uses (under `<storage>/automation_runs/_pending/`)
and spawns `engine.runner_worker` as a detached subprocess
(`start_new_session=True`). The tool returns immediately; poll the run
state with `list_execution_runs` / `get_execution_run` or watch for
`<run_id>.done.flag` next to the config file. The tool shares a global
per-session concurrency cap of **3** with the Flask app's MCP-triggered
runs — saturating it raises `RuntimeError` rather than silently
queueing.

`create_bug_report` is project-agnostic: omit `project_id` for a
Tedgie-style bug that hasn't been bound to a project yet (the
`bug_report.project_id` column is nullable).

## Chat tool (v1.6)

| Tool | Arguments | Returns |
|---|---|---|
| `tedgie_ask` | `question` (required), optional `lang` (`en` / `ua`), optional `project_id` (reserved) | `{text, intent, suggestions, follow_up}` — same envelope the Flask `/chat` endpoint returns |

Stateless — every call is a fresh request, no conversation history.
The MCP client owns the multi-turn loop. If the host has
`ANTHROPIC_API_KEY` set, Tedgie uses the AI path with prompt-cached
system blocks (see `engine.chatbot._ai_system_blocks`); otherwise it
falls back to the rule-based dispatcher.

`project_id` is accepted on the signature but ignored by the current
persona — kept future-proof for project-aware prompts.

## Run it manually

```powershell
# from the repo root
python -m mcp_server
```

The process blocks on stdin waiting for JSON-RPC; this is normal.
Ctrl+C to exit. Useful only for sanity-checking that the server boots —
real usage goes through a client.

## Register in Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (create the file if
absent) and add the `testfortge` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "testfortge": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "F:\\ClaudeProjects\\TestForTge",
      "env": {
        "FLASK_DEBUG": "1"
      }
    }
  }
}
```

Restart Claude Desktop. The server appears in the MCP section of the
settings dialog; tool calls show up as approval prompts in chat.

To point at a specific SQLite file (instead of the repo's default
`storage/testfortge.db`), add `"TESTFORTGE_DB": "C:\\path\\to\\file.db"`
to the `env` block. To point at Postgres, use `"DATABASE_URL": "..."`.

## Register in Claude Code

```powershell
claude mcp add testfortge -- python -m mcp_server
```

Run from the repo root so `cwd` resolves correctly. The Claude Code CLI
writes this to `~/.claude.json` under `mcpServers`. List with
`claude mcp list`, remove with `claude mcp remove testfortge`.

## How `engine.db` is reached

The MCP entry point (`mcp_server/__main__.py`) sets `FLASK_DEBUG=1`
before importing `engine.db`. This is the signal that bypasses
`_assert_prod_safety`, which otherwise refuses to boot a SQLite-backed
process — that check exists to stop someone running gunicorn against
SQLite by accident. The MCP server is a single-process CLI tool, so the
check doesn't apply, and the `FLASK_DEBUG=1` env tells `engine.db` to
relax it.

If you want the MCP server to read the production Render Postgres,
export the external `DATABASE_URL` (External Database URL, not the
internal one) and the server will connect to it directly. Treat that as
read-only operationally even though nothing in the code enforces it —
v1 has no write tools.

## Run it over HTTP (Streamable-HTTP transport)

The stdio entrypoint above is fine for a desktop-paired MCP client.
For a remote agent (Claude Code running on a teammate's box, a CI job,
etc.), boot the HTTP entrypoint instead:

```powershell
$env:MCP_BEARER_TOKEN = "sk-mcp-<32 random chars>"
python -m mcp_server.http_server
```

Defaults: binds to `0.0.0.0:8765`, MCP protocol at `/mcp`, public
health probe at `/healthz`. Override via `PORT` (or `MCP_HTTP_PORT`)
and `MCP_HTTP_HOST`.

Auth is a single shared bearer token. Every request to anything except
`/healthz` must carry:

```
Authorization: Bearer <MCP_BEARER_TOKEN>
```

Boot refuses to start if `MCP_BEARER_TOKEN` is unset — there is no
open-by-default mode because the surface includes write tools.

To register the HTTP server with an MCP client (Claude Desktop, Claude
Code), point it at the URL:

```json
{
  "mcpServers": {
    "testfortge-remote": {
      "url": "https://testfortge-mcp.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer <paste-token-here>"
      }
    }
  }
}
```

## Hosted on Render

The repo's `render.yaml` declares a second service `testfortge-mcp`
that uses the same Dockerfile as the Flask app — different start
command (`python -m mcp_server.http_server`), different env vars. On
`render blueprint apply` Render auto-generates `MCP_BEARER_TOKEN` and
binds the service at `testfortge-mcp.onrender.com`. Copy the token
from the Render dashboard into your MCP client config; rotate by
regenerating the value in Render and updating the client side.

The service shares the production Postgres (`DATABASE_URL`), so bugs
created via MCP appear in `/bug-reports` on the main service and runs
triggered via MCP show up in the operator's execution-runs list.

## Roadmap (not in v1.6)

* **Project-aware Tedgie prompts** — wire the optional `project_id`
  arg of `tedgie_ask` into the persona builder so it can reference
  the project's TC pack / current bug pile.
* **Conversation persistence** — opt-in `conversation_id` so the
  server holds the last N turns. Useful for thin clients that can't
  keep history on their side.
