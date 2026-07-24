"""Shared HTTP helpers and constants used across engine modules."""

import socket
import ssl
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, Optional

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class _UnsafeRedirectError(urllib.error.HTTPError):
    """Raised when a redirect targets a non-public address."""

    def __init__(self, url: str):
        super().__init__(url, 403, "Forbidden redirect target", Message(), None)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the destination is a public address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_url(newurl):
            raise _UnsafeRedirectError(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(
    req: urllib.request.Request,
    timeout: float | None = None,
    context: ssl.SSLContext | None = None,
):
    """Open a URL while validating the origin and every redirect destination.

    Rejects non-http(s) schemes, loopback, link-local, and RFC1918 hosts.
    """
    if not is_safe_url(req.full_url):
        raise _UnsafeRedirectError(req.full_url)
    handlers: list[urllib.request.BaseHandler] = [_SafeRedirectHandler()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return opener.open(req, **kwargs)


def create_ssl_context() -> ssl.SSLContext:
    """Return a verifying SSL context with legacy-renegotiation workaround."""
    context = ssl.create_default_context()
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return context


def build_headers(referer: Optional[str], accept: str = "*/*") -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def is_safe_url(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        for res in socket.getaddrinfo(hostname, None):
            ip_obj = ipaddress.ip_address(res[4][0])
            if ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local:
                return False
        return True
    except Exception:
        return False

