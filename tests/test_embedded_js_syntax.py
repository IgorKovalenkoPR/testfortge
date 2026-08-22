"""Every JavaScript string handed to ``page.evaluate`` has to parse.

E11: the walkthrough's `ctas` heuristic filed this defect against the
customer's site on every run —

    [Walkthrough] Heuristic 'ctas' raised Error:
    Page.evaluate: SyntaxError: missing ) after argument list

…and the cause was inside our own source:

    document.querySelectorAll(
        'button:not([type=hidden]), .w-button, a.button, '
        '[role="button"], input[type=submit]');

Two adjacent string literals with no ``+``. Python concatenates those
implicitly; JavaScript does not. So the heuristic threw on `page.evaluate`
for every page of every run and never once produced a finding — the only
thing it ever emitted was a bug reporting its own failure.

Nothing caught it because these constants are opaque strings to Python: they
are syntax-checked by the browser, at runtime, inside a `try` that converts
the exception into an ordinary finding. A unit test that imports the module
sees a valid `str` either way.

So: extract every embedded JS constant and put it through a real parser.
This is the cheap generalisation of a one-character fix — the same mistake
in any of the other constants would be just as invisible.
"""
from __future__ import annotations

import importlib
import inspect
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

#: Modules that embed JavaScript for Playwright to evaluate.
JS_HOST_MODULES = ("engine.walkthrough_runner", "engine.live_executor")

#: Module-level constants holding JS. Matched by name so a new one is picked
#: up automatically rather than needing to be listed here.
_JS_CONST = re.compile(r"^\s*(_JS_[A-Z0-9_]+)\s*=\s*[\"']{3}", re.MULTILINE)

_NODE = shutil.which("node")


def _js_constants(module_name: str) -> dict[str, str]:
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    found = {}
    for name in _JS_CONST.findall(src):
        # Read the value off the class/module rather than re-parsing the
        # literal, so escapes are resolved exactly as Python resolves them.
        value = getattr(mod, name, None)
        if value is None:
            for obj in vars(mod).values():
                if isinstance(obj, type) and hasattr(obj, name):
                    value = getattr(obj, name)
                    break
        if isinstance(value, str):
            found[name] = value
    return found


def _all_js_constants() -> list[tuple[str, str, str]]:
    out = []
    for mod in JS_HOST_MODULES:
        for name, src in _js_constants(mod).items():
            out.append((mod, name, src))
    return out


_CONSTANTS = _all_js_constants()


def test_the_constants_were_actually_found():
    """Guard the guard.

    If the naming convention changes, every parametrised case below would
    silently collapse to zero and this file would keep passing while
    checking nothing.
    """
    assert len(_CONSTANTS) >= 5, (
        f"found only {len(_CONSTANTS)} embedded JS constants — the _JS_* "
        "convention probably changed, so this guard is no longer looking "
        "at anything")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH")
@pytest.mark.parametrize(
    "module,name,source",
    _CONSTANTS,
    ids=[f"{m.split('.')[-1]}.{n}" for m, n, _ in _CONSTANTS],
)
def test_the_embedded_js_parses(module, name, source):
    # These are arrow-function expressions, not statements — wrap so that
    # `() => {...}` is a valid program on its own.
    program = f"void ({source});"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check.js"
        path.write_text(program, encoding="utf-8")
        proc = subprocess.run(
            [_NODE, "--check", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    assert proc.returncode == 0, (
        f"{module}.{name} is not valid JavaScript:\n{proc.stderr}")


@pytest.mark.parametrize(
    "module,name,source",
    _CONSTANTS,
    ids=[f"{m.split('.')[-1]}.{n}" for m, n, _ in _CONSTANTS],
)
def test_no_python_style_implicit_concatenation(module, name, source):
    """The specific trap, checked without a JS engine.

    Runs even where node is absent, because this is the mistake that
    actually happened and a skipped test is not a guard. Matches a string
    literal whose closing quote is followed only by whitespace and then
    another opening quote — valid Python, a syntax error in JavaScript.
    """
    offenders = []
    for pattern in (r"'[^'\n]*'\s*\n\s*'", r'"[^"\n]*"\s*\n\s*"'):
        for match in re.finditer(pattern, source):
            offenders.append(match.group(0).replace("\n", "\\n"))
    assert not offenders, (
        f"{module}.{name} concatenates string literals the Python way "
        f"(no '+'), which is a JavaScript syntax error: {offenders}")
