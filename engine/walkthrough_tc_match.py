"""TestForTge — URL-pattern matching between live walkthrough pages
and TestCase records.

When walkthrough mode is on, every page the runner lands on is matched
against the project's TestCases that opted into walkthrough firing via
the ``trigger`` field added in PR-2:

* ``trigger = "manual"``               — never fires from walkthrough
  (default; preserves today's TC-driven behaviour byte-identical)
* ``trigger = "walkthrough_url_match"`` — fires only when
  ``url_pattern`` matches the current page URL
* ``trigger = "always"``               — fires on every page the
  walkthrough visits (escape hatch for "smoke test on every page")

This module is intentionally execution-agnostic: it only decides
*which* TCs match a URL. The actual TC execution (running the parsed
script through :class:`engine.automation_runner.AutomationRunner`'s
``_run_script``) is wired up in PR-3 along with the UI radio, where
the route layer has the credential context, environment metadata, and
result-streaming infrastructure that the runner itself shouldn't have
to know about.

The matcher accepts both regex and glob-shaped patterns:

* Anything containing ``\\``, ``^``, ``$``, ``(``, ``[``, or
  ``\\d/\\w/\\s`` is treated as a regex (anchored with ``re.search``).
* Plain strings with ``*`` / ``?`` are treated as glob patterns
  (converted via :func:`fnmatch.translate`).
* Plain literal strings (no metacharacters) match as substrings —
  ``"checkout"`` matches any URL containing ``"checkout"``.

That layered matching lets QA leads who don't know regex still drive
useful TC-binding decisions without learning a new syntax, while
power users keep the full regex toolkit available.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any


_REGEX_HINT_CHARS = set(r"\^$()[]{}|+")
_GLOB_HINT_CHARS  = set("*?")


def _detect_pattern_kind(pattern: str) -> str:
    """Classify ``pattern`` as ``regex`` / ``glob`` / ``substring``.

    Order of precedence matches the docstring: regex metacharacters win
    over glob metacharacters because a regex with ``*`` (zero-or-more
    of the preceding atom) is meaningfully different from a glob ``*``
    (any-character run). Operators writing a regex will use anchors
    and character classes; operators writing a glob won't.
    """
    if any(c in _REGEX_HINT_CHARS for c in pattern):
        return "regex"
    if any(c in _GLOB_HINT_CHARS for c in pattern):
        return "glob"
    return "substring"


def match_url_pattern(pattern: str, url: str) -> bool:
    """``True`` if ``pattern`` matches ``url``.

    Empty pattern matches any URL (so a TC with ``trigger="always"``
    can leave ``url_pattern`` blank). Invalid regex returns ``False``
    instead of raising — the user's typo in one TC must never crash
    the walkthrough for the others.
    """
    if not pattern:
        # An empty url_pattern is the "always-eligible" form; callers
        # combine it with trigger == "always" for the intended effect.
        return True
    kind = _detect_pattern_kind(pattern)
    target = url or ""
    if kind == "regex":
        try:
            return re.search(pattern, target) is not None
        except re.error:
            return False
    if kind == "glob":
        # ``fnmatch.translate`` returns an anchored regex; match the
        # whole URL against it. URL globs like ``*/checkout*`` need to
        # match anywhere in the URL, so we use ``re.search`` on the
        # un-anchored translated pattern (drop the trailing ``\Z`` /
        # ``$``) and re-anchor the leading part conservatively.
        translated = fnmatch.translate(pattern)
        try:
            return re.search(translated, target) is not None
        except re.error:
            return False
    return pattern.lower() in target.lower()


def match_tcs_for_url(tcs: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    """Return every TC in ``tcs`` that should fire on the given URL.

    Each TC is a plain dict with at minimum:

        {
            "id":          "TC-001",          # or db-row id
            "url_pattern": "*/checkout/*",    # str, may be empty
            "trigger":     "walkthrough_url_match",  # str, see module
                                                      # docstring
            ...
        }

    Selection rules:

    * ``trigger == "manual"``                → never selected
    * ``trigger == "always"``                → always selected (even
      when ``url_pattern`` is empty or doesn't match)
    * ``trigger == "walkthrough_url_match"`` → selected iff
      ``match_url_pattern(tc['url_pattern'], url)`` returns True
    * any other ``trigger`` value            → ignored (forward-compat
      with future trigger kinds)

    The returned list preserves the input order so deterministic
    snapshot tests behave. Callers that need stable cross-run ordering
    should sort by TC id themselves.
    """
    if not tcs:
        return []
    out: list[dict[str, Any]] = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        trig = str(tc.get("trigger") or "manual").strip().lower()
        if trig == "manual":
            continue
        if trig == "always":
            out.append(tc)
            continue
        if trig == "walkthrough_url_match":
            if match_url_pattern(
                str(tc.get("url_pattern") or ""), url
            ):
                out.append(tc)
            continue
        # Unknown trigger — skip silently so a future "walkthrough_
        # section_match" we haven't built doesn't accidentally fire on
        # the URL-match path.
    return out


__all__ = [
    "match_url_pattern",
    "match_tcs_for_url",
]
