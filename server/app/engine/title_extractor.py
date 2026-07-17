"""Multi-source title/filename resolver for stream and direct media URLs."""

from __future__ import annotations

import html
import json
import mimetypes
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Any

from app.utils.logger import logger, redact_url

# Known media/container extensions we strip from candidate titles.
_MEDIA_EXTENSIONS = {
    "mp4",
    "m4v",
    "mkv",
    "webm",
    "mov",
    "avi",
    "flv",
    "wmv",
    "mpg",
    "mpeg",
    "m2ts",
    "ts",
    "m3u8",
    "mpd",
    "ism",
    "f4m",
    "m4s",
    "mp3",
    "m4a",
    "aac",
    "wav",
    "flac",
    "ogg",
    "opus",
    "wma",
}

# Substrings that make a URL/path look like media rather than an HTML page.
_MEDIA_URL_INDICATORS = (
    "/manifest",
    ".m3u8",
    ".mpd",
    ".ism",
    ".f4m",
    ".m4s",
    ".ts",
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".mov",
    ".m4a",
    ".mp3",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
    ".avi",
    ".flv",
    ".wmv",
    ".mpg",
    ".mpeg",
)

_GARBAGE_TITLES = {
    "",
    "video",
    "unknown",
    "no title",
    "untitled",
    "null",
    "none",
    "download",
    "downloaded",
    "downloaded_file",
    "media",
    "file",
    "movie",
    "stream",
    "index",
    "playlist",
    "manifest",
    "master",
    "chunklist",
    "chunk",
    "segment",
    "init",
    "video_1",
}

# Common segment/init filenames produced by HLS/DASH packagers (e.g. index-v1-a1.m4s).
_SEGMENT_NAME_RE = re.compile(
    r"^(index|init|segment|chunk|media|chunklist|seg)([-_]v\d+([-_]a\d+)?|[-_]\d+)?$",
    re.IGNORECASE,
)


@dataclass
class ResolvedFilename:
    """Container for a fully resolved filename and its components."""

    title: str
    extension: str | None
    filename: str
    source: str


class _TitleHTMLParser(HTMLParser):
    """Extract the best human-readable title from an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.og_title: str | None = None
        self.og_video_title: str | None = None
        self.twitter_title: str | None = None
        self.jsonld_name: str | None = None

        self._in_title = False
        self._in_ld_json = False
        self._title_chunks: list[str] = []
        self._script_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        lowered = tag.lower()

        if lowered == "title":
            self._in_title = True
            self._title_chunks = []
            return

        if lowered == "script" and attr_dict.get("type", "").lower() == "application/ld+json":
            self._in_ld_json = True
            self._script_chunks = []
            return

        if lowered == "meta":
            prop = attr_dict.get("property", "").lower()
            name = attr_dict.get("name", "").lower()
            content = html.unescape(attr_dict.get("content", "")).strip()
            if not content:
                return
            if prop == "og:title":
                self.og_title = content
            elif prop == "og:video:title":
                self.og_video_title = content
            elif prop in ("twitter:title", "twitter:label1") or name == "twitter:title":
                self.twitter_title = content

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Treat self-closing tags the same as regular start tags.
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)
        elif self._in_ld_json:
            self._script_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title" and self._in_title:
            self._in_title = False
            raw = "".join(self._title_chunks)
            self.title = html.unescape(raw).strip()
        elif lowered == "script" and self._in_ld_json:
            self._in_ld_json = False
            raw = "".join(self._script_chunks)
            if raw:
                self.jsonld_name = _extract_jsonld_name(raw)

    def best_title(self) -> str | None:
        for candidate in (
            self.og_title,
            self.og_video_title,
            self.twitter_title,
            self.jsonld_name,
            self.title,
        ):
            if candidate and candidate.strip():
                return candidate.strip()
        return None


def _extract_jsonld_name(script_text: str) -> str | None:
    """Pull a name/headline from JSON-LD, preferring video-like objects."""
    try:
        data = json.loads(script_text)
    except json.JSONDecodeError:
        return None

    video_types = {"videoobject", "movie", "mediaobject", "tvseries", "tvepisode"}

    def pick_name(obj: Any) -> str | None:
        if not isinstance(obj, dict):
            return None
        # Prefer explicit video-like entries in @graph.
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if not isinstance(item, dict):
                    continue
                type_val = (item.get("@type") or "").lower()
                if type_val in video_types or type_val == "webpage":
                    for key in ("name", "headline"):
                        val = item.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
        # Top-level name/headline.
        type_val = (obj.get("@type") or "").lower()
        for key in ("name", "headline"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # If this is a video object itself, return its name.
        if type_val in video_types:
            return None
        return None

    if isinstance(data, list):
        for item in data:
            name = pick_name(item)
            if name:
                return name
    else:
        name = pick_name(data)
        if name:
            return name

    return None


def _create_ssl_context() -> ssl.SSLContext:
    """Return a verifying SSL context with legacy-renegotiation workaround."""
    context = ssl.create_default_context()
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return context


def _build_headers(referer: str | None, accept: str = "*/*") -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _parse_content_disposition(header: str) -> str | None:
    """Return the filename* or filename parameter from a Content-Disposition header."""
    if not header:
        return None
    try:
        msg = Message()
        msg["content-disposition"] = header
        filename = msg.get_filename()
        if filename:
            # RFC 5987/2231 may leave percent-encoding; decode it.
            return urllib.parse.unquote(filename).strip()
    except Exception:
        pass
    return None


def _strip_media_extension(name: str) -> str:
    """Remove trailing known media extensions recursively so they can be re-added by yt-dlp."""
    if not name:
        return name
    name = name.rstrip().rstrip(".")
    # Match extension case-insensitively.
    pattern = r"\.(" + "|".join(re.escape(ext) for ext in _MEDIA_EXTENSIONS) + r")$"
    while True:
        stripped = re.sub(pattern, "", name, flags=re.IGNORECASE)
        if stripped == name:
            break
        name = stripped.rstrip(".")
    return name


def _extract_trailing_extension(name: str) -> str | None:
    """Return the last segment after a dot if it looks like a real extension."""
    if not name or "." not in name:
        return None
    base, ext = name.rsplit(".", 1)
    if not base or not ext:
        return None
    return ext


def _extract_basename(name: str) -> str:
    """Return the last path segment of a filename, discarding directories/queries."""
    if not name:
        return ""
    # Normalize Windows and URL separators to a common split character.
    normalized = name.replace("\\", "/")
    basename = normalized.split("/")[-1]
    # Drop query/fragment if somehow present.
    return basename.split("?")[0].split("#")[0]


def guess_media_extension(
    filename: str | None,
    mime: str | None,
    url: str,
) -> str | None:
    """
    Determine a safe media extension for a direct download.

    Order of preference:
      1. Explicit filename extension (if it is a known media type).
      2. MIME type mapping (if it maps to a known media extension).
      3. URL path extension.
    """
    if filename:
        ext = _extract_trailing_extension(filename)
        if ext and ext.lower() in _MEDIA_EXTENSIONS:
            return ext.lower()

    if mime:
        # mimetypes may return None or a generic extension like .bin; reject generics.
        clean_mime = mime.split(";")[0].strip().lower()
        guessed = mimetypes.guess_extension(clean_mime)
        if guessed:
            ext = guessed.lstrip(".").lower()
            if ext in _MEDIA_EXTENSIONS:
                return ext

    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        basename = path.split("/")[-1].split("?")[0].split("#")[0]
        ext = _extract_trailing_extension(basename)
        if ext and ext.lower() in _MEDIA_EXTENSIONS:
            return ext.lower()
    except Exception:
        pass

    return None


def sanitize_title(title: str, max_length: int = 120) -> str:
    """Make a string safe to use as a filename base (no extension handling)."""
    if not title:
        return ""

    # Normalize unicode and decode HTML entities.
    title = unicodedata.normalize("NFKC", title)
    title = html.unescape(title)

    # Drop control characters.
    title = "".join(ch for ch in title if ord(ch) >= 32 and ord(ch) != 127)

    # Replace filesystem-forbidden characters with a safe fullwidth equivalent or underscore.
    mappings = {
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
    for char, replacement in mappings.items():
        title = title.replace(char, replacement)

    # Collapse whitespace and trim.
    title = re.sub(r"\s+", " ", title)
    title = title.strip()
    title = title.rstrip(". ")

    if not title:
        return ""

    # Truncate on word boundary.
    if len(title) > max_length:
        truncated = title[:max_length].rsplit(" ", 1)[0].rstrip()
        title = truncated if truncated else title[:max_length]

    return title


def _is_garbage_title(title: str, url: str) -> bool:
    """Reject placeholders, URLs, hostnames, raw filenames, and stream segments."""
    if not title or not title.strip():
        return True

    lowered = title.strip().lower()
    if lowered in _GARBAGE_TITLES:
        return True

    if lowered in _MEDIA_EXTENSIONS:
        return True

    if lowered.startswith("http://") or lowered.startswith("https://"):
        return True

    # Reject segment/init names like index-v1-a1, init-1, segment-42, etc.
    if _SEGMENT_NAME_RE.match(lowered):
        return True

    # Strip known extensions recursively before checking for digit/hex garbage patterns
    lowered_stripped = _strip_media_extension(lowered)

    # Reject generic database IDs (numeric strings of length >= 8)
    if lowered_stripped.isdigit() and len(lowered_stripped) >= 8:
        return True

    # Reject hex-like/hash-like titles of length >= 12
    if re.match(r"^[0-9a-f]{12,}$", lowered_stripped):
        return True

    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.netloc.lower().lstrip("www.")
        if lowered == hostname or lowered == parsed.netloc.lower():
            return True

        path = parsed.path
        basename = path.split("/")[-1]
        if lowered == path.lower() or lowered == path.lower().lstrip("/"):
            return True
        if basename and lowered == basename.lower():
            return True
        # Reject if it is exactly the URL basename with or without extension.
        basename_lower = basename.lower()
        for ext in _MEDIA_EXTENSIONS:
            if lowered == f"{basename_lower.rsplit('.', 1)[0]}.{ext}".lower():
                return True
    except Exception:
        pass

    return False


def _fetch_headers(
    url: str,
    referer: str | None,
    timeout: float,
) -> urllib.error.HTTPMessage | None:
    """Fetch response headers, falling back from HEAD to a tiny GET on 405."""
    headers = _build_headers(referer, accept="*/*")
    ssl_ctx = _create_ssl_context()
    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return resp.headers
    except urllib.error.HTTPError as e:
        # Some CDNs/servers reject HEAD. Try a minimal GET with a byte range.
        if e.code == 405:
            try:
                headers["Range"] = "bytes=0-0"
                req = urllib.request.Request(url, method="GET", headers=headers)
                with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                    return resp.headers
            except Exception:
                pass
    except Exception:
        pass
    return None


def _fetch_html(
    url: str,
    referer: str | None,
    timeout: float,
    max_bytes: int,
) -> str | None:
    """Fetch the start of an HTML page and decode it."""
    headers = _build_headers(referer, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    ssl_ctx = _create_ssl_context()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("text/html"):
                return None
            data = resp.read(max_bytes)
            if not data:
                return None
            # Try common encodings; UTF-8 first (also handles missing/broken declarations).
            for encoding in ("utf-8", "utf-16", "iso-8859-1"):
                try:
                    return data.decode(encoding, errors="replace")
                except (UnicodeDecodeError, LookupError):
                    continue
    except Exception:
        pass
    return None


def _extract_from_url(url: str) -> str | None:
    """Derive a title from the last path segment of a URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        basename = path.split("/")[-1]
        if not basename:
            return None
        # Drop query/fragment if somehow present.
        basename = basename.split("?")[0].split("#")[0]
        stripped = _strip_media_extension(basename)
        if not stripped:
            return None
        # If what remains is generic, treat it as garbage at the resolve step.
        return stripped
    except Exception:
        return None


def _looks_like_media_url(url: str) -> bool:
    """Heuristic to avoid fetching a binary manifest as if it were an HTML page."""
    lower = url.lower()
    parsed = urllib.parse.urlparse(lower)
    path = parsed.path
    return any(ind in path or lower.endswith(ind) for ind in _MEDIA_URL_INDICATORS)


def _extract_title_from_html(html_text: str, url: str) -> str | None:
    """Parse HTML and return the best non-garbage title."""
    if not html_text:
        return None
    try:
        parser = _TitleHTMLParser()
        parser.feed(html_text)
    except Exception:
        return None

    candidate = parser.best_title()
    if candidate and not _is_garbage_title(candidate, url):
        return candidate
    return None


def resolve_title(
    url: str,
    referer: str | None = None,
    page_title: str | None = None,
    timeout: float = 5.0,
    max_html_bytes: int = 256_000,
    max_length: int = 120,
) -> str:
    """
    Resolve a human-readable media title from multiple sources.

    Priority:
      1. Content-Disposition filename from a HEAD request to the media URL.
      2. HTML metadata from the referer page (or the URL itself if it serves HTML).
      3. Browser tab/document title passed by the extension.
      4. Sanitized last path segment of the URL.
      5. Fallback "video".
    """
    # 1. Content-Disposition.
    try:
        headers = _fetch_headers(url, referer, timeout)
        if headers:
            cd = headers.get("Content-Disposition")
            if cd:
                filename = _parse_content_disposition(cd)
                if filename:
                    candidate = sanitize_title(_strip_media_extension(filename), max_length)
                    if candidate and not _is_garbage_title(candidate, url):
                        return candidate
    except Exception as e:
        logger.debug(f"Title Content-Disposition lookup failed for {redact_url(url)}: {e}")

    # 2. Page metadata. Prefer the referer; if absent, try the URL itself only when
    #    it looks like it may be an HTML page.
    page_url = referer or url
    if page_url and not _looks_like_media_url(page_url):
        try:
            html_text = _fetch_html(page_url, referer, timeout, max_html_bytes)
            if html_text:
                candidate = _extract_title_from_html(html_text, url)
                if candidate:
                    candidate = sanitize_title(candidate, max_length)
                    if candidate and not _is_garbage_title(candidate, url):
                        return candidate
        except Exception as e:
            logger.debug(f"Title HTML lookup failed for {redact_url(page_url)}: {e}")

    # 3. Browser / page title.
    if page_title:
        candidate = sanitize_title(page_title, max_length)
        if candidate and not _is_garbage_title(candidate, url):
            return candidate

    # 4. URL basename.
    url_title = _extract_from_url(url)
    if url_title:
        candidate = sanitize_title(url_title, max_length)
        if candidate and not _is_garbage_title(candidate, url):
            return candidate

    return "video"


def resolve_filename(
    url: str,
    filename: str | None = None,
    mime: str | None = None,
    referer: str | None = None,
    page_title: str | None = None,
    timeout: float = 5.0,
    max_length: int = 120,
    allow_network: bool = True,
) -> ResolvedFilename:
    """
    Resolve a clean media filename and title from all available sources.

    This is the single entry point the rest of the backend should use.  It
    combines title resolution with safe extension guessing so callers do not
    duplicate filename extraction logic.

    Resolution order for the title:
      1. A provided filename hint (basename stripped of extension).
      2. A provided page/tab title.
      3. Network sources (Content-Disposition, HTML metadata, URL basename)
         (only if ``allow_network`` is True).
      4. Fallback "video".

    The extension is guessed from the original filename, MIME type, or URL path.
    """
    source = "fallback"
    title: str | None = None

    # 1. Provided filename hint.
    if filename:
        basename = _extract_basename(filename)
        candidate = sanitize_title(_strip_media_extension(basename), max_length)
        if candidate and not _is_garbage_title(candidate, url):
            title = candidate
            source = "filename"

    # 2. Browser / page title.
    if title is None and page_title:
        candidate = sanitize_title(page_title, max_length)
        if candidate and not _is_garbage_title(candidate, url):
            title = candidate
            source = "page_title"

    # 3. Network sources.
    if title is None and allow_network:
        title = resolve_title(
            url,
            referer=referer,
            page_title=None,
            timeout=timeout,
            max_length=max_length,
        )
        source = "network" if title != "video" else "fallback"

    # Always fall back to a safe title.
    title = title or "video"

    # Resolve the best extension for this media.
    ext = guess_media_extension(filename, mime, url)

    full_filename = f"{title}.{ext}" if ext else title
    return ResolvedFilename(
        title=title,
        extension=ext,
        filename=full_filename,
        source=source,
    )
