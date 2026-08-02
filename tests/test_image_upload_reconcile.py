"""image_upload's settings list has to stay in step with the saved-images dir.

_reconcile_with_disk() only ever added, so a path whose file vanished stayed in
the list and every render raised on it — permanently, because nothing removed
it.
"""

import pytest

from plugins.image_upload.image_upload import ImageUpload


@pytest.fixture
def saved_dir(tmp_path, monkeypatch):
    """Point the plugin's saved-images directory at a temp dir.

    The plugin derives it from __file__, so the walk-up is patched rather than
    the constant — there isn't one.
    """
    import plugins.image_upload.image_upload as module

    directory = tmp_path / "saved"
    directory.mkdir()

    real_join = module.os.path.join

    def fake_join(*parts):
        if parts[-3:] == ("static", "images", "saved"):
            return str(directory)
        return real_join(*parts)

    monkeypatch.setattr(module.os.path, "join", fake_join)
    return directory


def _plugin():
    return ImageUpload({"id": "image_upload", "class": "ImageUpload"})


def _write(directory, name):
    path = directory / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


def test_a_vanished_file_is_dropped(saved_dir):
    kept = _write(saved_dir, "kept.png")
    gone = str(saved_dir / "gone.png")  # never created
    settings = {"imageFiles[]": [kept, gone]}

    _plugin()._reconcile_with_disk(settings)

    assert settings["imageFiles[]"] == [kept]


def test_a_file_on_disk_but_not_listed_is_added(saved_dir):
    """The recovery behaviour this function already had must survive."""
    listed = _write(saved_dir, "listed.png")
    stray = _write(saved_dir, "stray.png")
    settings = {"imageFiles[]": [listed]}

    _plugin()._reconcile_with_disk(settings)

    assert sorted(settings["imageFiles[]"]) == sorted([listed, stray])


def test_a_deliberately_removed_file_is_not_re_added(saved_dir):
    """Removing one in the UI must not be undone by reconciliation."""
    kept = _write(saved_dir, "kept.png")
    removed = _write(saved_dir, "removed.png")
    settings = {"imageFiles[]": [kept], "_previous_files": [kept, removed]}

    _plugin()._reconcile_with_disk(settings)

    assert settings["imageFiles[]"] == [kept]


def test_a_missing_saved_dir_leaves_the_list_alone(saved_dir):
    """A temporarily absent mount must not read as 'the user deleted everything'."""
    listed = _write(saved_dir, "listed.png")
    settings = {"imageFiles[]": [listed]}
    saved_dir.joinpath("listed.png").unlink()
    saved_dir.rmdir()

    _plugin()._reconcile_with_disk(settings)

    assert settings["imageFiles[]"] == [listed]


def test_an_empty_list_is_not_an_error(saved_dir):
    settings = {}
    _plugin()._reconcile_with_disk(settings)
    assert settings.get("imageFiles[]", []) == []
