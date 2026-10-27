"""Tests for the login blueprint."""

import os
from unittest.mock import MagicMock

import pytest
from flask import Flask

from blueprints.auth import auth_bp
from blueprints.main import main_bp


@pytest.fixture
def auth_client():
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "src", "templates"),
    )
    app.config["TESTING"] = True
    app.secret_key = "test"
    config = MagicMock()
    config.has_password.return_value = True
    config.check_password.side_effect = lambda password: password == "secret"
    app.config["DEVICE_CONFIG"] = config
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    return app.test_client()


def login(client, next_url):
    return client.post(
        "/login",
        query_string={"next": next_url},
        data={"password": "secret"},
        base_url="http://dashpi.local",
    )


@pytest.mark.parametrize("next_url, expected", [
    ("/loops?x=1", "/loops?x=1"),
    # What the access-control gate actually sends: request.url, absolute.
    ("http://dashpi.local/settings", "http://dashpi.local/settings"),
])
def test_login_follows_next_within_the_app(auth_client, next_url, expected):
    response = login(auth_client, next_url)
    assert response.status_code == 302
    assert response.headers["Location"] == expected


@pytest.mark.parametrize("next_url", [
    "https://evil.example/phish",
    "//evil.example/phish",
    "/\\evil.example/phish",
    "/\t/evil.example/phish",
    "javascript:alert(1)",
    "http://dashpi.local.evil.example/",
])
def test_login_ignores_next_pointing_elsewhere(auth_client, next_url):
    response = login(auth_client, next_url)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
