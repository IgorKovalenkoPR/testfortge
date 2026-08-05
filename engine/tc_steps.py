"""TestFortge — test steps as a list, not a text blob (E4.3).

``TestCase.test_steps`` is one Text column holding ``"1. Open …\\n2. Enter …"``.
Requirement 5 asks for steps that can be added, removed and reordered, which
is a list operation. This module is the only place that converts between the
two, so the editor, the runner-facing text and any future importer all agree
on what "step 3" means.

Why the ops live on the server
------------------------------
The obvious alternative is a list model in JavaScript that rewrites the blob
and PATCHes it. That works until two people edit the same case: the client
would send a whole new blob computed from what it read, and the version
check would pass or fail on the *blob*, not on the operation. Doing it here
means "move step 2 down" is applied to the row as it is now, under the same
optimistic-locking and audit path as any other field edit.

Numbering is a rendering detail
-------------------------------
The stored text carries ``1.``, ``2.`` … because that is the house style the
reference corpus uses and what the runner's heuristic parser reads. It is
*not* data: :func:`parse` strips it, the ops work on bare text, and
:func:`render` puts it back. So deleting step 2 of five renumbers the rest
instead of leaving a gap, which is the whole reason this is not a textarea.
"""
from __future__ import annotations

import re

# A leading marker to strip: "1.", "2)", "3 -", "- ", "* ", "Step 4:".
# Deliberately broad, because these blobs come from LLM output, from
# imported spreadsheets and from people typing — and a marker left in the
# text would be renumbered into "1. 1. Open the page".
_MARKER_RE = re.compile(
    r"""^\s*
        (?:step\s*)?            # "Step 4:"
        (?:
            \d+\s*[.)\-:]\s*    # 1.  2)  3 -  4:
          | [-*•]\s+       # -  *  •
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)

MAX_STEPS = 200
MAX_STEP_LENGTH = 2000


class StepError(ValueError):
    """A step operation that cannot be applied as asked."""


def strip_marker(line: str) -> str:
    """``"3. Open the page"`` → ``"Open the page"``."""
    return _MARKER_RE.sub("", line or "", count=1).strip()


def parse(blob: str | None) -> list[str]:
    """The stored text as a list of bare steps.

    Blank lines are dropped rather than kept as empty steps: they are
    formatting in the source text, and keeping them would produce numbered
    empty steps on the way back out.

    A blob whose steps wrap across lines cannot be recovered here — there is
    no marker to tell a continuation from a step. Such a line becomes its own
    step, which is what the runner's parser already assumes.
    """
    if not blob:
        return []
    steps = []
    for line in str(blob).splitlines():
        text = strip_marker(line)
        if text:
            steps.append(text)
    return steps


def render(steps: list[str]) -> str:
    """The list back to stored text, renumbered from 1."""
    return "\n".join(
        f"{i}. {text}" for i, text in enumerate(steps, start=1) if text)


def _check_text(text: str) -> str:
    text = strip_marker(text)
    if not text:
        raise StepError("A step cannot be empty.")
    if len(text) > MAX_STEP_LENGTH:
        raise StepError(
            f"A step is limited to {MAX_STEP_LENGTH} characters; "
            f"this one is {len(text)}.")
    return text


def _check_index(steps: list[str], index: int) -> int:
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise StepError("Step number must be a whole number.") from None
    if not 0 <= index < len(steps):
        raise StepError(
            f"There is no step {index + 1}: this case has {len(steps)}.")
    return index


def add(steps: list[str], text: str, *, index: int | None = None) -> list[str]:
    """Insert a step. ``index=None`` appends; otherwise insert *before* it."""
    if len(steps) >= MAX_STEPS:
        raise StepError(f"A test case is limited to {MAX_STEPS} steps.")
    text = _check_text(text)
    out = list(steps)
    if index is None:
        out.append(text)
        return out
    # An insert may legitimately target the position just past the end.
    try:
        position = int(index)
    except (TypeError, ValueError):
        raise StepError("Step number must be a whole number.") from None
    if not 0 <= position <= len(out):
        raise StepError(
            f"Cannot insert at step {position + 1}: this case has "
            f"{len(out)}.")
    out.insert(position, text)
    return out


def edit(steps: list[str], index: int, text: str) -> list[str]:
    """Replace one step's text."""
    index = _check_index(steps, index)
    out = list(steps)
    out[index] = _check_text(text)
    return out


def remove(steps: list[str], index: int) -> list[str]:
    """Delete one step. The rest renumber, which is the point."""
    index = _check_index(steps, index)
    out = list(steps)
    out.pop(index)
    return out


def move(steps: list[str], index: int, delta: int) -> list[str]:
    """Move a step by ``delta`` positions (-1 up, +1 down).

    Moving the first step up, or the last one down, is a no-op rather than an
    error: the button that does it is visible on every row, and telling
    someone off for pressing it is worse than doing nothing.
    """
    index = _check_index(steps, index)
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise StepError("Move distance must be a whole number.") from None
    target = index + delta
    if target < 0 or target >= len(steps):
        return list(steps)
    out = list(steps)
    out.insert(target, out.pop(index))
    return out


OPS = {"add": add, "edit": edit, "remove": remove, "move": move}


def apply(blob: str | None, op: str, *, index=None, text: str = "",
          delta: int = 0) -> str:
    """Apply one named operation to a stored blob and return the new blob.

    One entry point so the route does not branch on the op name, and so the
    op vocabulary is defined in exactly one place.
    """
    steps = parse(blob)
    if op == "add":
        steps = add(steps, text, index=index)
    elif op == "edit":
        steps = edit(steps, index, text)
    elif op == "remove":
        steps = remove(steps, index)
    elif op == "move":
        steps = move(steps, index, delta)
    else:
        raise StepError(
            f"{op!r} is not a step operation. Known: "
            f"{', '.join(sorted(OPS))}.")
    return render(steps)


__all__ = [
    "MAX_STEPS", "MAX_STEP_LENGTH", "OPS", "StepError",
    "add", "apply", "edit", "move", "parse", "remove", "render",
    "strip_marker",
]
