"""The live view must not show somebody else's run.

Found by walking Automation QA, 2026-08-31. Four routes serve the live
run view, and all four read one instance-wide directory:

    /test-execution/live/frame        automation_runs/_live/latest.png
    /test-execution/live/strip/<n>    automation_runs/_live/strip/NN.png
    /test-execution/live/info         automation_runs/_live/info.json
    /automation/asset/<path>          the same files, by path

None of them is scoped to a project or an organisation. The runner writes
``self._live_dir = os.path.join(runs_root, "_live")`` — one directory for
the whole instance — so whoever opens "Watch live" sees the frames of
whichever run is executing on that machine.

It is not only a screenshot. ``info.json`` carries ``base_url``,
``current_tc``, ``run_id`` and progress, so the JSON alone names another
organisation's site and the title of the case being run against it.

``engine/route_policy.py`` marks these ``"user"``, which asks for a
signed-in caller and says nothing about *which* tenant. The product does
mean to separate tenants — ORG_MODE exists and ``visible_projects``
filters by owner — and this is the same class of defect E5 fixed for
runs, where "a project-A run rendered project B".

``/automation/asset/`` is a second door to the same bytes with its own
path, so scoping the three live routes and leaving it open would fix
nothing.

**Amended on a second walk.** The enumeration above is of routes that
*serve* the live bytes, and two more read the same file to *judge* with it:

    /test-execution/run-status/<run_id>   info.json's ts, phase, counters
    /test-execution/live (no run_id)      info.json's ts

Neither asked whose run it was. So one tenant's heartbeat decided another
tenant's "has the worker died?" verdict, and the run-status branch quoted
their ``phase`` and case counters into a message the wrong caller read. The
lesson the first pass wrote down — after scoping N routes, look for another
address to the same bytes — was right and did not go far enough: a reader
that never returns the bytes is still a reader.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

import pytest


FRAME_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


def _live_dir() -> pathlib.Path:
    from routes.automation import STORAGE_ROOT
    d = pathlib.Path(STORAGE_ROOT) / "automation_runs" / "_live"
    (d / "strip").mkdir(parents=True, exist_ok=True)
    return d


def _write_live(owner_project_id: str, *, base_url: str, current_tc: str):
    """Stand in for a run in progress owned by ``owner_project_id``."""
    d = _live_dir()
    (d / "latest.png").write_bytes(FRAME_PNG)
    (d / "strip" / "00.png").write_bytes(FRAME_PNG)
    (d / "info.json").write_text(json.dumps({
        "project_id": owner_project_id,
        "run_id": "20260831_120000_abcdef",
        "status": "running", "step": 3,
        "cases_done": 1, "cases_total": 9,
        "current_tc": current_tc,
        "base_url": base_url,
        "headless": True, "ts": 1,
    }), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_live():
    yield
    d = _live_dir()
    for name in ("latest.png", "info.json", "strip/00.png"):
        try:
            (d / name).unlink()
        except OSError:
            pass


def _project(client, name: str) -> str:
    client.post("/projects/db/create", data={"project_name": name},
                follow_redirects=True)
    with client.session_transaction() as sess:
        return sess.get("project_id") or ""


OTHER = "b" * 32          # a project id this client does not own


class TestSomebodyElsesRun:
    def test_the_frame_is_not_served(self, client):
        mine = _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        assert mine and mine != OTHER
        _write_live(OTHER, base_url="https://their-staging.example",
                    current_tc="Verify that payouts settle")
        resp = client.get("/test-execution/live/frame")
        assert resp.status_code == 200, "the route should stay quiet, not error"
        assert resp.get_data() != FRAME_PNG, (
            "another project's live frame was served in full")

    def test_the_filmstrip_is_not_served(self, client):
        _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        _write_live(OTHER, base_url="https://their-staging.example",
                    current_tc="Verify that payouts settle")
        resp = client.get("/test-execution/live/strip/0")
        assert resp.get_data() != FRAME_PNG, (
            "another project's filmstrip slot was served in full")

    def test_the_progress_json_names_nothing(self, client):
        _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        _write_live(OTHER, base_url="https://their-staging.example",
                    current_tc="Verify that payouts settle")
        body = client.get("/test-execution/live/info").get_data(as_text=True)
        assert "their-staging.example" not in body, (
            f"the JSON named another organisation's site: {body}")
        assert "payouts settle" not in body, (
            f"the JSON named another organisation's test case: {body}")

    def test_the_generic_asset_route_is_not_a_second_door(self, client):
        _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        _write_live(OTHER, base_url="https://their-staging.example",
                    current_tc="Verify that payouts settle")
        resp = client.get(
            "/automation/asset/automation_runs/_live/latest.png")
        assert resp.get_data() != FRAME_PNG, (
            "the live frame was reachable by path, bypassing the scoping")


class TestTheStallCheckReadsOnlyItsOwnHeartbeat:
    """The two readers that judge rather than serve."""

    @staticmethod
    def _pending(rid: str, *, started: bool = True) -> pathlib.Path:
        from routes.automation import STORAGE_ROOT
        d = pathlib.Path(STORAGE_ROOT) / "automation_runs" / "_pending"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rid}.json").write_text(json.dumps({"cases": []}),
                                       encoding="utf-8")
        if started:
            (d / f"{rid}.started.flag").write_text("1", encoding="utf-8")
        return d

    @pytest.fixture
    def pending_run(self):
        rid = "zz" + uuid.uuid4().hex[:10]
        d = self._pending(rid)
        yield rid
        for suffix in (".json", ".started.flag"):
            try:
                (d / f"{rid}{suffix}").unlink()
            except OSError:
                pass

    def test_run_status_does_not_quote_another_runs_progress(self, client,
                                                            pending_run):
        """``current_tc`` and ``base_url`` were already guarded on
        ``/live/info``. The counters and the phase went out through here."""
        _project(client, "mine-status")
        _write_live(OTHER, base_url="https://theirs.example",
                    current_tc="Their case")
        got = client.get(f"/test-execution/run-status/{pending_run}")
        assert got.status_code == 200
        payload = got.get_json()
        # ``ts`` is 1 in the fixture, so a run this caller owned would read
        # as stalled — which is exactly what makes this assertion sharp.
        assert payload["status"] == "running", payload
        assert "cases_done" not in payload
        assert "phase" not in payload
        assert "9" not in json.dumps(payload)      # cases_total

    def test_my_own_stale_heartbeat_still_reports_a_stall(self, client,
                                                          pending_run):
        """The control. Without it the fix above is indistinguishable from
        deleting the stall detector, which is the thing an operator noticed
        the absence of: a nine-minute-dead worker still reading "running".
        """
        mine = _project(client, "mine-stall")
        _write_live(mine, base_url="https://mine.example",
                    current_tc="My case")
        payload = client.get(
            f"/test-execution/run-status/{pending_run}").get_json()
        assert payload["status"] == "stalled", payload
        assert payload["cases_total"] == 9

    def test_an_idle_instance_still_reports_running(self, client,
                                                    pending_run):
        """No live directory at all — the shape the poller has always
        handled, and the shape a foreign run now produces too."""
        payload = client.get(
            f"/test-execution/run-status/{pending_run}").get_json()
        assert payload["status"] == "running", payload

    def test_the_recent_runs_list_does_not_borrow_their_heartbeat(
            self, client, pending_run):
        """A fresh foreign heartbeat used to make this caller's started run
        read "running". It now reads "stalled", which is the same answer the
        list has always given when no heartbeat is visible — conservative,
        and it offers an idempotent partial import rather than hiding a dead
        worker. See the note at the call site."""
        _project(client, "mine-list")
        d = _live_dir()
        (d / "info.json").write_text(json.dumps({
            "project_id": OTHER, "run_id": "r", "status": "running",
            "cases_done": 1, "cases_total": 9,
            "ts": int(time.time() * 1000),          # very much alive
        }), encoding="utf-8")
        body = client.get("/test-execution/live").get_data(as_text=True)
        assert pending_run in body
        # The status the row carries, read from the rendered page rather
        # than from the route's locals.
        row = body.split(pending_run)[1][:600]
        assert "⚠" in row, row


class TestTheRunnerStampsTheOwner:
    def test_write_live_info_records_the_project(self, tmp_path):
        """The half the hand-written fixtures above cannot see.

        Every test in this file writes ``info.json`` itself, so all of
        them would keep passing if the runner stopped stamping the owner
        — and then no live view would work at all, because an unstamped
        run matches only a caller with no active project. Mutation
        testing found exactly that hole.
        """
        from engine.automation_runner import AutomationRunner
        runner = AutomationRunner(storage_root=str(tmp_path),
                                   base_url="https://my-staging.example",
                                   headless=True, record_video=False,
                                   project_id="a" * 32)
        live = tmp_path / "automation_runs" / "_live"
        live.mkdir(parents=True, exist_ok=True)
        runner._live_dir = str(live)
        runner._write_live_info("running")
        payload = json.loads((live / "info.json").read_text(encoding="utf-8"))
        assert payload.get("project_id") == "a" * 32, payload

    def test_a_runner_without_a_project_stamps_an_empty_owner(self, tmp_path):
        # Not the string "None": the routes compare it against
        # `resolve_active_project(...) or ""`.
        from engine.automation_runner import AutomationRunner
        runner = AutomationRunner(storage_root=str(tmp_path),
                                   base_url="", headless=True,
                                   record_video=False)
        live = tmp_path / "automation_runs" / "_live"
        live.mkdir(parents=True, exist_ok=True)
        runner._live_dir = str(live)
        runner._write_live_info("running")
        payload = json.loads((live / "info.json").read_text(encoding="utf-8"))
        assert payload.get("project_id") == "", payload


class TestMyOwnRun:
    def test_my_frame_still_arrives(self, client):
        mine = _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        _write_live(mine, base_url="https://my-staging.example",
                    current_tc="Verify that login succeeds")
        resp = client.get("/test-execution/live/frame")
        assert resp.get_data() == FRAME_PNG, (
            "my own live frame stopped being served")

    def test_my_filmstrip_still_arrives(self, client):
        mine = _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        _write_live(mine, base_url="https://my-staging.example",
                    current_tc="Verify that login succeeds")
        assert client.get(
            "/test-execution/live/strip/0").get_data() == FRAME_PNG

    def test_my_progress_json_still_arrives(self, client):
        mine = _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        _write_live(mine, base_url="https://my-staging.example",
                    current_tc="Verify that login succeeds")
        body = client.get("/test-execution/live/info").get_data(as_text=True)
        assert "my-staging.example" in body, body
        assert "login succeeds" in body, body

    def test_an_idle_instance_still_answers(self, client):
        # No run at all: the poller must get its idle payload rather than
        # an error, which is what it does today.
        _project(client, f"Mine {uuid.uuid4().hex[:6]}")
        body = client.get("/test-execution/live/info").get_data(as_text=True)
        assert "idle" in body, body
        assert client.get("/test-execution/live/frame").status_code == 200
