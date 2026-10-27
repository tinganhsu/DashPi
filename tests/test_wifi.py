"""Tests for the Wi-Fi captive-portal blueprint."""

import os
from html.parser import HTMLParser
from unittest.mock import MagicMock

import pytest
from flask import Flask

from blueprints.auth import auth_bp
from blueprints.wifi import wifi_bp
from utils.wifi_manager import STATE_AP_MODE, STATE_CONNECTED

# A nearby attacker picks the SSID, so it is untrusted input. This one breaks
# out of a double-quoted attribute without needing any angle brackets.
HOSTILE_SSID = 'x" onmouseover=alert(1) y'


class AttributeCollector(HTMLParser):
    """Collect every attribute the browser would see, keyed by tag."""

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture
def wifi_app(mock_device_config):
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "src", "templates"),
    )
    app.config["TESTING"] = True
    app.secret_key = "test"
    app.config["DEVICE_CONFIG"] = mock_device_config
    mock_device_config.get_config.return_value = "DashPi"

    wifi_manager = MagicMock()
    wifi_manager.state = STATE_AP_MODE
    wifi_manager.scan_networks.return_value = [
        {"ssid": HOSTILE_SSID, "signal": 80, "security": "WPA2"},
    ]
    app.config["WIFI_MANAGER"] = wifi_manager
    app.config["DISPLAY_MANAGER"] = MagicMock()
    app.register_blueprint(wifi_bp)
    app.register_blueprint(auth_bp)
    return app


def test_portal_escapes_ssid_in_attributes(wifi_app):
    html = wifi_app.test_client().get("/wifi").get_data(as_text=True)
    parser = AttributeCollector()
    parser.feed(html)

    items = [attrs for tag, attrs in parser.tags if attrs.get("class") == "network-item"]
    assert len(items) == 1
    assert "onmouseover" not in items[0]
    assert items[0]["data-ssid"] == HOSTILE_SSID


@pytest.fixture
def connect_app(wifi_app):
    wifi_app.config["WIFI_MANAGER"].connect.return_value = (True, "192.168.1.50")
    wifi_app.config["WIFI_MANAGER"].get_ap_password.return_value = "pw"
    wifi_app.config["WIFI_MANAGER"].get_hotspot_ip.return_value = "10.42.0.1"
    return wifi_app


@pytest.mark.parametrize("path, method", [
    ("/wifi", "get"),
    ("/wifi/scan", "get"),
    ("/wifi/connect", "post"),
])
def test_setup_endpoints_refuse_strangers_outside_ap_mode(connect_app, path, method):
    connect_app.config["WIFI_MANAGER"].state = STATE_CONNECTED
    client = connect_app.test_client()

    response = getattr(client, method)(path, json={"ssid": "AttackerAP", "password": "x"})

    assert response.status_code in (302, 401)
    connect_app.config["WIFI_MANAGER"].connect.assert_not_called()
    connect_app.config["WIFI_MANAGER"].scan_networks.assert_not_called()


def test_connect_is_open_to_strangers_in_ap_mode(connect_app):
    connect_app.config["WIFI_MANAGER"].state = STATE_AP_MODE
    client = connect_app.test_client()

    response = client.post("/wifi/connect", json={"ssid": "Home", "password": "x"})

    assert response.get_json()["success"] is True
    connect_app.config["WIFI_MANAGER"].connect.assert_called_once_with("Home", "x")


def test_signed_in_admin_can_scan_outside_ap_mode(connect_app):
    connect_app.config["WIFI_MANAGER"].state = STATE_CONNECTED
    client = connect_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True

    response = client.get("/wifi/scan")

    assert response.status_code == 200
    assert response.get_json()["networks"][0]["ssid"] == HOSTILE_SSID
