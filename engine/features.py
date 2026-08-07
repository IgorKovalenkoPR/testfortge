"""TestFortge — the programme's feature-flag registry.

Every flag that gates the multi-team programme (see
``docs/plans/team_platform_architecture.md``) is declared here once, with
its default and a sentence about what flipping it does. Nothing else in
the codebase is allowed to read one of these names out of ``os.environ``
directly.

Why a registry rather than ``os.environ.get("SOMETHING") == "1"``
scattered around
----------------------------------------------------------------
A bare ``environ.get`` is silently forgiving in exactly the wrong way. A
typo in the *name* reads as "off", and a typo in the *value*
(``EDITORS_ENABLED=true`` where the code compares to ``"1"``) also reads
as "off". Both look identical to a correctly-disabled feature, so the
symptom is "we flipped the flag and nothing happened" with nothing in
the logs. This module turns the first mistake into a ``KeyError`` at the
call site and the second into a working boolean: ``1``, ``true``, ``yes``
and ``on`` all mean on, in any casing.

It also gives ops a single place to answer "what is this instance
actually running?" — see :func:`snapshot`, which ``/readyz`` and the
Guide render.

Reading happens at call time, not import time
---------------------------------------------
Deliberate, and it matters twice. Tests monkeypatch the environment
after import, and a flag flipped in the Render dashboard takes effect on
the next request rather than needing a redeploy. The cost is a dict
lookup per call, which is not a cost.
"""
from __future__ import annotations

import os
from typing import NamedTuple

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


class Flag(NamedTuple):
    """One declared flag: its env var name, default, and purpose."""
    name: str
    default: bool
    epic: str
    purpose: str


#: The registry. Keys are the env var names operators set.
#:
#: ``epic`` ties each flag back to the programme plan so a stale flag is
#: findable once its epic ships and the flag should be retired — a flag
#: nobody can date is a flag nobody deletes.
FLAGS: dict[str, Flag] = {
    f.name: f for f in (
        Flag(
            "AUTH_ENABLED", False, "E1",
            "Real user accounts: email+password and Google sign-in. While "
            "off, the app keeps the anonymous-session model and the shared "
            "HTTP Basic gate (engine/basic_auth.py) is the only auth.",
        ),
        Flag(
            "ORG_MODE", False, "E2",
            "Organisations, membership and the admin/user role split. "
            "Requires AUTH_ENABLED — there is nothing to attach a role to "
            "without a user. See require_org_mode().",
        ),
        Flag(
            "WORKSPACE_DB_FIRST", False, "E3",
            "Makes Postgres the source of truth for a project's artefacts "
            "instead of the caller's Flask session. While off, "
            "engine/workspace.py reads the session first and the database "
            "second — today's behaviour exactly — so modules can be moved "
            "onto the repository one at a time without changing what a "
            "page shows. See docs/plans/adr/0001.",
        ),
        Flag(
            "EDITORS_ENABLED", False, "E4",
            "Inline editing of generated artefacts (estimation, test "
            "cases, checklist, bugs). Requires the DB-backed workspace "
            "(E3) to be the source of truth; editing a Flask session is "
            "not editing shared team data.",
        ),
        Flag(
            "DASHBOARD_V2", False, "E7",
            "Dashboard metrics aggregated per project from Postgres, plus "
            "per-user widget customisation. While off, metrics are "
            "computed from the caller's Flask session.",
        ),
        Flag(
            "STORAGE_BACKEND_CONFIGURABLE", False, "E8",
            "Lets an org admin choose where artefacts (screenshots, "
            "videos, uploads, export bundles) are stored. While off, "
            "everything goes to the local filesystem.",
        ),
        Flag(
            # The only flag here that defaults **on**, and the polarity is
            # the point: an operator who never touches it keeps the gate.
            # A `DROP_BASIC_GATE` spelled the other way would read as
            # ``if not is_enabled(...)`` at the call site, and a double
            # negative on the perimeter is a bad place to be clever.
            "BASIC_GATE_ENABLED", True, "E1",
            "The shared HTTP Basic password in front of the whole app. "
            "Set to 0 once AUTH_ENABLED is on and real accounts have "
            "replaced it (E1.8). Refuses to stand down while "
            "AUTH_ENABLED is off — see engine/basic_auth.py — because "
            "that combination has nothing behind it.",
        ),
    )
}


class UnknownFlag(KeyError):
    """Raised when code asks for a flag that was never declared.

    This is the whole point of the module: catching the typo at the call
    site instead of shipping a feature that is permanently off because
    someone wrote ``EDITOR_ENABLED``.
    """


def _coerce(raw: str, *, flag: str, default: bool) -> bool:
    """Parse an env value into a bool, tolerating the usual spellings.

    An unrecognised value is *not* silently false — it takes the flag's
    default and warns, because ``EDITORS_ENABLED=enabled`` is someone
    trying to turn something on and deserves better than silence.
    """
    val = (raw or "").strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    from engine.log import get_logger
    get_logger(__name__).warning(
        "feature flag %s has unparseable value %r — falling back to "
        "default %s. Use one of: %s / %s.",
        flag, raw, default,
        ",".join(sorted(_TRUTHY)), ",".join(sorted(_FALSY - {""})),
    )
    return default


def is_enabled(name: str) -> bool:
    """True when the declared flag *name* is on in this environment.

    Raises :class:`UnknownFlag` for a name that is not in :data:`FLAGS`.
    """
    try:
        flag = FLAGS[name]
    except KeyError:
        raise UnknownFlag(
            f"{name!r} is not a declared feature flag. Declared: "
            f"{', '.join(sorted(FLAGS))}. Add it to engine/features.py "
            f"(and to render.yaml) before using it."
        ) from None
    if name not in os.environ:
        return flag.default
    return _coerce(os.environ[name], flag=name, default=flag.default)


# ── Dependency rules ──────────────────────────────────────────────
#
# Some flags are meaningless alone. ORG_MODE without AUTH_ENABLED would
# ask "what role does this browser cookie have?", and EDITORS_ENABLED
# before the workspace refactor would let two people edit two private
# copies of the same test case. Rather than trusting every call site to
# remember, the dependency is declared and enforced in one place.
_REQUIRES: dict[str, tuple[str, ...]] = {
    "ORG_MODE": ("AUTH_ENABLED",),
    # ADR 0001's gate, expressed where it cannot be forgotten: an editor
    # writing into a Flask session edits a private copy of shared team
    # data, so it would have to be written twice.
    "EDITORS_ENABLED": ("WORKSPACE_DB_FIRST",),
    # Same reason: per-project metrics computed from the caller's session
    # are per-browser metrics wearing a project's name.
    "DASHBOARD_V2": ("WORKSPACE_DB_FIRST",),
}


def effective(name: str) -> bool:
    """Like :func:`is_enabled`, but honours the dependency rules above.

    A flag whose prerequisite is off reads as off no matter what the
    environment says. Route guards should call this, not
    :func:`is_enabled`.
    """
    if not is_enabled(name):
        return False
    return all(is_enabled(dep) for dep in _REQUIRES.get(name, ()))


def misconfigurations() -> list[str]:
    """Human-readable warnings about flags set but neutered by a missing
    prerequisite. Emitted once at boot so the "we turned it on and
    nothing happened" case shows up in the container log.
    """
    out: list[str] = []
    for name, deps in _REQUIRES.items():
        if not is_enabled(name):
            continue
        missing = [d for d in deps if not is_enabled(d)]
        if missing:
            out.append(
                f"{name} is on but has no effect: it requires "
                f"{', '.join(missing)}."
            )
    return out


def snapshot() -> dict[str, bool]:
    """Every declared flag and its effective value — for ops surfaces.

    Keys are sorted so a diff between two instances is readable.
    """
    return {name: effective(name) for name in sorted(FLAGS)}


__all__ = [
    "FLAGS", "Flag", "UnknownFlag",
    "is_enabled", "effective", "misconfigurations", "snapshot",
]
