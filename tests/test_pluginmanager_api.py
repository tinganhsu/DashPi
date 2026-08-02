"""Plugin management keys on where a plugin was found, not on its metadata.

The old test for "is this a third-party plugin?" was the presence of a
repository key in plugin-info.json, which the install CLI writes only when jq
is available. That made an otherwise fine install unmanageable, and would have
made a built-in removable if one ever gained the key.
"""

import json

import pytest
from flask import Flask


@pytest.fixture
def api_app(mock_device_config):
    """A Flask app with just the plugin manager API registered."""
    from plugins.pluginmanager.api import plugin_manage_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["DEVICE_CONFIG"] = mock_device_config
    app.register_blueprint(plugin_manage_bp)
    return app


def _plugins(*entries):
    return list(entries)


BUILTIN = {"id": "clock", "display_name": "Clock", "plugin_dir": "/src/plugins/clock",
           "user_installed": False}
INSTALLED = {"id": "mini_weather", "display_name": "Mini Weather",
             "plugin_dir": "/checkout/plugins/mini_weather", "user_installed": True,
             "repository": "https://github.com/someone/mini_weather"}
INSTALLED_NO_JQ = {"id": "no_jq", "display_name": "No jq",
                   "plugin_dir": "/checkout/plugins/no_jq", "user_installed": True}


class TestThirdPartyDetection:
    def test_only_user_installed_plugins_are_managed(self, api_app, mock_device_config):
        from plugins.pluginmanager import api

        mock_device_config.get_plugins.return_value = _plugins(BUILTIN, INSTALLED)

        with api_app.test_request_context():
            managed = api._third_party_plugins()

        assert [p["id"] for p in managed] == ["mini_weather"]

    def test_an_install_without_jq_is_still_managed(self, api_app, mock_device_config):
        """No repository key, because jq was missing when it was installed."""
        from plugins.pluginmanager import api

        mock_device_config.get_plugins.return_value = _plugins(BUILTIN, INSTALLED_NO_JQ)

        with api_app.test_request_context():
            managed = api._third_party_plugins()

        assert [p["id"] for p in managed] == ["no_jq"]

    def test_a_builtin_carrying_a_repository_key_is_not_managed(self, api_app, mock_device_config):
        from plugins.pluginmanager import api

        shipped_from_git = dict(BUILTIN, repository="https://github.com/tinganhsu/DashPi")
        mock_device_config.get_plugins.return_value = _plugins(shipped_from_git)

        with api_app.test_request_context():
            managed = api._third_party_plugins()

        assert managed == []


class TestUninstallGuard:
    def test_a_builtin_cannot_be_uninstalled(self, api_app, mock_device_config):
        mock_device_config.get_plugins.return_value = _plugins(BUILTIN, INSTALLED)

        resp = api_app.test_client().post(
            "/pluginmanager-api/uninstall",
            data=json.dumps({"plugin_id": "clock"}),
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert resp.get_json()["success"] is False


class TestOperationEnv:
    def test_the_cli_is_told_where_the_app_looks_for_plugins(self, api_app, mock_device_config):
        """Installer and discovery must not derive the root independently."""
        from plugins.pluginmanager import api

        mock_device_config.user_plugins_dir = "/checkout/plugins"

        with api_app.test_request_context():
            env = api._operation_env()

        assert env["DASHPI_PLUGINS_DIR"] == "/checkout/plugins"

    def test_it_overrides_an_inherited_value(self, api_app, mock_device_config, monkeypatch):
        """The launcher exports a PROJECT_DIR the CLI would derive a different root from."""
        from plugins.pluginmanager import api

        monkeypatch.setenv("DASHPI_PLUGINS_DIR", "/somewhere/stale")
        mock_device_config.user_plugins_dir = "/checkout/plugins"

        with api_app.test_request_context():
            env = api._operation_env()

        assert env["DASHPI_PLUGINS_DIR"] == "/checkout/plugins"
