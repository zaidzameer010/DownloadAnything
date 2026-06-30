/**
 * constants.js — Shared constants for DownloadAnything Chrome Extension
 */
"use strict";

const MEDIA_EXTS =
  "m3u8|mpd|mp4|webm|mkv|avi|mov|wmv|flv|mpg|mpeg|3gp|ts|mp3|aac|m4a|flac|wav|ogg|opus|wma|vid";

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
