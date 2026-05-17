"""Image utilities — download, resize, orientation, enhancement, and hashing."""

from PIL import Image, ImageEnhance, ImageOps, ImageFilter
from io import BytesIO
import os
import logging
import shutil
import subprocess
import tempfile
import zlib
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

def get_image(image_url):
    """Download image from URL using shared HTTP session with connection pooling."""
    session = get_http_session()
    response = session.get(image_url, timeout=30)
    img = None
    if 200 <= response.status_code < 300 or response.status_code == 304:
        buf = BytesIO(response.content)
        img = Image.open(buf).copy()
        buf.close()
    else:
        logger.error(f"Received non-200 response from {image_url}: status_code: {response.status_code}")
    return img

def change_orientation(image, orientation, inverted=False):
    """Rotate image to match the configured display orientation."""
    if orientation == 'horizontal':
        angle = 0
    elif orientation == 'vertical':
        angle = 90
    else:
        angle = 0

    if inverted:
        angle = (angle + 180) % 360

    return image.rotate(angle, expand=1)

def resize_image(image, desired_size, image_settings=None):
    """Crop and resize image to exact dimensions, maintaining aspect ratio."""
    if image_settings is None:
        image_settings = []
    img_width, img_height = image.size
    desired_width, desired_height = desired_size
    desired_width, desired_height = int(desired_width), int(desired_height)

    img_ratio = img_width / img_height
    desired_ratio = desired_width / desired_height

    keep_width = "keep-width" in image_settings

    x_offset, y_offset = 0,0
    new_width, new_height = img_width,img_height
    # Step 1: Determine crop dimensions
    desired_ratio = desired_width / desired_height
    if img_ratio > desired_ratio:
        # Image is wider than desired aspect ratio
        new_width = int(img_height * desired_ratio)
        if not keep_width:
            x_offset = (img_width - new_width) // 2
    else:
        # Image is taller than desired aspect ratio
        new_height = int(img_width / desired_ratio)
        if not keep_width:
            y_offset = (img_height - new_height) // 2

    # Step 2: Crop the image
    image = image.crop((x_offset, y_offset, x_offset + new_width, y_offset + new_height))

    # Step 3: Resize to the exact desired dimensions (if necessary)
    return image.resize((desired_width, desired_height), Image.BICUBIC)

def apply_image_enhancement(img, image_settings=None):
    """Apply brightness, contrast, saturation, and sharpness adjustments."""
    if image_settings is None:
        image_settings = {}
    # Convert image to RGB mode if necessary for enhancement operations
    # ImageEnhance requires RGB mode for operations like blend
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
        

    # Apply Brightness
    img = ImageEnhance.Brightness(img).enhance(image_settings.get("brightness", 1.0))

    # Apply Contrast
    img = ImageEnhance.Contrast(img).enhance(image_settings.get("contrast", 1.0))

    # Apply Saturation (Color)
    img = ImageEnhance.Color(img).enhance(image_settings.get("saturation", 1.0))

    # Apply Sharpness
    img = ImageEnhance.Sharpness(img).enhance(image_settings.get("sharpness", 1.0))

    return img

def optimize_for_eink(image, display_type, settings=None):
    """Apply conservative pre-processing for e-paper displays.

    The optimizer keeps the input dimensions and avoids forcing grayscale so
    Inky and color Waveshare panels can still map colors in their drivers.
    """
    if settings is None:
        settings = {}
    if settings.get("eink_optimization_enabled", True) is False:
        return image

    if not display_type:
        return image

    display_type = str(display_type).lower()
    if not ("inky" in display_type or "waveshare" in display_type or "e-paper" in display_type):
        return image

    original_mode = image.mode
    if image.mode == "L":
        luminance = ImageOps.autocontrast(image, cutoff=1)
        luminance = Image.blend(image, luminance, 0.65)
        luminance = luminance.point(lambda p: int(5 + (p * 245 / 255)))
        luminance = ImageEnhance.Contrast(luminance).enhance(1.08)
        return ImageEnhance.Sharpness(luminance).enhance(1.15)

    if image.mode != "RGB":
        image = image.convert("RGB")

    y, cb, cr = image.convert("YCbCr").split()
    adjusted_y = ImageOps.autocontrast(y, cutoff=1)
    adjusted_y = Image.blend(y, adjusted_y, 0.65)
    adjusted_y = adjusted_y.point(lambda p: int(5 + (p * 245 / 255)))
    adjusted_y = ImageEnhance.Contrast(adjusted_y).enhance(1.08)

    optimized = Image.merge("YCbCr", (adjusted_y, cb, cr)).convert("RGB")
    optimized = ImageEnhance.Contrast(optimized).enhance(1.04)
    optimized = ImageEnhance.Sharpness(optimized).enhance(1.15)

    if original_mode == "L":
        return optimized.convert("L")
    return optimized

def compute_image_hash(image):
    """Compute fast non-cryptographic hash of an image for change detection.

    Uses a small thumbnail + Adler-32 for speed. Downsampling to 100x60
    is sufficient to detect content changes while being ~160x faster than
    hashing the full image.
    """
    thumb = image.copy()
    thumb.thumbnail((100, 60), Image.NEAREST)
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    return format(zlib.adler32(thumb.tobytes()) & 0xffffffff, '08x')

def take_screenshot_html(html_str, dimensions, timeout_ms=None):
    """Render an HTML string to a screenshot image using a temporary file."""
    image = None
    html_file_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as html_file:
            html_file.write(html_str.encode("utf-8"))
            html_file_path = html_file.name

        image = take_screenshot(html_file_path, dimensions, timeout_ms)

    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")
    finally:
        if html_file_path and os.path.exists(html_file_path):
            os.remove(html_file_path)

    return image

def _find_chromium_binary():
    """Find the first available Chromium-based binary in system PATH."""
    candidates = ["chromium-headless-shell", "chromium", "chrome"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            logger.debug(f"Found browser binary: {candidate} at {path}")
            return candidate
    return None

def take_screenshot(target, dimensions, timeout_ms=None):
    """Take a screenshot of a local HTML file or URL using headless Chromium."""
    image = None
    img_file_path = None
    try:
        browser = _find_chromium_binary()
        if not browser:
            logger.error("No Chromium-based browser found. Install chromium, chromium-headless-shell, or chrome.")
            return None

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_file:
            img_file_path = img_file.name

        command = [
            browser,
            target,
            "--headless",
            f"--screenshot={img_file_path}",
            f"--window-size={dimensions[0]},{dimensions[1]}",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--use-gl=swiftshader",
            "--hide-scrollbars",
            "--in-process-gpu",
            "--js-flags=--jitless",
            "--disable-zero-copy",
            "--disable-gpu-memory-buffer-compositor-resources",
            "--disable-extensions",
            "--disable-plugins",
            "--mute-audio",
            "--renderer-process-limit=1",
            "--no-zygote",
            "--no-sandbox",
        ]
        if timeout_ms:
            command.append(f"--timeout={timeout_ms}")

        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0 or not os.path.exists(img_file_path):
            logger.error(f"Failed to take screenshot (return code: {result.returncode})")
            return None

        with Image.open(img_file_path) as img:
            image = img.copy()

    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")
    finally:
        if img_file_path and os.path.exists(img_file_path):
            os.remove(img_file_path)

    return image

def crossfade_frames(old_image, new_image, steps=10):
    """Generate crossfade blend frames between two images.

    Yields PIL Images blended from old_image to new_image.
    Both images must be the same size and mode (RGB).
    Uses ease-in-out curve for smoother perceived transition.
    """
    for i in range(1, steps + 1):
        # Ease-in-out: slow start and end, fast middle
        t = i / steps
        alpha = t * t * (3 - 2 * t)  # smoothstep
        yield Image.blend(old_image, new_image, alpha)


def pad_image_blur(img: Image, dimensions: tuple[int, int]) -> Image:
    """Letterbox an image with a blurred version of itself as the background."""
    bkg = ImageOps.fit(img, dimensions)
    bkg = bkg.filter(ImageFilter.BoxBlur(8))
    img = ImageOps.contain(img, dimensions)

    img_size = img.size
    bkg.paste(img, ((dimensions[0] - img_size[0]) // 2, (dimensions[1] - img_size[1]) // 2))
    return bkg
