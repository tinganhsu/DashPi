"""AI Photo Stylist plugin — restyles uploaded photos with Gemini image models."""

from io import BytesIO
from pathlib import Path
from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image, ImageDraw, ImageFont
from utils.app_utils import get_font, resolve_path, sanitize_filename
from urllib.parse import quote
import base64
import hashlib
import json
import logging
import math
import os
import random
import re
import subprocess
import time

logger = logging.getLogger(__name__)

GEMINI_IMAGE_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
]
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-image"
OPENAI_IMAGE_MODELS = ["gpt-image-2", "gpt-image-1"]
DEFAULT_OPENAI_MODEL = "gpt-image-2"
OPENAI_IMAGE_QUALITIES = {"auto", "low", "medium", "high"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heif", ".heic", ".avif"}
USAGE_STATE_VERSION = 1
LEGACY_USAGE_HISTORY_KEY = "styleUsageHistory"
DEFAULT_PROMPT_TEMPLATE = {
    "prompt_parts": [
        "{vibe_prompt}",
        (
            "Use the input photo as the source image. Preserve the person's facial structure, identity, "
            "expression, hairstyle, and recognizable features. The subject should remain clearly identifiable, "
            "but the artwork should fully follow the selected style prompt."
        ),
        (
            "Let the selected style strongly influence color, texture, linework, composition, lighting, "
            "background treatment, and overall artistic mood. Do not force a generic clean portrait look unless "
            "the selected style asks for it."
        ),
        (
            "Adapt the composition to {composition} while keeping the main subject prominent and recognizable. "
            "Fill the requested aspect ratio naturally and extend or regenerate the background in a style-consistent way. "
            "Make the image readable on a medium-resolution e-paper display, but preserve the distinctive visual language "
            "of the selected art style."
        ),
        (
            "Avoid text, typography, letters, captions, watermark, logo, signature, distorted facial features, "
            "extra limbs, extra fingers, blurry face, and unreadable facial details."
        ),
    ]
}


class AIPhotoStylist(BasePlugin):
    """Restyles plugin-owned uploaded photos and caches generated outputs."""

    @classmethod
    def get_blueprint(cls):
        """Return the AI Photo Stylist API blueprint."""
        from . import api

        return api.ai_photo_stylist_bp

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "OpenAI or Google Gemini",
            "expected_key": "OPEN_AI_SECRET or GOOGLE_GEMINI_SECRET",
        }
        template_params["available_images"] = self._get_available_images()
        template_params["cached_images"] = self._get_cached_images()
        template_params["cached_image_count"] = len(template_params["cached_images"])
        template_params.update(self._get_core_patch_template_params())

        try:
            template_params["vibes"] = self._load_vibes()
            template_params["vibes_error"] = ""
        except RuntimeError as exc:
            template_params["vibes"] = []
            template_params["vibes_error"] = str(exc)

        return template_params

    def _get_core_patch_template_params(self):
        params = {
            "core_needs_patch": False,
            "core_patch_missing": [],
            "core_auto_patch_started": False,
        }
        try:
            from .patch_core import check_core_patched

            is_patched, missing = check_core_patched()
            params["core_needs_patch"] = not is_patched
            params["core_patch_missing"] = missing
            if is_patched:
                return params

            patch_script = Path(self.get_plugin_dir("patch-core.sh"))
            if patch_script.is_file():
                subprocess.Popen(
                    ["bash", str(patch_script)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                params["core_auto_patch_started"] = True
        except Exception as exc:
            logger.warning("Could not check or start AI Photo Stylist core patch: %s", exc)
        return params

    def generate_image(self, settings, device_config):
        logger.info("=== AI Photo Stylist Plugin: Starting image generation ===")

        dimensions = self._get_dimensions(device_config)
        fit_mode = settings.get("fitMode", "fit")
        show_caption = settings.get("showCaption") == "true"
        self._remove_legacy_usage_settings(settings)

        image_path, is_cached_image = self._select_source_image(settings)
        if is_cached_image:
            logger.info(f"AI Photo Stylist selected cached image directly: {os.path.basename(image_path)}")
            return self.image_loader.from_file(image_path, dimensions, resize=True, fit_mode=fit_mode)

        vibe = self._select_vibe(settings, image_path)
        orientation = device_config.get_config("orientation")
        final_prompt = self._build_prompt(vibe, settings.get("customPrompt", ""), orientation)
        provider = settings.get("provider", "gemini")

        try:
            if provider == "openai":
                api_key = device_config.load_env_key("OPEN_AI_SECRET")
                if not api_key:
                    raise RuntimeError("OpenAI API Key not configured. Add OPEN_AI_SECRET in Settings > API Keys.")
                model = settings.get("openaiImageModel", DEFAULT_OPENAI_MODEL)
                if model not in OPENAI_IMAGE_MODELS:
                    raise RuntimeError("Invalid OpenAI image model provided.")
                quality = self._normalize_openai_quality(settings.get("openaiImageQuality", "medium"))
                generated = self._generate_with_openai(api_key, model, image_path, final_prompt, dimensions, quality)
            elif provider == "gemini":
                api_key = device_config.load_env_key("GOOGLE_GEMINI_SECRET")
                if not api_key:
                    raise RuntimeError("Google Gemini API Key not configured. Add GOOGLE_GEMINI_SECRET in Settings > API Keys.")
                model = settings.get("geminiImageModel", DEFAULT_GEMINI_MODEL)
                if model not in GEMINI_IMAGE_MODELS:
                    raise RuntimeError("Invalid Gemini image model provided.")
                generated = self._generate_with_gemini(api_key, model, image_path, final_prompt, dimensions)
            else:
                raise RuntimeError("Invalid AI provider provided.")

            cached_path = self._save_cached_image(generated, image_path, vibe)
            self._mark_style_used(settings, image_path, vibe)
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
                "thumbnail_url": self._thumbnail_url_for_upload(path),
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
                "thumbnail_url": self._static_url_for_path(path),
            })
        return images

    def _thumbnail_url_for_upload(self, path):
        thumb_path = self._thumbnail_dir() / f"{path.stem}.jpg"
        if thumb_path.is_file():
            return self._static_url_for_path(thumb_path)
        return self._static_url_for_path(path)

    @staticmethod
    def _static_url_for_path(path):
        try:
            static_dir = Path(resolve_path("static")).resolve()
            resolved = Path(path).resolve()
            rel_path = resolved.relative_to(static_dir)
            return "/static/" + "/".join(quote(part) for part in rel_path.parts)
        except (OSError, ValueError):
            pass

        known_dirs = [
            (AIPhotoStylist._thumbnail_dir(), ("images", "ai_photo_stylist", "uploads", "thumbs")),
            (AIPhotoStylist._upload_dir(), ("images", "ai_photo_stylist", "uploads")),
            (AIPhotoStylist._cached_dir(), ("images", "ai_photo_stylist", "cached")),
        ]
        for base_dir, static_parts in known_dirs:
            try:
                resolved = Path(path).resolve()
                rel_path = resolved.relative_to(base_dir.resolve())
                return "/static/" + "/".join(quote(part) for part in (*static_parts, *rel_path.parts))
            except (OSError, ValueError):
                continue
        return ""

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
            if not candidates and not cached_candidates:
                raise RuntimeError("No AI Photo Stylist images found. Upload photos in this plugin first.")

            if candidates:
                candidates = self._prioritize_random_source_candidates(candidates, settings)
                selected = random.choice(candidates)
                return selected, False

            selected = random.choice(cached_candidates)
            return selected, True

        image_path = settings.get("sourceImagePath") or (image_paths[0] if image_paths else "")
        if not image_path:
            raise RuntimeError("No source photo selected. Upload and select a photo first.")
        if not self._is_valid_upload_path(image_path):
            raise RuntimeError("Invalid source photo path.")
        if not os.path.isfile(image_path):
            raise RuntimeError("Selected source photo was not found. Re-select or upload it again.")
        return image_path, False

    def _select_vibe(self, settings, image_path=None):
        custom_vibe = self._custom_vibe_from_settings(settings)
        if custom_vibe:
            return custom_vibe

        vibes = self._load_vibes()
        if settings.get("randomizeVibe") == "true":
            if image_path:
                unused_vibes = self._unused_vibes_for_source(settings, image_path, vibes)
                if unused_vibes:
                    return random.choice(unused_vibes)
            return random.choice(vibes)

        vibe_id = settings.get("vibeId", "")
        vibe = next((item for item in vibes if item["id"] == vibe_id), None)
        if vibe:
            return vibe
        if not vibe_id and vibes:
            return vibes[0]
        raise RuntimeError("Selected vibe was not found in vibe-pic.json.")

    def _custom_vibe_from_settings(self, settings):
        prompt = (settings.get("customVibePrompt") or "").strip()
        if not prompt:
            return None
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        return {
            "id": f"custom_{digest}",
            "name": "Custom Prompt",
            "prompt": prompt,
        }

    def _prioritize_random_source_candidates(self, candidates, settings):
        custom_vibe = self._custom_vibe_from_settings(settings)
        vibes = [custom_vibe] if custom_vibe else self._load_vibes()
        vibe_ids = {vibe["id"] for vibe in vibes}
        state = self._load_usage_state()
        history = state["photos"]
        candidates = list(dict.fromkeys(candidates))
        self._prune_usage_state(state, candidates)

        if settings.get("randomizeVibe") == "true":
            unused_sources = [
                path for path in candidates
                if not (set(history.get(self._history_key_for_image(path), [])) & vibe_ids)
            ]
            if unused_sources:
                return unused_sources

            sources_with_unused_vibes = [
                path for path in candidates
                if vibe_ids - set(history.get(self._history_key_for_image(path), []))
            ]
            if sources_with_unused_vibes:
                return sources_with_unused_vibes

            self._reset_usage_history(candidates, state)
            return candidates

        vibe_id = settings.get("vibeId", "")
        selected_vibe = next((item for item in vibes if item["id"] == vibe_id), vibes[0])
        unused_sources = [
            path for path in candidates
            if selected_vibe["id"] not in set(history.get(self._history_key_for_image(path), []))
        ]
        if unused_sources:
            never_styled_sources = [
                path for path in unused_sources
                if not (set(history.get(self._history_key_for_image(path), [])) & vibe_ids)
            ]
            return never_styled_sources or unused_sources

        self._reset_usage_history(candidates, state)
        return candidates

    def _unused_vibes_for_source(self, settings, image_path, vibes):
        history = self._load_usage_state()["photos"]
        used_vibes = set(history.get(self._history_key_for_image(image_path), []))
        return [vibe for vibe in vibes if vibe["id"] not in used_vibes]

    def _mark_style_used(self, settings, image_path, vibe):
        state = self._load_usage_state()
        history = state["photos"]
        key = self._history_key_for_image(image_path)
        used_vibes = history.setdefault(key, [])
        if vibe["id"] not in used_vibes:
            used_vibes.append(vibe["id"])
        self._save_usage_state(state)

    def _reset_usage_history(self, current_image_paths, state=None):
        state = state or self._load_usage_state()
        current_keys = {self._history_key_for_image(path) for path in current_image_paths}
        state["photos"] = {
            key: value for key, value in state["photos"].items()
            if key not in current_keys
        }
        self._save_usage_state(state)

    def _prune_usage_state(self, state, current_image_paths):
        current_keys = {self._history_key_for_image(path) for path in current_image_paths}
        pruned = {
            key: value for key, value in state["photos"].items()
            if key in current_keys
        }
        if pruned != state["photos"]:
            state["photos"] = pruned
            self._save_usage_state(state)

    def _load_usage_state(self):
        state_path = self._usage_state_path()
        if not state_path.is_file():
            return {"version": USAGE_STATE_VERSION, "photos": {}}

        try:
            with state_path.open(encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Could not load AI Photo Stylist usage state, starting fresh: {exc}")
            return {"version": USAGE_STATE_VERSION, "photos": {}}

        if not isinstance(state, dict):
            return {"version": USAGE_STATE_VERSION, "photos": {}}
        history = state.get("photos", {})
        if not isinstance(history, dict):
            history = {}

        clean_history = {}
        for image_key, vibe_ids in history.items():
            if not isinstance(image_key, str):
                continue
            if isinstance(vibe_ids, str):
                vibe_ids = [vibe_ids]
            if not isinstance(vibe_ids, list):
                continue
            clean_history[image_key] = [vibe_id for vibe_id in vibe_ids if isinstance(vibe_id, str)]
        return {"version": USAGE_STATE_VERSION, "photos": clean_history}

    def _save_usage_state(self, state):
        state_path = self._usage_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": USAGE_STATE_VERSION,
            "photos": state.get("photos", {}),
        }
        tmp_path = state_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, state_path)

    def _remove_legacy_usage_settings(self, settings):
        settings.pop(LEGACY_USAGE_HISTORY_KEY, None)
        settings.pop("lastStyledSourceImagePath", None)
        settings.pop("lastStyledVibeId", None)

    def _history_key_for_image(self, image_path):
        try:
            resolved_path = str(Path(image_path).resolve())
        except OSError:
            resolved_path = str(image_path)
        return hashlib.sha1(resolved_path.encode("utf-8")).hexdigest()[:16]

    def _build_prompt(self, vibe, custom_prompt, orientation="horizontal"):
        composition = "vertical composition, portrait orientation"
        if orientation != "vertical":
            composition = "horizontal composition, landscape orientation"

        prompt_parts = [
            part.replace("{vibe_prompt}", vibe["prompt"]).replace("{composition}", composition)
            for part in self._load_default_prompt_parts()
        ]
        custom_prompt = (custom_prompt or "").strip()
        if custom_prompt:
            prompt_parts.append(custom_prompt)
        return " ".join(prompt_parts)

    def _load_default_prompt_parts(self):
        prompt_path = Path(self.get_plugin_dir(os.path.join("resources", "default-prompt.json")))
        if not prompt_path.is_file():
            return DEFAULT_PROMPT_TEMPLATE["prompt_parts"]

        try:
            with prompt_path.open(encoding="utf-8") as f:
                prompt_template = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid default-prompt.json: {exc}") from exc

        if not isinstance(prompt_template, dict):
            raise RuntimeError("Invalid default-prompt.json: root value must be an object.")

        prompt_parts = prompt_template.get("prompt_parts")
        if not isinstance(prompt_parts, list):
            raise RuntimeError("Invalid default-prompt.json: prompt_parts must be a list.")

        clean_parts = [part.strip() for part in prompt_parts if isinstance(part, str) and part.strip()]
        if not clean_parts:
            raise RuntimeError("Invalid default-prompt.json: prompt_parts must include at least one prompt string.")
        if not any("{vibe_prompt}" in part for part in clean_parts):
            raise RuntimeError("Invalid default-prompt.json: prompt_parts must include {vibe_prompt}.")

        return clean_parts

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

    def _generate_with_openai(self, api_key, model, image_path, prompt, dimensions, quality):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenAI SDK not installed. Run: pip install openai") from exc

        api_key = api_key.encode("ascii", errors="ignore").decode("ascii").strip()
        client = OpenAI(api_key=api_key)

        size = self._openai_size_for_model(model, dimensions)
        request_args = {
            "model": model,
            "prompt": prompt,
            "image": self._openai_image_file(image_path),
            "quality": quality,
            "size": size,
        }
        if model == "gpt-image-1":
            request_args["input_fidelity"] = "high"

        logger.info(f"OpenAI Photo Stylist settings: model={model}, quality={quality}, size={size}")

        try:
            response = client.images.edit(**request_args)
        finally:
            request_args["image"].close()

        return self._openai_response_to_image(response)

    def _openai_response_to_image(self, response):
        image_base64 = getattr(response.data[0], "b64_json", None)
        if image_base64:
            buf = BytesIO(base64.b64decode(image_base64))
            image = Image.open(buf).convert("RGB").copy()
            buf.close()
            return image

        image_url = getattr(response.data[0], "url", None)
        if image_url:
            from utils.http_client import get_http_session
            session = get_http_session()
            download = session.get(image_url, timeout=30)
            download.raise_for_status()
            buf = BytesIO(download.content)
            image = Image.open(buf).convert("RGB").copy()
            buf.close()
            return image

        raise RuntimeError("OpenAI returned no image data.")

    def _openai_image_file(self, image_path):
        buf = BytesIO()
        with Image.open(image_path) as input_image:
            input_image.convert("RGB").save(buf, format="PNG")
        buf.seek(0)
        buf.name = "source.png"
        return buf

    def _openai_size_for_model(self, model, dimensions):
        width, height = dimensions
        if width <= 0 or height <= 0:
            return "1024x1024"

        if model == "gpt-image-1":
            ratio = width / height
            if 0.85 <= ratio <= 1.15:
                return "1024x1024"
            return "1536x1024" if ratio > 1 else "1024x1536"

        return self._openai_gpt_image_2_size(width, height)

    def _openai_gpt_image_2_size(self, width, height):
        max_edge = 3840
        min_pixels = 655360
        max_pixels = 8294400
        ratio = width / height

        if ratio > 3:
            width = height * 3
        elif ratio < 1 / 3:
            height = width * 3

        pixels = width * height
        scale = math.sqrt(min_pixels / pixels) if pixels < min_pixels else 1
        if max(width, height) * scale > max_edge:
            scale = max_edge / max(width, height)
        if width * height * scale * scale > max_pixels:
            scale = math.sqrt(max_pixels / (width * height))

        target_w = self._round_up_to_multiple(width * scale, 16)
        target_h = self._round_up_to_multiple(height * scale, 16)

        while target_w * target_h < min_pixels:
            if target_w <= target_h:
                target_w += 16
            else:
                target_h += 16
        while target_w * target_h > max_pixels or max(target_w, target_h) > max_edge:
            if target_w >= target_h and target_w > 16:
                target_w -= 16
            elif target_h > 16:
                target_h -= 16
            else:
                break

        return f"{int(target_w)}x{int(target_h)}"

    @staticmethod
    def _round_up_to_multiple(value, multiple):
        return int(math.ceil(value / multiple) * multiple)

    @staticmethod
    def _normalize_openai_quality(quality):
        quality = (quality or "medium").strip().lower()
        return quality if quality in OPENAI_IMAGE_QUALITIES else "medium"

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
    def _thumbnail_dir():
        return AIPhotoStylist._upload_dir() / "thumbs"

    @staticmethod
    def _cached_dir():
        return Path(resolve_path(os.path.join("static", "images", "ai_photo_stylist", "cached")))

    @staticmethod
    def _usage_state_path():
        return Path(resolve_path(os.path.join("static", "images", "ai_photo_stylist", "style_usage.json")))

    @staticmethod
    def _slugify(value):
        value = value.lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        return value.strip("_")
