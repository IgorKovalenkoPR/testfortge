"""Test execution, recording, walkthroughs, crawling and locators never import
the LLM client (ADR 0003 §4.9: "Test execution needs no change; it does not
use the LLM").

An automation audit found execution LLM-free; this keeps it so. The modules
are found by name, not listed by hand, so a new ``*_runner.py`` is guarded
the day it is added. Static (AST) rather than ``sys.modules`` after import:
importing a module can pull the client in transitively through code that
never calls it, and what this pins is that none of these modules reaches
for it directly.
"""
from __future__ import annotations

import ast
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIRS = ("engine", "routes")
_GUARDED = re.compile(r"exec|runner|record|walkthrough|crawl|locator")
_FORBIDDEN = {"engine.llm_client", "engine.llm_cache", "anthropic"}


def _guarded_modules() -> list[str]:
    out = []
    for d in _DIRS:
        for name in sorted(os.listdir(os.path.join(_ROOT, d))):
            if name.endswith(".py") and _GUARDED.search(name):
                out.append(os.path.join(_ROOT, d, name))
    for name in sorted(os.listdir(_ROOT)):
        if name.endswith(".py") and _GUARDED.search(name):
            out.append(os.path.join(_ROOT, name))
    return out


def _imports(path: str) -> set[str]:
    """Every module *path* imports, absolute or relative, at any depth."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    package = os.path.basename(os.path.dirname(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = f"{package}.{base}" if base else package
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_the_guard_sees_the_modules_it_is_about():
    names = {os.path.basename(p) for p in _guarded_modules()}
    for expected in ("live_executor.py", "automation_runner.py",
                     "runner_worker.py", "recorder.py",
                     "walkthrough_runner.py", "site_crawler.py",
                     "locator_registry.py", "execution.py"):
        assert expected in names, expected


def test_no_execution_module_imports_the_llm_client():
    offenders = {}
    for path in _guarded_modules():
        hit = sorted(m for m in _imports(path)
                     if any(m == f or m.startswith(f + ".")
                            for f in _FORBIDDEN))
        if hit:
            offenders[os.path.relpath(path, _ROOT)] = hit
    assert not offenders, (
        "execution/recording/crawling must stay LLM-free (ADR 0003 §4.9); "
        f"these import the LLM client: {offenders}")
