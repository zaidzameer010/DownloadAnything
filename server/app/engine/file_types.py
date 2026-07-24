"""MIME-based content type classification.

Single source of truth for high-level file types used by probe, intercept,
and UI. Classification is Content-Type driven — no extension allowlists.
"""

from __future__ import annotations

# High-level types returned by classify_mime / used as mediaType for direct files.
FILE_TYPE_VIDEO = "video"
FILE_TYPE_AUDIO = "audio"
FILE_TYPE_IMAGE = "image"
FILE_TYPE_DOCUMENT = "document"
FILE_TYPE_ARCHIVE = "archive"
FILE_TYPE_INSTALLER = "installer"
FILE_TYPE_FONT = "font"
FILE_TYPE_TEXT = "text"
FILE_TYPE_STREAM = "stream"
FILE_TYPE_TORRENT = "torrent"
FILE_TYPE_OTHER = "other"

# Engine routing buckets (orthogonal to display file type for media engines).
ENGINE_YTDLP = "ytdlp"
ENGINE_STREAM = "stream"
ENGINE_FILE = "file"
ENGINE_TORRENT = "torrent"

# Direct-download types that use the generic file downloader path (not yt-dlp merge).
DIRECT_FILE_TYPES = frozenset({
    FILE_TYPE_VIDEO,
    FILE_TYPE_AUDIO,
    FILE_TYPE_IMAGE,
    FILE_TYPE_DOCUMENT,
    FILE_TYPE_ARCHIVE,
    FILE_TYPE_INSTALLER,
    FILE_TYPE_FONT,
    FILE_TYPE_TEXT,
    FILE_TYPE_OTHER,
    ENGINE_FILE,  # legacy "file" label still treated as direct
})



# Exact application/* subtypes → high-level type.
_APPLICATION_EXACT: dict[str, str] = {
    # Streams
    "application/vnd.apple.mpegurl": FILE_TYPE_STREAM,
    "application/x-mpegurl": FILE_TYPE_STREAM,
    "application/dash+xml": FILE_TYPE_STREAM,
    # Torrents
    "application/x-bittorrent": FILE_TYPE_TORRENT,
    # Archives
    "application/zip": FILE_TYPE_ARCHIVE,
    "application/x-zip-compressed": FILE_TYPE_ARCHIVE,
    "application/x-7z-compressed": FILE_TYPE_ARCHIVE,
    "application/x-rar-compressed": FILE_TYPE_ARCHIVE,
    "application/vnd.rar": FILE_TYPE_ARCHIVE,
    "application/gzip": FILE_TYPE_ARCHIVE,
    "application/x-gzip": FILE_TYPE_ARCHIVE,
    "application/x-tar": FILE_TYPE_ARCHIVE,
    "application/x-bzip": FILE_TYPE_ARCHIVE,
    "application/x-bzip2": FILE_TYPE_ARCHIVE,
    "application/x-xz": FILE_TYPE_ARCHIVE,
    "application/zstd": FILE_TYPE_ARCHIVE,
    "application/x-zstd": FILE_TYPE_ARCHIVE,
    "application/x-lzma": FILE_TYPE_ARCHIVE,
    "application/x-lzip": FILE_TYPE_ARCHIVE,
    "application/vnd.ms-cab-compressed": FILE_TYPE_ARCHIVE,
    # Installers / packages / executables
    "application/x-msdownload": FILE_TYPE_INSTALLER,
    "application/x-msdos-program": FILE_TYPE_INSTALLER,
    "application/vnd.microsoft.portable-executable": FILE_TYPE_INSTALLER,
    "application/x-msi": FILE_TYPE_INSTALLER,
    "application/x-ms-installer": FILE_TYPE_INSTALLER,
    "application/x-apple-diskimage": FILE_TYPE_INSTALLER,
    "application/x-xar": FILE_TYPE_INSTALLER,
    "application/vnd.android.package-archive": FILE_TYPE_INSTALLER,
    "application/java-archive": FILE_TYPE_INSTALLER,
    "application/x-debian-package": FILE_TYPE_INSTALLER,
    "application/vnd.debian.binary-package": FILE_TYPE_INSTALLER,
    "application/x-redhat-package-manager": FILE_TYPE_INSTALLER,
    "application/x-rpm": FILE_TYPE_INSTALLER,
    "application/x-executable": FILE_TYPE_INSTALLER,
    "application/x-elf": FILE_TYPE_INSTALLER,
    "application/x-mach-binary": FILE_TYPE_INSTALLER,
    "application/vnd.microsoft.windows.package.desktop": FILE_TYPE_INSTALLER,
    "application/x-apple-aspen-config": FILE_TYPE_INSTALLER,
    # Documents
    "application/pdf": FILE_TYPE_DOCUMENT,
    "application/msword": FILE_TYPE_DOCUMENT,
    "application/rtf": FILE_TYPE_DOCUMENT,
    "application/vnd.ms-excel": FILE_TYPE_DOCUMENT,
    "application/vnd.ms-powerpoint": FILE_TYPE_DOCUMENT,
    "application/vnd.oasis.opendocument.text": FILE_TYPE_DOCUMENT,
    "application/vnd.oasis.opendocument.spreadsheet": FILE_TYPE_DOCUMENT,
    "application/vnd.oasis.opendocument.presentation": FILE_TYPE_DOCUMENT,
    "application/epub+zip": FILE_TYPE_DOCUMENT,
    "application/x-mobipocket-ebook": FILE_TYPE_DOCUMENT,
    # Fonts often mislabeled under application/*
    "application/font-woff": FILE_TYPE_FONT,
    "application/font-woff2": FILE_TYPE_FONT,
    "application/vnd.ms-fontobject": FILE_TYPE_FONT,
    "application/x-font-ttf": FILE_TYPE_FONT,
    "application/x-font-otf": FILE_TYPE_FONT,
}

# Prefix matches under application/*
_APPLICATION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("application/vnd.openxmlformats-officedocument.", FILE_TYPE_DOCUMENT),
    ("application/vnd.ms-excel.", FILE_TYPE_DOCUMENT),
    ("application/vnd.ms-powerpoint.", FILE_TYPE_DOCUMENT),
    ("application/vnd.oasis.opendocument.", FILE_TYPE_DOCUMENT),
)


def normalize_mime(mime: str | None) -> str:
    """Return lower-case type/subtype without parameters."""
    if not mime:
        return ""
    return mime.split(";", 1)[0].strip().lower()


def classify_mime(mime: str | None) -> str:
    """Map a Content-Type to a high-level file type bucket.

    Returns one of: video, audio, image, document, archive, installer,
    font, text, stream, torrent, other.
    """
    clean = normalize_mime(mime)
    if not clean:
        return FILE_TYPE_OTHER

    if clean in _APPLICATION_EXACT:
        return _APPLICATION_EXACT[clean]

    major, _, minor = clean.partition("/")
    if major == "video":
        return FILE_TYPE_VIDEO
    if major == "audio":
        return FILE_TYPE_AUDIO
    if major == "image":
        return FILE_TYPE_IMAGE
    if major == "font":
        return FILE_TYPE_FONT
    if major == "text":
        # HTML/CSS/JS pages are not download targets we label specially.
        if minor in {"html", "css", "javascript", "ecmascript"}:
            return FILE_TYPE_OTHER
        return FILE_TYPE_TEXT

    if major == "application":
        for prefix, file_type in _APPLICATION_PREFIXES:
            if clean.startswith(prefix):
                return file_type
        # Generic binary — no type signal beyond "other".
        if clean in {"application/octet-stream", "binary/octet-stream", "application/force-download"}:
            return FILE_TYPE_OTHER
        return FILE_TYPE_OTHER

    return FILE_TYPE_OTHER


def is_direct_download_type(file_type: str | None) -> bool:
    """True when downloads should use the generic/direct file path."""
    if not file_type:
        return False
    return file_type in DIRECT_FILE_TYPES


def get_engine_media_type(file_type: str | None, *, default: str = ENGINE_FILE) -> str:
    """Map a file type to the engine routing mediaType.

    - torrent → torrent
    - stream → stream
    - everything else downloadable as a blob → file
    """
    if file_type == FILE_TYPE_TORRENT:
        return ENGINE_TORRENT
    if file_type == FILE_TYPE_STREAM:
        return ENGINE_STREAM
    if file_type == ENGINE_YTDLP:
        return ENGINE_YTDLP
    if is_direct_download_type(file_type):
        return ENGINE_FILE
    return default
