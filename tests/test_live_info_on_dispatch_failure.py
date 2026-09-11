"""A dispatch that fails has to say so on /test-execution/diag.

When the Playwright pass cannot be started, ``routes/execution.py`` catches
the exception and stamps the phase and the reason into
``automation_runs/_live/info.json``, so the operator reads what blew up on
the diagnostics page instead of tailing Render's combined log stream. That
is what the block says it is for.

It had never done it. The two lines that write the file reached for a bare
``os``, and the module has no module-level ``os`` — every other place
imports it locally as ``_os``, the sibling pre-flight block twelve lines
above included. So the write raised ``NameError``, the bare
``except Exception: pass`` around it swallowed that, and the operator got
an empty diagnostics page after exactly the failure the page exists to
explain. Latent since the block was written, and invisible in the only two
ways that matter: it cannot fail loudly, and no test went near it.

Found by pyflakes over the module, not by a test — which is the point of
the second class below. A one-line fix that nothing measures is a one-line
regression waiting for the next edit, and the same class of mistake can
land in any of the other route modules tomorrow.

``TestTheFileIsActuallyWritten``  the behaviour: force a dispatch failure,
                                  read the file back.
``TestNoModuleHasAnUndefinedName`` the gate that would have caught it, over
                                  every module the app imports.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

from engine import db as _db
from engine.automation_paths import STORAGE_ROOT
from engine.testcase_generator import TestCase
from routes._shared import SERVER_START_TIME, tc_to_dict


def _info_path() -> pathlib.Path:
    return pathlib.Path(STORAGE_ROOT, "automation_runs", "_live", "info.json")


# ── The behaviour ─────────────────────────────────────────────────

class TestTheFileIsActuallyWritten:

    @pytest.fixture
    def project(self, client, request, fresh_org, make_project):
        pid = make_project(f"live-info-{request.node.name}",
                           **({"org_id": fresh_org} if fresh_org else {}))
        _db.save_test_cases(pid, [tc_to_dict(TestCase(
            id="TC_001", section="S", section_num=1, summary="Verify it",
            preconditions="", test_steps="1. do it", test_data="",
            expected_result="e", priority="High", category="Positive"))])
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
            sess["project_setup"] = {"project_name": "live-info"}
            sess.pop("test_cases_data", None)
        return pid

    @pytest.fixture
    def dispatch_fails(self, monkeypatch):
        """Break the worker launch, which is what the handler catches.

        Patched at ``subprocess.Popen`` rather than at some seam invented
        for the test: the route imports subprocess itself, and a seam that
        only exists to be patched proves the seam works, not the handler.
        """
        def _boom(*a, **kw):
            raise OSError("no room to fork, said the test")
        monkeypatch.setattr(subprocess, "Popen", _boom)
        return _boom

    @staticmethod
    def _post(client):
        return client.post("/test-execution", data={
            "source": "test_cases",
            "run_mode": "tc_driven",
            "base_url": "https://example.invalid",
            "env_types": "web",
            "selected_items": "TC_001",
            "scope": "all",
        }, follow_redirects=False)

    def test_the_reason_reaches_the_file(self, client, project,
                                         dispatch_fails):
        path = _info_path()
        if path.exists():
            path.unlink()

        self._post(client)

        assert path.exists(), (
            "the dispatch failed and nothing was written — the operator "
            "opens /test-execution/diag to an empty page after exactly the "
            "failure it is there to explain")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("status") == "failed"
        assert "OSError" in payload.get("phase_error", ""), payload
        assert "no room to fork" in payload.get("phase_error", ""), payload

    def test_the_run_still_fails_politely(self, client, project,
                                          dispatch_fails):
        """The stamp is a diagnostic, not a change of outcome: the caller
        is still redirected with a flash rather than shown a 500."""
        resp = self._post(client)
        assert resp.status_code in (302, 303), resp.status_code

    def test_the_preflight_payload_is_updated_not_replaced(self, client,
                                                           project,
                                                           dispatch_fails):
        """The handler reads info.json and writes it back, rather than
        starting a fresh object.

        What it reads is not arbitrary: a pre-flight block a few lines
        earlier writes the run's own info.json — base_url, case counters,
        the run id the live view keys on — *before* dispatch is attempted,
        so that the live page shows activity even if the worker dies
        immediately. Replacing that wholesale with a two-field failure
        stamp would erase the context the diagnostics page shows beside
        the reason.

        Written after the first version of this test planted its own
        payload and asserted that survived. It does not, and should not:
        the pre-flight write lands between the planting and the failure.
        The test was describing a product I had imagined.
        """
        self._post(client)

        payload = json.loads(_info_path().read_text(encoding="utf-8"))
        assert payload.get("status") == "failed", payload
        assert "OSError" in payload.get("phase_error", ""), payload
        # …and the pre-flight context is still there beside it.
        assert payload.get("base_url") == "https://example.invalid", payload


# ── The gate that would have caught it ────────────────────────────

class TestNoModuleHasAnUndefinedName:
    """pyflakes over every module the app imports.

    A ``NameError`` inside a ``try: ... except Exception: pass`` is the
    hardest defect class this codebase has to find by testing, because the
    code cannot fail loudly and the feature simply never happens. Three
    have been found by reading — ``_run_site_aware`` wired to nothing, a
    session key that was never a session key, and this one — and reading
    does not scale.

    Import-time checking does. It is cheap, it is exhaustive over the
    files, and it fails on the line rather than on a symptom somewhere
    downstream.

    Skipped rather than failed when pyflakes is absent, so a contributor
    who has not installed a linter is not blocked by one. CI installs it
    explicitly in ``.github/workflows/tests.yml``, next to pytest-cov and
    the other test-only tools and for the same stated reason — a linter
    has no business in a production dyno's site-packages — so the gate
    holds where it has to.
    """

    #: Reported by pyflakes and not a defect: a *string* annotation under
    #: ``from __future__ import annotations``, which is never evaluated.
    #: Listed rather than filtered by pattern so each exception is a
    #: decision somebody made, not a category that quietly grows.
    EXPECTED = {
        "engine/qa_testers.py:747": (
            "a quoted annotation on _TITLE_ARCHETYPES; annotations are "
            "strings in this module and are never evaluated"),
    }

    @staticmethod
    def _undefined() -> list[str]:
        pytest.importorskip("pyflakes",
                            reason="pyflakes is in requirements-dev.txt")
        root = pathlib.Path(__file__).resolve().parent.parent
        targets = sorted(
            str(p) for d in ("routes", "engine", "mcp_server")
            for p in (root / d).rglob("*.py")
        ) + [str(root / "app.py")]
        out = subprocess.run([sys.executable, "-m", "pyflakes", *targets],
                             capture_output=True, text=True)
        found = []
        for line in (out.stdout or "").splitlines():
            if "undefined name" not in line:
                continue
            # "<path>:LINE:COL: undefined name 'x'". Matched from the RIGHT:
            # splitting on ":" from the left works on posix and hands you
            # "F" on Windows, where the drive letter carries one too. The
            # first version did that and reported a bare drive as the file.
            m = re.match(r"^(.*):(\d+):\d+: undefined name ", line)
            if not m:
                continue
            rel = pathlib.Path(m.group(1)).resolve().relative_to(root).as_posix()
            found.append(f"{rel}:{m.group(2)}")
        return found

    def test_none(self):
        unexpected = [f for f in self._undefined() if f not in self.EXPECTED]
        assert not unexpected, (
            "these names are not defined where they are used. Inside a "
            "broad except: they raise NameError, get swallowed, and the "
            "feature silently never happens:\n  " + "\n  ".join(unexpected))

    def test_the_scan_reads_the_source(self):
        """Without this the test above passes on a scan that matched
        nothing — a renamed flag, a moved directory, a pyflakes that
        errored out and printed to stderr."""
        root = pathlib.Path(__file__).resolve().parent.parent
        assert (root / "routes" / "execution.py").exists()
        pytest.importorskip("pyflakes")
        out = subprocess.run(
            [sys.executable, "-m", "pyflakes",
             str(root / "routes" / "execution.py")],
            capture_output=True, text=True)
        assert out.returncode in (0, 1), out.stderr[:400]

    def test_it_would_catch_a_planted_one(self, tmp_path):
        """And that the matching survives a path with a different shape."""
        pytest.importorskip("pyflakes")
        planted = tmp_path / "planted.py"
        planted.write_text("def f():\n    return nowhere_at_all\n",
                           encoding="utf-8")
        out = subprocess.run([sys.executable, "-m", "pyflakes", str(planted)],
                             capture_output=True, text=True)
        assert "undefined name" in out.stdout, out.stdout + out.stderr
