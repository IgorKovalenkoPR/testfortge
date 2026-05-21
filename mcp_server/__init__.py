"""TestFortge MCP server.

Exposes read-only TestFortge data (projects, test cases, bugs, execution
runs, walkthrough findings) as MCP tools for external LLM clients. Runs
locally over stdio; the client (Claude Desktop / Claude Code) spawns it
as a subprocess and talks to it via JSON-RPC on stdin/stdout.

The server reuses :mod:`engine.db` for all DB access — same models,
same connection rules. See ``mcp_server/README.md`` for setup.
"""
