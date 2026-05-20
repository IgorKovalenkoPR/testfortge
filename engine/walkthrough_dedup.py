"""TestForTge — Cross-env / cross-device finding deduplication.

Ported from TFWefloLab's ``server.js`` (see ``server/server.js:516–:560``
in the upstream repo). A walkthrough run typically visits the same site
under multiple environments (Web / Mobile Web / iOS / Android), and the
same underlying defect — a missing ``<html lang>``, a broken hero
image, a misconfigured social link — fires once per environment. Without
dedup the operator sees 4× the same bug card; with dedup they see one
bug listing every environment where it was observed.

The dedup is intentionally pure and side-effect free so it can run as a
post-processing step in either:

* :class:`engine.walkthrough_runner.WalkthroughRunner` (intra-run /
  intra-device dedup — collapses duplicate findings emitted by the same
  page across different heuristics or repeated visits), or
* the per-env loop in :mod:`routes.execution` (cross-env dedup — runs
  AFTER every environment has produced its own findings list, before
  bugs are persisted to the DB).

The function is deterministic: the same input findings produce the same
deduped output, ordered by the first occurrence of each fingerprint.
That stability matters for diff-based snapshot tests and for the UI's
default ordering, which mirrors the order the walkthrough emitted
findings in.
"""

from __future__ import annotations

import re
from typing import Any


# Patterns that normalise dynamic substrings out of finding messages so
# device-specific counts collapse. Mirrors TFWefloLab's
# ``fingerprint()`` regex set (server.js:528–:535) so a port of the
# upstream test fixtures behaves identically.
_NORM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "(3 nodes)" / "(7 node)" → "(N nodes)"
    (re.compile(r"\(\d+\s+nodes?\)", re.I),       "(N nodes)"),
    # "visible 3→5" / "visible 0 → 12" → "visible N→N"
    (re.compile(r"\bvisible\s+\d+\s*[→\-]+\s*\d+\b", re.I), "visible N→N"),
    # "120ms" / "12.5 s" / "24px" / "3.5%" → "Nms" / "Ns" / "Npx" / "N%"
    (re.compile(r"\d+(?:\.\d+)?\s*(ms|s|px|%)"),  r"N\1"),
    # Collapse runs of whitespace introduced by the substitutions.
    (re.compile(r"\s+"),                           " "),
]


def _norm_message(msg: str) -> str:
    """Normalise a free-text finding message for fingerprinting.

    Removes the dynamic numeric fragments that the walkthrough heuristics
    embed for human readability (`"3 nav links"`, `"tap target 18x18px"`)
    so the same underlying defect produces the same fingerprint across
    devices. Returns the cleaned, trimmed string — never raises, never
    returns ``None``.
    """
    out = msg or ""
    for pat, repl in _NORM_PATTERNS:
        out = pat.sub(repl, out)
    return out.strip()


def fingerprint(finding: dict[str, Any]) -> str:
    """Stable identity string for a finding.

    Composed of the four axes that uniquely identify a defect regardless
    of which environment / device surfaced it:

    * ``severity`` — Critical / Major / Minor / Trivial
    * ``area``    — high-level bucket (Images / Navigation / Footer / ...)
    * normalised ``message``  — the human-language description with
      dynamic numbers normalised out (see :func:`_norm_message`)
    * ``element`` — selector / src / role hint identifying the specific
      DOM node when the heuristic supplied one; empty string is fine
      when the defect is page-wide (e.g. missing ``<title>``)

    URL is intentionally NOT part of the fingerprint — the same defect
    on the same template across two languages (``/en/about`` and
    ``/ua/about``) is one defect, not two. Operators who care about
    per-URL surfacing can still see it via the ``urls`` list on the
    deduped record.
    """
    parts = [
        str(finding.get("severity") or "").strip(),
        str(finding.get("area") or "").strip(),
        _norm_message(str(finding.get("message") or "")),
        str(finding.get("element") or "").strip(),
    ]
    return "|".join(parts)


def dedupe(
    findings_by_env: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse duplicate findings, recording every environment + URL
    they were seen on.

    Accepts either:

    * a mapping of ``{env_label: [finding, ...]}`` — used by the cross-
      env caller in :mod:`routes.execution`, where each environment has
      its own findings list; or
    * a flat ``[finding, ...]`` list — used by the intra-run caller in
      :mod:`engine.walkthrough_runner` (treated as a single anonymous
      environment).

    Each output finding carries the original first-observed fields plus:

    * ``environments`` — ordered list of env labels where the defect
      surfaced; deduped, insertion-ordered, empty when the input was a
      flat list
    * ``urls``         — every distinct page URL the defect was observed
      on, insertion-ordered
    * ``occurrences``  — total raw count across all envs / URLs (useful
      for sorting "noisy" defects to the top)

    Returns the deduped findings in the order they were FIRST observed,
    which keeps the UI stable (operators see the same top-of-list across
    re-runs unless the underlying findings change).
    """
    if isinstance(findings_by_env, list):
        iterable: list[tuple[str, list[dict[str, Any]]]] = [
            ("", list(findings_by_env or []))
        ]
    else:
        iterable = [(label, list(items or []))
                    for label, items in (findings_by_env or {}).items()]

    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for env_label, items in iterable:
        for finding in items:
            if not isinstance(finding, dict):
                continue
            key = fingerprint(finding)
            if key not in seen:
                # First time we've seen this defect — copy the source
                # dict so downstream mutation (label appends, attachment
                # merges) does not poison the original list.
                entry = dict(finding)
                entry["environments"] = []
                entry["urls"] = []
                entry["occurrences"] = 0
                entry["fingerprint"] = key
                seen[key] = entry
                order.append(key)
            entry = seen[key]
            if env_label and env_label not in entry["environments"]:
                entry["environments"].append(env_label)
            url = (finding.get("url") or "").strip()
            if url and url not in entry["urls"]:
                entry["urls"].append(url)
            entry["occurrences"] += 1

    return [seen[k] for k in order]


__all__ = [
    "fingerprint",
    "dedupe",
]
