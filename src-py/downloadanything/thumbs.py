"""Thumbnail fetching for the UI proxy.

Hotlink-protected CDNs 403 the webview's own Referer; the backend fetches
cover art with the page referer instead. Kept out of `app.py` so the fetch
path is testable without booting the Litestar app.
"""

import threading
from typing import Any
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from .settings import DEFAULT_UA

THUMB_MAX_BYTES = 8 * 1024 * 1024

_local = threading.local()


def _session() -> Any:
    """Per-thread curl_cffi session — a strip of thumbs shares one TLS
    connection per host instead of a handshake per image."""
    session = getattr(_local, "session", None)
    if session is None:
        from curl_cffi import requests as creq

        session = creq.Session(impersonate="chrome")
        _local.session = session
    return session


def _headers(referer: str) -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _fetch_curl(url: str, headers: dict[str, str]) -> tuple[bytes, str]:
    r = _session().get(url, headers=headers, timeout=10)
    if r.status_code >= 400:
        raise ValueError(f"http {r.status_code}")
    content_type = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        raise ValueError("not an image")
    if len(r.content) > THUMB_MAX_BYTES:
        raise ValueError("image too large")
    return r.content, content_type


def _fetch_urlopen(url: str, headers: dict[str, str]) -> tuple[bytes, str]:
    with urlopen(UrlRequest(url, headers=headers), timeout=10) as response:
        content_type = (response.headers.get_content_type() or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError("not an image")
        body = response.read(THUMB_MAX_BYTES + 1)
        if len(body) > THUMB_MAX_BYTES:
            raise ValueError("image too large")
        return body, content_type


def fetch_thumbnail(url: str, referer: str) -> tuple[bytes, str]:
    """Fetch an image, returning `(bytes, content_type)`.

    Raises for non-image responses and images over [`THUMB_MAX_BYTES`].
    """
    headers = _headers(referer)
    try:
        return _fetch_curl(url, headers)
    except ImportError:
        return _fetch_urlopen(url, headers)
