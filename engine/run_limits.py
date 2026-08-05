"""
TestForTge — fair use for browser runs (E5.5).

The manual walk costs nothing to run concurrently: it is a person reading a
page. A browser run is different. On the free tier the whole service has
about half a gigabyte, one Chromium takes a large fraction of it, and two
started at once do not queue — they get OOM-killed, which surfaces as a
run that simply stops with no verdict and no explanation. The acceptance
criterion for this task is exactly that: exceeding the limit must produce a
comprehensible queue, not a 500.

So this refuses the *second* run rather than letting the platform kill both.
Refusing is not a downgrade from queueing: a queue that holds a request for
twenty minutes on a free dyno that sleeps after fifteen is a worse promise
than "one at a time, here is what is running".

Two things it takes care to get right, both learned from how these runs
actually fail:

* **Stale runs must not block forever.** An OOM kill leaves ``finished_at``
  NULL, because nothing gets the chance to write it. Counting that row
  forever would wedge the project permanently, and the operator's only
  recovery would be SQL. So a run older than the staleness window stops
  counting, and the refusal says which runs it counted.
* **The scope is the organisation, not the project.** The memory is shared
  by the whole service, so two projects in one team starting a run each
  costs exactly as much as one project starting two. Scoping the limit per
  project would make it trivially bypassable by switching project — and
  the bypass would be an OOM, not an error message.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from engine.log import get_logger

log = get_logger(__name__)

#: Run modes that put a browser on the box. "manual" is absent on purpose —
#: it is a person clicking, and it costs nothing to have ten open.
BROWSER_MODES = ("tc_driven", "walkthrough", "live")

_DEFAULT_MAX_CONCURRENT = 1
_DEFAULT_STALE_MINUTES = 30


def max_concurrent() -> int:
    """How many browser runs one organisation may have in flight.

    Read at call time rather than import time so a deployment can raise it
    without a code change — and so tests can set it per case instead of
    reloading the module.
    """
    raw = os.environ.get("TESTFORTGE_MAX_CONCURRENT_RUNS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CONCURRENT
    # 0 or negative would mean "no runs at all", which no operator means to
    # configure; treat it as "no limit" instead, which is the other thing
    # they might.
    return value if value > 0 else 10_000


def stale_after() -> timedelta:
    raw = os.environ.get("TESTFORTGE_RUN_STALE_MINUTES", "")
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = _DEFAULT_STALE_MINUTES
    return timedelta(minutes=max(1, minutes))


@dataclass
class Decision:
    allowed: bool
    #: The runs that were counted against the limit, newest first.
    active: list[dict] = field(default_factory=list)
    #: Runs ignored because they are older than the staleness window.
    stale: list[dict] = field(default_factory=list)
    limit: int = 0

    def message(self) -> str:
        """Why the run was refused, in terms the operator can act on.

        Names the run that is holding the slot and when it started, because
        "try again later" without that is indistinguishable from a bug.
        """
        if self.allowed:
            return ""
        parts = []
        if self.limit == 1:
            parts.append("A browser run is already in progress for this team.")
        else:
            parts.append(f"{len(self.active)} browser runs are already in "
                         f"progress for this team (the limit is {self.limit}).")
        for run in self.active[:3]:
            started = str(run.get("started_at") or "")[:16].replace("T", " ")
            mode = str((run.get("env_payload") or {}).get("mode") or "run")
            label = f"#{run.get('id')} ({mode})"
            parts.append(f"{label} started at {started}." if started
                         else f"{label} is running.")
        parts.append("Wait for it to finish, or open it from Runs to see "
                     "where it is. Browser runs are limited because the "
                     "service has one machine's worth of memory and two at "
                     "once are killed rather than queued.")
        if self.stale:
            # Say it, rather than letting somebody wonder why an old run is
            # not blocking: the alternative reading is that the limit is
            # broken.
            parts.append(f"({len(self.stale)} older run(s) were ignored as "
                         f"stale.)")
        return " ".join(parts)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def split_by_age(runs: list[dict], *, now: datetime | None = None,
                 window: timedelta | None = None) -> tuple[list[dict], list[dict]]:
    """``(counted, stale)``. A run with no timestamp counts — a missing
    ``started_at`` is not evidence of age, and treating it as stale would
    let a broken write disable the limit."""
    now = now or _utcnow()
    window = window or stale_after()
    counted: list[dict] = []
    stale: list[dict] = []
    for run in runs:
        started = _as_aware(run.get("started_at"))
        if started is not None and now - started > window:
            stale.append(run)
        else:
            counted.append(run)
    return counted, stale


def check(project_ids: list[str], *, limit: int | None = None,
          now: datetime | None = None) -> Decision:
    """May another browser run start across *project_ids*?

    Takes project ids rather than an org id so the caller owns the mapping:
    with organisations off there is no org, and the honest scope is then the
    projects the caller can reach.
    """
    cap = max_concurrent() if limit is None else limit
    runs: list[dict] = []
    if project_ids:
        from engine import db as _db
        for pid in project_ids:
            try:
                for run in _db.list_open_runs(pid, limit=20):
                    mode = str((run.get("env_payload") or {}).get("mode") or "")
                    # A run with no mode recorded predates the field. Counted
                    # as a browser run: under-counting risks the OOM this
                    # exists to prevent, and over-counting only costs a wait.
                    if mode in BROWSER_MODES or not mode:
                        runs.append(run)
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("run limit lookup failed for %s: %s", pid, exc)
    runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    counted, stale = split_by_age(runs, now=now)
    return Decision(allowed=len(counted) < cap, active=counted, stale=stale,
                    limit=cap)


__all__ = ["BROWSER_MODES", "Decision", "check", "max_concurrent",
           "split_by_age", "stale_after"]
