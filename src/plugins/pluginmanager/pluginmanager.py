"""Plugin Manager plugin - UI for installing third-party DashPi plugins."""

import logging
import os
import subprocess

from PIL import Image, ImageDraw

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import get_font

logger = logging.getLogger(__name__)


class PluginManager(BasePlugin):
    """UI-only plugin for managing third-party plugins from GitHub."""

    @staticmethod
    def _get_plugin_last_commit_date(plugin_id):
        """Return the local git commit timestamp for an installed plugin."""
        try:
            from config import Config

            plugin_dir = os.path.join(Config.BASE_DIR, "plugins", plugin_id)
            if not os.path.isdir(os.path.join(plugin_dir, ".git")):
                return None

            result = subprocess.run(
                ["git", "-C", plugin_dir, "log", "-1", "--format=%ci", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as exc:
            logger.debug("Could not get commit date for plugin %s: %s", plugin_id, exc)
        return None

    def generate_settings_template(self):
        """Expose installed third-party plugins to the settings template."""
        template_params = super().generate_settings_template()
        try:
            from flask import current_app

            device_config = current_app.config.get("DEVICE_CONFIG")
            plugins = device_config.get_plugins() if device_config else []
            third_party = [dict(plugin) for plugin in plugins if plugin.get("repository")]
            for plugin in third_party:
                plugin_id = plugin.get("id")
                plugin["version_date"] = (
                    self._get_plugin_last_commit_date(plugin_id) if plugin_id else None
                ) or "Unknown"
            template_params["third_party_plugins"] = third_party
        except RuntimeError:
            template_params["third_party_plugins"] = []
        return template_params

    def generate_image(self, settings, device_config):
        """Render a simple placeholder if this UI-only plugin is displayed."""
        width, height = device_config.get_resolution()
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = get_font("Jost-SemiBold", max(18, int(min(width, height) * 0.07)))
        draw.text(
            (width / 2, height / 2),
            "Plugin Manager",
            fill="black",
            font=font,
            anchor="mm",
        )
        return image
