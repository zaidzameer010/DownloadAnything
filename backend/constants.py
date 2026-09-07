"""Shared constant data: media-extension sets, container classifications, protocol keys.

Single home for the mappings and lists that more than one module reasons about,
so extension knowledge is never duplicated or allowed to drift between files.
Behavior (regexes derived from these, matching logic) stays with its consumers;
only the data lives here.
"""

from yt_dlp.utils import MEDIA_EXTENSIONS

# Containers that should be remuxed through FFmpegVideoRemuxer. Audio-only
# containers are left in their native format rather than being forced into a
# video container.
VIDEO_CONTAINERS = frozenset(MEDIA_EXTENSIONS.video)

# Every media extension yt-dlp knows, flattened for extension fallback scans
# when a URL path's own extension is not a real media type.
MEDIA_EXTS = frozenset((*MEDIA_EXTENSIONS.video, *MEDIA_EXTENSIONS.audio, *MEDIA_EXTENSIONS.manifests))

# Non-media extensions that must never be treated as a downloadable stream.
NON_MEDIA_EXTENSIONS = frozenset({
    "jpg", "jpeg", "png", "webp", "gif", "avif",
    "vtt", "srt", "ass", "ssa", "lrc", "ttml", "dfxp", "smi", "json3",
})

# Direct-file extensions a <video src> URL may claim while its body actually
# serves a stream manifest (KVS-style get_file/...mp4/ links).
MANIFEST_MEDIA_EXTS = frozenset({
    'mp4', 'webm', 'mkv', 'mov', 'm4v', 'flv', 'avi', 'wmv', 'mpg', 'mpeg',
    '3gp', 'f4v', 'm4a', 'mp3', 'aac', 'ogg', 'oga', 'opus', 'wav', 'flac',
})

# Per-download status keys polled from aria2-next's JSON-RPC API.
RPC_KEYS = [
    "gid",
    "status",
    "completedLength",
    "totalLength",
    "downloadSpeed",
    "errorCode",
    "errorMessage",
]
