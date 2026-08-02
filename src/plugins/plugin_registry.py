"""Plugin registry — discovers, loads, and instantiates plugins from the plugins directory."""

import os
import importlib
import logging
import sys
from utils.app_utils import resolve_path
from pathlib import Path

logger = logging.getLogger(__name__)
PLUGINS_DIR = 'plugins'
PLUGIN_CLASSES = {}

# Ids loaded from the user root. Tracked separately from PLUGIN_CLASSES, which
# also holds the built-ins: a reload must never be able to drop one of those.
USER_PLUGIN_IDS = set()


def plugin_template_roots(device_config):
    """Directories Jinja must search to find a plugin's settings.html.

    A plugin names its template "<id>/settings.html", relative to the root it
    lives in, so every root has to be on the search path. Built-in first, to
    match the precedence discovery and the import system already use.

    The user root is included whether or not it exists yet: FileSystemLoader
    resolves on each lookup, so the first plugin installed into a fresh system
    is found without rebuilding the loader.
    """
    return [device_config.builtin_plugins_dir, device_config.user_plugins_dir]


def _ensure_importable(plugin_dir):
    """Make ``plugins.<id>`` resolvable for a plugin outside the built-in root.

    ``plugins`` is a regular package, so a second root has to be spliced onto
    its ``__path__``; putting the directory on sys.path would not do it. The
    built-in root stays first, so a user plugin can never win the import even
    if discovery somehow let a duplicate id through.

    A plugin directory has no ``__init__.py`` and therefore resolves as a
    namespace package, which is also what makes a plugin's own relative imports
    (``from .constants import ...``) work.
    """
    import plugins

    parent = str(Path(plugin_dir).parent)
    if parent not in plugins.__path__:
        plugins.__path__.append(parent)
        logger.info(f"Added plugin root {parent}")


def load_plugins(plugins_config):
    plugins_module_path = Path(resolve_path(PLUGINS_DIR))
    for plugin in plugins_config:
        plugin_id = plugin.get('id')
        if plugin.get("disabled", False):
            logger.info(f"Plugin {plugin_id} is disabled, skipping.")
            continue

        # Config records where each plugin was found. The fallback keeps a
        # hand-built config (tests, callers predating the second root) working.
        plugin_dir = Path(plugin.get("plugin_dir") or plugins_module_path / plugin_id)
        if not plugin_dir.is_dir():
            logger.error(f"Could not find plugin directory {plugin_dir} for '{plugin_id}', skipping.")
            continue

        module_path = plugin_dir / f"{plugin_id}.py"
        if not module_path.is_file():
            logger.error(f"Could not find module path {module_path} for '{plugin_id}', skipping.")
            continue

        _ensure_importable(plugin_dir)

        module_name = f"plugins.{plugin_id}.{plugin_id}"
        try:
            module = importlib.import_module(module_name)
            plugin_class = getattr(module, plugin.get("class"), None)

            if plugin_class:
                # Create an instance of the plugin class and add it to the plugin_classes dictionary
                PLUGIN_CLASSES[plugin_id] = plugin_class(plugin)
                if plugin.get("user_installed"):
                    USER_PLUGIN_IDS.add(plugin_id)

        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_id}: {e}")


def _forget_modules(plugin_id):
    """Drop a plugin's modules so the next import reads from disk.

    Clearing beats importlib.reload() here: a plugin is a package of several
    modules (api.py, constants.py, …) and reload only re-runs the one module
    handed to it.
    """
    prefix = f"plugins.{plugin_id}"
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        del sys.modules[name]


def _exposes_blueprint(instance):
    """Whether this plugin needs a restart to be fully wired up.

    Flask refuses register_blueprint() once the app has served a request, so a
    plugin that ships one cannot be finished in-process.
    """
    get_blueprint = getattr(instance, "get_blueprint", None)
    if not callable(get_blueprint):
        return False
    try:
        return get_blueprint() is not None
    except Exception as e:
        logger.error(f"Plugin get_blueprint() raised: {e}")
        return False


def reload_user_plugins(device_config, app=None):
    """Sync the user-installed plugins to what is on disk, without a restart.

    Returns what changed and whether a restart is still owed. Everything the
    web UI reads — the plugin grid, /plugin/<id>, the loops page, the asset
    route — resolves through the config list and PLUGIN_CLASSES at request
    time, so refreshing both is what makes an install visible immediately.
    """
    # The CLI has just created directories the import system already cached as
    # absent, and may have installed dependencies into the venv.
    importlib.invalidate_caches()

    if app is not None:
        # An updated plugin keeps rendering the settings.html compiled before
        # the update otherwise: TEMPLATES_AUTO_RELOAD follows debug, which is
        # off in production, so Jinja never re-reads a template it has cached.
        app.jinja_env.cache.clear()

    device_config.reload_plugins()
    on_disk = {
        plugin["id"]: plugin
        for plugin in device_config.get_plugins()
        if plugin.get("user_installed") and plugin.get("id")
    }

    loaded, removed = [], []
    restart_reason = None

    # Only ids loaded from the user root are candidates for unloading, so a
    # built-in can never be dropped.
    for plugin_id in sorted(USER_PLUGIN_IDS - set(on_disk)):
        PLUGIN_CLASSES.pop(plugin_id, None)
        USER_PLUGIN_IDS.discard(plugin_id)
        _forget_modules(plugin_id)
        removed.append(plugin_id)
        logger.info(f"Unloaded plugin '{plugin_id}'")

    for plugin_id, plugin in sorted(on_disk.items()):
        # Re-import unconditionally: an update overwrites the same id in place,
        # and comparing revisions here would duplicate what the CLI already did.
        _forget_modules(plugin_id)
        load_plugins([plugin])
        instance = PLUGIN_CLASSES.get(plugin_id)
        if instance is None:
            USER_PLUGIN_IDS.discard(plugin_id)
            continue
        loaded.append(plugin_id)
        if restart_reason is None and _exposes_blueprint(instance):
            restart_reason = (
                f"'{plugin_id}' registers its own web routes, and Flask only "
                "accepts those while the server is starting up."
            )

    return {
        "loaded": loaded,
        "removed": removed,
        "restart_required": restart_reason is not None,
        "restart_reason": restart_reason,
    }

def get_plugin_instance(plugin_config):
    plugin_id = plugin_config.get("id")
    # Retrieve the plugin class factory function
    plugin_class = PLUGIN_CLASSES.get(plugin_id)
    
    if plugin_class:
        # Initialize the plugin with its configuration
        return plugin_class
    else:
        raise ValueError(f"Plugin '{plugin_id}' is not registered.")


def register_plugin_blueprints(app):
    """Register Flask blueprints exposed by loaded plugins.

    Plugins can opt in by implementing ``get_blueprint()`` on their plugin
    class/instance and returning a Flask Blueprint. Registration must happen
    before the app starts serving requests.
    """
    for plugin_id, plugin_instance in PLUGIN_CLASSES.items():
        try:
            get_blueprint = getattr(plugin_instance, "get_blueprint", None)
            if not get_blueprint:
                continue

            blueprint = get_blueprint()
            if blueprint:
                app.register_blueprint(blueprint)
                logger.info("Registered blueprint for plugin '%s'", plugin_id)
        except Exception as exc:
            logger.warning(
                "Failed to register blueprint for plugin '%s': %s",
                plugin_id,
                exc,
            )
