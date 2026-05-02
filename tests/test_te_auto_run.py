"""Server-side auto-run + Postgres pack rehydrate (Phase A).

Regression target: a JS-disabled tester uploads a TC pack via the
upload form on /test-execution. With JS, our client-side hook
auto-clicks Run; without JS, nothing happened — pack landed in
session, but the operator had to manually click Run. Phase A wires a
server-side /test-execution/auto-run endpoint that the upload route
redirects to (303-style) when ?auto_run=1 was set, so the run kicks
off without any JS.

Persistence (Phase B): on revisit, /test-execution rehydrates the
session pack from Postgres if the active project saved one earlier.
"""
import io


class TestAutoRunEndpoint:
    def _seed_tc(self, client, n=3):
        with client.session_transaction() as s:
            s["test_cases_data"] = [{
                "id": f"TC-{i}", "section": "X", "section_num": 1,
                "summary": "s", "preconditions": "", "test_steps": "",
                "test_data": "", "expected_result": "", "issues": "",
                "comment": "", "user_story_id": "",
                "category": "Positive", "priority": "High",
                "status": "Unchecked", "testing_type": "Functional",
            } for i in range(n)]
            s["_session_active_since"] = 9_999_999_999

    def test_auto_run_with_pack_creates_test_run(self, client):
        self._seed_tc(client, n=4)
        r = client.get("/test-execution/auto-run", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/test-execution")

        with client.session_transaction() as s:
            runs = s.get("test_runs") or []
            assert len(runs) == 1
            run = runs[0]
            assert run["env_type"] == "web"
            assert run["automation_used"] is False
            results = run["results"]
            assert len(results) == 4   # all 4 TC executed
            for r_ in results:
                assert r_["status"] in ("Passed", "Failed", "Blocked")

    def test_auto_run_without_pack_redirects_with_flash(self, client):
        # Empty session — nothing to run.
        r = client.get("/test-execution/auto-run", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/test-execution")
        with client.session_transaction() as s:
            assert not s.get("test_runs")

    def test_upload_with_auto_run_lands_at_auto_run_endpoint(self, client):
        """End-to-end without JS: upload from /test-execution Referer
        with auto_run=1 → upload route 302 to /test-execution/auto-run
        → that endpoint runs and 302 to /test-execution."""
        csv = b"ID,Section,Summary,Steps,Expected\nTC-1,A,Empty,1.x,Err\n"
        r = client.post(
            "/test-cases/upload",
            data={"upload_file": (io.BytesIO(csv), "p.csv"),
                  "upload_mode": "replace", "auto_run": "1"},
            headers={"Referer": "http://localhost/test-execution"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/test-execution/auto-run")

        # Follow once — auto-run route does the work, then 302s home.
        r2 = client.get(r.headers["Location"], follow_redirects=False)
        assert r2.status_code == 302
        assert r2.headers["Location"].endswith("/test-execution")
        with client.session_transaction() as s:
            assert s.get("test_runs"), "auto-run produced no test_runs"


class TestPackRehydrate:
    def test_rehydrate_loads_tc_from_db_when_session_empty(self, client):
        from engine import db as _db
        # Seed a project + TC rows.
        pid = _db.upsert_project(name="Rehydrate Test", owner_sid="rehydrate-sid")
        _db.save_test_cases(pid, [{
            "id": "TC-DB-1", "section": "S", "section_num": 1,
            "summary": "from-db", "preconditions": "", "test_steps": "",
            "test_data": "", "expected_result": "", "issues": "",
            "comment": "", "user_story_id": "",
            "category": "Positive", "priority": "High",
            "status": "Unchecked", "testing_type": "Functional",
        }])
        with client.session_transaction() as s:
            s["project_id"] = pid
            s["_session_active_since"] = 9_999_999_999
            # Deliberately do NOT seed test_cases_data — rehydrate
            # must populate it from the DB.

        r = client.get("/test-execution")
        assert r.status_code == 200
        with client.session_transaction() as s:
            tc = s.get("test_cases_data") or []
            assert tc, "rehydrate failed to populate test_cases_data"
            assert tc[0]["id"] == "TC-DB-1"

    def test_rehydrate_does_not_clobber_existing_session(self, client):
        from engine import db as _db
        pid = _db.upsert_project(name="No-Clobber Test", owner_sid="no-clobber-sid")
        _db.save_test_cases(pid, [{
            "id": "TC-DB-A", "section": "X", "section_num": 1,
            "summary": "from-db", "preconditions": "", "test_steps": "",
            "test_data": "", "expected_result": "", "issues": "",
            "comment": "", "user_story_id": "",
            "category": "Positive", "priority": "High",
            "status": "Unchecked", "testing_type": "Functional",
        }])
        with client.session_transaction() as s:
            s["project_id"] = pid
            s["_session_active_since"] = 9_999_999_999
            # Session ALREADY has different data — rehydrate must keep
            # session\'s version, not replace with DB.
            s["test_cases_data"] = [{"id": "TC-SESSION-ONLY"}]

        client.get("/test-execution")
        with client.session_transaction() as s:
            tc = s.get("test_cases_data") or []
            assert tc[0]["id"] == "TC-SESSION-ONLY"
