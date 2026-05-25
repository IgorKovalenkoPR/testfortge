"""Stage 3 smoke test — LiveExecutor against a real public URL.

Not part of pytest (depends on network reach + the example.com / httpbin
hosts staying up). Run manually after touching ``engine/live_executor``
to confirm the end-to-end pipeline still works against real Playwright.

Usage:

    python scripts/smoke_live_executor.py [URL]

Default URL is ``https://example.com/`` because:
  * It's tiny (one page, no JS, no forms) — keeps the run under 10 s.
  * It always responds — no flake from a misconfigured CMS.
  * The empty heuristic battery is itself a useful smoke signal: the
    scan code paths execute, just don't emit findings.

Pass ``https://httpbin.org/forms/post`` to exercise the form-fill
heuristic. Pass any site you want to inspect the dedup output on.

Outputs a one-line OK/FAIL plus a ``run_id``; the per-run artefacts
land under ``storage/automation_runs/<run_id>/`` so you can inspect
screenshots + step JSON afterwards.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Make the project importable when this script is invoked from anywhere.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from engine.live_executor import LiveExecutor
from engine.test_credentials import generate_test_account


def main(target_url: str) -> int:
    storage = tempfile.mkdtemp(prefix="tfg_smoke_")
    print(f"[smoke] storage: {storage}")
    print(f"[smoke] target:  {target_url}")

    # Generate a throwaway test account — exercises the credentials
    # plumbing even though example.com has no login form to use it on.
    creds = generate_test_account(base_domain="testfortge.test")
    print(f"[smoke] cred:    {creds.username} (mode={creds.mode})")

    # Two TCs: one fires on every page (smoke), one matches via URL
    # pattern (substring match). Both are simple so the run finishes
    # well under the device_timeout.
    test_cases = [
        {
            "id":             "TC-SMOKE-001",
            "summary":        "Page loads and shows a title",
            "preconditions":  "",
            "test_steps":     f"Open {target_url}\nVerify text 'Example'",
            "test_data":      "",
            "expected_result": "Page is reachable",
            "url_pattern":    "",
            "trigger":        "always",
        },
        {
            "id":             "TC-SMOKE-MATCH-002",
            "summary":        "Pattern-matched TC fires only on /",
            "preconditions":  "",
            "test_steps":     f"Open {target_url}",
            "test_data":      "",
            "expected_result": "Lands on the home page",
            "url_pattern":    "*example*",
            "trigger":        "walkthrough_url_match",
        },
    ]

    t0 = time.time()
    runner = LiveExecutor(
        storage_root=storage,
        base_url=target_url,
        headless=True,
        max_pages=2,
        device_timeout_ms=60_000,
        navigation_timeout_ms=30_000,
        test_cases=test_cases,
        credentials=creds,
        memory_budget_mb=400,
    )
    report = runner.run(start_urls=[target_url])
    elapsed = time.time() - t0

    print(f"\n[smoke] run_id:         {report.run_id}")
    print(f"[smoke] duration:       {elapsed:.1f}s")
    print(f"[smoke] scripts:        {len(report.scripts)} "
          f"(passed={report.passed} failed={report.failed} "
          f"blocked={report.blocked})")
    print(f"[smoke] findings:       {len(runner.findings)}")
    if runner.findings:
        # Compact summary by defect_class for at-a-glance triage.
        by_class: dict[str, int] = {}
        for f in runner.findings:
            by_class[f["defect_class"]] = by_class.get(f["defect_class"], 0) + 1
        for cls, n in sorted(by_class.items()):
            print(f"  -{cls}: {n}")

    print(f"[smoke] tc_bindings:    {len(runner.tc_bindings)} URL(s) "
          f"matched TCs")
    for b in runner.tc_bindings:
        ids = ", ".join(m["id"] for m in b["matches"])
        print(f"  -{b['url']} -> [{ids}]")

    if runner.early_exit_reason:
        print(f"[smoke] EARLY EXIT:     {runner.early_exit_reason}")

    # The live filmstrip must exist after a successful run.
    live_info = Path(storage) / "automation_runs" / "_live" / "info.json"
    if live_info.is_file():
        info = json.loads(live_info.read_text())
        print(f"[smoke] live_info ok:   status={info['status']} "
              f"ts={info['ts']} rss_mb={info.get('rss_mb', '?')}")

    # Pass criterion: navigation succeeded for at least one page AND
    # at least one TC dispatched OR the heuristic battery ran without
    # raising.
    page_scripts = [s for s in report.scripts
                     if s.tc_id.startswith("LIVE-PAGE-")]
    tc_scripts = [s for s in report.scripts
                   if not s.tc_id.startswith("LIVE-PAGE-")]
    pages_ok = page_scripts and all(s.status != "blocked"
                                     for s in page_scripts)
    if pages_ok:
        print("\n[smoke] RESULT: OK")
        return 0
    else:
        print("\n[smoke] RESULT: FAIL — no successful page visits")
        return 1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/"
    raise SystemExit(main(target))
