"""TestFortge — Detached subprocess worker for /test-execution.

Runs the Playwright pass in a process that is fully detached from the
gunicorn worker (``start_new_session=True`` on the spawning side).
Gunicorn killing the request thread does NOT kill this process, so
operator-reported 502/503 around case 17–37 of long runs are no longer
a wall-clock problem.

Lifecycle
---------
1. The Flask POST handler writes a config JSON describing the run to
   ``<storage>/automation_runs/_pending/<run_id>.json`` and spawns this
   worker via ``subprocess.Popen``.
2. The worker reads the config, marks status=running in the live
   ``info.json`` (so /test-execution/live updates kick in), runs
   :class:`engine.automation_runner.AutomationRunner`, then writes a
   serialised report + automation_assets dict to
   ``<storage>/automation_runs/<run_id>/result.json``.
3. A ``done.flag`` is touched last — the route polls for that file to
   know the worker finished cleanly.
4. ``/test-execution/results/<run_id>`` is the GET endpoint that loads
   the JSON, runs the per-env post-processing in the request thread
   (fast — no Playwright), writes results into the user's session, and
   renders the existing test_execution.html.

Failures
--------
Any exception in the worker is caught and written to result.json with
status="failed" and a traceback so the operator can see what blew up
without digging through Render logs.

Stdout/stderr
-------------
Redirected to ``<run_dir>/worker.log`` by the spawner; the worker
itself uses :mod:`logging` writing to that file. Best-effort.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

# Make the project root importable when this module is invoked via
# `python -m engine.runner_worker`. The spawner sets cwd; we still
# add the parent directory so direct path-style invocation works too.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _serialise_step(s) -> dict[str, Any]:
    """Convert a StepResult dataclass into a plain dict for JSON."""
    return {
        "index": getattr(s, "index", 0),
        "action": getattr(s, "action", ""),
        "raw": getattr(s, "raw", ""),
        "status": getattr(s, "status", ""),
        "duration_ms": getattr(s, "duration_ms", 0),
        "comment": getattr(s, "comment", ""),
        "screenshot_before": getattr(s, "screenshot_before", "") or "",
        "screenshot_after": getattr(s, "screenshot_after", "") or "",
        "screenshot_failure": getattr(s, "screenshot_failure", "") or "",
        "console_errors": list(getattr(s, "console_errors", []) or []),
    }


def _serialise_script(r) -> dict[str, Any]:
    """ScriptResult -> dict."""
    return {
        "tc_id": getattr(r, "tc_id", ""),
        "summary": getattr(r, "summary", ""),
        "status": getattr(r, "status", ""),
        "duration_ms": getattr(r, "duration_ms", 0),
        "video_path": getattr(r, "video_path", "") or "",
        "comment": getattr(r, "comment", ""),
        "final_url": getattr(r, "final_url", "") or "",
        "steps": [_serialise_step(s) for s in (getattr(r, "steps", []) or [])],
    }


def _serialise_report(rep) -> dict[str, Any]:
    """RunReport -> dict (matches the post-run flow's expectations)."""
    return {
        "run_id": getattr(rep, "run_id", ""),
        "started_at": getattr(rep, "started_at", ""),
        "finished_at": getattr(rep, "finished_at", ""),
        "base_url": getattr(rep, "base_url", "") or "",
        "headless": bool(getattr(rep, "headless", True)),
        "total": int(getattr(rep, "total", 0)),
        "passed": int(getattr(rep, "passed", 0)),
        "failed": int(getattr(rep, "failed", 0)),
        "blocked": int(getattr(rep, "blocked", 0)),
        "duration_ms": int(getattr(rep, "duration_ms", 0)),
        "scripts": [_serialise_script(r) for r in
                     (getattr(rep, "scripts", []) or [])],
    }


def _build_automation_assets(report_dict: dict[str, Any],
                              storage_root: str) -> dict[str, Any]:
    """Mirror the inline assets-builder from routes/execution.py so the
    results endpoint can hand the same dict to the per-env loop without
    re-running the validation logic."""
    assets: dict[str, Any] = {}
    for r in (report_dict.get("scripts") or []):
        cid = r.get("tc_id") or ""
        if not cid:
            continue
        shots: list[str] = []
        fail_shots: list[str] = []
        failure_step: dict | None = None
        prev_after = ""
        for step in (r.get("steps") or []):
            after = step.get("screenshot_after") or ""
            if after:
                abs_p = os.path.join(storage_root,
                                     after.replace("/", os.sep))
                try:
                    if (os.path.isfile(abs_p)
                            and os.path.getsize(abs_p) > 0):
                        shots.append(after)
                except OSError:
                    pass
            fail = step.get("screenshot_failure") or ""
            if fail:
                abs_f = os.path.join(storage_root,
                                     fail.replace("/", os.sep))
                try:
                    if (os.path.isfile(abs_f)
                            and os.path.getsize(abs_f) > 0):
                        fail_shots.append(fail)
                        if failure_step is None:
                            failure_step = {
                                "index": step.get("index", 0),
                                "action": step.get("action", ""),
                                "comment": step.get("comment", ""),
                                "screenshot": fail,
                                "context_screenshot": prev_after,
                                "console_errors": list(
                                    step.get("console_errors") or [])[:5],
                            }
                except OSError:
                    pass
            if after:
                prev_after = after
        video = r.get("video_path") or ""
        if video:
            abs_v = os.path.join(storage_root,
                                 video.replace("/", os.sep))
            try:
                if not (os.path.isfile(abs_v)
                        and os.path.getsize(abs_v) > 0):
                    video = ""
            except OSError:
                video = ""
        assets[cid] = {
            "status": r.get("status", ""),
            "video": video,
            "screenshots": shots,
            "failure_screenshots": fail_shots,
            "failure_step": failure_step,
            "final_url": r.get("final_url") or "",
            "duration_ms": r.get("duration_ms", 0),
        }
    return assets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config_path", help="Path to run config JSON")
    args = parser.parse_args()

    # Load config first — we need run_id for the result-path even on
    # early failures.
    try:
        with open(args.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as exc:
        print(f"runner_worker: cannot read config {args.config_path}: {exc}",
              file=sys.stderr)
        return 2

    storage_root = config["storage_root"]
    pre_run_id = config.get("run_id", "")  # may be empty; runner will assign one

    # Output dir is determined AFTER the runner picks its own run_id
    # (we mirror that to result.json so the results endpoint can find
    # both via _pending/<config_id>.json and via the live info.json).
    config_id = os.path.splitext(os.path.basename(args.config_path))[0]
    pending_dir = os.path.join(storage_root, "automation_runs", "_pending")
    os.makedirs(pending_dir, exist_ok=True)
    result_path = os.path.join(pending_dir, f"{config_id}.result.json")
    done_path = os.path.join(pending_dir, f"{config_id}.done.flag")

    # Mark started immediately so the route can distinguish
    # "subprocess never spawned" from "subprocess running".
    started_path = os.path.join(pending_dir, f"{config_id}.started.flag")
    try:
        with open(started_path, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception:
        pass

    try:
        from engine.automation_runner import AutomationRunner
        from engine.automation_qa import scripts_from_session

        runner_kwargs = config.get("runner_kwargs") or {}
        # Tuples come back as lists from JSON — restore where the
        # AutomationRunner ctor expects tuples.
        for key in ("viewport", "viewport_override"):
            v = runner_kwargs.get(key)
            if isinstance(v, list):
                runner_kwargs[key] = tuple(v)

        # Credentials are passed as a dict; reconstruct dataclass if any.
        cred_dict = config.get("credentials") or None
        if cred_dict:
            try:
                from engine.test_credentials import TestCredentials
                runner_kwargs["credentials"] = TestCredentials(**cred_dict)
            except Exception as exc:
                print(f"runner_worker: cred reconstruction failed: {exc}",
                      file=sys.stderr)

        runner = AutomationRunner(storage_root=storage_root, **runner_kwargs)
        items_data = config.get("items_data") or []
        scripts = scripts_from_session(items_data, config.get("base_url", ""))

        report = runner.run(scripts)
        rep_dict = _serialise_report(report)
        assets = _build_automation_assets(rep_dict, storage_root)

        payload = {
            "status": "done",
            "config_id": config_id,
            "report": rep_dict,
            "automation_assets": assets,
            "config_echo": {
                "base_url": config.get("base_url", ""),
                "items_data": items_data,
                "selected_ids": config.get("selected_ids") or [],
                "env_types": config.get("env_types") or [],
                "manual_statuses": config.get("manual_statuses") or {},
                "manual_bug_refs": config.get("manual_bug_refs") or {},
                "session_id": config.get("session_id", ""),
                "tester_id": config.get("tester_id", ""),
                "tester_name": config.get("tester_name", ""),
                "testing_types": config.get("testing_types") or [],
                "site_url": config.get("site_url", ""),
                "headless": bool(config.get("headless", True)),
                "record_video": bool(config.get("record_video", False)),
                "affects_version": config.get("affects_version", ""),
                "source": config.get("source", "test_cases"),
                "item_type": config.get("item_type", "test_case"),
                "envs": config.get("envs", {}),
            },
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        # Atomic write — partial result.json must never be observed.
        tmp = result_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, result_path)
        return 0
    except BaseException as exc:
        # Capture any failure (KeyboardInterrupt, SystemExit included)
        # so the operator sees a useful error in the results page rather
        # than a stuck "running" status forever.
        tb = traceback.format_exc()
        try:
            payload = {
                "status": "failed",
                "config_id": config_id,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": tb[-3000:],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = result_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, result_path)
        except Exception:
            pass
        print(f"runner_worker: FAILED {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print(tb, file=sys.stderr)
        return 1
    finally:
        # done.flag is written last and is what the polling route waits
        # on. Touching it after result.json guarantees the result file
        # is fully written and atomic-renamed before the route reads it.
        try:
            with open(done_path, "w", encoding="utf-8") as f:
                f.write(datetime.now(timezone.utc).isoformat())
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
