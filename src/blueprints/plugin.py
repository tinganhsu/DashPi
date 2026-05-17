"""Plugin blueprint — plugin settings pages, image upload/delete, and update endpoints."""

from flask import Blueprint, request, jsonify, current_app, render_template, send_from_directory, send_file
from plugins.plugin_registry import get_plugin_instance
from utils.app_utils import resolve_path, handle_request_files, parse_form, sanitize_filename
from refresh_task import ManualRefresh
from io import BytesIO
import json
import os
import logging
import time
import zipfile

logger = logging.getLogger(__name__)
plugin_bp = Blueprint("plugin", __name__)

AI_PHOTO_STYLIST_UPLOAD_DIR = os.path.join("static", "images", "ai_photo_stylist", "uploads")
AI_PHOTO_STYLIST_CACHED_DIR = os.path.join("static", "images", "ai_photo_stylist", "cached")
AI_PHOTO_STYLIST_SETTINGS_KEY = "plugin_last_settings_ai_photo_stylist"
IMAGE_UPLOAD_EXTENSIONS = {'pdf', 'png', 'avif', 'jpg', 'jpeg', 'gif', 'webp', 'heif', 'heic'}


def _is_path_in_dir(file_path, directory):
    """Return True when file_path resolves inside directory."""
    try:
        abs_path = os.path.abspath(file_path)
        abs_dir = os.path.abspath(directory)
        return os.path.commonpath([abs_path, abs_dir]) == abs_dir
    except (OSError, ValueError):
        return False


def _ai_photo_stylist_upload_dir():
    upload_dir = resolve_path(AI_PHOTO_STYLIST_UPLOAD_DIR)
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _ai_photo_stylist_cached_dir():
    cached_dir = resolve_path(AI_PHOTO_STYLIST_CACHED_DIR)
    os.makedirs(cached_dir, exist_ok=True)
    return cached_dir


def _is_allowed_image_file(file_path):
    extension = os.path.splitext(file_path)[1].replace('.', '').lower()
    return extension in IMAGE_UPLOAD_EXTENSIONS

@plugin_bp.route('/plugin/<plugin_id>')
def plugin_page(plugin_id):
    """Render plugin settings page. Restores last-used or loop settings."""
    device_config = current_app.config['DEVICE_CONFIG']
    loop_manager = device_config.get_loop_manager()

    # Check for loop edit/add mode (coming from loops page)
    loop_name = request.args.get('loop_name', '')
    edit_mode = request.args.get('edit_mode', 'false') == 'true'
    add_mode = request.args.get('add_mode', 'false') == 'true'

    # If editing a loop plugin, get existing settings
    existing_settings = {}
    existing_refresh_interval = None
    if edit_mode and loop_name:
        loop = loop_manager.get_loop(loop_name)
        if loop:
            plugin_ref = next((ref for ref in loop.plugin_order if ref.plugin_id == plugin_id), None)
            if plugin_ref:
                existing_settings = plugin_ref.plugin_settings or {}
                existing_refresh_interval = plugin_ref.refresh_interval_seconds

    # Find the plugin by id
    plugin_config = device_config.get_plugin(plugin_id)
    if plugin_config:
        try:
            plugin = get_plugin_instance(plugin_config)
            template_params = plugin.generate_settings_template()

            template_params["loops"] = loop_manager.get_loop_names()
            template_params["loop_edit_mode"] = edit_mode or add_mode
            template_params["loop_add_mode"] = add_mode
            template_params["loop_name"] = loop_name
            template_params["loop_refresh_interval"] = existing_refresh_interval

            # If in edit mode, merge loop settings with last-used settings.
            # Last-used settings provide operational data (file lists, etc.);
            # loop settings override with the user's saved preferences.
            if edit_mode:
                last_used = device_config.get_config(
                    f"plugin_last_settings_{plugin_id}", default={}
                )
                if last_used or existing_settings:
                    merged = dict(last_used) if last_used else {}
                    merged.update(existing_settings or {})
                    template_params["plugin_settings"] = merged
            elif not edit_mode:
                # Try to inherit settings so users don't re-enter preferences.
                # Priority: 1) last-used settings, 2) existing loop instance
                last_used = device_config.get_config(
                    f"plugin_last_settings_{plugin_id}", default=None
                )
                if last_used:
                    template_params["plugin_settings"] = last_used
                else:
                    for loop in loop_manager.loops:
                        for ref in loop.plugin_order:
                            if ref.plugin_id == plugin_id and ref.plugin_settings:
                                template_params["plugin_settings"] = dict(ref.plugin_settings)
                                break
                        if "plugin_settings" in template_params:
                            break
        except Exception as e:
            logger.exception("EXCEPTION CAUGHT: " + str(e))
            return jsonify({"error": f"An error occurred: {str(e)}"}), 500
        return render_template('plugin.html', plugin=plugin_config, **template_params)
    else:
        return "Plugin not found", 404

@plugin_bp.route('/images/<plugin_id>/<path:filename>')
def image(plugin_id, filename):
    """Serve static files from a plugin's directory (icons, images, etc.)."""
    # Resolve plugins directory dynamically
    plugins_dir = resolve_path("plugins")

    # Construct the full path to the plugin's file
    plugin_dir = os.path.join(plugins_dir, plugin_id)

    # Security check to prevent directory traversal
    safe_path = os.path.abspath(os.path.join(plugin_dir, filename))
    if not safe_path.startswith(os.path.abspath(plugins_dir)):
        return "Invalid path", 403

    # Convert to absolute path for send_from_directory
    abs_plugin_dir = os.path.abspath(plugin_dir)

    # Check if the directory and file exist
    if not os.path.isdir(abs_plugin_dir):
        logger.error(f"Plugin directory not found: {abs_plugin_dir}")
        return "Plugin directory not found", 404

    if not os.path.isfile(safe_path):
        logger.error(f"File not found: {safe_path}")
        return "File not found", 404

    # Serve the file from the plugin directory
    return send_from_directory(abs_plugin_dir, filename)

@plugin_bp.route('/upload_image', methods=['POST'])
def upload_image():
    """Upload a single image file to disk. Returns the saved file path.
    Used for immediate per-file uploads with progress feedback."""
    try:
        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({"error": "No file provided"}), 400

        allowed_extensions = {'pdf', 'png', 'avif', 'jpg', 'jpeg', 'gif', 'webp', 'heif', 'heic'}
        extension = os.path.splitext(file.filename)[1].replace('.', '').lower()
        if extension not in allowed_extensions:
            return jsonify({"error": f"File type .{extension} not allowed"}), 400

        file_name = sanitize_filename(file.filename)
        file_save_dir = resolve_path(os.path.join("static", "images", "saved"))
        os.makedirs(file_save_dir, exist_ok=True)
        file_path = os.path.join(file_save_dir, file_name)

        # Save raw bytes to disk (no PIL processing to avoid OOM)
        file.save(file_path)

        # Validate file content matches an image type (magic bytes check)
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                img.verify()
        except Exception:
            os.remove(file_path)
            return jsonify({"error": "File is not a valid image"}), 400

        # Fix EXIF orientation for JPEGs (skip very large images)
        if extension in {'jpg', 'jpeg'}:
            try:
                from PIL import Image, ImageOps
                with Image.open(file_path) as img:
                    w, h = img.size
                    megapixels = (w * h) / 1_000_000
                    if megapixels <= 50:
                        transposed = ImageOps.exif_transpose(img)
                        if transposed is not img:
                            transposed.save(file_path)
                            transposed.close()
                import gc; gc.collect()
            except Exception as e:
                logger.warning(f"EXIF processing error for {file_name}: {e}")

        logger.info(f"Uploaded image: {file_name} ({os.path.getsize(file_path)} bytes)")
        return jsonify({"success": True, "file_path": file_path, "file_name": file_name}), 200

    except Exception as e:
        logger.exception(f"Error uploading image: {str(e)}")
        return jsonify({"error": "Upload failed"}), 500


@plugin_bp.route('/check_files', methods=['POST'])
def check_files():
    """Check which files from a list exist on disk. Only allows paths under the saved images directory."""
    try:
        data = request.get_json()
        file_paths = data.get('file_paths', [])

        # Security: only allow checking files in the saved images directory
        saved_dir = os.path.abspath(resolve_path(os.path.join("static", "images", "saved")))
        result = {}
        for fp in file_paths:
            abs_path = os.path.abspath(fp)
            if abs_path.startswith(saved_dir):
                result[fp] = os.path.isfile(abs_path)
            else:
                result[fp] = False

        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": "Failed to check files"}), 500


@plugin_bp.route('/delete_image', methods=['POST'])
def delete_image():
    """Delete a single uploaded image file from disk and update saved settings."""
    device_config = current_app.config['DEVICE_CONFIG']
    try:
        data = request.get_json()
        file_path = data.get('file_path', '')
        if not file_path:
            return jsonify({"error": "No file path provided"}), 400

        # Security: only allow deleting from the saved images directory
        saved_dir = os.path.abspath(resolve_path(os.path.join("static", "images", "saved")))
        abs_path = os.path.abspath(file_path)
        if not abs_path.startswith(saved_dir):
            return jsonify({"error": "Invalid file path"}), 403

        if os.path.exists(abs_path):
            os.remove(abs_path)
            logger.info(f"Deleted image: {os.path.basename(abs_path)}")

        # Remove from saved settings so UI stays in sync
        for key in ["plugin_last_settings_image_upload", "auto_refresh_tracking"]:
            settings = device_config.get_config(key, default={})
            if key == "auto_refresh_tracking":
                settings = settings.get("plugin_settings", {})
            file_list = settings.get("imageFiles[]", [])
            if file_path in file_list:
                file_list.remove(file_path)

        device_config.write_config()

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.exception(f"Error deleting image: {str(e)}")
        return jsonify({"error": f"Delete failed: {str(e)}"}), 500


@plugin_bp.route('/save_image_list', methods=['POST'])
def save_image_list():
    """Auto-save the current image file list to plugin_last_settings after upload/removal."""
    device_config = current_app.config['DEVICE_CONFIG']
    try:
        data = request.get_json()
        file_paths = data.get('file_paths', [])
        if not isinstance(file_paths, list):
            return jsonify({"error": "Invalid file_paths"}), 400

        settings = device_config.get_config(
            "plugin_last_settings_image_upload", default={}
        )
        settings["imageFiles[]"] = file_paths
        device_config.update_value(
            "plugin_last_settings_image_upload", settings, write=True
        )
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.exception(f"Error saving image list: {str(e)}")
        return jsonify({"error": str(e)}), 500


@plugin_bp.route('/plugin/ai_photo_stylist/upload_image', methods=['POST'])
def ai_photo_stylist_upload_image():
    """Upload a photo into AI Photo Stylist's private source image directory."""
    try:
        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({"error": "No file provided"}), 400

        extension = os.path.splitext(file.filename)[1].replace('.', '').lower()
        if extension not in IMAGE_UPLOAD_EXTENSIONS:
            return jsonify({"error": f"File type .{extension} not allowed"}), 400

        file_name = sanitize_filename(file.filename)
        stem, ext = os.path.splitext(file_name)
        upload_dir = _ai_photo_stylist_upload_dir()
        file_path = os.path.join(upload_dir, file_name)
        if os.path.exists(file_path):
            file_path = os.path.join(upload_dir, f"{stem}_{int(time.time())}{ext}")
            file_name = os.path.basename(file_path)

        file.save(file_path)

        try:
            from PIL import Image
            with Image.open(file_path) as img:
                img.verify()
        except Exception:
            os.remove(file_path)
            return jsonify({"error": "File is not a valid image"}), 400

        if extension in {'jpg', 'jpeg'}:
            try:
                from PIL import Image, ImageOps
                with Image.open(file_path) as img:
                    w, h = img.size
                    megapixels = (w * h) / 1_000_000
                    if megapixels <= 50:
                        transposed = ImageOps.exif_transpose(img)
                        if transposed is not img:
                            transposed.save(file_path)
                            transposed.close()
            except Exception as e:
                logger.warning(f"AI Photo Stylist EXIF processing error for {file_name}: {e}")

        logger.info(f"AI Photo Stylist uploaded image: {file_name} ({os.path.getsize(file_path)} bytes)")
        return jsonify({"success": True, "file_path": file_path, "file_name": file_name}), 200

    except Exception as e:
        logger.exception(f"Error uploading AI Photo Stylist image: {str(e)}")
        return jsonify({"error": "Upload failed"}), 500


@plugin_bp.route('/plugin/ai_photo_stylist/check_files', methods=['POST'])
def ai_photo_stylist_check_files():
    """Check existence of files under AI Photo Stylist's private upload directory."""
    try:
        data = request.get_json() or {}
        file_paths = data.get('file_paths', [])
        upload_dir = _ai_photo_stylist_upload_dir()

        result = {}
        for file_path in file_paths:
            if _is_path_in_dir(file_path, upload_dir):
                result[file_path] = os.path.isfile(os.path.abspath(file_path))
            else:
                result[file_path] = False

        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"Error checking AI Photo Stylist files: {str(e)}")
        return jsonify({"error": "Failed to check files"}), 500


@plugin_bp.route('/plugin/ai_photo_stylist/delete_image', methods=['POST'])
def ai_photo_stylist_delete_image():
    """Delete one AI Photo Stylist upload/cache file and update saved settings when needed."""
    device_config = current_app.config['DEVICE_CONFIG']
    try:
        data = request.get_json() or {}
        file_path = data.get('file_path', '')
        if not file_path:
            return jsonify({"error": "No file path provided"}), 400

        upload_dir = _ai_photo_stylist_upload_dir()
        cached_dir = _ai_photo_stylist_cached_dir()
        is_upload = _is_path_in_dir(file_path, upload_dir)
        is_cached = _is_path_in_dir(file_path, cached_dir)
        if not is_upload and not is_cached:
            return jsonify({"error": "Invalid file path"}), 403

        abs_path = os.path.abspath(file_path)
        if os.path.exists(abs_path) and not os.path.isfile(abs_path):
            return jsonify({"error": "Invalid file path"}), 400
        if os.path.isfile(abs_path):
            os.remove(abs_path)
            logger.info(f"Deleted AI Photo Stylist image: {os.path.basename(abs_path)}")

        if is_upload:
            settings = device_config.get_config(AI_PHOTO_STYLIST_SETTINGS_KEY, default={})
            file_list = settings.get("imageFiles[]", [])
            if file_path in file_list:
                file_list.remove(file_path)
                settings["imageFiles[]"] = file_list
                if settings.get("sourceImagePath") == file_path:
                    settings["sourceImagePath"] = file_list[0] if file_list else ""
                device_config.update_value(AI_PHOTO_STYLIST_SETTINGS_KEY, settings, write=True)

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.exception(f"Error deleting AI Photo Stylist image: {str(e)}")
        return jsonify({"error": f"Delete failed: {str(e)}"}), 500


@plugin_bp.route('/plugin/ai_photo_stylist/download_cached')
def ai_photo_stylist_download_cached():
    """Download all AI Photo Stylist cached images as a zip archive."""
    try:
        cached_dir = _ai_photo_stylist_cached_dir()
        image_paths = [
            os.path.join(cached_dir, filename)
            for filename in sorted(os.listdir(cached_dir))
            if not filename.startswith(".")
            and os.path.isfile(os.path.join(cached_dir, filename))
            and _is_allowed_image_file(filename)
        ]

        if not image_paths:
            return jsonify({"error": "No cached AI Photo Stylist images found"}), 404

        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for image_path in image_paths:
                zf.write(image_path, arcname=os.path.basename(image_path))
        archive.seek(0)

        return send_file(
            archive,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"ai_photo_stylist_cached_{int(time.time())}.zip",
        )
    except Exception as e:
        logger.exception(f"Error downloading AI Photo Stylist cache: {str(e)}")
        return jsonify({"error": "Download failed"}), 500


@plugin_bp.route('/plugin/ai_photo_stylist/download_cached/<path:filename>')
def ai_photo_stylist_download_cached_file(filename):
    """Download one AI Photo Stylist cached image by filename."""
    try:
        file_name = sanitize_filename(os.path.basename(filename))
        if not file_name or file_name != filename or not _is_allowed_image_file(file_name):
            return jsonify({"error": "Invalid cached image filename"}), 400

        cached_dir = _ai_photo_stylist_cached_dir()
        file_path = os.path.join(cached_dir, file_name)
        if not os.path.isfile(file_path):
            return jsonify({"error": "Cached image not found"}), 404

        return send_from_directory(
            cached_dir,
            file_name,
            as_attachment=True,
            download_name=file_name,
        )
    except Exception as e:
        logger.exception(f"Error downloading AI Photo Stylist cached image: {str(e)}")
        return jsonify({"error": "Download failed"}), 500


@plugin_bp.route('/plugin/ai_photo_stylist/save_image_list', methods=['POST'])
def ai_photo_stylist_save_image_list():
    """Persist the AI Photo Stylist upload list to plugin_last_settings."""
    device_config = current_app.config['DEVICE_CONFIG']
    try:
        data = request.get_json() or {}
        file_paths = data.get('file_paths', [])
        if not isinstance(file_paths, list):
            return jsonify({"error": "Invalid file_paths"}), 400

        upload_dir = _ai_photo_stylist_upload_dir()
        safe_paths = [path for path in file_paths if _is_path_in_dir(path, upload_dir)]
        settings = device_config.get_config(AI_PHOTO_STYLIST_SETTINGS_KEY, default={})
        settings["imageFiles[]"] = safe_paths
        if settings.get("sourceImagePath") not in safe_paths:
            settings["sourceImagePath"] = safe_paths[0] if safe_paths else ""
        device_config.update_value(AI_PHOTO_STYLIST_SETTINGS_KEY, settings, write=True)
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.exception(f"Error saving AI Photo Stylist image list: {str(e)}")
        return jsonify({"error": str(e)}), 500


@plugin_bp.route('/update_now_async', methods=['POST'])
def update_now_async():
    """Non-blocking update endpoint. Queues the update and returns immediately.
    Use status polling (e.g. status.json) to track progress."""
    device_config = current_app.config['DEVICE_CONFIG']
    refresh_task = current_app.config['REFRESH_TASK']

    try:
        plugin_settings = parse_form(request.form)

        # Show upload progress in live status
        file_list = request.files.getlist('imageFiles[]')
        new_files = [f for f in file_list if f.filename]
        if new_files:
            refresh_task._set_global_status("uploading", f"Saving {len(new_files)} image(s) to disk...")

        plugin_settings.update(handle_request_files(request.files))
        plugin_id = plugin_settings.pop("plugin_id")

        # Remember settings for next time the plugin page is opened
        # Don't overwrite with empty dict (e.g., from curl with just plugin_id)
        if plugin_settings:
            device_config.update_value(
                f"plugin_last_settings_{plugin_id}", dict(plugin_settings)
            )

        if refresh_task.running:
            queued = refresh_task.queue_manual_update(ManualRefresh(plugin_id, plugin_settings))
            if queued:
                return jsonify({"success": True, "message": "Update queued"}), 202
            else:
                return jsonify({"error": "Refresh task not running"}), 500
        else:
            return jsonify({"error": "Refresh task not running"}), 500

    except Exception as e:
        logger.exception(f"Error in update_now_async: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

@plugin_bp.route('/update_now', methods=['POST'])
def update_now():
    """Blocking update endpoint — generates image and waits for display."""
    device_config = current_app.config['DEVICE_CONFIG']
    refresh_task = current_app.config['REFRESH_TASK']
    display_manager = current_app.config['DISPLAY_MANAGER']

    try:
        plugin_settings = parse_form(request.form)
        plugin_settings.update(handle_request_files(request.files))
        plugin_id = plugin_settings.pop("plugin_id")

        # Remember settings for next time the plugin page is opened
        # Don't overwrite with empty dict (e.g., from curl with just plugin_id)
        if plugin_settings:
            device_config.update_value(
                f"plugin_last_settings_{plugin_id}", dict(plugin_settings)
            )

        # For stocks plugin, merge in saved settings (autoRefresh, etc.) if not in form
        if plugin_id == "stocks":
            saved_settings = device_config.get_config("stocks_plugin_settings", default={})
            for key, value in saved_settings.items():
                if key not in plugin_settings or not plugin_settings.get(key):
                    plugin_settings[key] = value
            logger.debug(f"Stocks update_now with merged settings: {plugin_settings}")

        # Check if refresh task is running
        if refresh_task.running:
            refresh_task.manual_update(ManualRefresh(plugin_id, plugin_settings))
        else:
            # In development mode, directly update the display
            logger.info("Refresh task not running, updating display directly")
            plugin_config = device_config.get_plugin(plugin_id)
            if not plugin_config:
                return jsonify({"error": f"Plugin '{plugin_id}' not found"}), 404

            plugin = get_plugin_instance(plugin_config)
            image = plugin.generate_image(plugin_settings, device_config)
            display_manager.display_image(image, image_settings=plugin_config.get("image_settings", []))

    except Exception as e:
        logger.exception(f"Error in update_now: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    return jsonify({"success": True, "message": "Display updated"}), 200


# Stocks plugin - saved settings and tickers management
@plugin_bp.route('/plugin/stocks/settings', methods=['GET'])
def get_stocks_settings():
    """Get saved stocks plugin settings (autoRefresh, etc.)."""
    device_config = current_app.config['DEVICE_CONFIG']
    settings = device_config.get_config("stocks_plugin_settings", default={})
    return jsonify({"settings": settings})


@plugin_bp.route('/plugin/stocks/settings', methods=['POST'])
def save_stocks_settings():
    """Save stocks plugin settings."""
    device_config = current_app.config['DEVICE_CONFIG']
    data = request.get_json()

    if not data or "settings" not in data:
        return jsonify({"error": "No settings provided"}), 400

    settings = data["settings"]
    device_config.update_value("stocks_plugin_settings", settings, write=True)
    return jsonify({"success": True, "settings": settings})


@plugin_bp.route('/plugin/stocks/tickers', methods=['GET'])
def get_saved_tickers():
    """Get the list of saved stock tickers."""
    device_config = current_app.config['DEVICE_CONFIG']
    saved_tickers = device_config.get_config("stocks_saved_tickers", default=[])
    return jsonify({"tickers": saved_tickers})


@plugin_bp.route('/plugin/stocks/tickers', methods=['POST'])
def save_tickers():
    """Save the list of stock tickers (used for reordering)."""
    device_config = current_app.config['DEVICE_CONFIG']
    data = request.get_json()

    if not data or "tickers" not in data:
        return jsonify({"error": "No tickers provided"}), 400

    new_order = data["tickers"]
    if not isinstance(new_order, list):
        return jsonify({"error": "Tickers must be a list"}), 400

    # Get current tickers to preserve name data
    current_tickers = device_config.get_config("stocks_saved_tickers", default=[])

    # Build lookup of current ticker data
    ticker_data = {}
    for t in current_tickers:
        if isinstance(t, dict):
            ticker_data[t["symbol"]] = t
        else:
            ticker_data[t] = {"symbol": t, "name": t}

    # Reorder based on new_order, preserving ticker data
    reordered = []
    for symbol in new_order[:6]:
        symbol_upper = symbol.strip().upper() if isinstance(symbol, str) else symbol
        if symbol_upper in ticker_data:
            reordered.append(ticker_data[symbol_upper])

    device_config.update_value("stocks_saved_tickers", reordered, write=True)
    return jsonify({"success": True, "tickers": reordered})


@plugin_bp.route('/plugin/stocks/tickers/<ticker>', methods=['DELETE'])
def remove_ticker(ticker):
    """Remove a single ticker from the saved list."""
    device_config = current_app.config['DEVICE_CONFIG']
    saved_tickers = device_config.get_config("stocks_saved_tickers", default=[])

    ticker_upper = ticker.upper()
    # Handle both old format (string) and new format (dict)
    new_list = []
    found = False
    for t in saved_tickers:
        symbol = t["symbol"] if isinstance(t, dict) else t
        if symbol == ticker_upper:
            found = True
        else:
            new_list.append(t)

    if found:
        device_config.update_value("stocks_saved_tickers", new_list, write=True)
        return jsonify({"success": True, "tickers": new_list})

    return jsonify({"error": "Ticker not found"}), 404


@plugin_bp.route('/plugin/stocks/tickers/add', methods=['POST'])
def add_ticker():
    """Add a ticker to the saved list after validating it."""
    device_config = current_app.config['DEVICE_CONFIG']
    data = request.get_json()

    if not data or "ticker" not in data:
        return jsonify({"error": "No ticker provided"}), 400

    ticker = data["ticker"].strip().upper()
    if not ticker:
        return jsonify({"error": "Invalid ticker"}), 400

    saved_tickers = device_config.get_config("stocks_saved_tickers", default=[])

    # Check if ticker already exists (compare symbols only)
    existing_symbols = [t["symbol"] if isinstance(t, dict) else t for t in saved_tickers]
    if ticker in existing_symbols:
        return jsonify({"error": "Ticker already exists", "tickers": saved_tickers}), 400

    if len(saved_tickers) >= 6:
        return jsonify({"error": "Maximum 6 tickers allowed", "tickers": saved_tickers}), 400

    # Validate ticker using yfinance (with timeout to prevent Flask thread hang)
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
        stock = yf.Ticker(ticker)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: stock.info)
            info = future.result(timeout=15)

        # Check if we got valid data
        name = info.get("shortName") or info.get("longName")
        if not name or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            return jsonify({"error": f"'{ticker}' is not a valid ticker symbol"}), 400

        # Store as object with symbol and name
        ticker_obj = {"symbol": ticker, "name": name}
        saved_tickers.append(ticker_obj)
        device_config.update_value("stocks_saved_tickers", saved_tickers, write=True)
        return jsonify({"success": True, "tickers": saved_tickers, "added": ticker_obj})

    except Exception as e:
        logger.error(f"Error validating ticker {ticker}: {str(e)}")
        return jsonify({"error": f"Could not validate ticker '{ticker}'"}), 400
