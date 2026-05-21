"""Entry point so ``python -m mcp_server`` boots the FastMCP server."""
from __future__ import annotations

import os
import sys

# engine.db._assert_prod_safety refuses to boot a SQLite-backed process
# unless FLASK_DEBUG=1 is set — its heuristic for "this is local dev,
# not gunicorn under load". The MCP server is a CLI tool, single-process,
# so we tell engine.db that explicitly before importing it. setdefault
# preserves an explicit override from the launching shell / client config.
os.environ.setdefault("FLASK_DEBUG", "1")

from mcp_server.server import main  # noqa: E402  — env var must be set first

if __name__ == "__main__":
    sys.exit(main())
