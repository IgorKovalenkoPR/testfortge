"""Name what ran before a failure on the same xdist worker.

A test that passes alone and fails in the suite was broken by something
that ran earlier *on its worker*. ``pytest -v`` under xdist prefixes every
result with the worker (``[gw3] [ 41%] PASSED tests/test_x.py::t``), so the
order is in the log — but the log needs a token to read. This prints one
``::warning::`` annotation per worker that failed, listing the files that
worker ran before its first failure, which annotations carry without one.

Usage: python .github/scripts/xdist_neighbours.py /tmp/suite.txt
"""
from __future__ import annotations

import re
import sys

_LINE = re.compile(
    r"^\[(gw\d+)\]\s+\[\s*\d+%\]\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"\s+(\S+)")


def neighbours(lines) -> dict[str, tuple[str, list[str]]]:
    """worker -> (first failing node id, files run before it, in order)."""
    seen: dict[str, list[str]] = {}
    out: dict[str, tuple[str, list[str]]] = {}
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        worker, outcome, node = m.groups()
        if worker in out:
            continue
        path = node.split("::", 1)[0]
        if outcome in ("FAILED", "ERROR"):
            before = [f for f in seen.get(worker, []) if f != path]
            out[worker] = (node, before)
            continue
        files = seen.setdefault(worker, [])
        if not files or files[-1] != path:
            files.append(path)
    return out


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8", errors="replace") as fh:
        found = neighbours(fh)
    for worker, (node, before) in sorted(found.items()):
        tail = before[-25:]
        more = f"(+{len(before) - len(tail)} earlier) " if len(before) > 25 else ""
        print(f"::warning::{worker} first failure {node} -- ran before it: "
              f"{more}{', '.join(f.rsplit('/', 1)[-1] for f in tail)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
