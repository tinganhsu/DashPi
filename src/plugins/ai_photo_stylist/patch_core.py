"""Core patch helpers for plugin blueprint support.

This fallback is intended for installing AI Photo Stylist on older DashPi or
InkyPi builds that do not yet expose plugin-owned Flask blueprints.
"""

from pathlib import Path
import os
import re


REGISTRY_FUNCTION = '''

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
'''


def _project_dir():
    """Return project root, the parent directory of src/."""
    env_project_dir = os.environ.get("PROJECT_DIR")
    if env_project_dir:
        return Path(env_project_dir).resolve()

    try:
        from config import Config

        return Path(Config.BASE_DIR).resolve().parent
    except ImportError:
        return Path(__file__).resolve().parents[3]


def _entrypoint_path(project_dir):
    for filename in ("dashpi.py", "inkypi.py"):
        candidate = project_dir / "src" / filename
        if candidate.is_file():
            return candidate
    return project_dir / "src" / "dashpi.py"


def check_core_patched():
    """Return (is_patched, missing_parts) for plugin blueprint support."""
    project_dir = _project_dir()
    missing = []

    registry_path = project_dir / "src" / "plugins" / "plugin_registry.py"
    if registry_path.is_file():
        registry_content = registry_path.read_text(encoding="utf-8")
        if "def register_plugin_blueprints(app):" not in registry_content:
            missing.append("plugin_registry.py: missing register_plugin_blueprints()")
    else:
        missing.append("plugin_registry.py: file not found")

    entrypoint = _entrypoint_path(project_dir)
    if entrypoint.is_file():
        entrypoint_content = entrypoint.read_text(encoding="utf-8")
        if "register_plugin_blueprints(app)" not in entrypoint_content:
            missing.append(f"{entrypoint.name}: missing register_plugin_blueprints(app)")
    else:
        missing.append("app entrypoint not found")

    return not missing, missing


def patch_core_files():
    """Patch core files to support plugin-owned Flask blueprints."""
    project_dir = _project_dir()
    registry_path = project_dir / "src" / "plugins" / "plugin_registry.py"
    entrypoint = _entrypoint_path(project_dir)

    if not registry_path.is_file():
        return False, f"File not found: {registry_path}"
    if not entrypoint.is_file():
        return False, f"File not found: {entrypoint}"

    try:
        _patch_registry(registry_path)
        _patch_entrypoint(entrypoint)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _patch_registry(registry_path):
    content = registry_path.read_text(encoding="utf-8")
    if "def register_plugin_blueprints(app):" in content:
        return

    if not content.endswith("\n"):
        content += "\n"
    registry_path.write_text(content + REGISTRY_FUNCTION, encoding="utf-8")


def _patch_entrypoint(entrypoint):
    content = entrypoint.read_text(encoding="utf-8")
    if "register_plugin_blueprints" not in content:
        content = _add_registry_import(content)

    if "register_plugin_blueprints(app)" not in content:
        content = _add_registration_call(content)

    entrypoint.write_text(content, encoding="utf-8")


def _add_registry_import(content):
    pattern = re.compile(r"^from plugins\.plugin_registry import ([^\n]+)$", re.MULTILINE)
    match = pattern.search(content)
    if match:
        names = [name.strip() for name in match.group(1).split(",")]
        if "register_plugin_blueprints" not in names:
            names.append("register_plugin_blueprints")
        import_line = "from plugins.plugin_registry import " + ", ".join(names)
        return content[:match.start()] + import_line + content[match.end():]

    marker = "from waitress import serve"
    import_line = "from plugins.plugin_registry import register_plugin_blueprints\n"
    if marker in content:
        return content.replace(marker, import_line + marker, 1)
    return import_line + content


def _add_registration_call(content):
    lines = content.splitlines()
    insert_at = None
    for index, line in enumerate(lines):
        if line.strip().startswith("app.register_blueprint("):
            insert_at = index + 1

    if insert_at is None:
        marker_index = next(
            (i for i, line in enumerate(lines) if "# Register Blueprints" in line),
            None,
        )
        insert_at = marker_index + 1 if marker_index is not None else len(lines)

    lines.insert(insert_at, "register_plugin_blueprints(app)")
    return "\n".join(lines) + "\n"
