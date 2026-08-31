"""PR-E — backend tests for /api/recorder-session/{start,finish}.

The extension is plain JS we don't pytest, but the endpoints it hits
have to be rock-solid: bad inputs must not corrupt SessionDrafts, and
the segmenter pipeline must produce a real review URL. These tests
pin both the success path (start → finish → review URL) and the four
big failure modes (flag off, missing project, unknown token, no steps).
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from engine import db


# Minimal valid AutomationStep dict — same shape content.js sends.
def _make_step(action="click", target='role=button[name="Sign in"]',
                value="", label="role=button:Sign in"):
    return {
        "action": action,
        "target": target,
        "value": value,
        "raw": f'page.locator("{target}").{action}()',
        "comment": "",
        "target_alternates": ["text=Sign in", "css=button.primary"],
        "locator_label": label,
        "kind": "action",
        "assertion_type": "",
    }


@pytest.fixture
def ext_project(client):
    pid = db.upsert_project(
        name=f"ext-api-{os.urandom(4).hex()}",
        base_url="https://app.example.com",
    )
    with client.session_transaction() as s:
        s["project_id"] = pid
        s["active_project_id"] = pid
        s["_session_active_since"] = 9_999_999_999
    yield pid
    db.delete_project(pid)


class TestRecorderSessionStart:
    def test_returns_token_and_finish_url_when_flag_on(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert "token" in body and len(body["token"]) >= 30
        assert body["project_id"] == ext_project
        assert body["finish_url"].endswith("/api/recorder-session/finish")
        assert "{token}" in body["review_url_template"]

    def test_returns_403_when_flag_off(self, client, ext_project):
        env = {k: v for k, v in os.environ.items()
                if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "recorder_disabled"

    def test_returns_400_without_active_project(self, client):
        """No active project in session AND no project_id in body —
        must refuse so the extension never gets a token that can't
        be resolved on finish."""
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_active_project"

    def test_explicit_project_id_in_body_overrides_session(self, client):
        # Plant a project but don't bind it in session — body should win.
        pid = db.upsert_project(
            name=f"ext-body-{os.urandom(4).hex()}",
            base_url="https://x.test")
        try:
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post("/api/recorder-session/start",
                                    json={"project_id": pid})
            assert resp.status_code == 200
            assert resp.get_json()["project_id"] == pid
        finally:
            db.delete_project(pid)

    def test_cors_headers_present(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={})
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_preflight_options_returns_204(self, client, ext_project):
        resp = client.open("/api/recorder-session/start", method="OPTIONS")
        assert resp.status_code == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


class TestRecorderSessionFinish:
    def _start_and_get_token(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={"project_id": ext_project})
        assert resp.status_code == 200
        return resp.get_json()["token"]

    def test_full_round_trip_creates_session_draft(self, client, ext_project):
        """Happy path: start → finish with valid steps → review_url
        points at a SessionDraft the GET review-session route can
        actually load."""
        token = self._start_and_get_token(client, ext_project)

        # Stub the LLM segmenter — keep the test pure.
        from engine import session_segmenter as _seg
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post(
                    "/api/recorder-session/finish",
                    json={
                        "token": token,
                        "steps": [
                            _make_step(action="goto",
                                        target="https://x.test/login",
                                        label=""),
                            _make_step(),
                        ],
                    },
                )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert "/test-cases/review-session/" in body["review_url"]
        assert body["proposed_tc_count"] >= 1

        # Confirm the SessionDraft exists.
        draft_token = body["review_url"].rsplit("/", 1)[-1]
        draft = db.get_session_draft(draft_token)
        assert draft is not None
        assert draft["project_id"] == ext_project
        assert len(draft["proposed_tcs"]) >= 1

    def test_unknown_token_returns_404(self, client, ext_project):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": "bogus-never-issued",
                       "steps": [_make_step()]},
            )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "unknown_token"

    def test_empty_steps_rejected(self, client, ext_project):
        token = self._start_and_get_token(client, ext_project)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": token, "steps": []},
            )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_valid_steps"

    def test_malformed_step_dropped_silently(self, client, ext_project):
        """_decode_recorded_steps drops items without an action; the
        remaining valid step still creates a draft."""
        token = self._start_and_get_token(client, ext_project)
        from engine import session_segmenter as _seg
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post(
                    "/api/recorder-session/finish",
                    json={
                        "token": token,
                        "steps": [
                            {"raw": "garbage with no action"},  # dropped
                            _make_step(),                        # kept
                        ],
                    },
                )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_token_consumed_on_finish(self, client, ext_project):
        """A second finish with the same token must fail — protects
        against duplicate uploads from a buggy extension or a
        replay attack."""
        token = self._start_and_get_token(client, ext_project)
        from engine import session_segmenter as _seg
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                first = client.post(
                    "/api/recorder-session/finish",
                    json={"token": token, "steps": [_make_step()]})
                assert first.status_code == 200
                second = client.post(
                    "/api/recorder-session/finish",
                    json={"token": token, "steps": [_make_step()]})
        assert second.status_code == 404
        assert second.get_json()["error"] == "unknown_token"

    def test_returns_403_when_flag_off(self, client, ext_project):
        env = {k: v for k, v in os.environ.items()
                if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": "x", "steps": [_make_step()]},
            )
        assert resp.status_code == 403

    def test_cors_headers_on_finish(self, client, ext_project):
        token = self._start_and_get_token(client, ext_project)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post(
                "/api/recorder-session/finish",
                json={"token": token, "steps": [_make_step()]},
            )
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


class TestRecorderTelemetry:
    """PR-F — the /finish endpoint accepts an optional deep-capture blob
    (network + console + DOM), sanitises + caps it server-side, and the
    review page renders it."""

    def _start(self, client, pid):
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.post("/api/recorder-session/start",
                                json={"project_id": pid})
        return resp.get_json()["token"]

    def _finish(self, client, token, steps, telemetry=None):
        from engine import session_segmenter as _seg
        body = {"token": token, "steps": steps}
        if telemetry is not None:
            body["telemetry"] = telemetry
        with mock.patch.object(_seg, "_call_llm", return_value=[]):
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                return client.post("/api/recorder-session/finish", json=body)

    def test_telemetry_stored_and_returned(self, client, ext_project):
        token = self._start(client, ext_project)
        tele = {
            "network": [
                {"method": "GET", "url": "https://x/ok", "status": 200,
                 "ok": True, "type": "xhr"},
                {"method": "POST", "url": "https://x/bad", "status": 500,
                 "ok": False, "type": "fetch"},
            ],
            "console": [
                {"level": "log", "text": "hi", "source": "console"},
                {"level": "error", "text": "kaboom", "source": "exception"},
            ],
            "dom_snapshots": [
                {"url": "https://x/", "title": "Home",
                 "interactive": [{"tag": "button", "locator": "role=button",
                                   "name": "Go"}], "element_count": 1},
            ],
            "meta": {"debugger_ok": True, "debugger_error": ""},
        }
        resp = self._finish(client, token, [_make_step()], telemetry=tele)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        # Counts are computed server-side, not trusted from the client.
        assert body["telemetry_counts"]["network"] == 2
        assert body["telemetry_counts"]["network_failures"] == 1
        assert body["telemetry_counts"]["console_errors"] == 1

        draft_token = body["review_url"].rsplit("/", 1)[-1]
        draft = db.get_session_draft(draft_token)
        assert draft["telemetry"] is not None
        assert len(draft["telemetry"]["network"]) == 2
        assert draft["telemetry"]["dom_snapshots"][0]["title"] == "Home"

    def test_telemetry_capped_server_side(self, client, ext_project):
        """A client that ignores its own caps can't bloat the row — the
        server clips network to 500 entries."""
        token = self._start(client, ext_project)
        net = [{"method": "GET", "url": f"https://x/{i}", "status": 200,
                 "ok": True} for i in range(900)]
        resp = self._finish(client, token, [_make_step()],
                             telemetry={"network": net})
        assert resp.status_code == 200
        assert resp.get_json()["telemetry_counts"]["network"] == 500

    def test_malformed_telemetry_ignored(self, client, ext_project):
        """Garbage telemetry doesn't break finish — it's dropped to
        None and the draft is created regardless."""
        token = self._start(client, ext_project)
        resp = self._finish(client, token, [_make_step()],
                             telemetry="not-a-dict")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_no_telemetry_still_works(self, client, ext_project):
        """Older extension that omits telemetry entirely — unchanged
        happy path."""
        token = self._start(client, ext_project)
        resp = self._finish(client, token, [_make_step()])
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_debugger_failure_note_preserved(self, client, ext_project):
        """When the debugger couldn't attach, no net/console captured but
        the meta error is kept so the review page can explain the thin
        panel."""
        token = self._start(client, ext_project)
        tele = {"network": [], "console": [], "dom_snapshots": [],
                "meta": {"debugger_ok": False,
                          "debugger_error": "Cannot attach to chrome://"}}
        resp = self._finish(client, token, [_make_step()], telemetry=tele)
        assert resp.status_code == 200
        draft_token = resp.get_json()["review_url"].rsplit("/", 1)[-1]
        draft = db.get_session_draft(draft_token)
        assert draft["telemetry"]["meta"]["debugger_ok"] is False
        assert "chrome://" in draft["telemetry"]["meta"]["debugger_error"]


class TestUiTrigger:
    """The /test-cases page must surface the 🎬 Start session recording
    button when RECORDER_ENABLED=1 and hide it when off. Without the
    button, the extension has no entry point and the feature is
    silently dead."""

    def test_button_visible_when_flag_on(self, client, ext_project):
        # Plant a TC so the page renders the testcases tab.
        db.save_test_cases(ext_project, [{
            "id": "TC-001", "section": "Login", "section_num": 1,
            "summary": "Sign in", "preconditions": "",
            "test_steps": "1. Open", "test_data": "",
            "expected_result": "Welcome", "issues": "", "comment": "",
            "user_story_id": "US-1", "category": "Positive",
            "priority": "High", "status": "Unchecked",
            "testing_type": "Functional", "url_pattern": "",
            "trigger": "manual"},
        ])
        with client.session_transaction() as s:
            s["test_cases_data"] = db.load_test_cases(ext_project)
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert 'id="ext-recorder-start"' in body
        assert 'id="ext-recorder-modal"' in body
        assert "/api/recorder-session/start" in body

    def test_csrf_protection_does_not_block_endpoints(self, app, ext_project,
                                                      sign_in):
        """Regression for PR-E hotfix: production has CSRFProtect on
        every POST, but recorder endpoints must be exempt because:
        * /finish is called cross-origin from the extension (no CSRF
          token possible);
        * /start is called via fetch() from the modal — keeping both
          exempt is consistent with /debug/walkthrough's pattern.

        Test re-enables CSRF (conftest disables it for the rest of the
        suite) and verifies the POSTs still succeed with no token."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            with app.test_client() as csrf_client:
                # This client is built here rather than taken from the
                # fixture, so it has to be signed in here too: /start is
                # role-gated now (see tests/test_recorder_token_scope.py)
                # and an anonymous caller gets a redirect, which reads
                # exactly like the CSRF rejection this test is about.
                sign_in(csrf_client)
                with csrf_client.session_transaction() as s:
                    s["project_id"] = ext_project
                    s["active_project_id"] = ext_project
                    s["_session_active_since"] = 9_999_999_999
                with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                    # /start — must NOT 400 with CSRF error.
                    resp = csrf_client.post(
                        "/api/recorder-session/start",
                        json={"project_id": ext_project},
                    )
                assert resp.status_code == 200, (
                    f"CSRF blocked /start: {resp.get_data(as_text=True)}")
                token = resp.get_json()["token"]

                # /finish — same expectation.
                from engine import session_segmenter as _seg
                with mock.patch.object(_seg, "_call_llm", return_value=[]):
                    with mock.patch.dict(os.environ,
                                          {"RECORDER_ENABLED": "1"}):
                        resp = csrf_client.post(
                            "/api/recorder-session/finish",
                            json={"token": token,
                                   "steps": [_make_step()]},
                        )
                assert resp.status_code == 200, (
                    f"CSRF blocked /finish: {resp.get_data(as_text=True)}")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_button_hidden_when_flag_off(self, client, ext_project):
        db.save_test_cases(ext_project, [{
            "id": "TC-001", "section": "Login", "section_num": 1,
            "summary": "Sign in", "preconditions": "", "test_steps": "",
            "test_data": "", "expected_result": "", "issues": "",
            "comment": "", "user_story_id": "US-1",
            "category": "Positive", "priority": "High",
            "status": "Unchecked", "testing_type": "Functional",
            "url_pattern": "", "trigger": "manual"},
        ])
        with client.session_transaction() as s:
            s["test_cases_data"] = db.load_test_cases(ext_project)
        env = {k: v for k, v in os.environ.items()
                if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["FLASK_DEBUG"] = "1"
            resp = client.get("/test-cases")
        body = resp.get_data(as_text=True)
        assert 'id="ext-recorder-start"' not in body


class TestTheProjectComesFromPostgresNotTheSession:
    """The session key is empty more often than it looks.

    ``session["project_id"]`` is set when somebody picks a project in
    this session. It is empty after a fresh sign-in, and empty after the
    free plan wipes the filesystem session store on restart — while the
    project itself sits in Postgres and the picker renders it correctly.
    So /start answered "no_active_project" on a page whose header named
    the active project. ``resolve_active_project()`` exists for exactly
    this and is what every other project read in the module uses.

    Found by walking the recorder end to end on staging. No unit test
    had a session without the key, so nothing pointed at it.
    """

    def test_a_session_without_the_key_still_finds_the_project(
            self, client):
        # The project is created the way a browser creates one, so it is
        # owned by this session in Postgres — that ownership is how
        # resolve_active_project finds it. Planting an ownerless row
        # instead would make the resolver return "" for the right reason
        # and the assertion would blame the wrong thing.
        name = f"ext-nosess-{os.urandom(4).hex()}"
        created = client.post("/projects/db/create",
                              data={"project_name": name, "next": "/"},
                              follow_redirects=False)
        assert created.status_code in (302, 303), created.status_code

        with client.session_transaction() as s:
            pid = s.get("project_id")
            assert pid, "project creation did not pin a project"
            # Now drop it. This is the state a fresh sign-in leaves, and
            # the state the free plan leaves after a restart wipes the
            # filesystem session store.
            s.pop("project_id", None)
            s["_session_active_since"] = 9_999_999_999

        try:
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post("/api/recorder-session/start", json={})
            assert resp.status_code == 200, resp.get_json()
            assert resp.get_json().get("project_id") == pid
        finally:
            db.delete_project(pid)

    def test_an_explicit_project_id_still_wins(self, client, ext_project):
        other = db.upsert_project(
            name=f"ext-explicit-{os.urandom(4).hex()}",
            base_url="https://other.example.com",
        )
        try:
            with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
                resp = client.post("/api/recorder-session/start",
                                   json={"project_id": other})
            assert resp.status_code == 200
            assert resp.get_json().get("project_id") == other
        finally:
            db.delete_project(other)


# ── The extension download ───────────────────────────────────────────

class TestTheExtensionArchive:
    """/recorder/extension.zip — the install path for people without git.

    The button beside this download mints a token and opens a tab; the
    capture itself lives in the extension's content-script. So an
    instruction that ended at "select the extension/ folder from your
    checkout" made the recorder unusable by exactly the audience it was
    built for — testers who did not want a terminal.
    """

    def _archive(self, client):
        import io
        import zipfile
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1"}):
            resp = client.get("/recorder/extension.zip")
        assert resp.status_code == 200, resp.status_code
        return resp, zipfile.ZipFile(io.BytesIO(resp.get_data()))

    def test_it_serves_a_real_zip_as_an_attachment(self, client,
                                                   ext_project):
        resp, _ = self._archive(client)
        assert resp.mimetype == "application/zip"
        disposition = resp.headers.get("Content-Disposition", "")
        assert "attachment" in disposition
        assert "testfortge-recorder.zip" in disposition

    def test_the_archive_is_a_loadable_extension(self, client, ext_project):
        # Chrome's Load unpacked needs a manifest at the root of the
        # folder it is pointed at. One top-level folder, manifest inside.
        _, zf = self._archive(client)
        names = zf.namelist()
        assert "testfortge-recorder/manifest.json" in names, names[:10]
        roots = {n.split("/")[0] for n in names}
        assert roots == {"testfortge-recorder"}, (
            f"unzipping would scatter {len(roots)} entries into Downloads")

    def test_the_manifest_survives_the_round_trip(self, client,
                                                  ext_project):
        # Not "a file called manifest.json exists" — a corrupted archive
        # would pass that and fail in Chrome with a useless message.
        _, zf = self._archive(client)
        manifest = json.loads(
            zf.read("testfortge-recorder/manifest.json").decode("utf-8"))
        assert manifest.get("manifest_version")
        assert manifest.get("name")

    def test_it_carries_the_content_script_that_does_the_capturing(
            self, client, ext_project):
        # The reason the download exists at all.
        _, zf = self._archive(client)
        assert "testfortge-recorder/content.js" in zf.namelist()

    def test_no_editor_droppings_ship_to_a_tester(self, client,
                                                  ext_project):
        _, zf = self._archive(client)
        for name in zf.namelist():
            leaf = name.rsplit("/", 1)[-1]
            assert not leaf.startswith("."), name
            assert "__pycache__" not in name, name

    def test_a_host_outside_the_pilot_serves_nothing(self, client,
                                                     ext_project):
        env = {k: v for k, v in os.environ.items()
               if k != "RECORDER_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            resp = client.get("/recorder/extension.zip")
        assert resp.status_code == 403

    def test_an_anonymous_caller_gets_nothing(self, anon_client):
        # Listed in route_policy.POLICY as "login". The build is not a
        # secret, but an open path serves it to anyone who guesses the
        # URL, and the fail-closed table is where that decision lives.
        with mock.patch.dict(os.environ, {"RECORDER_ENABLED": "1",
                                          "AUTH_ENABLED": "1",
                                          "ORG_MODE": "1"}):
            resp = anon_client.get("/recorder/extension.zip")
        assert resp.status_code in (302, 401, 403), resp.status_code
        assert resp.mimetype != "application/zip"

    def test_a_second_download_is_byte_identical(self, client, ext_project):
        # The archive is cached on the folder's mtime. A cache keyed
        # wrongly would show up as two different builds of the same
        # extension, which is a bug nobody thinks to suspect.
        first, _ = self._archive(client)
        second, _ = self._archive(client)
        assert first.get_data() == second.get_data()
        assert first.headers.get("ETag") == second.headers.get("ETag")

