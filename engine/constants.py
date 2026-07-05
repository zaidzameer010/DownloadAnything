"""
constants.py — Shared constants for the DownloadAnything backend engine.
"""

MEDIA_EXTS = (
    "m3u8|mpd|mp4|webm|mkv|avi|mov|wmv|flv|mpg|mpeg|3gp|ts|mp3|aac|m4a|flac|wav|ogg|opus|wma"
)

MEDIA_EXTS_SET = frozenset(MEDIA_EXTS.split("|"))

FILE_EXTS = (
    "zip|rar|7z|tar|gz|bz2|xz|dmg|iso|bin|img|pdf|epub|doc|docx|xls|xlsx|ppt|pptx|exe|msi|apk|pkg"
)

GENERIC_STREAM_NAMES = frozenset({
    "download", "index", "master", "playlist", "stream", "video", "audio",
    "media", "manifest", "chunklist", "output", "main", "live", "hls", "dash",
    "m3u8", "mpd", "ts", "chunk", "segment", "fragment", "part", "track"
})

MIME_TO_EXT = {
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/x-tar": "tar",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
    "application/x-msdownload": "exe",
    "application/x-executable": "exe",
    "application/vnd.android.package-archive": "apk",
    "application/x-debian-package": "deb",
    "application/x-redhat-package-manager": "rpm",
    "application/epub+zip": "epub",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/x-m4a": "m4a",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "video/quicktime": "mov",
    "video/x-msvideo": "avi",
    "application/x-mpegurl": "m3u8",
    "application/vnd.apple.mpegurl": "m3u8",
    "application/dash+xml": "mpd",
}

# The following constants have been relocated from other engine modules
from engine.models import TaskStatus

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TITLE_SUFFIXES = (
    "YouTube",
    "Twitch",
    "Vimeo",
    "Netflix",
    "Disney+",
    "TikTok",
    "Twitter",
    "X",
    "Facebook",
    "Instagram",
    "Reddit",
    "Dailymotion",
    "Rumble",
    "Bilibili",
    "Odysee",
    "PeerTube",
    "Niconico",
    "SoundCloud",
    "Spotify",
    "Prime Video",
    "Apple TV",
)

ACTIVE_STATES = frozenset(
    {
        TaskStatus.DOWNLOADING,
        TaskStatus.STITCHING,
        TaskStatus.EMBEDDING,
        TaskStatus.FINALIZING,
    }
)

BROADCAST_INTERVAL = 0.1

PP_STATUS = {
    "Merger": TaskStatus.STITCHING,
    "FFmpegMerger": TaskStatus.STITCHING,
    "FFmpegEmbedSubtitle": TaskStatus.EMBEDDING,
    "EmbedThumbnail": TaskStatus.EMBEDDING,
    "MoveFiles": TaskStatus.FINALIZING,
}

STREAM_PROTOCOLS = frozenset(
    {"m3u8", "m3u8_native", "dash", "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpte"}
)

