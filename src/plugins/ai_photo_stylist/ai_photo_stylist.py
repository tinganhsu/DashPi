"""AI Photo Stylist plugin — restyles uploaded photos with Gemini image models."""

from io import BytesIO
from pathlib import Path
from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image, ImageDraw, ImageFont
from utils.app_utils import get_font, resolve_path, sanitize_filename
import hashlib
import json
import logging
import os
import random
import re
import time

logger = logging.getLogger(__name__)

GEMINI_IMAGE_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
]
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-image"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heif", ".heic", ".avif"}


class AIPhotoStylist(BasePlugin):
    """Restyles plugin-owned uploaded photos and caches generated outputs."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "Google Gemini",
            "expected_key": "GOOGLE_GEMINI_SECRET",
        }
        template_params["available_images"] = self._get_available_images()
        template_params["cached_images"] = self._get_cached_images()
        template_params["cached_image_count"] = len(template_params["cached_images"])

        try:
            template_params["vibes"] = self._load_vibes()
            template_params["vibes_error"] = ""
        except RuntimeError as exc:
            template_params["vibes"] = []
            template_params["vibes_error"] = str(exc)

        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== AI Photo Stylist Plugin: Starting image generation ===")

        dimensions = self._get_dimensions(device_config)
        fit_mode = settings.get("fitMode", "fit")
        show_caption = settings.get("showCaption") == "true"

        image_path, is_cached_image = self._select_source_image(settings)
        if is_cached_image:
            logger.info(f"AI Photo Stylist selected cached image directly: {os.path.basename(image_path)}")
            return self.image_loader.from_file(image_path, dimensions, resize=True, fit_mode=fit_mode)

        api_key = device_config.load_env_key("GOOGLE_GEMINI_SECRET")
        if not api_key:
            raise RuntimeError("Google Gemini API Key not configured. Add GOOGLE_GEMINI_SECRET in Settings > API Keys.")

        vibe = self._select_vibe(settings)
        final_prompt = self._build_prompt(vibe, settings.get("customPrompt", ""))
        model = settings.get("geminiImageModel", DEFAULT_GEMINI_MODEL)
        if model not in GEMINI_IMAGE_MODELS:
            raise RuntimeError("Invalid Gemini image model provided.")

        try:
            generated = self._generate_with_gemini(api_key, model, image_path, final_prompt, dimensions)
            cached_path = self._save_cached_image(generated, image_path, vibe)
            logger.info(f"Cached generated image: {cached_path}")

            image = self.image_loader.resize_image(generated, dimensions, fit_mode=fit_mode)
            if show_caption:
                image = self._add_caption(image, image_path, vibe)

            logger.info("=== AI Photo Stylist Plugin: Image generation complete ===")
            return image

        except Exception as exc:
            logger.exception(f"AI Photo Stylist generation failed, attempting cached fallback: {exc}")
            fallback = self._load_random_cached_image(dimensions, fit_mode)
            if fallback:
                logger.info("AI Photo Stylist served a cached fallback image")
                return fallback
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"AI Photo Stylist failed and no cached images are available: {str(exc)[:120]}")

    def _get_dimensions(self, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            return dimensions[::-1]
        return dimensions

    def _get_available_images(self):
        images = []
        upload_dir = self._upload_dir()
        if not upload_dir.is_dir():
            return images

        for path in sorted(upload_dir.iterdir()):
            if path.name.startswith(".") or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            images.append({
                "name": path.name,
                "path": str(path),
            })
        return images

    def _get_cached_images(self):
        images = []
        cached_dir = self._cached_dir()
        if not cached_dir.is_dir():
            return images

        for path in sorted(cached_dir.iterdir()):
            if path.name.startswith(".") or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            images.append({
                "name": path.name,
                "path": str(path),
            })
        return images

    def _load_vibes(self):
        vibes_path = Path(self.get_plugin_dir(os.path.join("resources", "vibe-pic.json")))
        if not vibes_path.is_file():
            raise RuntimeError("Missing vibe-pic.json. Place it in ai_photo_stylist/resources.")

        try:
            with vibes_path.open(encoding="utf-8") as f:
                raw_vibes = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid vibe-pic.json: {exc}") from exc

        if not isinstance(raw_vibes, list):
            raise RuntimeError("Invalid vibe-pic.json: root value must be a list.")

        vibes = []
        seen = set()
        for index, item in enumerate(raw_vibes):
            if not isinstance(item, dict):
                raise RuntimeError(f"Invalid vibe-pic.json: item {index + 1} must be an object.")

            name = (item.get("name") or item.get("style_name") or "").strip()
            prompt = (item.get("prompt") or "").strip()
            vibe_id = (item.get("id") or self._slugify(name) or f"vibe_{index + 1}").strip()

            if not name or not prompt:
                raise RuntimeError(f"Invalid vibe-pic.json: item {index + 1} needs style_name/name and prompt.")
            if vibe_id in seen:
                raise RuntimeError(f"Invalid vibe-pic.json: duplicate vibe id '{vibe_id}'.")

            seen.add(vibe_id)
            vibes.append({
                "id": vibe_id,
                "name": name,
                "prompt": prompt,
            })

        if not vibes:
            raise RuntimeError("Invalid vibe-pic.json: at least one vibe is required.")
        return vibes

    def _select_source_image(self, settings):
        image_paths = settings.get("imageFiles[]", [])
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        image_paths = [path for path in image_paths if path]

        if settings.get("randomizePhoto") == "true":
            candidates = [path for path in image_paths if self._is_valid_upload_path(path)]
            if not candidates:
                candidates = [item["path"] for item in self._get_available_images()]
            cached_candidates = []
            if settings.get("includeCachedInRandom") == "true":
                cached_candidates = self._get_cached_image_paths()
                candidates.extend(cached_candidates)
            if not candidates:
                raise RuntimeError("No AI Photo Stylist images found. Upload photos in this plugin first.")
            selected = random.choice(candidates)
            return selected, selected in cached_candidates

        image_path = settings.get("sourceImagePath") or (image_paths[0] if image_paths else "")
        if not image_path:
            raise RuntimeError("No source photo selected. Upload and select a photo first.")
        if not self._is_valid_upload_path(image_path):
            raise RuntimeError("Invalid source photo path.")
        if not os.path.isfile(image_path):
            raise RuntimeError("Selected source photo was not found. Re-select or upload it again.")
        return image_path, False

    def _select_vibe(self, settings):
        vibes = self._load_vibes()
        if settings.get("randomizeVibe") == "true":
            return random.choice(vibes)

        vibe_id = settings.get("vibeId", "")
        vibe = next((item for item in vibes if item["id"] == vibe_id), None)
        if vibe:
            return vibe
        if not vibe_id and vibes:
            return vibes[0]
        raise RuntimeError("Selected vibe was not found in vibe-pic.json.")

    def _build_prompt(self, vibe, custom_prompt):
        prompt_parts = [
            vibe["prompt"],
            "Use the input photo as the source image. Preserve the main subject identity, pose, and core composition.",
            "Do not add text, captions, watermarks, logos, borders, or frames.",
        ]
        custom_prompt = (custom_prompt or "").strip()
        if custom_prompt:
            prompt_parts.append(custom_prompt)
        return " ".join(prompt_parts)

    def _generate_with_gemini(self, api_key, model, image_path, prompt, dimensions):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("Google Gemini SDK not installed. Run: pip install google-genai") from exc

        api_key = api_key.encode("ascii", errors="ignore").decode("ascii").strip()
        client = genai.Client(api_key=api_key)

        aspect_ratio = "16:9" if dimensions[0] >= dimensions[1] else "9:16"
        with Image.open(image_path) as input_image:
            input_image = input_image.convert("RGB").copy()

        response = client.models.generate_content(
            model=model,
            contents=[prompt, input_image],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
            ),
        )

        parts = getattr(response, "parts", None) or []
        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if inline_data is not None:
                data = getattr(inline_data, "data", None)
                if data:
                    buf = BytesIO(data)
                    image = Image.open(buf).convert("RGB").copy()
                    buf.close()
                    return image

        raise RuntimeError("Gemini returned no image in response.")

    def _save_cached_image(self, image, image_path, vibe):
        cached_dir = self._cached_dir()
        cached_dir.mkdir(parents=True, exist_ok=True)

        source_stem = Path(image_path).stem
        source_stem = sanitize_filename(source_stem)[:48] or "photo"
        vibe_id = sanitize_filename(vibe["id"])[:48] or "vibe"
        digest = hashlib.sha1(f"{image_path}:{vibe['id']}:{time.time()}".encode("utf-8")).hexdigest()[:8]
        filename = f"{int(time.time())}_{source_stem}_{vibe_id}_{digest}.png"
        path = cached_dir / filename
        image.convert("RGB").save(path, format="PNG")
        return str(path)

    def _load_random_cached_image(self, dimensions, fit_mode):
        candidates = self._get_cached_image_paths()
        if not candidates:
            return None
        image_path = random.choice(candidates)
        return self.image_loader.from_file(image_path, dimensions, resize=True, fit_mode=fit_mode)

    def _get_cached_image_paths(self):
        return [item["path"] for item in self._get_cached_images()]

    def _add_caption(self, image, image_path, vibe):
        img = image.convert("RGBA") if image.mode != "RGBA" else image
        draw = ImageDraw.Draw(img, "RGBA")
        width, height = img.size
        title = f"{Path(image_path).stem} · {vibe['name']}"

        font_size = max(13, int(height * 0.03))
        try:
            font = get_font("Jost", font_size, "bold")
        except Exception:
            font = ImageFont.load_default()

        padding = max(8, int(height * 0.018))
        max_text_width = width - padding * 2
        bbox = draw.textbbox((0, 0), title, font=font)
        while bbox[2] - bbox[0] > max_text_width and len(title) > 8:
            title = title[:-4] + "..."
            bbox = draw.textbbox((0, 0), title, font=font)

        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        bar_h = text_h + padding * 2
        bar_top = height - bar_h
        draw.rectangle([0, bar_top, width, height], fill=(0, 0, 0, 165))
        draw.text(((width - text_w) // 2, bar_top + padding), title, font=font, fill=(255, 255, 255, 245))
        return img.convert("RGB")

    def _is_valid_upload_path(self, image_path):
        upload_dir = self._upload_dir().resolve()
        try:
            path = Path(image_path).resolve()
        except OSError:
            return False
        return path.is_file() and upload_dir in path.parents and path.suffix.lower() in IMAGE_EXTENSIONS

    def cleanup(self, settings):
        image_paths = settings.get("imageFiles[]", [])
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        for image_path in image_paths:
            if not self._is_valid_upload_path(image_path):
                continue
            try:
                os.remove(image_path)
                logger.info(f"Deleted AI Photo Stylist upload: {image_path}")
            except OSError as exc:
                logger.warning(f"Failed to delete AI Photo Stylist upload {image_path}: {exc}")

    @staticmethod
    def _upload_dir():
        return Path(resolve_path(os.path.join("static", "images", "ai_photo_stylist", "uploads")))

    @staticmethod
    def _cached_dir():
        return Path(resolve_path(os.path.join("static", "images", "ai_photo_stylist", "cached")))

    @staticmethod
    def _slugify(value):
        value = value.lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        return value.strip("_")
