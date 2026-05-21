# TestFortge MCP Server (v1, read-only)

Local stdio MCP server that exposes TestFortge data — projects, test
cases, bug reports, execution runs, walkthrough findings — to MCP-aware
LLM clients (Claude Desktop, Claude Code, etc.).

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

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `list_projects` | — | every project, sorted by `updated_at` desc, with `test_cases_count`, `checklist_count`, `bug_count` |
| `list_test_cases` | `project_id`, optional `status`, `trigger` | TCs with full fields incl. `url_pattern` + `trigger` (Sprint 5 walkthrough binding) |
| `list_bug_reports` | optional `project_id`, `severity`, `status`, `source` | bugs newest first; `source` ∈ {tedgie, execution, manual, import} |
| `list_execution_runs` | `project_id`, optional `limit` (≤500) | recent runs with status, timestamps, `env_payload`, `stats` |
| `get_execution_run` | `run_id` | one run plus its `case_results` (per-TC outcomes + screenshots) |
| `walkthrough_findings_stats` | optional `paths[]` | per-defect-class summary; auto-globs `automation_runs/*.result.json` if `paths` omitted |

All write operations (creating bugs, triggering test runs, mutating
projects) are intentionally **out of scope for v1**. See "Roadmap" below.

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

## Roadmap (not in v1)

* **Write tools** — `create_bug_report`, `trigger_test_execution`,
  `trigger_walkthrough`. Higher blast radius; needs a confirmation
  pattern on the client side.
* **HTTP/SSE transport** — host the same server on Render so the tools
  are reachable from any machine. Requires a token-auth layer (TestFortge
  is currently Basic Auth, not suitable for MCP clients).
* **Tedgie chat tool** — `tedgie_ask(project_id, question)`. Plumbs
  through the chatbot's prompt-cached system blocks. Belongs in v3 once
  the read + write surfaces stabilise.
