"""Engine × Platform × Browser matrix (Feature #6) + versioned OS list (#5).

Locks down the contract used by the Test Execution form:
    /test-execution renders the new <optgroup>-style OS-version selector
    AND posting an (os_version, browser) pair makes the route resolve a
    Playwright engine + UA + viewport via resolve_platform_browser().
"""
import pytest


class TestVersionedDataExposedToTemplate:
    def test_get_renders_versioned_optgroups(self, client):
        """With test cases in session, the GET /test-execution form must
        emit per-OS-family <optgroup> blocks for both Web and Mobile Web
        environments."""
        with client.session_transaction() as s:
            s["test_cases_data"] = [{
                "id": "TC1", "section": "A", "section_num": 1,
                "summary": "s", "preconditions": "", "test_steps": "1",
                "test_data": "", "expected_result": "", "issues": "",
                "comment": "", "user_story_id": "",
                "category": "Positive", "priority": "High",
                "status": "Unchecked", "testing_type": "Functional",
            }]
            s["_session_active_since"] = 9_999_999_999  # bypass session-clear hook

        r = client.get("/test-execution")
        assert r.status_code == 200
        body = r.get_data(as_text=True)

        # New selector + hidden mirror exists on the Web env panel.
        assert 'name="web_os_version"' in body
        assert 'id="web_platform_hidden"' in body
        # Same on Mobile Web.
        assert 'name="mw_os_version"' in body
        assert 'id="mw_os_hidden"' in body
        # Versioned options actually rendered.
        for needle in ("Windows 11", "Windows 10",
                       "macOS Sonoma (14)", "Ubuntu 24.04",
                       "iOS 18", "Android 14"):
            assert needle in body, f"missing {needle!r}"


class TestMatrixResolution:
    def test_exact_match_picks_engine_and_viewport(self):
        from engine.qa_testers import resolve_platform_browser
        # Win 11 + Chrome → chromium, 1920x1080, Win UA
        e = resolve_platform_browser("Windows 11", "Chrome")
        assert e["engine"] == "chromium"
        assert e["viewport"] == (1920, 1080)
        assert "Windows NT" in e["ua"]

        # macOS Sonoma + Safari → webkit
        e = resolve_platform_browser("macOS Sonoma (14)", "Safari")
        assert e["engine"] == "webkit"
        assert "Mac OS X" in e["ua"]

        # Win 11 + Firefox → firefox
        e = resolve_platform_browser("Windows 11", "Firefox")
        assert e["engine"] == "firefox"
        assert "Firefox/" in e["ua"]

    def test_family_fallback_for_unknown_version(self):
        """A new OS version not yet in the matrix must still resolve via
        the family-level fallback so the runner doesn't crash with a
        missing UA."""
        from engine.qa_testers import resolve_platform_browser
        e = resolve_platform_browser("Windows 99", "Chrome")
        # Falls back via PLATFORM_BROWSER_FAMILY[("Windows","Chrome")]
        # which currently mirrors Windows 11 + Chrome.
        assert e["engine"] == "chromium"
        assert "Windows NT" in e["ua"]

    def test_default_for_completely_unknown_pair(self):
        from engine.qa_testers import resolve_platform_browser, PLATFORM_BROWSER_DEFAULT
        e = resolve_platform_browser("PlanNineFromOuterSpace 0.1", "Lynx")
        assert e["engine"] == PLATFORM_BROWSER_DEFAULT["engine"]
        assert e["viewport"] == PLATFORM_BROWSER_DEFAULT["viewport"]


class TestRunnerHonoursMatrix:
    """The AutomationRunner constructor must accept and store the
    matrix-supplied engine/UA/viewport without breaking call-sites that
    don\'t pass them."""

    def test_default_construction_unchanged(self, tmp_path):
        from engine.automation_runner import AutomationRunner
        r = AutomationRunner(storage_root=str(tmp_path), base_url="https://x")
        assert r.engine_kind == "chromium"
        assert r.user_agent == ""
        assert r.viewport == (1280, 800)

    def test_matrix_args_applied(self, tmp_path):
        from engine.automation_runner import AutomationRunner
        r = AutomationRunner(storage_root=str(tmp_path), base_url="https://x",
                             engine_kind="webkit",
                             user_agent="Mozilla/5.0 (mac) Safari/605",
                             viewport_override=(1680, 1050))
        assert r.engine_kind == "webkit"
        assert r.user_agent == "Mozilla/5.0 (mac) Safari/605"
        assert r.viewport == (1680, 1050)

    def test_invalid_engine_falls_back_to_chromium(self, tmp_path):
        from engine.automation_runner import AutomationRunner
        r = AutomationRunner(storage_root=str(tmp_path), base_url="https://x",
                             engine_kind="netscape4")
        assert r.engine_kind == "chromium"
