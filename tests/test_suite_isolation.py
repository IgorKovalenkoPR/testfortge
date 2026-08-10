"""M-1 — two runs of this suite must not stand in each other's way.

E10's confirmation run started a background regression and the browser gate
at the same time and collected **nine failures**, none of which were
regressions::

    test_failed_item_can_file_a_bug_carrying_the_testers_words
    test_the_limit_counts_across_the_whole_scope
    test_another_project_s_rows_are_untouched
    test_a_vanished_session_is_not_blamed_on_the_user             …and five more

The same leg alone was 4 581 green. Eight of the nine were two processes
inserting into one SQLite scratch file and then counting each other's rows;
the ninth was two processes sharing ``flask_session/``. Reproduced here
before the fix — 9 failed in one run, 4 581 passed in the other — because a
concurrency claim taken from a report rather than from a machine is the kind
that turns out to have been about something else.

That is not a product defect, and it costs more than a product defect of the
same size: it rules out ``pytest-xdist``, it rules out a long run in the
background while you work, and its failures look exactly like real ones. The
one it produced was called *"a vanished session is not blamed on the user"* —
a name that reads as a genuine regression in session handling.

Why this file rather than an assertion in ``conftest.py``
--------------------------------------------------------
The fix is four environment variables and a per-process directory, and the
cheapest way to lose it is for someone to reinstate a fixed path for a good
local reason. So the guard runs **a second pytest in a second interpreter**
and compares the paths it resolves with ours. Asserting that our own paths
contain our own pid would pass in a world where every process computes the
same string a different way; only a second process can show that two runs
disagree about where to write. Same argument as
``feedback_gate_measuring_wrong_chain``: measure the chain that runs, not a
reconstruction of it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from app import app as flask_app
from engine import automation_paths
from engine import db as _db


#: Set in the child so the guard below does not spawn a grandchild.
CHILD_MARKER = "TFG_ISOLATION_CHILD"

#: What the paths are called in the emitted report, and where each one comes
#: from. Read through the code that uses them rather than from ``os.environ``
#: — the environment is the input to the fix, not its effect.
def _paths() -> dict[str, str]:
    return {
        "database": _db.database_url(),
        "sessions": str(flask_app.config["SESSION_FILE_DIR"]),
        "uploads": str(flask_app.config["UPLOAD_FOLDER"]),
        "artefacts": str(automation_paths.STORAGE_ROOT),
    }


def test_emit_paths_for_the_isolation_guard(capsys):
    """Print this process's paths as JSON. Run directly by the guard below.

    Harmless on its own: as part of a normal run it asserts that the four
    paths exist, which is the weakest true thing that can be said about
    them.
    """
    report = _paths()
    with capsys.disabled():
        print("TFG_PATHS " + json.dumps(report))
    assert report["database"]
    for key in ("sessions", "uploads", "artefacts"):
        assert os.path.isdir(report[key]), f"{key} is not a directory"


def _second_run(**env_changes) -> dict[str, str]:
    """Start a real second pytest and return the paths it resolved.

    ``None`` as a value removes the variable — the difference between "a
    fresh terminal" and "a subprocess of this one" is the whole subject of
    the second test below.
    """
    here = pathlib.Path(__file__).resolve()
    env = dict(os.environ)
    env[CHILD_MARKER] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # The child must be a plain invocation. Inheriting PYTEST_ADDOPTS —
    # which CI sets to "-n auto --dist loadfile" — makes it spawn its own
    # workers, and then the paths it prints belong to a worker rather than
    # to the run being compared. Found by turning xdist on in CI: both
    # tests below went red while nothing about isolation had changed.
    env.pop("PYTEST_ADDOPTS", None)
    for name, value in env_changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:randomly",
         f"{here.name}::test_emit_paths_for_the_isolation_guard"],
        cwd=str(here.parent), env=env, capture_output=True, timeout=600)
    out = done.stdout.decode("utf-8", "replace")
    assert done.returncode == 0, f"the second pytest failed:\n{out}"
    marker = [line for line in out.splitlines()
              if line.startswith("TFG_PATHS ")]
    assert marker, f"the second run printed no paths:\n{out}"
    return json.loads(marker[-1][len("TFG_PATHS "):])


def _shared_with(theirs: dict[str, str]) -> list[str]:
    ours = _paths()
    return sorted(key for key in ours
                  if os.path.normcase(str(ours[key]))
                  == os.path.normcase(str(theirs[key])))


class TestTwoRunsDoNotShareAnything:
    """The property, measured against a second live process."""

    @pytest.mark.skipif(os.environ.get(CHILD_MARKER) == "1",
                        reason="this IS the second process")
    def test_a_second_pytest_resolves_four_different_paths(self):
        """Someone else runs the suite in another terminal."""
        theirs = _second_run(
            # A fresh terminal has none of these. Handing them over would
            # give the child the answer and prove nothing.
            TESTFORTGE_DB=None, SESSION_FILE_DIR=None, UPLOAD_FOLDER=None,
            STORAGE_FOLDER=None, STORAGE_ROOT=None, PYTEST_XDIST_WORKER=None,
            TFG_TEST_PATHS_OWNER=None)

        shared = _shared_with(theirs)
        assert not shared, (
            f"two concurrent runs would share {shared}. That is the M-1 "
            f"defect returning: one scratch database and one session "
            f"directory produced nine failures that looked like "
            f"regressions.\n  this run: {_paths()}\n  second: {theirs}")

    @pytest.mark.skipif(os.environ.get(CHILD_MARKER) == "1",
                        reason="this IS the second process")
    def test_an_xdist_worker_does_not_inherit_its_controllers_paths(self):
        """The half the first test cannot see — and it was broken.

        An xdist worker is a **subprocess of the controller**, so it starts
        with the controller's environment already containing
        ``TESTFORTGE_DB`` — set by the controller's own copy of
        ``conftest.py``. A conftest that treats "already set" as "the
        developer chose this" therefore isolates separate invocations
        perfectly and puts every worker of one run on one database.

        Measured, after the first version of the fix and with the test above
        green: ``pytest -n 4`` failed with 31 projects where the test had
        created one. The environment here is inherited **deliberately**,
        with only ``PYTEST_XDIST_WORKER`` added, because that is exactly
        what xdist hands a worker.
        """
        theirs = _second_run(PYTEST_XDIST_WORKER="gw-guard")

        shared = _shared_with(theirs)
        assert not shared, (
            f"an xdist worker would share {shared} with its controller, so "
            f"`pytest -n` puts every worker on the same database. A value "
            f"this process set is not a value the developer chose.\n"
            f"  controller: {_paths()}\n  worker: {theirs}")

    @pytest.mark.skipif(os.environ.get(CHILD_MARKER) == "1",
                        reason="this IS the second process")
    def test_the_database_is_not_the_developers_own(self):
        """The older half of the same rule, kept explicit.

        Before the scratch database existed the suite ran against
        ``storage/testfortge.db`` — the developer's real one — and filled it
        with 12 231 bug rows. The per-process path fixed the concurrency
        problem and could quietly undo this one by pointing somewhere
        convenient inside the checkout.
        """
        url = _db.database_url()
        if not url.startswith("sqlite:"):
            pytest.skip("this run is on a server database, which is fine")
        checkout = pathlib.Path(__file__).resolve().parent.parent
        path = pathlib.Path(url.replace("sqlite:///", "", 1)).resolve()
        assert checkout not in path.parents, (
            f"the suite is writing its database inside the checkout "
            f"({path}); that is the developer's own data directory")


class TestTheParallelCIStaysSafe:
    """M-1 made the suite parallel-safe for **SQLite**. CI also has one
    Postgres service container, and that is not per-worker.

    ``-n auto`` with the default ``--dist load`` hands out individual tests,
    so two workers could run two Postgres migration tests against the same
    throwaway database at once — the same collision M-1 removed, in the one
    place the fix does not reach. ``--dist loadfile`` distributes a file at
    a time, which keeps that whole file inside one worker.

    So the pairing is a rule, not a preference, and the workflow is read
    here rather than trusted.
    """

    @staticmethod
    def _workflow() -> str:
        return (pathlib.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "tests.yml").read_text(
                    encoding="utf-8")

    def test_parallel_execution_keeps_the_postgres_tests_together(self):
        text = self._workflow()
        if "-n auto" not in text and "-n " not in text:
            pytest.skip("CI does not run the suite in parallel")
        assert "--dist loadgroup" in text, (
            "CI runs pytest in parallel without --dist loadgroup. Tests "
            "would be spread per test, and three files write to one shared "
            "Postgres service container — two workers in it reproduce the "
            "false failures M-1 removed.")

    def test_every_file_using_the_shared_postgres_carries_the_mark(self):
        """Written after ``--dist loadfile`` was tried first: it keeps one
        file together and says nothing about three of them. This is the
        assertion that caught it, and it is the one that keeps catching a
        fourth file."""
        root = pathlib.Path(__file__).resolve().parent
        unmarked = []
        for path in sorted(root.glob("test_*.py")):
            if path.name == pathlib.Path(__file__).name:
                continue          # this file only names the variable
            text = path.read_text(encoding="utf-8", errors="replace")
            if "TFG_TEST_POSTGRES_URL" not in text:
                continue
            if 'xdist_group("postgres")' not in text:
                unmarked.append(path.name)
        assert not unmarked, (
            f"{unmarked} use the shared Postgres database and are not in "
            f"the 'postgres' xdist group, so CI can run them in parallel "
            f"against one database. Add "
            f'pytestmark = pytest.mark.xdist_group("postgres").')
