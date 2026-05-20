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


def _file_md5(path: str, chunk: int = 65536) -> str:
    """MD5 hex digest of a file. Used to dedupe consecutive byte-
    identical screenshots in a TC's gallery — operator-reported the
    runner produces 3-4 nearly-identical shots per TC because most
    steps don't change page state visibly. Hashing once per shot is
    cheap (~1 ms for a 1280x800 PNG) and lets us drop the noise
    without losing the genuine "before / after" pairs."""
    import hashlib
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()
    except Exception:
        return ""


def _build_automation_assets(report_dict: dict[str, Any],
                              storage_root: str) -> dict[str, Any]:
    """Mirror the inline assets-builder from routes/execution.py so the
    results endpoint can hand the same dict to the per-env loop without
    re-running the validation logic.

    Consecutive byte-identical shots are deduped per TC (only the
    first occurrence is kept). Most TCs spend several steps on the
    same page (expect_text, scroll, expect_url), producing identical
    "after" frames; deduping gives the gallery a clean visual story
    instead of a wall of look-alikes.
    """
    assets: dict[str, Any] = {}
    for r in (report_dict.get("scripts") or []):
        cid = r.get("tc_id") or ""
        if not cid:
            continue
        shots: list[str] = []
        seen_hashes: list[str] = []  # ordered, parallel to shots
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
                        h = _file_md5(abs_p)
                        # Drop only when the SAME hash already lives at
                        # the tail of shots. Non-adjacent duplicates
                        # are still meaningful (e.g. user navigated
                        # away and back).
                        if not seen_hashes or seen_hashes[-1] != h:
                            shots.append(after)
                            seen_hashes.append(h)
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


def _write_terminated_artifacts(
    *,
    signum: int,
    config_id: str,
    error_path: str,
    result_path: str,
    done_path: str,
) -> None:
    """Write error.flag → result.json → done.flag (in that order) when
    the worker is killed by SIGTERM/SIGINT. Pulled out of ``main()`` so
    unit tests can exercise the exact body of the signal handler
    without spawning a subprocess (a SIGTERM round-trip is unreliable
    on Windows where ``os.kill(pid, SIGTERM)`` skips Python handlers).

    Every write is wrapped in its own try/except — the handler is best-
    effort: half-written artefacts are better than none, since the
    UI's polling code only insists on done.flag.
    """
    # Close the live browser first; the kill is racy and any extra
    # latency here delays the kernel cleaning up Chromium.
    try:
        from engine import automation_runner as _ar
        b = getattr(_ar, "_CURRENT_BROWSER", None)
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
    except Exception:
        pass
    # error.flag — concise human-readable reason.
    try:
        with open(error_path, "w", encoding="utf-8") as f:
            f.write(
                f"terminated by signal {signum} at "
                f"{datetime.now(timezone.utc).isoformat()}"
            )
    except Exception:
        pass
    # result.json — atomic write so the polling route never sees a
    # partial file.
    try:
        payload = {
            "status": "terminated",
            "config_id": config_id,
            "error": f"Worker killed by signal {signum}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = result_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, result_path)
    except Exception:
        pass
    # done.flag LAST — that's the file the route polls.
    try:
        with open(done_path, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


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

    # ── SIGTERM / SIGINT handler ───────────────────────────────────────
    # Without this, gunicorn/host SIGTERM kills the worker mid-run; the
    # `finally` below runs only on KeyboardInterrupt (SIGINT) — bare
    # SIGTERM leaves no done.flag, the UI polls for 120 s before
    # surfacing a confusing "stalled" message. We catch both signals,
    # force-close the active Playwright browser (otherwise Chromium
    # outlives us and holds ~250 MB), write an error.flag with the
    # reason, write result.json with status="terminated", and finally
    # touch done.flag so the polling route sees the failure on its
    # next tick. POSIX exit codes 143 (SIGTERM) and 130 (SIGINT).
    import signal as _signal

    error_path = os.path.join(pending_dir, f"{config_id}.error.flag")

    def _on_terminate(signum, _frame):  # noqa: D401 — signal handler
        _write_terminated_artifacts(
            signum=signum,
            config_id=config_id,
            error_path=error_path,
            result_path=result_path,
            done_path=done_path,
        )
        # 143 = 128 + SIGTERM(15); 130 = 128 + SIGINT(2).
        if hasattr(_signal, "SIGTERM") and signum == _signal.SIGTERM:
            sys.exit(143)
        sys.exit(130)

    # Windows Python has no SIGTERM in some builds — guard with
    # hasattr. SIGINT exists on all platforms.
    if hasattr(_signal, "SIGTERM"):
        try:
            _signal.signal(_signal.SIGTERM, _on_terminate)
        except (ValueError, OSError):
            # signal.signal raises if not called from the main thread,
            # which happens under pytest-xdist worker processes.
            pass
    try:
        _signal.signal(_signal.SIGINT, _on_terminate)
    except (ValueError, OSError):
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

        # TFWefloLab integration PR-1: dispatch on ``mode``. Default
        # ``"tc_driven"`` keeps every existing run path byte-identical;
        # ``"walkthrough"`` only fires when the WALKTHROUGH_MODE_ENABLED
        # env var is set, so the scaffold lands safely on prod.
        mode = (config.get("mode") or "tc_driven").strip().lower()
        if mode == "walkthrough":
            from engine.walkthrough_runner import (
                WalkthroughRunner, feature_enabled as _wt_enabled,
            )
            if not _wt_enabled():
                raise RuntimeError(
                    "walkthrough mode requested but "
                    "WALKTHROUGH_MODE_ENABLED is not set"
                )
            wt_cfg = config.get("walkthrough") or {}
            # AutomationRunner-shaped kwargs that the scaffold also
            # accepts (headless, viewport, record_video). Unknown keys
            # are dropped by WalkthroughRunner.__init__'s ``**_ignored``.
            wt_kwargs = {
                k: runner_kwargs.get(k)
                for k in ("headless", "viewport", "record_video")
                if k in runner_kwargs
            }
            wt_kwargs.update({
                "max_pages": int(wt_cfg.get("max_pages", 6)),
                "device_timeout_ms": int(wt_cfg.get("device_timeout_ms",
                                                      480000)),
                "navigation_timeout_ms": int(
                    wt_cfg.get("navigation_timeout_ms", 45000)),
                "max_form_fills": int(wt_cfg.get("max_form_fills", 5)),
                "axe_enabled": bool(wt_cfg.get("axe_enabled", True)),
                # PR-2: project TestCases as plain dicts so the runner
                # can record URL-pattern bindings per visited page.
                # The route layer materialises these from the DB in
                # PR-3; for now the debug endpoint can supply them
                # directly in the config JSON.
                "test_cases": list(wt_cfg.get("test_cases") or []),
            })
            runner = WalkthroughRunner(
                storage_root=storage_root,
                base_url=config.get("base_url", ""),
                **wt_kwargs,
            )
            start_urls = wt_cfg.get("start_urls") or [config.get("base_url", "")]
            report = runner.run(start_urls=start_urls)
            items_data: list = []
            # PR-2: surface the heuristic findings to the route layer.
            # The result.json now carries two parallel views — raw, in
            # the order the runner emitted them, plus a deduped view
            # collapsed by ``walkthrough_dedup.fingerprint``. The route
            # picks whichever fits the rendering target; PR-3's findings
            # subtab will read the deduped one to mirror TFWefloLab's
            # cross-device collapsing.
            walkthrough_findings = list(runner.findings)
            try:
                walkthrough_findings_deduped = runner.dedupe_findings()
            except Exception as exc:  # pragma: no cover — defensive
                print(f"runner_worker: dedupe failed: {exc}",
                      file=sys.stderr)
                walkthrough_findings_deduped = walkthrough_findings
            walkthrough_tc_bindings = list(
                getattr(runner, "tc_bindings", []) or []
            )
        else:
            runner = AutomationRunner(storage_root=storage_root, **runner_kwargs)
            items_data = config.get("items_data") or []
            scripts = scripts_from_session(items_data, config.get("base_url", ""))

            report = runner.run(scripts)
            walkthrough_findings = []
            walkthrough_findings_deduped = []
            walkthrough_tc_bindings = []
        rep_dict = _serialise_report(report)
        assets = _build_automation_assets(rep_dict, storage_root)

        payload = {
            "status": "done",
            "config_id": config_id,
            "report": rep_dict,
            "automation_assets": assets,
            # PR-2: empty lists for tc_driven mode keep the result.json
            # schema stable across modes — the route layer can read
            # ``walkthrough_findings`` unconditionally and just iterate
            # an empty list in the TC-driven branch.
            "walkthrough_findings":         walkthrough_findings,
            "walkthrough_findings_deduped": walkthrough_findings_deduped,
            "walkthrough_tc_bindings":      walkthrough_tc_bindings,
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
        # S3.3: write a DashboardMetricSnapshot so the trend chart on
        # /test-metrics gets a fresh data point on every completed run.
        # Best-effort: a snapshot failure must not crash the worker —
        # the run result is already on disk, the dashboard can recover
        # on the next page load. Safe to call here because S3.4's WAL +
        # busy_timeout pragmas (engine/db.py) prevent the detached
        # subprocess from deadlocking with the gunicorn worker on
        # concurrent writes.
        try:
            pid = (config.get("project_id") or "").strip()
            if pid:
                from engine.test_metrics_generator import snapshot_metrics_from_db
                snapshot_metrics_from_db(pid)
        except Exception as exc:  # pragma: no cover — best-effort
            try:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "runner_worker: metric snapshot failed: %s", exc)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
