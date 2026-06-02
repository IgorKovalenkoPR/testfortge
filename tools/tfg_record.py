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
from engine.locator_registry import (LocatorCandidate,  # noqa: E402
                                      register_candidates,
                                      strategy_of)
from engine.recorder_parser import parse_codegen_output  # noqa: E402


def _recorder_enabled() -> bool:
    return os.environ.get("RECORDER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tfg_record",
        description=(
            "Capture Playwright codegen steps and either attach them to "
            "an existing TC (--tc) or land them in a review draft that "
            "becomes one or more new TCs (--tc omitted; PR-D flow). "
            "Pilot feature — requires RECORDER_ENABLED=1."
        ),
    )
    p.add_argument("--project", required=True,
                   help="32-char project id (from list_projects).")
    # PR-D: --tc is now OPTIONAL. With it → legacy "attach to existing
    # TC" path (PR-B/A/C behaviour, byte-identical). Without it →
    # session is segmented via LLM into 1..N proposed TCs and a
    # SessionDraft is staged at /test-cases/review-session/<token> for
    # the operator to review + Save.
    p.add_argument("--tc", required=False, default=None, dest="tc_id",
                   help="(Optional) TC external id to attach steps to. "
                        "Omit for review-mode — the session becomes "
                        "one or more proposed TCs you confirm in the "
                        "browser.")
    # PR-D: when in review-mode, this is the TestForTge base URL the
    # CLI prints the review-link against. Defaults to the local dev
    # server.
    p.add_argument("--review-base-url", default=None, dest="review_base_url",
                   help="(Review-mode only) TestForTge base URL for the "
                        "review link. Default: $TESTFORTGE_BASE_URL "
                        "or http://127.0.0.1:5000.")
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

    # PR-D dual-mode dispatch:
    #   --tc <ID>   → legacy "attach to existing TC" (PR-B/A/C path).
    #   --tc absent → review-mode: segment session into 1..N proposed
    #                 TCs + stage a SessionDraft + print review URL.
    attach_mode = bool(args.tc_id)

    if attach_mode:
        # Resolve TC up front — refuse to launch the browser if the
        # target does not exist, so the operator does not waste a
        # recording. Skipped for review-mode (no target TC needed).
        tcs = db.load_test_cases(args.project)
        if not any(t.get("id") == args.tc_id for t in tcs):
            print(f"error: TC '{args.tc_id}' not found in project "
                  f"'{args.project}'. Check `python -m mcp_server` "
                  f"list_test_cases for the right id.", file=sys.stderr)
            return 3
    else:
        # Review-mode still needs the project to exist.
        if not db.get_project(args.project):
            print(f"error: project '{args.project}' not found.",
                  file=sys.stderr)
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

    if attach_mode:
        return _finish_attach_mode(args, steps, capture_path, delete_after)
    return _finish_review_mode(args, steps, capture_path, delete_after)


def _finish_attach_mode(args, steps, capture_path, delete_after) -> int:
    """Legacy PR-B/A/C path — attach steps to an existing TC."""
    steps_dicts = [dataclasses.asdict(s) for s in steps]
    ok = db.update_tc_automation_steps(args.project, args.tc_id, steps_dicts)
    if not ok:
        print(f"error: TC '{args.tc_id}' disappeared between resolve and "
              f"attach — race or concurrent delete?", file=sys.stderr)
        return 7

    registered = _register_chain_locators(args.project, steps)

    if delete_after:
        try:
            capture_path.unlink()
        except OSError:
            pass

    print(f"ok: attached {len(steps)} recorded step(s) to "
          f"{args.tc_id} (project {args.project}).")
    if registered:
        print(f"     registered {registered} locator label(s) "
              f"into the Page Object DB.")
    if not delete_after:
        print(f"     capture kept at: {capture_path}")
    return 0


def _finish_review_mode(args, steps, capture_path, delete_after) -> int:
    """PR-D path — segment session, stage a SessionDraft, print
    review URL. The CLI never writes TCs directly in this mode; the
    operator confirms each ProposedTC in the browser."""
    import secrets
    from engine.session_segmenter import segment

    proposed = segment(steps)
    if not proposed:
        print("warning: segmenter produced no flows.", file=sys.stderr)
        return 8

    token = secrets.token_urlsafe(32)
    draft_payload = [pc.to_dict() for pc in proposed]
    draft_id = db.create_session_draft(
        project_id=args.project,
        token=token,
        proposed_tcs=draft_payload,
    )
    if draft_id is None:
        print(f"error: could not create session draft for project "
              f"'{args.project}'.", file=sys.stderr)
        return 9

    # Locator registry still benefits from the captured chains —
    # populate the Page Object DB now so the runner has it ready when
    # the operator saves the TCs. Cheap and idempotent.
    registered = _register_chain_locators(args.project, steps)

    if delete_after:
        try:
            capture_path.unlink()
        except OSError:
            pass

    review_url = _build_review_url(args, token)
    print(
        f"ok: captured {len(steps)} step(s); segmented into "
        f"{len(proposed)} flow(s)."
    )
    for i, pc in enumerate(proposed, start=1):
        print(f"  {i}. [{pc.suggested_suite}] {pc.summary} "
              f"({len(pc.steps)} step{'s' if len(pc.steps) != 1 else ''})")
    print(f"\nReview + save at: {review_url}")
    print("Link expires in 24h.")
    if registered:
        print(f"({registered} locator label(s) registered in Page "
              f"Object DB.)")
    if not delete_after:
        print(f"Capture kept at: {capture_path}")
    return 0


def _register_chain_locators(project_id: str, steps) -> int:
    """Feed each step's locator chain into the Page Object DB. Shared
    by both attach-mode and review-mode — duplicated bumps are
    harmless (the DB helper UPSERTs on (project_id, label))."""
    registered = 0
    for s in steps:
        label = (s.locator_label or "").strip()
        primary = (s.target or "").strip()
        if not (label and primary):
            continue
        all_targets = [primary]
        for a in s.target_alternates or []:
            a_s = (a or "").strip()
            if a_s and a_s not in all_targets:
                all_targets.append(a_s)
        cands = [LocatorCandidate(strategy=strategy_of(t), value=t)
                  for t in all_targets]
        try:
            if register_candidates(project_id, label, cands):
                registered += 1
        except Exception:
            continue
    return registered


def _build_review_url(args, token: str) -> str:
    """Compose the operator-facing review link. Honours --review-base-url,
    then $TESTFORTGE_BASE_URL, then the local dev default. Trims any
    trailing slash on the base so the path concatenates cleanly."""
    base = (args.review_base_url
            or os.environ.get("TESTFORTGE_BASE_URL")
            or "http://127.0.0.1:5000").rstrip("/")
    return f"{base}/test-cases/review-session/{token}"


if __name__ == "__main__":
    raise SystemExit(main())
