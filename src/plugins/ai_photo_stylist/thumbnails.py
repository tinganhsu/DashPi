"""Small JPEG previews for the AI Photo Stylist settings page.

The page used to show full-size originals as its tiles: multi-megabyte phone
photos, and AI results saved as full-resolution PNGs. Previews are made here
on the server, so every format PIL can read gets one (HEIC included), not
only uploads whose browser happened to send a canvas-made thumbnail.
"""

from pathlib import Path
from urllib.parse import quote
import logging
import os

from PIL import Image, ImageOps

from utils.app_utils import sanitize_filename

logger = logging.getLogger(__name__)

# Tiles render at roughly 150px; twice that stays sharp on high-DPI screens.
THUMBNAIL_EDGE = 320
# Same ceiling the upload route uses for EXIF rotation; beyond it a Pi Zero
# risks running out of memory just to make a preview.
MAX_SOURCE_PIXELS = 50_000_000

THUMBNAIL_ROUTE = "/plugin/ai_photo_stylist/thumbnail"


def thumbnail_path(thumb_dir, source_path):
    """Where the preview for ``source_path`` lives (keyed on the file stem)."""
    stem = sanitize_filename(Path(source_path).stem) or "photo"
    return Path(thumb_dir) / f"{stem}.jpg"


def thumbnail_url(kind, source_path):
    """URL of the preview route, versioned by mtime so it can be cached long."""
    try:
        version = int(os.path.getmtime(source_path))
    except OSError:
        version = 0
    return f"{THUMBNAIL_ROUTE}/{kind}/{quote(Path(source_path).name)}?v={version}"


def ensure_thumbnail(source_path, thumb_dir, image=None):
    """Return the preview path, creating it if missing or older than the source.

    ``image`` lets a caller that already holds the decoded picture skip
    reading it back from disk. Returns None when no preview can be made.
    """
    source = Path(source_path)
    thumb = thumbnail_path(thumb_dir, source)
    try:
        if thumb.is_file() and thumb.stat().st_mtime >= source.stat().st_mtime:
            return thumb
    except OSError:
        return None

    try:
        if image is None:
            with Image.open(source) as img:
                if img.width * img.height > MAX_SOURCE_PIXELS:
                    return None
                # JPEGs decode straight at a reduced scale, which is most of
                # the memory and time saved on a phone photo.
                img.draft("RGB", (THUMBNAIL_EDGE, THUMBNAIL_EDGE))
                preview = ImageOps.exif_transpose(img).convert("RGB")
        else:
            preview = image.convert("RGB")
        preview.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE))

        thumb.parent.mkdir(parents=True, exist_ok=True)
        temp = thumb.with_name(thumb.name + ".tmp")
        preview.save(temp, format="JPEG", quality=80)
        os.replace(temp, thumb)
        return thumb
    except Exception as exc:
        logger.warning("AI Photo Stylist could not make a thumbnail for %s: %s", source.name, exc)
        return None
