"""
cookies.py — Utilities for parsing and formatting cookies for use with yt-dlp.
"""
import logging
import uuid
import contextlib
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("dma-engine")


def parse_cookie_header(cookie_header_str: str) -> dict[str, str]:
    """Parse raw client-side Cookie header (name1=value1; name2=value2) into a dict."""
    cookies = {}
    for part in cookie_header_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, val = part.split("=", 1)
            cookies[name.strip()] = val.strip()
    return cookies


def get_cookie_domains(url: str) -> list[str]:
    """Generate target cookie domains for a given URL to cover all subdomains and parents."""
    parsed = urlparse(url)
    host = parsed.netloc or parsed.hostname or ""
    if not host:
        return []
    host = host.split(":")[0]  # strip port

    domains = [host]
    if not host.startswith("."):
        domains.append("." + host)

    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        parent = "." + ".".join(parts[i:])
        if parent not in domains:
            domains.append(parent)
        parent_no_dot = ".".join(parts[i:])
        if parent_no_dot not in domains:
            domains.append(parent_no_dot)
    return domains


def generate_netscape_cookies(cookie_str: str, url: str) -> str:
    """Generate Netscape cookie file content from Cookie header and URL."""
    cookies = parse_cookie_header(cookie_str)
    domains = get_cookie_domains(url)

    lines = [
        "# Netscape HTTP Cookie File",
        "# http://curl.haxx.se/rfc/cookie_spec.html",
        "# This is a generated file! Do not edit.",
        "",
    ]

    for domain in domains:
        for name, value in cookies.items():
            # fields: domain, flag, path, secure, expiration, name, value
            # Using far future timestamp 2082758400 (2035-12-31) to prevent expiration issues.
            domain_flag = "TRUE" if domain.startswith(".") else "FALSE"
            lines.append(f"{domain}\t{domain_flag}\t/\tTRUE\t2082758400\t{name}\t{value}")

    return "\n".join(lines)


def extract_cookie_header(headers: dict[str, str] | None) -> str | None:
    """Case-insensitive extraction of Cookie header value."""
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == "cookie":
            return v
    return None


@contextlib.contextmanager
def cookies_context(headers: dict[str, str] | None, url: str):
    """Context manager that writes cookies to a temporary file if present,
    yielding the Path to the temporary file, and cleans it up on exit.
    """
    from engine.config import TMP_DIR

    cookie_str = extract_cookie_header(headers)
    if not cookie_str:
        yield None
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = TMP_DIR / f"temp_cookies_{uuid.uuid4().hex}.txt"
    prepared = False
    try:
        content = generate_netscape_cookies(cookie_str, url)
        cookie_file.write_text(content, encoding="utf-8")
        prepared = True
    except Exception as exc:
        logger.error("Failed to generate or write temporary cookies file: %s", exc)

    try:
        yield cookie_file if prepared else None
    finally:
        if cookie_file.exists():
            try:
                cookie_file.unlink()
            except OSError as exc:
                logger.warning("Could not delete temporary cookies file %s: %s", cookie_file, exc)
