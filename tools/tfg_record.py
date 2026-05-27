"""``tfg record`` — capture Playwright codegen steps into a TestForTge TC.

Usage::

    python -m tools.tfg_record \\
        --project <project_id> --tc <TC_ID> --url <start_url>

    # Re-import an existing codegen capture without launching the browser:
    python -m tools.tfg_record \\
        --project <project_id> --tc <TC_ID> --from-file path/to/captured.py

Flow:

1. Verify the host opted into the Recorder pilot (``RECORDER_ENABLED=1``).
2. Resolve the TC by ``(project_id, external_id)`` in the local DB.
3. Launch ``playwright codegen --target python-async`` at ``--url`` (or
   read ``--from-file``). Operator clicks through the scenario; codegen
   writes Python to a temp file as they go and exits when the browser
   window closes.
4. Parse the captured Python into ``list[AutomationStep]`` via
   :mod:`engine.recorder_parser`.
5. Write the parsed list to ``TestCase.automation_steps_json`` via
   :func:`engine.db.update_tc_automation_steps`. The runner picks the
   recording up automatically on its next pass.

Deviation from the original plan (see ``docs/plans/recorder_integration.md``):
the default path goes through the **local DB** directly, not the MCP
HTTP tool. Reason: the spike showed the MCP HTTP client setup adds an
asyncio dependency the CLI does not need for the pilot's local use
case. The MCP ``record_steps_attach`` tool is still wired and tested —
a future ``--mcp-url`` flag will reuse it for cross-machine workflows.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

# Add repo root to sys.path so this script works as `python -m tools.tfg_record`
# AND as `python tools/tfg_record.py` from a checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine import db  # noqa: E402
from engine import recorder  # noqa: E402
from engine.recorder_parser import parse_codegen_output  # noqa: E402


def _recorder_enabled() -> bool:
    return os.environ.get("RECORDER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tfg_record",
        description=(
            "Capture Playwright codegen steps and attach them to a "
            "TestForTge TC. Pilot feature — requires RECORDER_ENABLED=1."
        ),
    )
    p.add_argument("--project", required=True,
                   help="32-char project id (from list_projects).")
    p.add_argument("--tc", required=True, dest="tc_id",
                   help="TC external id (TC-001 / SC1_002 style).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url",
                     help="Start URL for the codegen recording session.")
    src.add_argument("--from-file",
                     help="Path to an existing codegen capture to re-import.")
    p.add_argument("--test-id-attributes",
                   default=recorder.DEFAULT_TEST_ID_ATTRIBUTES,
                   help="Comma-separated data attributes codegen prefers "
                        "for locators (default: %(default)s).")
    p.add_argument("--timeout-s", type=int, default=None,
                   help="Max recording duration in seconds. Falls back to "
                        "RECORDER_TIMEOUT_S env (default 1800).")
    p.add_argument("--browser", default="chromium",
                   choices=("chromium", "firefox", "webkit"))
    p.add_argument("--keep-capture", action="store_true",
                   help="Do not delete the codegen capture file after "
                        "parsing — useful for debugging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not _recorder_enabled():
        print("error: RECORDER_ENABLED is not set. Export RECORDER_ENABLED=1 "
              "to opt this host into the Recorder pilot.", file=sys.stderr)
        return 2

    # Resolve TC up front — refuse to launch the browser if the target
    # does not exist, so the operator does not waste a recording.
    tcs = db.load_test_cases(args.project)
    if not any(t.get("id") == args.tc_id for t in tcs):
        print(f"error: TC '{args.tc_id}' not found in project "
              f"'{args.project}'. Check `python -m mcp_server` "
              f"list_test_cases for the right id.", file=sys.stderr)
        return 3

    if args.from_file:
        capture_path = Path(args.from_file).resolve()
        if not capture_path.is_file():
            print(f"error: --from-file path does not exist: {capture_path}",
                  file=sys.stderr)
            return 4
        delete_after = False
    else:
        if not recorder.codegen_available():
            print("error: Playwright codegen is not available. Install with:\n"
                  "  pip install playwright\n"
                  "  python -m playwright install chromium",
                  file=sys.stderr)
            return 5
        fd, tmp_path = tempfile.mkstemp(suffix="_recorded.py", prefix="tfg_")
        os.close(fd)
        capture_path = Path(tmp_path)
        delete_after = not args.keep_capture
        try:
            recorder.run_codegen(
                args.url,
                capture_path,
                test_id_attributes=args.test_id_attributes,
                timeout_s=args.timeout_s,
                browser=args.browser,
            )
        except KeyboardInterrupt:
            print("\nrecording cancelled by user", file=sys.stderr)
            return 130

    src = capture_path.read_text(encoding="utf-8")
    steps = parse_codegen_output(src)
    if not steps:
        print("warning: parser found no recorded steps. "
              "Capture preserved at: " + str(capture_path), file=sys.stderr)
        return 6

    steps_dicts = [dataclasses.asdict(s) for s in steps]
    ok = db.update_tc_automation_steps(args.project, args.tc_id, steps_dicts)
    if not ok:
        print(f"error: TC '{args.tc_id}' disappeared between resolve and "
              f"attach — race or concurrent delete?", file=sys.stderr)
        return 7

    if delete_after:
        try:
            capture_path.unlink()
        except OSError:
            pass

    print(f"ok: attached {len(steps)} recorded step(s) to "
          f"{args.tc_id} (project {args.project}).")
    if not delete_after:
        print(f"     capture kept at: {capture_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
