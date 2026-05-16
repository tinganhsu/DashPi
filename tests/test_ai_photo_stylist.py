"""Tests for the AI Photo Stylist plugin."""

from io import BytesIO
from unittest.mock import MagicMock
import zipfile

import pytest
from PIL import Image


AI_PHOTO_STYLIST_CONFIG = {
    "id": "ai_photo_stylist",
    "display_name": "AI Photo Stylist",
    "class": "AIPhotoStylist",
}


def assert_valid_image(img, expected_size=None):
    assert isinstance(img, Image.Image)
    assert img.size[0] > 0 and img.size[1] > 0
    if expected_size:
        assert img.size == expected_size


def _create_test_image(path, size=(120, 80), color="blue"):
    image = Image.new("RGB", size, color)
    image.save(path)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    from plugins.ai_photo_stylist.ai_photo_stylist import AIPhotoStylist

    upload_dir = tmp_path / "uploads"
    cached_dir = tmp_path / "cached"
    upload_dir.mkdir()
    cached_dir.mkdir()

    monkeypatch.setattr(AIPhotoStylist, "_upload_dir", staticmethod(lambda: upload_dir))
    monkeypatch.setattr(AIPhotoStylist, "_cached_dir", staticmethod(lambda: cached_dir))
    instance = AIPhotoStylist(AI_PHOTO_STYLIST_CONFIG)
    instance._test_upload_dir = upload_dir
    instance._test_cached_dir = cached_dir
    return instance


def test_loads_user_vibe_pic_format(plugin):
    vibes = plugin._load_vibes()
    assert vibes
    assert {"id", "name", "prompt"} <= set(vibes[0].keys())
    assert vibes[0]["name"] == "浮世繪風格 (Ukiyo-e)"


def test_settings_template_includes_cached_image_count(plugin):
    _create_test_image(plugin._test_cached_dir / "first.png")
    _create_test_image(plugin._test_cached_dir / "second.jpg")
    (plugin._test_cached_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    (plugin._test_cached_dir / "notes.txt").write_text("not an image\n", encoding="utf-8")

    template = plugin.generate_settings_template()

    assert template["cached_image_count"] == 2
    assert [item["name"] for item in template["cached_images"]] == ["first.png", "second.jpg"]


def test_missing_gemini_key_raises(plugin, mock_device_config, tmp_path):
    img_path = plugin._test_upload_dir / "source.png"
    _create_test_image(img_path)
    mock_device_config.load_env_key.return_value = None

    with pytest.raises(RuntimeError, match="Google Gemini API Key"):
        plugin.generate_image({"imageFiles[]": [str(img_path)]}, mock_device_config)


def test_generate_image_caches_success(plugin, mock_device_config):
    img_path = plugin._test_upload_dir / "source.png"
    _create_test_image(img_path)
    mock_device_config.load_env_key.return_value = "gemini-key"
    plugin._generate_with_gemini = MagicMock(return_value=Image.new("RGB", (640, 360), "green"))

    img = plugin.generate_image({
        "imageFiles[]": [str(img_path)],
        "vibeId": plugin._load_vibes()[0]["id"],
        "fitMode": "fit",
    }, mock_device_config)

    assert_valid_image(img, (800, 480))
    cached = list(plugin._test_cached_dir.glob("*.png"))
    assert len(cached) == 1


def test_generation_error_uses_cached_fallback(plugin, mock_device_config):
    img_path = plugin._test_upload_dir / "source.png"
    cached_path = plugin._test_cached_dir / "fallback.png"
    _create_test_image(img_path)
    _create_test_image(cached_path, size=(300, 200), color="red")
    mock_device_config.load_env_key.return_value = "gemini-key"
    plugin._generate_with_gemini = MagicMock(side_effect=RuntimeError("Gemini down"))

    img = plugin.generate_image({
        "imageFiles[]": [str(img_path)],
        "vibeId": plugin._load_vibes()[0]["id"],
        "fitMode": "fit",
    }, mock_device_config)

    assert_valid_image(img, (800, 480))


def test_generation_error_without_cache_raises(plugin, mock_device_config):
    img_path = plugin._test_upload_dir / "source.png"
    _create_test_image(img_path)
    mock_device_config.load_env_key.return_value = "gemini-key"
    plugin._generate_with_gemini = MagicMock(side_effect=RuntimeError("Gemini down"))

    with pytest.raises(RuntimeError, match="Gemini down"):
        plugin.generate_image({"imageFiles[]": [str(img_path)]}, mock_device_config)


def test_rejects_non_plugin_upload_path(plugin, mock_device_config, tmp_path):
    img_path = tmp_path / "outside.png"
    _create_test_image(img_path)
    mock_device_config.load_env_key.return_value = "gemini-key"

    with pytest.raises(RuntimeError, match="Invalid source photo path"):
        plugin.generate_image({"sourceImagePath": str(img_path)}, mock_device_config)


def test_random_photo_and_vibe(plugin, mock_device_config):
    first = plugin._test_upload_dir / "first.png"
    second = plugin._test_upload_dir / "second.png"
    _create_test_image(first)
    _create_test_image(second)
    mock_device_config.load_env_key.return_value = "gemini-key"
    plugin._generate_with_gemini = MagicMock(return_value=Image.new("RGB", (640, 360), "green"))

    img = plugin.generate_image({
        "imageFiles[]": [str(first), str(second)],
        "randomizePhoto": "true",
        "randomizeVibe": "true",
    }, mock_device_config)

    assert_valid_image(img, (800, 480))


def test_random_photo_can_serve_cached_image_without_gemini(plugin, mock_device_config, monkeypatch):
    source = plugin._test_upload_dir / "source.png"
    cached = plugin._test_cached_dir / "cached.png"
    _create_test_image(source)
    _create_test_image(cached, size=(300, 200), color="red")
    mock_device_config.load_env_key.return_value = None
    plugin._generate_with_gemini = MagicMock()

    monkeypatch.setattr(
        "plugins.ai_photo_stylist.ai_photo_stylist.random.choice",
        lambda candidates: str(cached),
    )

    img = plugin.generate_image({
        "imageFiles[]": [str(source)],
        "randomizePhoto": "true",
        "includeCachedInRandom": "true",
        "fitMode": "fit",
    }, mock_device_config)

    assert_valid_image(img, (800, 480))
    mock_device_config.load_env_key.assert_not_called()
    plugin._generate_with_gemini.assert_not_called()


def test_vertical_orientation(plugin, mock_device_config):
    img_path = plugin._test_upload_dir / "source.png"
    _create_test_image(img_path)
    mock_device_config.load_env_key.return_value = "gemini-key"
    mock_device_config.get_config.side_effect = lambda key=None, default=None: (
        "vertical" if key == "orientation" else default
    )
    plugin._generate_with_gemini = MagicMock(return_value=Image.new("RGB", (360, 640), "green"))

    img = plugin.generate_image({"imageFiles[]": [str(img_path)]}, mock_device_config)

    assert_valid_image(img, (480, 800))


def test_cleanup_only_removes_plugin_uploads(plugin, tmp_path):
    inside = plugin._test_upload_dir / "inside.png"
    outside = tmp_path / "outside.png"
    _create_test_image(inside)
    _create_test_image(outside)

    plugin.cleanup({"imageFiles[]": [str(inside), str(outside)]})

    assert not inside.exists()
    assert outside.exists()


def test_download_cached_images_zip(client, monkeypatch, tmp_path):
    cached_dir = tmp_path / "cached"
    cached_dir.mkdir()
    _create_test_image(cached_dir / "first.png")
    _create_test_image(cached_dir / "second.jpg")
    (cached_dir / "notes.txt").write_text("not an image\n", encoding="utf-8")

    monkeypatch.setattr(
        "blueprints.plugin._ai_photo_stylist_cached_dir",
        lambda: str(cached_dir),
    )

    resp = client.get("/plugin/ai_photo_stylist/download_cached")

    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    with zipfile.ZipFile(BytesIO(resp.data)) as zf:
        assert sorted(zf.namelist()) == ["first.png", "second.jpg"]


def test_delete_cached_image_does_not_update_upload_settings(client, monkeypatch, mock_device_config, tmp_path):
    upload_dir = tmp_path / "uploads"
    cached_dir = tmp_path / "cached"
    upload_dir.mkdir()
    cached_dir.mkdir()
    cached_file = cached_dir / "cached.png"
    _create_test_image(cached_file)

    monkeypatch.setattr(
        "blueprints.plugin._ai_photo_stylist_upload_dir",
        lambda: str(upload_dir),
    )
    monkeypatch.setattr(
        "blueprints.plugin._ai_photo_stylist_cached_dir",
        lambda: str(cached_dir),
    )

    resp = client.post(
        "/plugin/ai_photo_stylist/delete_image",
        json={"file_path": str(cached_file)},
    )

    assert resp.status_code == 200
    assert not cached_file.exists()
    mock_device_config.update_value.assert_not_called()
