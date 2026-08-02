"""A plugin installed outside src/plugins has to work exactly like a built-in.

Discovery records where each plugin was found (see test_config.py); these tests
cover the three places that used to rebuild that path from the built-in root
and so were wrong for anything else: the import in plugin_registry, the
settings-template lookup in BasePlugin, and the asset route.
"""

import json
import os
import sys

import pytest
from unittest.mock import patch

PLUGIN_SOURCE = '''\
from plugins.base_plugin.base_plugin import BasePlugin


class MiniWeather(BasePlugin):
    def generate_image(self, settings, device_config):
        return None
'''


@pytest.fixture
def user_root(tmp_path):
    """An installed plugin in a second root, with the global state restored after."""
    import plugins
    from plugins import plugin_registry

    root = tmp_path / "plugins"
    plugin_dir = root / "mini_weather"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "mini_weather.py").write_text(PLUGIN_SOURCE)
    (plugin_dir / "plugin-info.json").write_text(
        json.dumps({"id": "mini_weather", "display_name": "Mini Weather", "class": "MiniWeather"})
    )
    (plugin_dir / "settings.html").write_text("<p>mini weather settings</p>")

    original_path = list(plugins.__path__)
    original_classes = dict(plugin_registry.PLUGIN_CLASSES)
    original_modules = set(sys.modules)

    yield plugin_dir

    plugins.__path__[:] = original_path
    plugin_registry.PLUGIN_CLASSES.clear()
    plugin_registry.PLUGIN_CLASSES.update(original_classes)
    for name in set(sys.modules) - original_modules:
        if name.startswith("plugins."):
            del sys.modules[name]


def _config_entry(plugin_dir):
    return {
        "id": "mini_weather",
        "display_name": "Mini Weather",
        "class": "MiniWeather",
        "plugin_dir": str(plugin_dir),
        "user_installed": True,
    }


class TestLoadingFromTheUserRoot:
    def test_a_plugin_outside_the_builtin_root_is_imported(self, user_root):
        from plugins import plugin_registry

        plugin_registry.load_plugins([_config_entry(user_root)])

        instance = plugin_registry.PLUGIN_CLASSES.get("mini_weather")
        assert instance is not None
        assert type(instance).__name__ == "MiniWeather"

    def test_the_user_root_is_spliced_onto_the_package_path(self, user_root):
        import plugins
        from plugins import plugin_registry

        plugin_registry.load_plugins([_config_entry(user_root)])

        assert str(user_root.parent) in plugins.__path__
        # Built-in root stays first, so it always wins the import.
        assert plugins.__path__[0] != str(user_root.parent)

    def test_a_plugin_without_plugin_dir_still_resolves_to_the_builtin_root(self, user_root):
        """Callers predating the second root pass no plugin_dir."""
        from plugins import plugin_registry

        plugin_registry.load_plugins([{"id": "clock", "class": "Clock"}])

        assert "clock" in plugin_registry.PLUGIN_CLASSES

    def test_a_missing_directory_is_skipped_not_raised(self, user_root):
        from plugins import plugin_registry

        entry = _config_entry(user_root)
        entry["plugin_dir"] = str(user_root.parent / "not_installed")
        plugin_registry.load_plugins([entry])

        assert "mini_weather" not in plugin_registry.PLUGIN_CLASSES


class TestPluginDirResolution:
    def test_base_plugin_reads_plugin_dir_from_config(self, user_root):
        from plugins.base_plugin.base_plugin import BasePlugin

        plugin = BasePlugin(_config_entry(user_root))

        assert plugin.get_plugin_dir() == str(user_root)
        assert plugin.get_plugin_dir("settings.html") == os.path.join(str(user_root), "settings.html")

    def test_base_plugin_falls_back_to_the_builtin_root(self):
        from plugins.base_plugin.base_plugin import BasePlugin, PLUGINS_DIR

        plugin = BasePlugin({"id": "clock", "class": "Clock"})

        assert plugin.get_plugin_dir() == os.path.join(PLUGINS_DIR, "clock")

    def test_settings_template_is_found_in_the_user_root(self, user_root):
        """Without plugin_dir this silently fell back to the generic form."""
        from plugins.base_plugin.base_plugin import BasePlugin

        plugin = BasePlugin(_config_entry(user_root))
        params = plugin.generate_settings_template()

        assert params["settings_template"] == "mini_weather/settings.html"


class TestAssetRoute:
    def test_assets_are_served_from_the_user_root(self, user_root, flask_app):
        from blueprints import plugin as plugin_bp_module

        (user_root / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        config = flask_app.config["DEVICE_CONFIG"]
        config.get_plugin.return_value = _config_entry(user_root)

        with flask_app.test_request_context():
            resp = plugin_bp_module.image("mini_weather", "icon.png")

        assert resp.status_code == 200

    def test_traversal_out_of_a_user_plugin_is_still_refused(self, user_root, flask_app):
        from blueprints import plugin as plugin_bp_module

        (user_root.parent / "secret.txt").write_text("not yours")
        config = flask_app.config["DEVICE_CONFIG"]
        config.get_plugin.return_value = _config_entry(user_root)

        with flask_app.test_request_context():
            body, status = plugin_bp_module.image("mini_weather", "../secret.txt")

        assert status == 403

    def test_an_unknown_plugin_still_resolves_against_the_builtin_root(self, flask_app, tmp_path):
        """base_plugin has no plugin-info.json, so it is never in the config list."""
        from blueprints import plugin as plugin_bp_module

        builtin = tmp_path / "builtin"
        (builtin / "base_plugin").mkdir(parents=True)
        (builtin / "base_plugin" / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        config = flask_app.config["DEVICE_CONFIG"]
        config.get_plugin.return_value = None

        with patch.object(plugin_bp_module, "resolve_path", return_value=str(builtin)):
            with flask_app.test_request_context():
                resp = plugin_bp_module.image("base_plugin", "frame.png")

        assert resp.status_code == 200
