/**
 * constants.js — Shared constants for DownloadAnything Chrome Extension
 */
"use strict";

const MEDIA_EXTS =
  "m3u8|mpd|mp4|webm|mkv|avi|mov|wmv|flv|mpg|mpeg|3gp|ts|mp3|aac|m4a|flac|wav|ogg|opus|wma";

const FILE_EXTS =
  "zip|rar|7z|tar|gz|bz2|xz|dmg|iso|bin|img|pdf|epub|doc|docx|xls|xlsx|ppt|pptx|exe|msi|apk|pkg";

const MEDIA_MIME = /video\/|audio\/|mpegurl|dash\+xml/i;

const FILE_MIME = /application\/(?:pdf|zip|x-7z-compressed|x-rar-compressed)/i;

const MIME_TO_EXT = {
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
};

const GENERIC_NAMES = new Set([
  "download", "index", "master", "playlist", "stream", "video", "audio",
  "media", "manifest", "chunklist", "output", "main", "live", "hls", "dash",
  "m3u8", "mpd", "ts", "chunk", "segment", "fragment", "part", "track"
]);

// Relocated constants and name lists
const BACKEND_BASE = "http://127.0.0.1:8000";
const WS_URL = `${BACKEND_BASE.replace(/^http/, "ws")}/ws/progress`;
const REQUEST_TIMEOUT_MS = 150000;
const BACKGROUND_REQUEST_TIMEOUT_MS = 150000;
const SETTINGS_CACHE_TTL = 5000;

const CACHE_CAP = 200;
const MAX_STREAMS_PER_TAB = 50;
const TEMP_HEADERS_CAP = 300;

const TITLE_SUFFIXES = [
  "YouTube", "Twitch", "Vimeo", "Netflix", "Disney+", "TikTok", "Twitter",
  "X", "Facebook", "Instagram", "Reddit", "Dailymotion", "Rumble", "Bilibili",
  "Odysee", "PeerTube", "Niconico", "SoundCloud", "Spotify", "Prime Video", "Apple TV",
];

const GENERIC_TITLES = new Set([
  "download", "index", "master", "playlist", "stream", "video", "audio",
  "media", "manifest", "chunklist", "output", "main", "live", "hls", "dash",
  "m3u8", "mpd", "ts", "chunk", "segment", "fragment", "part", "track",
]);

const STREAM_PRIORITY = /\.(m3u8|mpd)(?:\?|#|$)/i;

const TIER_STYLES = {
  native: { bg: "#202023", color: "#ffffff", label: "⚡ yt-dlp Native" },
  stream: { bg: "#0c2b18", color: "#30d158", label: "📡 Stream (HLS/DASH)" },
  direct: { bg: "#2b1c03", color: "#ff9f0a", label: "⬇ Direct Download" },
};

const STREAM_REGEX = new RegExp(`\\.(${MEDIA_EXTS}|${FILE_EXTS})(?:\\?|#|$)`, "i");
const SEGMENT_REGEX =
  /(?:^|[-_])(?:chunk|seg(?:ment)?|fragment|part)[-_0-9]*\.(?:ts|m4s|aac|mp4)|(?:^|[-_])\d+\.(?:ts|m4s)/i;

