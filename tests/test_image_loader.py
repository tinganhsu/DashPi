"""Streamed downloads in AdaptiveImageLoader must always release their response."""

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
import requests
from PIL import Image

from utils.image_loader import AdaptiveImageLoader


class FakeResponse:
    def __init__(self, content, error=None, slow=False):
        self.content = content
        self.error = error
        self.slow = slow
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size):
        yield self.content
        if self.slow:
            yield b""


def _png_bytes():
    data = BytesIO()
    Image.new("RGB", (8, 6), "red").save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize("low_resource", [False, True])
@pytest.mark.parametrize("outcome", ["success", "http_error", "invalid_image", "deadline"])
def test_streamed_image_response_is_closed(low_resource, outcome):
    response = FakeResponse(
        b"not an image" if outcome == "invalid_image" else _png_bytes(),
        requests.HTTPError("download failed") if outcome == "http_error" else None,
        slow=outcome == "deadline",
    )
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response

    loader = AdaptiveImageLoader.__new__(AdaptiveImageLoader)
    loader.is_low_resource = low_resource
    loader.max_megapixels = 20

    # Second monotonic() read is past the deadline only in the "deadline" case.
    clock = iter([0, 10_000] if outcome == "deadline" else [0, 0, 0, 0])
    with patch("utils.image_loader._validate_url", return_value="93.184.216.34"), \
         patch("utils.image_loader.requests.Session", return_value=session), \
         patch("utils.image_loader.time.monotonic", side_effect=lambda: next(clock, 0)):
        image = loader.from_url("https://example.com/image.png", (4, 3), timeout_ms=1000)

    assert response.closed
    if outcome == "success":
        assert image.size == (4, 3)
        assert image.getpixel((0, 0)) == (255, 0, 0)
    else:
        assert image is None


class TestPrivateAddressOptIn:
    """Private/loopback targets stay blocked unless a caller explicitly opts in (Immich on a LAN)."""

    def test_private_ip_blocked_by_default(self):
        from utils.image_loader import _validate_url
        with pytest.raises(ValueError, match="blocked address"):
            _validate_url("http://192.168.1.50:2283/api/assets/x/thumbnail")

    def test_private_ip_allowed_when_opted_in(self):
        from utils.image_loader import _validate_url
        assert _validate_url("http://192.168.1.50:2283/x", allow_private=True) == "192.168.1.50"
        assert _validate_url("http://127.0.0.1:2283/x", allow_private=True) == "127.0.0.1"

    def test_opt_in_still_rejects_non_http_schemes(self):
        from utils.image_loader import _validate_url
        with pytest.raises(ValueError, match="scheme"):
            _validate_url("file:///etc/passwd", allow_private=True)

    def test_from_url_blocks_private_by_default(self):
        loader = AdaptiveImageLoader.__new__(AdaptiveImageLoader)
        loader.is_low_resource = False
        with pytest.raises(ValueError, match="blocked address"):
            loader.from_url("http://10.0.0.5/image.png", (4, 3))

    @pytest.mark.parametrize("low_resource", [False, True])
    def test_from_url_downloads_private_when_opted_in(self, low_resource):
        response = FakeResponse(_png_bytes())
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        loader = AdaptiveImageLoader.__new__(AdaptiveImageLoader)
        loader.is_low_resource = low_resource
        loader.max_megapixels = 20

        with patch("utils.image_loader.requests.Session", return_value=session):
            image = loader.from_url("http://10.0.0.5/image.png", (4, 3), allow_private=True)

        assert image.size == (4, 3)
        assert session.get.call_args.args[0] == "http://10.0.0.5/image.png"
