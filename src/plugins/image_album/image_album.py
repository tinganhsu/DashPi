"""Image Album plugin — displays photos from an Immich or Google Photos album."""

import logging
from random import choice

from PIL import Image, ImageColor, ImageOps
from utils.http_client import get_http_session
from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import pad_image_blur

logger = logging.getLogger(__name__)


class ImmichProvider:
    """Fetches album photos from a self-hosted Immich server."""

    # Ordered from preferred to last resort. The first URL that answers 200 is
    # cached in _preview_url_template so the probe cost is paid once per
    # provider. Previews are server-resized JPEGs, far smaller than originals
    # (which can be 50MP+ RAW/HEIC files that a Pi Zero cannot decode).
    #   GET /api/assets/{id}/thumbnail?size=preview   (current Immich)
    #   GET /api/assets/{id}/thumbnail                (older, preview by default)
    _PREVIEW_URL_TEMPLATES = [
        "{base}/api/assets/{id}/thumbnail?size=preview",
        "{base}/api/assets/{id}/thumbnail",
    ]
    _ORIGINAL_URL_TEMPLATE = "{base}/api/assets/{id}/original"

    def __init__(self, base_url: str, key: str, image_loader):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.headers = {"x-api-key": self.key}
        self.image_loader = image_loader
        self.session = get_http_session()
        self._preview_url_template: str | None = None

    def _resolve_preview_url(self, asset_id: str) -> str:
        """Return the best available preview URL for *asset_id*, probing once."""
        if self._preview_url_template is None:
            for template in self._PREVIEW_URL_TEMPLATES:
                url = template.format(base=self.base_url, id=asset_id)
                try:
                    # Streaming GET reads only the status line and headers;
                    # Immich does not reliably answer HEAD on these endpoints.
                    with self.session.get(url, headers=self.headers, timeout=10, stream=True) as r:
                        status = r.status_code
                except Exception as e:
                    logger.debug(f"Immich preview probe {url} failed: {e}, trying next")
                    continue
                if status == 200:
                    self._preview_url_template = template
                    logger.info(f"Immich preview endpoint resolved: {template}")
                    return url
                logger.debug(f"Immich preview probe {url} -> {status}, trying next")

            logger.warning(
                "No Immich preview endpoint responded 200; falling back to /original. "
                "Check that the API key has the asset.view permission."
            )
            self._preview_url_template = self._ORIGINAL_URL_TEMPLATE

        return self._preview_url_template.format(base=self.base_url, id=asset_id)

    def get_album_id(self, album: str) -> str:
        logger.debug(f"Fetching albums from {self.base_url}")
        r = self.session.get(f"{self.base_url}/api/albums", headers=self.headers, timeout=15)
        r.raise_for_status()
        albums = r.json()

        matching_albums = [a for a in albums if a["albumName"] == album]
        if not matching_albums:
            raise RuntimeError(f"Album '{album}' not found.")

        return matching_albums[0]["id"]

    def get_assets(self, album_id: str) -> list[dict]:
        """Fetch the image assets of an album.

        Uses POST /api/search/metadata (Immich v3 no longer embeds assets in
        GET /api/albums/{id}), paging through large albums. Videos are filtered
        out server-side since the display can only show stills. Falls back to
        the album detail endpoint for older servers where search returns nothing.
        """
        all_items = []
        page = 1
        max_pages = 100  # Safety limit to prevent infinite pagination

        logger.debug(f"Fetching image assets for album {album_id} via search/metadata")
        while page <= max_pages:
            body = {
                "albumIds": [album_id],
                "type": "IMAGE",
                "withExif": False,
                "size": 1000,
                "page": page,
            }
            r = self.session.post(f"{self.base_url}/api/search/metadata", json=body,
                                  headers=self.headers, timeout=15)
            if r.status_code != 200:
                logger.debug(f"search/metadata returned {r.status_code} on page {page}")
                break
            assets_data = (r.json() or {}).get("assets", {})
            page_items = assets_data.get("items", [])
            all_items.extend(page_items)
            if not page_items or ("nextPage" in assets_data and not assets_data["nextPage"]):
                break
            page += 1

        if all_items:
            logger.debug(f"Search API found {len(all_items)} image assets")
            return all_items

        # Fallback: older Immich versions embed assets in the album detail
        logger.debug(f"Search API empty/failed, falling back to /api/albums/{album_id}")
        r2 = self.session.get(f"{self.base_url}/api/albums/{album_id}", headers=self.headers,
                              params={"withAssets": "true"}, timeout=15)
        r2.raise_for_status()
        assets = r2.json().get("assets", []) or []
        image_assets = [a for a in assets if str(a.get("type", "")).upper() == "IMAGE"]
        logger.debug(f"Album detail found {len(image_assets)} image assets ({len(assets)} total)")
        return image_assets

    def get_image(self, album: str, dimensions: tuple[int, int], resize: bool = True, fit_mode: str = 'fill') -> Image.Image | None:
        """
        Get a random image from the album.

        Args:
            album: Album name
            dimensions: Target dimensions (width, height)
            resize: Whether to let loader resize
            fit_mode: How to fit image ('fit', 'fill')

        Returns:
            PIL Image or None on error
        """
        try:
            logger.info(f"Getting id for album '{album}'")
            album_id = self.get_album_id(album)
            logger.info(f"Getting assets from album id {album_id}")
            assets = self.get_assets(album_id)

            if not assets:
                logger.error(f"No assets found in album '{album}'")
                return None

        except Exception as e:
            logger.error(f"Error retrieving album data from {self.base_url}: {e}")
            return None

        # Select random asset
        selected_asset = choice(assets)
        asset_id = selected_asset["id"]
        asset_url = self._resolve_preview_url(asset_id)

        logger.info(f"Selected random asset: {asset_id}")
        logger.debug(f"Downloading from: {asset_url}")

        # Use adaptive image loader for memory-efficient processing
        img = self.image_loader.from_url(
            asset_url,
            dimensions,
            timeout_ms=40000,
            resize=resize,
            headers=self.headers,
            fit_mode=fit_mode,
            # Immich is self-hosted and usually on the LAN; the URL comes from
            # the owner's plugin settings, not from remote content.
            allow_private=True
        )

        if not img:
            logger.error(f"Failed to load image {asset_id} from Immich")
            return None

        logger.info(f"Successfully loaded image: {img.size[0]}x{img.size[1]}")
        return img


class ImageAlbum(BasePlugin):
    """Renders a random photo from a configured Immich or Google Photos album."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['api_key'] = {
            "required": True,
            "service": "Immich",
            "expected_key": "IMMICH_KEY"
        }
        return template_params

    def generate_image(self, settings, device_config):
        """Fetch a random photo from the configured album and render it for display."""
        logger.info("=== Image Album Plugin: Starting image generation ===")

        orientation = device_config.get_config("orientation")
        dimensions = device_config.get_resolution()

        if orientation == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(f"Vertical orientation detected, dimensions: {dimensions[0]}x{dimensions[1]}")

        img = None
        album_provider = settings.get("albumProvider")
        logger.info(f"Album provider: {album_provider}")

        # Display mode: fit (letterbox), fill (crop), or blur (blurred background)
        # Migrate old padImage setting if fitMode not set
        fit_mode = settings.get('fitMode')
        if not fit_mode:
            if settings.get('padImage') == 'true':
                fit_mode = 'blur' if settings.get('backgroundOption', 'blur') == 'blur' else 'fit'
            else:
                fit_mode = 'fill'

        logger.debug(f"Settings: fit_mode={fit_mode}")

        # For blur mode, load without resize (manual padding needed)
        use_native_resize = fit_mode in ('fit', 'fill')

        match album_provider:
            case "Immich":
                key = device_config.load_env_key("IMMICH_KEY")
                if not key:
                    logger.error("Immich API Key not configured")
                    raise RuntimeError("Immich API Key not configured.")

                url = settings.get('url')
                if not url:
                    logger.error("Immich URL not provided")
                    raise RuntimeError("Immich URL is required.")

                album = settings.get('album')
                if not album:
                    logger.error("Album name not provided")
                    raise RuntimeError("Album name is required.")

                logger.info(f"Immich URL: {url}")
                logger.info(f"Album: {album}")

                provider = ImmichProvider(url, key, self.image_loader)
                img = provider.get_image(album, dimensions, resize=use_native_resize, fit_mode=fit_mode)

                if not img:
                    logger.error("Failed to retrieve image from Immich")
                    raise RuntimeError("Failed to load image, please check logs.")
            case _:
                logger.error(f"Unknown album provider: {album_provider}")
                raise RuntimeError(f"Unsupported album provider: {album_provider}")

        if img is None:
            logger.error("Image is None after provider processing")
            raise RuntimeError("Failed to load image, please check logs.")

        # Blur mode: manual padding with blurred background
        if fit_mode == 'blur':
            logger.debug("Applying blur background padding")
            img = pad_image_blur(img, dimensions)

        logger.info("=== Image Album Plugin: Image generation complete ===")
        return img
