"""Multi-source title/filename resolver for stream and direct media URLs."""

from __future__ import annotations

import html
import orjson
import mimetypes
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Any

from app.utils.http import build_headers, create_ssl_context, is_safe_url
from app.utils.logger import get_logger, redact_url

logger = get_logger(__name__)


# Path tokens that strongly suggest a non-HTML resource (stream manifests etc.).
# Used only to decide whether to fetch HTML metadata from a URL — not an allowlist.
_NON_HTML_PATH_TOKENS = (
    "/manifest",
    ".m3u8",
    ".mpd",
    ".ism",
    ".f4m",
)

# Trailing file extension pattern (2–8 alphanumerics). Used to strip any ext
# from titles — not a curated type map.
_TRAILING_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,8})$")

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
    "loading",
    "loader",
    "chunklist",
    "chunk",
    "segment",
    "init",
    "video_1",
    # Common CDN/script handler names that should never become titles.
    "remote_control",
    "remote",
    "control",
    "handler",
    "proxy",
    "gateway",
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
        data = orjson.loads(script_text)
    except orjson.JSONDecodeError:
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


def _strip_trailing_extension(name: str) -> str:
    """Remove trailing file extension(s) so they can be re-added from MIME/filename.

    Strips at most two trailing extensions (e.g. archive.tar.gz → archive).
    """
    if not name:
        return name
    name = name.rstrip().rstrip(".")
    for _ in range(2):
        match = _TRAILING_EXT_RE.search(name)
        if not match:
            break
        base = name[: match.start()]
        if not base:
            break
        name = base.rstrip(".")
    return name


def _extract_trailing_extension(name: str) -> str | None:
    """Return the last segment after a dot if it looks like a real extension."""
    if not name or "." not in name:
        return None
    base, ext = name.rsplit(".", 1)
    if not base or not ext:
        return None
    # Reject spaces / very long "extensions".
    if " " in ext or len(ext) > 8 or not re.fullmatch(r"[A-Za-z0-9]+", ext):
        return None
    return ext


def _extract_basename(name: str) -> str:
    """Return the last path segment of a filename, discarding directories/queries/fragments."""
    if not name:
        return ""
    # Use urllib.parse for URLs, or fall back to path splitting.
    try:
        parsed = urllib.parse.urlparse(name)
        if parsed.path:
            path = parsed.path
        else:
            # No scheme/path — treat as a bare filename or Windows path.
            path = name.replace("\\", "/")
    except Exception:
        path = name.replace("\\", "/")
    return path.split("/")[-1].split("?")[0].split("#")[0]


# Explicit MIME → extension overrides for common/ambiguous types.
# Values are extension strings *without* the leading dot.
COMMON_MIME_OVERRIDES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "application/vnd.apple.mpegurl": "m3u8",
    "application/x-mpegurl": "m3u8",
    "application/dash+xml": "mpd",
    "application/x-apple-diskimage": "dmg",
    "application/x-msi": "msi",
    "application/x-msdownload": "exe",
    "application/x-debian-package": "deb",
    "application/x-redhat-package-manager": "rpm",
    "application/vnd.android.package-archive": "apk",
    "application/x-7z-compressed": "7z",
    "application/x-rar-compressed": "rar",
    "application/vnd.rar": "rar",
}

# Generic binary types that tell us nothing about the real extension.
# Skip mimetypes.guess_extension for these and trust filename/URL instead.
_GENERIC_BINARY_MIMES = frozenset({
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
    "application/download",
})


def _mime_to_extension(mime: str) -> str | None:
    """Return a clean extension for a MIME type, with overrides and normalization."""
    clean = mime.split(";", 1)[0].strip().lower()
    if clean in _GENERIC_BINARY_MIMES:
        return None
    if clean in COMMON_MIME_OVERRIDES:
        return COMMON_MIME_OVERRIDES[clean]
    guessed = mimetypes.guess_extension(clean, strict=False)
    if not guessed:
        return None
    ext = guessed.lstrip(".").lower()
    # Normalize a few common mimetypes quirks.
    if ext == "jpe":
        ext = "jpg"
    return ext


def guess_file_extension(
    filename: str | None,
    mime: str | None,
    url: str,
) -> str | None:
    """
    Determine a file extension for a direct download.

    Order of preference:
      1. Explicit filename extension.
      2. MIME type → extension (overrides + stdlib mimetypes).
      3. Query parameter filename (e.g. remote_control.php?file=.../id_720p.mp4).
      4. URL path extension.
    No curated extension allowlist — any plausible trailing token is accepted.
    """
    if filename:
        ext = _extract_trailing_extension(_extract_basename(filename))
        if ext:
            return ext.lower()

    if mime:
        ext = _mime_to_extension(mime)
        if ext:
            return ext

    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("file", "filename", "path", "media", "url", "src", "video"):
            for val in query.get(key, []):
                if not val:
                    continue
                decoded = urllib.parse.unquote(val)
                part = (
                    decoded.split("/")[-1]
                    .split("\\")[-1]
                    .split("?")[0]
                    .split("#")[0]
                )
                ext = _extract_trailing_extension(part)
                if ext:
                    return ext.lower()

        path = urllib.parse.unquote(parsed.path)
        basename = path.split("/")[-1].split("?")[0].split("#")[0]
        ext = _extract_trailing_extension(basename)
        if ext:
            return ext.lower()
    except Exception:
        pass

    return None


# Back-compat alias used by older call sites.
guess_media_extension = guess_file_extension
_strip_media_extension = _strip_trailing_extension


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


def _is_unusable_stem(title: str) -> bool:
    """Reject placeholders, bare URLs, stream segment names, and opaque IDs."""
    if not title or not title.strip():
        return True

    lowered = title.strip().lower()
    if lowered in _GARBAGE_TITLES:
        return True

    if lowered.startswith("http://") or lowered.startswith("https://"):
        return True

    # Reject segment/init names like index-v1-a1, init-1, segment-42, etc.
    if _SEGMENT_NAME_RE.match(lowered):
        return True

    lowered_stripped = _strip_trailing_extension(lowered)

    # Reject generic database IDs (numeric strings of length >= 8).
    # Some sites prefix ids with a leading minus (e.g. beeg.com URLs).
    numeric_stripped = lowered_stripped.lstrip("-")
    if numeric_stripped.isdigit() and len(numeric_stripped) >= 8:
        return True

    # Reject hex-like/hash-like titles of length >= 12.
    if re.match(r"^[0-9a-f]{12,}$", numeric_stripped):
        return True

    # Reject numeric-id based filenames like "586907_720p", "12345_hd".
    if re.match(r"^\d{3,}[-_](720p|1080p|480p|360p|240p|720|1080|hd|sd|fullhd|fhd|uhd|4k)$", lowered_stripped, re.IGNORECASE):
        return True
    # Reject bare resolution tokens like "720p" or "1080p".
    if re.match(r"^\d{3,}p$", lowered_stripped):
        return True

    return False


def _url_basenames(url: str) -> set[str]:
    """Return candidate basenames derived from a URL's path and query params.

    Some CDNs serve media through a handler like ``remote_control.php`` and put
    the real filename in a query parameter (``file=.../586907_720p.mp4``).
    """
    basenames: set[str] = set()
    if not url:
        return basenames
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path or "")
        basename = path.split("/")[-1]
        if basename:
            basenames.add(basename.lower())
            basenames.add(_strip_trailing_extension(basename).lower())

        query = urllib.parse.parse_qs(parsed.query)
        for key in ("file", "filename", "path", "media", "url", "src", "video"):
            for val in query.get(key, []):
                if not val:
                    continue
                decoded = urllib.parse.unquote(val)
                # The value may itself be a URL or a path.
                part = (
                    decoded.split("/")[-1]
                    .split("\\")[-1]
                    .split("?")[0]
                    .split("#")[0]
                )
                if part:
                    basenames.add(part.lower())
                    basenames.add(_strip_trailing_extension(part).lower())
    except Exception:
        pass
    return basenames


def _is_garbage_title(title: str, url: str) -> bool:
    """Reject page/network titles that are hostnames or bare CDN path basenames.

    Explicit browser/filename hints use ``_is_unusable_stem`` only — matching the
    URL basename is normal and desirable for direct downloads.
    """
    if _is_unusable_stem(title):
        return True

    lowered = title.strip().lower()

    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.netloc.lower().lstrip("www.")
        if lowered == hostname or lowered == parsed.netloc.lower():
            return True
        host_first = hostname.split(".")[0]
        if host_first and lowered == host_first:
            return True

        # Reject titles that are exactly the URL's own basename, including
        # filenames hidden in query parameters (e.g. remote_control.php?file=.../id_720p.mp4).
        if lowered in _url_basenames(url):
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
    if not is_safe_url(url):
        return None
    headers = build_headers(referer, accept="*/*")
    ssl_ctx = create_ssl_context()
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
    if not is_safe_url(url):
        return None
    headers = build_headers(referer, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    ssl_ctx = create_ssl_context()
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
    """Derive a title from the last path segment of a URL and its query params."""
    try:
        # Prefer a basename that looks like a real media filename. Many CDN
        # handlers put the actual filename in a query parameter (file=.../id_720p.mp4).
        basenames = sorted(_url_basenames(url), key=len, reverse=True)
        for b in basenames:
            stripped = _strip_trailing_extension(b)
            if stripped and not _is_unusable_stem(stripped):
                return stripped
        return None
    except Exception:
        return None


def _looks_like_binary_url(url: str) -> bool:
    """Heuristic to avoid fetching stream manifests / binary URLs as HTML pages."""
    lower = url.lower()
    try:
        parsed = urllib.parse.urlparse(lower)
        path = parsed.path or ""
    except Exception:
        path = lower
    if any(token in path or lower.endswith(token) for token in _NON_HTML_PATH_TOKENS):
        return True
    # If the last path segment has a file extension, treat as non-HTML.
    basename = path.rsplit("/", 1)[-1]
    return bool(_extract_trailing_extension(basename))


def _clean_page_title(page_title: str, url: str | None = None) -> str | None:
    """Extract the media title from a browser tab/page title.

    Many sites format tabs as ``Performer | Title | Site`` or ``Title - Site``.
    This strips the site/hostname segment and returns the most likely media title.
    """
    if not page_title:
        return None
    title = page_title.strip()
    if not title:
        return None

    hostname = None
    host_first = None
    if url:
        try:
            hostname = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
            host_first = hostname.split(".")[0]
        except Exception:
            pass

    # Common separators used to combine title with performer/site.
    separators = (" | ", " - ", " – ", " — ", " // ")
    for sep in separators:
        if sep not in title:
            continue
        parts = [p.strip() for p in title.split(sep)]
        filtered = []
        for p in parts:
            lowered = p.lower()
            if hostname and lowered == hostname:
                continue
            if host_first and lowered == host_first:
                continue
            if _is_unusable_stem(p):
                continue
            filtered.append(p)
        if filtered:
            # Prefer the longest remaining segment; in "Performer | Title | Site"
            # arrangements the actual media title is usually the longest.
            filtered.sort(key=len, reverse=True)
            candidate = sanitize_title(filtered[0])
            if candidate and not _is_garbage_title(candidate, url or ""):
                return candidate

    # No known separator; sanitize the whole thing and reject pure site names.
    candidate = sanitize_title(title)
    if candidate:
        if host_first and candidate.lower() == host_first:
            return None
        if not _is_garbage_title(candidate, url or ""):
            return candidate
    return None


def _title_contains_url_id(title: str, url: str) -> bool:
    """Return True when ``title`` contains the long numeric id from ``url``."""
    if not title or not url:
        return False
    try:
        path = urllib.parse.urlparse(url).path
        part = path.split("/")[-1]
        if not part:
            return False
        stem = _strip_trailing_extension(part).lstrip("-")
        if stem.isdigit() and len(stem) >= 8:
            return stem in title
    except Exception:
        pass
    return False


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
                    candidate = sanitize_title(_strip_trailing_extension(filename), max_length)
                    if candidate and not _is_garbage_title(candidate, url):
                        return candidate
    except Exception as e:
        logger.debug(f"Title Content-Disposition lookup failed for {redact_url(url)}: {e}")

    # 2. Page metadata. Prefer the referer; if absent, try the URL itself only when
    #    it looks like it may be an HTML page. Validate the extracted title against
    #    the page URL, not the media URL, so a site-only title like "Beeg" is
    #    rejected when it comes from the referer page.
    page_url = referer or url
    if page_url and not _looks_like_binary_url(page_url):
        try:
            html_text = _fetch_html(page_url, referer, timeout, max_html_bytes)
            if html_text:
                candidate = _extract_title_from_html(html_text, page_url)
                if candidate:
                    candidate = sanitize_title(candidate, max_length)
                    if candidate and not _is_garbage_title(candidate, page_url):
                        return candidate
        except Exception as e:
            logger.debug(f"Title HTML lookup failed for {redact_url(page_url)}: {e}")

    # 3. Browser / page title.
    if page_title:
        candidate = _clean_page_title(page_title, referer or url)
        if candidate:
            return candidate

    # 4. URL basename.
    # Use _is_unusable_stem here (not _is_garbage_title): a title derived from
    # the URL's own basename will always match the basename, so the broader
    # _is_garbage_title check would reject every legitimate fallback.
    url_title = _extract_from_url(url)
    if url_title:
        candidate = sanitize_title(url_title, max_length)
        if candidate and not _is_unusable_stem(candidate):
            return candidate

    return "video"


def resolve_filename(
    url: str,
    filename: str | None = None,
    mime: str | None = None,
    referer: str | None = None,
    page_title: str | None = None,
    preferred_ext: str | None = None,
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
      2. Network sources (Content-Disposition, HTML metadata, URL basename)
         (only if ``allow_network`` is True).
      3. A provided page/tab title (cleaned and used as a fallback).
      4. Fallback "video".

    Extension order:
      1. preferred_ext (format chosen by the client, e.g. mp4/mkv).
      2. Extension from filename / MIME / URL path.
    """
    source = "fallback"
    title: str | None = None

    # 1. Provided filename hint.
    # Use milder validation — matching the URL basename is fine for direct files.
    if filename:
        basename = _extract_basename(filename)
        candidate = sanitize_title(_strip_trailing_extension(basename), max_length)
        if candidate and not _is_unusable_stem(candidate):
            title = candidate
            source = "filename"

    # 2. Network sources (page-extracted title, Content-Disposition, URL basename).
    if title is None and allow_network:
        title = resolve_title(
            url,
            referer=referer,
            page_title=None,
            timeout=timeout,
            max_length=max_length,
        )
        if title != "video":
            source = "network"
        else:
            title = None

    # 3. Browser / page title.
    if title is None and page_title:
        candidate = _clean_page_title(page_title, referer or url)
        if candidate:
            title = candidate
            source = "page_title"

    # Always fall back to a safe title.
    title = title or "video"

    # Extension: preferred_ext (chosen format) wins, then filename/MIME/URL.
    ext: str | None = None
    if preferred_ext:
        clean_pref = preferred_ext.lstrip(".").strip().lower()
        if clean_pref and re.fullmatch(r"[a-z0-9]{1,8}", clean_pref):
            ext = clean_pref
    if not ext:
        ext = guess_file_extension(filename, mime, url)

    full_filename = f"{title}.{ext}" if ext else title
    return ResolvedFilename(
        title=title,
        extension=ext,
        filename=full_filename,
        source=source,
    )
