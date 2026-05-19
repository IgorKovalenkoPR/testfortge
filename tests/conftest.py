"""Shared fixtures for all test levels."""

import os
import sys
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests run against SQLite by default. The production-safety guard in
# ``engine.db.init_db`` refuses to boot on SQLite unless FLASK_DEBUG=1
# (or the explicit TESTFORTGE_ALLOW_SQLITE_PROD escape hatch). Set the
# debug flag here, before any module that touches the DB gets imported,
# so the whole suite sees a consistent local-dev environment.
os.environ.setdefault("FLASK_DEBUG", "1")

from app import app as flask_app


@pytest.fixture
def app():
    flask_app.config["TESTING"] = True
    flask_app.config["SESSION_TYPE"] = "filesystem"
    # Disable CSRF checking during tests — the clients POST without
    # rendering the templates that supply the token, and we trust the
    # test client itself. Production keeps CSRF enabled.
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _no_network_crawl(monkeypatch):
    # Several tests post real URLs (testfort.com, example.com) into
    # /checklist or /test-cases, which trigger engine.site_crawler.crawl_site
    # on the route's sync path. The crawler fetches up to MAX_PAGES * 8s
    # which can blow past the 60s sync_gen deadline on CI runners where
    # outbound HTTP is slow or rate-limited, producing a 302 redirect and
    # a failing assert resp.status_code == 200. Stub it to a fast empty
    # SiteAnalysis so the rule-based fallback path takes over and the
    # suite stays deterministic. Tests that need real crawl behaviour
    # can monkeypatch over this with their own stub.
    from engine.site_crawler import SiteAnalysis
    monkeypatch.setattr(
        "engine.site_crawler.crawl_site",
        lambda url, **kw: SiteAnalysis(base_url=url, domain=""),
    )
