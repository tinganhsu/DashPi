"""Tests for plugin registry helpers."""

from flask import Blueprint, Flask


def test_register_plugin_blueprints_registers_exposed_blueprint(monkeypatch):
    from plugins import plugin_registry

    bp = Blueprint("example_plugin_api", __name__)

    @bp.route("/example-plugin-api/ping")
    def ping():
        return {"ok": True}

    class PluginWithBlueprint:
        def get_blueprint(self):
            return bp

    monkeypatch.setattr(
        plugin_registry,
        "PLUGIN_CLASSES",
        {"example": PluginWithBlueprint()},
    )

    app = Flask(__name__)
    plugin_registry.register_plugin_blueprints(app)

    response = app.test_client().get("/example-plugin-api/ping")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_register_plugin_blueprints_skips_plugins_without_blueprint(monkeypatch):
    from plugins import plugin_registry

    monkeypatch.setattr(plugin_registry, "PLUGIN_CLASSES", {"plain": object()})

    app = Flask(__name__)
    plugin_registry.register_plugin_blueprints(app)

    assert not app.blueprints
