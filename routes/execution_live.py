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
        live_info_path = os.path.join(STORAGE_ROOT, "automation_runs",
                                       "_live", "info.json")
        # Read live info.json once so per-run stall checks share its ts.
        live_ts = 0
        try:
            if os.path.isfile(live_info_path):
                with open(live_info_path, "r", encoding="utf-8") as f:
                    live_ts = int((json.load(f) or {}).get("ts", 0))
        except Exception:
            pass
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
