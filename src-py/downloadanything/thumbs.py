"""Thumbnail fetching for the UI proxy.

Hotlink-protected CDNs 403 the webview's own Referer; the backend fetches
cover art with the page referer instead. Kept out of `app.py` so the fetch
path is testable without booting the Litestar app.
"""

from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from .settings import DEFAULT_UA

THUMB_MAX_BYTES = 8 * 1024 * 1024


def fetch_thumbnail(url: str, referer: str) -> tuple[bytes, str]:
    """Fetch an image, returning `(bytes, content_type)`.

    Raises for non-image responses and images over [`THUMB_MAX_BYTES`].
    """
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    with urlopen(UrlRequest(url, headers=headers), timeout=10) as response:
        content_type = (response.headers.get_content_type() or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError("not an image")
        body = response.read(THUMB_MAX_BYTES + 1)
        if len(body) > THUMB_MAX_BYTES:
            raise ValueError("image too large")
        return body, content_type
