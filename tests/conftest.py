"""Shared fixtures for all test levels."""

import os
import sys
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
