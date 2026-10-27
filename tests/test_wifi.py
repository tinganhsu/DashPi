"""Tests for the Wi-Fi captive-portal blueprint."""

import os
from html.parser import HTMLParser
from unittest.mock import MagicMock

import pytest
from flask import Flask

from blueprints.wifi import wifi_bp

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
    app.config["DEVICE_CONFIG"] = mock_device_config
    mock_device_config.get_config.return_value = "DashPi"

    wifi_manager = MagicMock()
    wifi_manager.scan_networks.return_value = [
        {"ssid": HOSTILE_SSID, "signal": 80, "security": "WPA2"},
    ]
    app.config["WIFI_MANAGER"] = wifi_manager
    app.config["DISPLAY_MANAGER"] = MagicMock()
    app.register_blueprint(wifi_bp)
    return app


def test_portal_escapes_ssid_in_attributes(wifi_app):
    html = wifi_app.test_client().get("/wifi").get_data(as_text=True)
    parser = AttributeCollector()
    parser.feed(html)

    items = [attrs for tag, attrs in parser.tags if attrs.get("class") == "network-item"]
    assert len(items) == 1
    assert "onmouseover" not in items[0]
    assert items[0]["data-ssid"] == HOSTILE_SSID
