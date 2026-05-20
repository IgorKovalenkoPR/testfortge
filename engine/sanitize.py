"""TestForTge — Display-side sanitization (Sprint 4 task 4.4).

LLM-input wrappers in :mod:`engine.llm_safety` keep adversarial text
from steering the model. This module covers the *other* half: stripping
prompt-injection-style lines from text that is rendered to other
operators.

The DB keeps the original verbatim — we never lose user data — and the
LLM still sees the raw text inside the wrapping tags (so the model can
reason about the described system-under-test). Only the *display*
surface is filtered, so operator A cannot smuggle an instruction line
into a bug title that operator B reads on the same project.
"""

from __future__ import annotations

import re


_INJECTION_LINE_RE = re.compile(
    r"^\s*(ignore|disregard|forget)\s+(all\s+|the\s+)?(prior|previous|above)\b.*$",
    re.IGNORECASE,
)


def strip_display(text: str | None) -> str:
    """Remove injection-style lines from ``text`` for safe display.

    Drops any line matching ``^(ignore|disregard|forget) (all|the)?
    (prior|previous|above) ...``. Returns the remaining lines joined
    with newlines, with leading/trailing whitespace stripped.

    The original is kept in the DB and in LLM-input wrapping — this
    helper only filters the *display* surface (bug titles, comments
    surfaced in dashboards, etc.).
    """
    if not text:
        return ""
    kept: list[str] = []
    for line in str(text).splitlines():
        if _INJECTION_LINE_RE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


__all__ = ["strip_display"]
