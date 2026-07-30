"""Unit tests for the shared access-control module."""

from __future__ import annotations

import pytest
from flask import Flask, jsonify, redirect, render_template_string, session
from jinja2 import DictLoader

from utils.access_control import (
    BASE_LOCKOUT_SECONDS,
    DEFAULT_PUBLIC_ENDPOINTS,
    FREE_LOGIN_ATTEMPTS,
    MAX_LOCKOUT_SECONDS,
    PRE_AUTH_ALLOW,
    LoginThrottle,
    csrf_origin_ok,
    install_access_control,
    resolve_secret_key,
)


class FakeConfig:
    def __init__(self, password: bool = True) -> None:
        self.password = password
        self.expected = "correct-horse"

    def has_password(self) -> bool:
        return self.password


@pytest.fixture
def gated_app():
    app = Flask(__name__)
    app.secret_key = "test"
    config = FakeConfig()
    app.config["FAKE_CONFIG"] = config
    app.jinja_env.loader = DictLoader({"login.html": "throttled: {{ error }}"})

    @app.route("/login", endpoint="auth.login", methods=["GET", "POST"])
    def login():
        from flask import request

        if request.method == "POST":
            if request.form.get("password") == config.expected:
                session["authenticated"] = True
                return redirect("/")
            return render_template_string("bad password"), 200
        return render_template_string("login form")

    @app.route("/setup_password", endpoint="auth.setup_password")
    def setup_password():
        return "setup"

    @app.route("/", endpoint="main.main_page")
    def main_page():
        return "dashboard"

    @app.route("/display", endpoint="main.display_page")
    def display_page():
        return "public display"

    @app.route("/api/config", endpoint="api_config")
    def api_config():
        return jsonify({"ok": True})

    @app.route("/api/render/next", endpoint="api_render_next", methods=["POST"])
    def api_render_next():
        return jsonify({"ok": True})

    @app.route("/api/device/ping", endpoint="device_ping")
    def device_ping():
        return jsonify({"ok": True})

    install_access_control(app, config)
    return app


@pytest.fixture
def client(gated_app):
    return gated_app.test_client()


def login(client, password="correct-horse"):
    return client.post(
        "/login",
        data={"password": password},
        headers={"Origin": "http://localhost"},
    )


def test_browser_pages_redirect_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_api_calls_get_json_401_not_an_html_redirect(client):
    response = client.get("/api/config")
    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "unauthorized"}


def test_public_display_stays_open(client):
    assert client.get("/display").status_code == 200


def test_authenticated_session_gets_through(client):
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/api/config").status_code == 200


def test_setup_is_forced_before_a_password_exists(gated_app):
    gated_app.config["FAKE_CONFIG"].password = False
    response = gated_app.test_client().get("/")
    assert response.status_code == 302
    assert "/setup_password" in response.headers["Location"]


def test_mutation_without_origin_is_blocked(client):
    login(client)
    response = client.post("/api/render/next")
    assert response.status_code == 403
    assert response.get_json()["error"] == "csrf_validation_failed"


def test_mutation_from_our_own_origin_is_allowed(client):
    login(client)
    response = client.post("/api/render/next", headers={"Origin": "http://localhost"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("origin", "referer", "host", "expected"),
    [
        ("http://box:3000", None, "box:3000", True),
        ("https://box:3000", None, "box:3000", True),
        ("http://box:3001", None, "box:3000", False),
        ("null", None, "box:3000", False),
        (None, "http://box:3000/settings", "box:3000", True),
        (None, "http://box:3000.evil.example/x", "box:3000", False),
        (None, None, "box:3000", False),
    ],
)
def test_csrf_origin_ok(origin, referer, host, expected):
    assert csrf_origin_ok(origin, referer, host) is expected


def test_pre_auth_allow_skips_session_gate():
    app = Flask(__name__)
    app.secret_key = "test"
    config = FakeConfig()

    @app.route("/api/device/ping", endpoint="device_ping")
    def device_ping():
        return jsonify({"ok": True})

    def pre_auth():
        from flask import request

        if request.path.startswith("/api/device/"):
            return PRE_AUTH_ALLOW
        return None

    install_access_control(app, config, pre_auth=pre_auth)
    assert app.test_client().get("/api/device/ping").status_code == 200


def test_pre_auth_can_deny():
    app = Flask(__name__)
    app.secret_key = "test"
    config = FakeConfig()

    @app.route("/api/device/ping", endpoint="device_ping")
    def device_ping():
        return jsonify({"ok": True})

    def pre_auth():
        from flask import request

        if request.path.startswith("/api/device/"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    install_access_control(app, config, pre_auth=pre_auth)
    assert app.test_client().get("/api/device/ping").status_code == 401


def test_repeated_failures_get_locked_out(client):
    for _ in range(FREE_LOGIN_ATTEMPTS):
        assert login(client, "wrong").status_code == 200
    response = login(client, "wrong")
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_backoff_grows_and_is_capped():
    throttle = LoginThrottle()
    for _ in range(FREE_LOGIN_ATTEMPTS):
        throttle.record_failure("10.0.0.1")
    first = throttle.retry_after("10.0.0.1")
    assert 0 < first <= BASE_LOCKOUT_SECONDS

    throttle.record_failure("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") > first

    for _ in range(40):
        throttle.record_failure("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") <= MAX_LOCKOUT_SECONDS


def test_secret_key_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_SECRET_KEY", "from-env")
    assert resolve_secret_key(tmp_path / "secret_key") == "from-env"


def test_secret_key_is_generated_and_then_reused(monkeypatch, tmp_path):
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    path = tmp_path / "secret_key"
    generated = resolve_secret_key(path)
    assert len(generated) == 64
    assert resolve_secret_key(path) == generated
    assert path.stat().st_mode & 0o777 == 0o600


def test_secret_key_has_no_hardcoded_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    first = resolve_secret_key(tmp_path / "a")
    second = resolve_secret_key(tmp_path / "b")
    assert first != second


def test_install_sets_secret_key_from_path(monkeypatch, tmp_path):
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    app = Flask(__name__)
    config = FakeConfig()
    path = tmp_path / "secret_key"
    install_access_control(app, config, secret_path=path)
    assert app.secret_key
    assert path.read_text(encoding="utf-8").strip() == app.secret_key


def test_default_public_endpoints_cover_display_and_auth():
    assert "main.display_page" in DEFAULT_PUBLIC_ENDPOINTS
    assert "auth.login" in DEFAULT_PUBLIC_ENDPOINTS
