"""ImmichProvider: asset listing across Immich versions and preview endpoint selection."""

from unittest.mock import MagicMock, patch

import pytest

from plugins.image_album.image_album import ImmichProvider


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.__enter__.return_value = r
    if status >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return r


@pytest.fixture
def provider():
    with patch("plugins.image_album.image_album.get_http_session") as get_session:
        get_session.return_value = MagicMock()
        p = ImmichProvider("http://immich.example:2283/", "key", MagicMock())
    return p


class TestGetAssets:
    def test_requests_images_only_and_follows_pages(self, provider):
        provider.session.post.side_effect = [
            _resp(payload={"assets": {"items": [{"id": "a"}, {"id": "b"}], "nextPage": "2"}}),
            _resp(payload={"assets": {"items": [{"id": "c"}], "nextPage": None}}),
        ]
        assets = provider.get_assets("album1")

        assert [a["id"] for a in assets] == ["a", "b", "c"]
        assert provider.session.post.call_count == 2
        first_body = provider.session.post.call_args_list[0].kwargs["json"]
        assert first_body["albumIds"] == ["album1"]
        assert first_body["type"] == "IMAGE"
        assert first_body["page"] == 1
        assert provider.session.post.call_args_list[1].kwargs["json"]["page"] == 2
        provider.session.get.assert_not_called()

    def test_without_next_page_field_stops_on_empty_page(self, provider):
        # Servers that omit nextPage: keep paging until a page comes back empty.
        provider.session.post.side_effect = [
            _resp(payload={"assets": {"items": [{"id": "a"}]}}),
            _resp(payload={"assets": {"items": []}}),
        ]
        assert [a["id"] for a in provider.get_assets("album1")] == ["a"]
        assert provider.session.post.call_count == 2

    def test_falls_back_to_album_detail_and_drops_videos(self, provider):
        provider.session.post.return_value = _resp(status=400, payload={})
        provider.session.get.return_value = _resp(payload={"assets": [
            {"id": "img", "type": "IMAGE"},
            {"id": "vid", "type": "VIDEO"},
        ]})

        assets = provider.get_assets("album1")

        assert [a["id"] for a in assets] == ["img"]
        url = provider.session.get.call_args.args[0]
        assert url == "http://immich.example:2283/api/albums/album1"
        assert provider.session.get.call_args.kwargs["params"] == {"withAssets": "true"}

    def test_empty_search_result_also_falls_back(self, provider):
        provider.session.post.return_value = _resp(payload={"assets": {"items": [], "nextPage": None}})
        provider.session.get.return_value = _resp(payload={"assets": []})
        assert provider.get_assets("album1") == []
        provider.session.get.assert_called_once()


class TestPreviewUrl:
    def test_uses_current_preview_endpoint_and_caches_it(self, provider):
        provider.session.get.return_value = _resp(200)

        first = provider._resolve_preview_url("a1")
        second = provider._resolve_preview_url("a2")

        assert first == "http://immich.example:2283/api/assets/a1/thumbnail?size=preview"
        assert second == "http://immich.example:2283/api/assets/a2/thumbnail?size=preview"
        assert provider.session.get.call_count == 1  # probed once only
        assert provider.session.get.call_args.kwargs["stream"] is True

    def test_falls_back_to_legacy_thumbnail(self, provider):
        provider.session.get.side_effect = [_resp(400), _resp(200)]
        assert provider._resolve_preview_url("a1") == "http://immich.example:2283/api/assets/a1/thumbnail"

    def test_falls_back_to_original_when_no_preview_works(self, provider):
        provider.session.get.side_effect = [_resp(403), Exception("timeout")]
        assert provider._resolve_preview_url("a1") == "http://immich.example:2283/api/assets/a1/original"
        # Decision is cached; no further probes.
        assert provider._resolve_preview_url("a2") == "http://immich.example:2283/api/assets/a2/original"
        assert provider.session.get.call_count == 2

    def test_get_image_downloads_preview(self, provider):
        provider.session.get.side_effect = [
            _resp(payload=[{"albumName": "Family", "id": "album1"}]),  # /api/albums
            _resp(200),                                                # preview probe
        ]
        provider.session.post.return_value = _resp(payload={"assets": {"items": [{"id": "a1"}], "nextPage": None}})
        provider.image_loader.from_url.return_value = MagicMock(size=(800, 480))

        img = provider.get_image("Family", (800, 480))

        assert img is not None
        url = provider.image_loader.from_url.call_args.args[0]
        assert url == "http://immich.example:2283/api/assets/a1/thumbnail?size=preview"
        assert provider.image_loader.from_url.call_args.kwargs["headers"] == {"x-api-key": "key"}


def test_immich_download_allows_lan_server(provider):
    """Self-hosted Immich usually lives on a private address; the loader must be told that is OK."""
    provider.session.get.side_effect = [
        _resp(payload=[{"albumName": "Family", "id": "album1"}]),
        _resp(200),
    ]
    provider.session.post.return_value = _resp(payload={"assets": {"items": [{"id": "a1"}], "nextPage": None}})
    provider.image_loader.from_url.return_value = MagicMock(size=(800, 480))

    provider.get_image("Family", (800, 480))

    assert provider.image_loader.from_url.call_args.kwargs["allow_private"] is True
