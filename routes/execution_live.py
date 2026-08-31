"""TestFortge — Live-view of in-progress automation runs.

  * GET /test-execution/live              — dashboard HTML
  * GET /test-execution/live/frame        — latest screenshot PNG
  * GET /test-execution/live/strip/<slot> — filmstrip ring-buffer slot
  * GET /test-execution/live/info         — progress JSON consumed by poller

Extracted from ``routes/execution.py`` in the Stage 7 refactor. These
four endpoints form a self-contained read-only surface over the
``automation_runs/_live/`` directory that the worker subprocesses
write to while a run is in progress. None of them share helpers with
the main execution flow, so the extraction is purely an organisational
move — file size at the source goes down by ~140 LOC.
"""

from __future__ import annotations

from flask import Flask, Response, render_template, session

from engine.log import get_logger

from ._shared import resolve_active_project

log = get_logger(__name__)


# Inlined 1x1 transparent PNG — served when a frame / strip slot is
# missing so the polling <img> tag never 404s mid-run. Defined at
# module scope so the bytes aren't reconstructed on every miss.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
    b"\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


#: The live directory is one per instance, not one per project — the
#: runner writes ``automation_runs/_live/`` and nothing else. So these
#: routes showed whoever asked the frames, the filmstrip and the
#: progress JSON of whichever run was executing on the machine,
#: including another organisation's. The JSON is the worse half: it
#: carries ``base_url`` and ``current_tc``, so it names their site and
#: the case being run against it. route_policy marks these "user",
#: which asks for a signed-in caller and says nothing about which
#: tenant.
#:
#: Scoping the directory itself would be the deeper fix and a much
#: larger one. This is the narrow one: the runner stamps the owning
#: project into info.json, and a request from anywhere else is answered
#: as if the instance were idle — the shape the poller already handles
#: when no run exists.
def _live_owner_project(live_dir) -> str:
    """The project whose run wrote this live directory, or ""."""
    import json
    import os
    try:
        with open(os.path.join(live_dir, "info.json"), "r",
                  encoding="utf-8") as f:
            return str((json.load(f) or {}).get("project_id") or "")
    except Exception:
        return ""


def _live_is_mine(live_dir) -> bool:
    """Whether the caller's active project owns the run in progress.

    Two empties match on purpose: a run with no project (and a caller
    with none either) is the single-user case, and refusing it would
    take the live view away from an instance that has no tenants to
    separate. A run stamped with a project is visible only to that
    project.
    """
    try:
        mine = resolve_active_project(session) or ""
    except Exception:      # pragma: no cover — no session context
        mine = ""
    return _live_owner_project(live_dir) == mine


def live_heartbeat_if_mine(storage_root) -> int:
    """The live run's ``ts``, or 0 when the run in progress is not ours.

    Public because two **more** readers of ``_live/info.json`` exist outside
    the four routes above, and neither asked whose run it was: the stall
    check in ``/test-execution/run-status/<run_id>`` and the recent-runs
    list on ``/test-execution/live``. Both compare that timestamp against a
    120-second window to decide whether a worker has died — so with another
    organisation's run executing, one tenant's heartbeat decided another
    tenant's verdict. The run-status branch also quoted ``phase`` and the
    case counters straight out of it, into a message the wrong caller read.

    0 means **no evidence**, which both callers already treat as "running" —
    the answer they give when no run is live at all. That is a real loss and
    it is the honest one: while somebody else's run holds the directory, a
    genuinely stalled run of ours is no longer detected. Detecting it needs
    a per-run heartbeat, which is the instance-wide ``_live/`` directory
    again; scoping that directory is the deeper fix this stops short of.
    """
    import os
    live_dir = os.path.join(storage_root, "automation_runs", "_live")
    if not _live_is_mine(live_dir):
        return 0
    import json
    try:
        with open(os.path.join(live_dir, "info.json"), "r",
                  encoding="utf-8") as f:
            return int((json.load(f) or {}).get("ts") or 0)
    except Exception:
        return 0


def live_info_if_mine(storage_root) -> dict:
    """The whole live payload, or ``{}`` when the run is not ours.

    Same rule as :func:`live_heartbeat_if_mine`; separate because the
    run-status route quotes ``phase`` and the case counters as well as the
    timestamp, and reading them from a live directory we do not own is how
    they reached the wrong caller.
    """
    import json
    import os
    live_dir = os.path.join(storage_root, "automation_runs", "_live")
    if not _live_is_mine(live_dir):
        return {}
    try:
        with open(os.path.join(live_dir, "info.json"), "r",
                  encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def register(app: Flask) -> None:
    @app.route("/test-execution/live", methods=["GET"])
    def test_execution_live():
        # When the user lands here without a ?run_id= query param (back-
        # button, bookmark, manual nav after a Render restart), surface
        # the most recent pending runs so they can pick up where they
        # left off — especially the case where the worker subprocess
        # is still chewing through cases but the browser tab lost the
        # query string.
        import os, glob, time, json
        from routes.automation import STORAGE_ROOT
        pending_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_pending")
        # Read live info.json once so per-run stall checks share its ts —
        # and only when the run in progress is this caller's. It used to be
        # read unconditionally, so another organisation's heartbeat decided
        # whether this caller's runs looked alive.
        live_ts = live_heartbeat_if_mine(STORAGE_ROOT)
        recent_runs: list[dict] = []
        try:
            if os.path.isdir(pending_dir):
                config_files = sorted(
                    glob.glob(os.path.join(pending_dir, "*.json")),
                    key=lambda p: os.path.getmtime(p),
                    reverse=True,
                )[:5]
                for cf in config_files:
                    rid = os.path.splitext(os.path.basename(cf))[0]
                    if rid.endswith(".result"):
                        # skip the result-file mirrors
                        continue
                    started = os.path.isfile(
                        os.path.join(pending_dir, f"{rid}.started.flag"))
                    done = os.path.isfile(
                        os.path.join(pending_dir, f"{rid}.done.flag"))
                    has_result = os.path.isfile(
                        os.path.join(pending_dir, f"{rid}.result.json"))
                    if done and has_result:
                        rstatus = "done"
                    elif started:
                        # Stall check — the worker pings _live/info.json
                        # constantly while running. >120 s with no ping
                        # almost always means OOM-kill on free tier.
                        # Without this the table claimed "running" for
                        # a 9-min-dead worker (operator-reported).
                        # ``live_ts`` is 0 both when nothing is live and
                        # when what *is* live belongs to another
                        # organisation, and both land on "stalled" — which
                        # offers "import partial results", an idempotent
                        # read. Conservative on purpose rather than
                        # considered: this heartbeat is one global value
                        # judged against up to five pending runs, so it was
                        # only ever meaningful when exactly one run was live
                        # and it was the one being judged. The operator-
                        # reported complaint was the opposite error — a
                        # nine-minute-dead worker still reading "running".
                        live_age = (time.time() - live_ts / 1000.0
                                     if live_ts else 9999)
                        rstatus = "stalled" if live_age > 120 else "running"
                    else:
                        rstatus = "queued"
                    recent_runs.append({
                        "run_id": rid,
                        "status": rstatus,
                        "started_at": int(os.path.getmtime(cf)),
                        "age_s": int(time.time() - os.path.getmtime(cf)),
                    })
        except Exception as exc:
            log.debug("recent_runs scan failed: %s", exc)
        return render_template("test_execution_live.html",
                               recent_runs=recent_runs)

    @app.route("/test-execution/live/frame")
    def test_execution_live_frame():
        """Serve the most recent frame from automation_runs/_live/latest.png
        with strict no-cache headers so the browser always re-fetches."""
        import os
        from flask import send_file
        from routes.automation import STORAGE_ROOT
        live_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_live")
        path = os.path.join(live_dir, "latest.png")
        if not os.path.isfile(path) or not _live_is_mine(live_dir):
            resp = Response(_TINY_PNG, mimetype="image/png")
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            return resp
        resp = send_file(path, mimetype="image/png", max_age=0)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    @app.route("/test-execution/live/strip/<int:slot>")
    def test_execution_live_strip(slot):
        """Serve the filmstrip ring-buffer slot 00..11. Each call is
        cheap because the runner pre-writes the PNG via os.replace —
        we just stream the file with no-cache headers."""
        import os
        from flask import send_file
        from routes.automation import STORAGE_ROOT
        if slot < 0 or slot >= 12:
            r = Response(_TINY_PNG, mimetype="image/png")
            r.headers["Cache-Control"] = "no-store"
            return r
        live_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_live")
        path = os.path.join(live_dir, "strip", f"{slot:02d}.png")
        if not os.path.isfile(path) or not _live_is_mine(live_dir):
            r = Response(_TINY_PNG, mimetype="image/png")
            r.headers["Cache-Control"] = "no-store"
            return r
        r = send_file(path, mimetype="image/png", max_age=0)
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return r

    @app.route("/test-execution/live/info")
    def test_execution_live_info():
        """Serve the live progress JSON consumed by the polling page."""
        import json
        import os
        from routes.automation import STORAGE_ROOT
        live_dir = os.path.join(STORAGE_ROOT, "automation_runs", "_live")
        path = os.path.join(live_dir, "info.json")
        if not os.path.isfile(path) or not _live_is_mine(live_dir):
            payload = {"status": "idle", "step": 0, "cases_done": 0,
                       "cases_total": 0, "current_tc": "", "ts": 0}
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {"status": "idle"}
        resp = Response(json.dumps(payload), mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp


__all__ = ["register"]
