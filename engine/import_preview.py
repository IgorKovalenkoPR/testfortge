"""TestFortge — why an import found nothing, and how to fix it (E4.8).

``engine.imports`` matches a file's headers against alias lists. When a team's
spreadsheet calls the column "Scenario" instead of "Summary", nothing matches,
every row parses to ``None``, and the upload reports **0 rows** — which tells
the user their file is wrong when in fact only its vocabulary is.

This module turns that dead end into a mapping:

* :func:`analyse` says which target fields were recognised, from which header,
  which headers were ignored, and which *required* fields are missing — the
  last being the difference between "imported with gaps" and "cannot import".
* :func:`resolve` turns an explicit ``{target: source header}`` choice into the
  column map the parser already consumes, so a user's mapping and the
  automatic one take exactly the same path afterwards.

Deliberately not a stashed-file two-step
----------------------------------------
The obvious flow is upload → preview → confirm, with the file parked
server-side under a token. That needs a temp store, a TTL, a cleaner and an
authorisation check on the token, to save the user one click. Instead the
report names the headers it found and the page asks for the mapping with the
file re-selected: same outcome, none of that surface. If the click ever proves
to be the thing people complain about, the store is the follow-up.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.imports import CL_ALIASES, TC_ALIASES, _build_header_map, _norm
from engine.log import get_logger

log = get_logger(__name__)

#: A row needs one of these to be a row at all — the parser drops anything
#: with neither, which is what produced "0 rows".
REQUIRED: dict[str, tuple[str, ...]] = {
    "test_cases": ("summary", "test_steps"),
    "checklist": ("objective",),
}

ALIASES = {"test_cases": TC_ALIASES, "checklist": CL_ALIASES}


@dataclass
class Mapping:
    """What the headers in a file mean, and what could not be worked out."""
    kind: str
    headers: list[str] = field(default_factory=list)
    #: target field → the header it was taken from
    mapped: dict[str, str] = field(default_factory=dict)
    #: headers that matched no target field
    ignored: list[str] = field(default_factory=list)
    #: target fields with no header
    unmapped: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether the parser can produce rows at all."""
        return any(name in self.mapped for name in REQUIRED[self.kind])

    @property
    def missing_required(self) -> list[str]:
        if self.usable:
            return []
        return list(REQUIRED[self.kind])

    def targets(self) -> list[str]:
        """Every field a user may map, mapped ones first."""
        known = list(ALIASES[self.kind])
        return sorted(known, key=lambda name: (name not in self.mapped, name))

    def message(self) -> str:
        """One sentence explaining the outcome, for a flash."""
        if not self.headers:
            return ("The file has no header row, so there is nothing to map. "
                    "Add one naming each column.")
        if not self.usable:
            needed = " or ".join(n.replace("_", " ")
                                 for n in self.missing_required)
            return (f"None of the columns look like a {needed}. Found: "
                    f"{_sample(self.headers)}. Choose which column holds "
                    f"what below and upload the file again.")
        parts = [f"{len(self.mapped)} column(s) recognised"]
        if self.ignored:
            parts.append(f"{len(self.ignored)} ignored ({_sample(self.ignored)})")
        return "; ".join(parts) + "."


def _sample(values, limit: int = 6) -> str:
    shown = [str(v) for v in list(values)[:limit]]
    return ", ".join(shown) + (" …" if len(values) > limit else "")


def analyse(kind: str, headers, *, override: dict | None = None) -> Mapping:
    """What the automatic matcher makes of these headers, plus any override."""
    if kind not in ALIASES:
        raise ValueError(f"{kind!r} is not an importable kind.")
    headers = [str(h or "").strip() for h in (headers or [])]
    aliases = ALIASES[kind]

    col_map = _build_header_map(headers, aliases)
    if override:
        col_map = resolve(kind, headers, override, base=col_map)

    mapped = {target: headers[index] for target, index in col_map.items()
              if 0 <= index < len(headers)}
    used = set(col_map.values())
    return Mapping(
        kind=kind,
        headers=headers,
        mapped=mapped,
        ignored=[h for i, h in enumerate(headers) if i not in used and h],
        unmapped=sorted(set(aliases) - set(mapped)),
    )


def resolve(kind: str, headers, override: dict, *,
            base: dict | None = None) -> dict:
    """``{target: source header}`` → the column map the parser consumes.

    An override naming a header the file does not have is ignored rather than
    fatal: the form offers the file's own headers, so a mismatch means a stale
    form, and refusing the whole import over one stale select would be worse
    than importing what can be read.
    """
    headers = [str(h or "").strip() for h in (headers or [])]
    by_norm = {_norm(h): i for i, h in enumerate(headers) if h}
    out = dict(base or {})
    for target, source in (override or {}).items():
        if target not in ALIASES[kind]:
            continue
        source = str(source or "").strip()
        if not source:
            out.pop(target, None)      # explicitly "do not import this"
            continue
        index = by_norm.get(_norm(source))
        if index is None:
            log.info("import mapping ignored: %r is not a column in the file",
                     source)
            continue
        out[target] = index
    return out


def dedup(existing: list[dict], incoming: list[dict], *,
          id_key: str = "id") -> tuple[list[dict], list[str]]:
    """Drop incoming rows whose public id is already in the pack.

    For ``append``. Without it a second upload of the same file doubles the
    pack — and E4.4a's uniqueness pass would renumber the copies, so the
    duplicates would look like new work rather than a repeat.

    Compared on the id only. Comparing content would silently drop a row
    somebody deliberately re-imported after editing it elsewhere.
    """
    seen = {str(row.get(id_key)) for row in existing if row.get(id_key)}
    kept, skipped = [], []
    for row in incoming:
        row_id = str(row.get(id_key) or "")
        if row_id and row_id in seen:
            skipped.append(row_id)
            continue
        kept.append(row)
        if row_id:
            seen.add(row_id)
    return kept, skipped


__all__ = ["ALIASES", "REQUIRED", "Mapping", "analyse", "dedup", "resolve"]
