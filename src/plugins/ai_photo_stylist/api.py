"""API routes for AI Photo Stylist uploads, cache downloads, and file lists."""

from io import BytesIO
from urllib.parse import quote
import logging
import os
import time
import zipfile

from flask import Blueprint, current_app, jsonify, request, send_file, send_from_directory

from utils.app_utils import resolve_path, sanitize_filename

logger = logging.getLogger(__name__)

ai_photo_stylist_bp = Blueprint("ai_photo_stylist_api", __name__)

UPLOAD_DIR = os.path.join("static", "images", "ai_photo_stylist", "uploads")
CACHED_DIR = os.path.join("static", "images", "ai_photo_stylist", "cached")
THUMB_DIR = os.path.join(UPLOAD_DIR, "thumbs")
SETTINGS_KEY = "plugin_last_settings_ai_photo_stylist"
IMAGE_UPLOAD_EXTENSIONS = {"pdf", "png", "avif", "jpg", "jpeg", "gif", "webp", "heif", "heic"}


def _is_path_in_dir(file_path, directory):
    """Return True when file_path resolves inside directory."""
    try:
        abs_path = os.path.abspath(file_path)
        abs_dir = os.path.abspath(directory)
        return os.path.commonpath([abs_path, abs_dir]) == abs_dir
    except (OSError, ValueError):
        return False


def _upload_dir():
    upload_dir = resolve_path(UPLOAD_DIR)
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _cached_dir():
    cached_dir = resolve_path(CACHED_DIR)
    os.makedirs(cached_dir, exist_ok=True)
    return cached_dir


def _thumb_dir():
    thumb_dir = resolve_path(THUMB_DIR)
    os.makedirs(thumb_dir, exist_ok=True)
    return thumb_dir


def _static_url_for_path(file_path):
    try:
        static_dir = os.path.abspath(resolve_path("static"))
        abs_path = os.path.abspath(file_path)
        rel_path = os.path.relpath(abs_path, static_dir)
        if rel_path.startswith(".."):
            return ""
        return "/static/" + "/".join(quote(part) for part in rel_path.split(os.sep))
    except (OSError, ValueError):
        return ""


def _thumb_path_for(file_name):
    stem = os.path.splitext(file_name)[0]
    thumb_name = f"{sanitize_filename(stem) or 'photo'}.jpg"
    return os.path.join(_thumb_dir(), thumb_name)


def _is_allowed_image_file(file_path):
    extension = os.path.splitext(file_path)[1].replace(".", "").lower()
    return extension in IMAGE_UPLOAD_EXTENSIONS


@ai_photo_stylist_bp.route("/plugin/ai_photo_stylist/upload_image", methods=["POST"])
def upload_image():
    """Upload a photo into AI Photo Stylist's private source image directory."""
    file_path = ""
    try:
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "No file provided"}), 400

        extension = os.path.splitext(file.filename)[1].replace(".", "").lower()
        if extension not in IMAGE_UPLOAD_EXTENSIONS:
            return jsonify({"error": f"File type .{extension} not allowed"}), 400

        file_name = sanitize_filename(file.filename)
        stem, ext = os.path.splitext(file_name)
        upload_dir = _upload_dir()
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

        if extension in {"jpg", "jpeg"}:
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
            except Exception as exc:
                logger.warning("AI Photo Stylist EXIF processing error for %s: %s", file_name, exc)

        logger.info(
            "AI Photo Stylist uploaded image: %s (%s bytes)",
            file_name,
            os.path.getsize(file_path),
        )
        thumbnail_url = _static_url_for_path(file_path)
        thumbnail = request.files.get("thumbnail")
        if thumbnail and thumbnail.filename:
            thumb_path = _thumb_path_for(file_name)
            thumbnail.save(thumb_path)
            try:
                from PIL import Image

                with Image.open(thumb_path) as img:
                    img.verify()
            except Exception:
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                if os.path.exists(file_path):
                    os.remove(file_path)
                return jsonify({"error": "Thumbnail is not a valid image"}), 400
            thumbnail_url = _static_url_for_path(thumb_path)

        return jsonify({
            "success": True,
            "file_path": file_path,
            "file_name": file_name,
            "thumbnail_url": thumbnail_url,
        }), 200

    except Exception:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        logger.exception("Error uploading AI Photo Stylist image")
        return jsonify({"error": "Upload failed"}), 500


@ai_photo_stylist_bp.route("/plugin/ai_photo_stylist/check_files", methods=["POST"])
def check_files():
    """Check existence of files under AI Photo Stylist's private upload directory."""
    try:
        data = request.get_json() or {}
        file_paths = data.get("file_paths", [])
        upload_dir = _upload_dir()

        result = {}
        for file_path in file_paths:
            if _is_path_in_dir(file_path, upload_dir):
                result[file_path] = os.path.isfile(os.path.abspath(file_path))
            else:
                result[file_path] = False

        return jsonify(result), 200
    except Exception:
        logger.exception("Error checking AI Photo Stylist files")
        return jsonify({"error": "Failed to check files"}), 500


@ai_photo_stylist_bp.route("/plugin/ai_photo_stylist/delete_image", methods=["POST"])
def delete_image():
    """Delete one AI Photo Stylist upload/cache file and update saved settings when needed."""
    device_config = current_app.config["DEVICE_CONFIG"]
    try:
        data = request.get_json() or {}
        file_path = data.get("file_path", "")
        if not file_path:
            return jsonify({"error": "No file path provided"}), 400

        upload_dir = _upload_dir()
        cached_dir = _cached_dir()
        is_upload = _is_path_in_dir(file_path, upload_dir)
        is_cached = _is_path_in_dir(file_path, cached_dir)
        if not is_upload and not is_cached:
            return jsonify({"error": "Invalid file path"}), 403

        abs_path = os.path.abspath(file_path)
        if os.path.exists(abs_path) and not os.path.isfile(abs_path):
            return jsonify({"error": "Invalid file path"}), 400
        if os.path.isfile(abs_path):
            os.remove(abs_path)
            logger.info("Deleted AI Photo Stylist image: %s", os.path.basename(abs_path))
        if is_upload:
            thumb_path = _thumb_path_for(os.path.basename(abs_path))
            if os.path.isfile(thumb_path):
                os.remove(thumb_path)
                logger.info("Deleted AI Photo Stylist thumbnail: %s", os.path.basename(thumb_path))

        if is_upload:
            settings = device_config.get_config(SETTINGS_KEY, default={})
            file_list = settings.get("imageFiles[]", [])
            if file_path in file_list:
                file_list.remove(file_path)
                settings["imageFiles[]"] = file_list
                if settings.get("sourceImagePath") == file_path:
                    settings["sourceImagePath"] = file_list[0] if file_list else ""
                device_config.update_value(SETTINGS_KEY, settings, write=True)

        return jsonify({"success": True}), 200

    except Exception as exc:
        logger.exception("Error deleting AI Photo Stylist image")
        return jsonify({"error": f"Delete failed: {str(exc)}"}), 500


@ai_photo_stylist_bp.route("/plugin/ai_photo_stylist/download_cached")
def download_cached():
    """Download all AI Photo Stylist cached images as a zip archive."""
    try:
        cached_dir = _cached_dir()
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
    except Exception:
        logger.exception("Error downloading AI Photo Stylist cache")
        return jsonify({"error": "Download failed"}), 500


@ai_photo_stylist_bp.route("/plugin/ai_photo_stylist/download_cached/<path:filename>")
def download_cached_file(filename):
    """Download one AI Photo Stylist cached image by filename."""
    try:
        file_name = sanitize_filename(os.path.basename(filename))
        if not file_name or file_name != filename or not _is_allowed_image_file(file_name):
            return jsonify({"error": "Invalid cached image filename"}), 400

        cached_dir = _cached_dir()
        file_path = os.path.join(cached_dir, file_name)
        if not os.path.isfile(file_path):
            return jsonify({"error": "Cached image not found"}), 404

        return send_from_directory(
            cached_dir,
            file_name,
            as_attachment=True,
            download_name=file_name,
        )
    except Exception:
        logger.exception("Error downloading AI Photo Stylist cached image")
        return jsonify({"error": "Download failed"}), 500


@ai_photo_stylist_bp.route("/plugin/ai_photo_stylist/save_image_list", methods=["POST"])
def save_image_list():
    """Persist the AI Photo Stylist upload list to plugin_last_settings."""
    device_config = current_app.config["DEVICE_CONFIG"]
    try:
        data = request.get_json() or {}
        file_paths = data.get("file_paths", [])
        if not isinstance(file_paths, list):
            return jsonify({"error": "Invalid file_paths"}), 400

        upload_dir = _upload_dir()
        safe_paths = [path for path in file_paths if _is_path_in_dir(path, upload_dir)]
        settings = device_config.get_config(SETTINGS_KEY, default={})
        settings["imageFiles[]"] = safe_paths
        if settings.get("sourceImagePath") not in safe_paths:
            settings["sourceImagePath"] = safe_paths[0] if safe_paths else ""
        device_config.update_value(SETTINGS_KEY, settings, write=True)
        return jsonify({"success": True}), 200
    except Exception as exc:
        logger.exception("Error saving AI Photo Stylist image list")
        return jsonify({"error": str(exc)}), 500
