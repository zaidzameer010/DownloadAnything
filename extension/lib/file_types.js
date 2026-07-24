// MIME-based content type classification.
// Single source of truth for high-level file types used by intercept + sniff.
// No extension allowlists — Content-Type (and a few stream path tokens) only.

export const FILE_TYPE_VIDEO = "video";
export const FILE_TYPE_AUDIO = "audio";
export const FILE_TYPE_IMAGE = "image";
export const FILE_TYPE_DOCUMENT = "document";
export const FILE_TYPE_ARCHIVE = "archive";
export const FILE_TYPE_INSTALLER = "installer";
export const FILE_TYPE_FONT = "font";
export const FILE_TYPE_TEXT = "text";
export const FILE_TYPE_STREAM = "stream";
export const FILE_TYPE_TORRENT = "torrent";
export const FILE_TYPE_OTHER = "other";

// Stream protocol labels used by the sniffer UI.
export const STREAM_HLS = "HLS";
export const STREAM_DASH = "DASH";
export const CANDIDATE_MEDIA = "MEDIA";

const HLS_MIME_TYPES = new Set([
	"application/vnd.apple.mpegurl",
	"application/x-mpegurl",
]);
const DASH_MIME_TYPES = new Set(["application/dash+xml"]);

const APPLICATION_EXACT = new Map([
	["application/vnd.apple.mpegurl", FILE_TYPE_STREAM],
	["application/x-mpegurl", FILE_TYPE_STREAM],
	["application/dash+xml", FILE_TYPE_STREAM],
	["application/x-bittorrent", FILE_TYPE_TORRENT],
	["application/zip", FILE_TYPE_ARCHIVE],
	["application/x-zip-compressed", FILE_TYPE_ARCHIVE],
	["application/x-7z-compressed", FILE_TYPE_ARCHIVE],
	["application/x-rar-compressed", FILE_TYPE_ARCHIVE],
	["application/vnd.rar", FILE_TYPE_ARCHIVE],
	["application/gzip", FILE_TYPE_ARCHIVE],
	["application/x-gzip", FILE_TYPE_ARCHIVE],
	["application/x-tar", FILE_TYPE_ARCHIVE],
	["application/x-bzip", FILE_TYPE_ARCHIVE],
	["application/x-bzip2", FILE_TYPE_ARCHIVE],
	["application/x-xz", FILE_TYPE_ARCHIVE],
	["application/zstd", FILE_TYPE_ARCHIVE],
	["application/x-zstd", FILE_TYPE_ARCHIVE],
	["application/vnd.ms-cab-compressed", FILE_TYPE_ARCHIVE],
	["application/x-msdownload", FILE_TYPE_INSTALLER],
	["application/x-msdos-program", FILE_TYPE_INSTALLER],
	["application/vnd.microsoft.portable-executable", FILE_TYPE_INSTALLER],
	["application/x-msi", FILE_TYPE_INSTALLER],
	["application/x-ms-installer", FILE_TYPE_INSTALLER],
	["application/x-apple-diskimage", FILE_TYPE_INSTALLER],
	["application/x-xar", FILE_TYPE_INSTALLER],
	["application/vnd.android.package-archive", FILE_TYPE_INSTALLER],
	["application/java-archive", FILE_TYPE_INSTALLER],
	["application/x-debian-package", FILE_TYPE_INSTALLER],
	["application/vnd.debian.binary-package", FILE_TYPE_INSTALLER],
	["application/x-redhat-package-manager", FILE_TYPE_INSTALLER],
	["application/x-rpm", FILE_TYPE_INSTALLER],
	["application/x-executable", FILE_TYPE_INSTALLER],
	["application/x-elf", FILE_TYPE_INSTALLER],
	["application/x-mach-binary", FILE_TYPE_INSTALLER],
	["application/pdf", FILE_TYPE_DOCUMENT],
	["application/msword", FILE_TYPE_DOCUMENT],
	["application/rtf", FILE_TYPE_DOCUMENT],
	["application/vnd.ms-excel", FILE_TYPE_DOCUMENT],
	["application/vnd.ms-powerpoint", FILE_TYPE_DOCUMENT],
	["application/vnd.oasis.opendocument.text", FILE_TYPE_DOCUMENT],
	["application/vnd.oasis.opendocument.spreadsheet", FILE_TYPE_DOCUMENT],
	["application/vnd.oasis.opendocument.presentation", FILE_TYPE_DOCUMENT],
	["application/epub+zip", FILE_TYPE_DOCUMENT],
	["application/font-woff", FILE_TYPE_FONT],
	["application/font-woff2", FILE_TYPE_FONT],
	["application/vnd.ms-fontobject", FILE_TYPE_FONT],
	["application/x-font-ttf", FILE_TYPE_FONT],
	["application/x-font-otf", FILE_TYPE_FONT],
]);

const APPLICATION_PREFIXES = [
	["application/vnd.openxmlformats-officedocument.", FILE_TYPE_DOCUMENT],
	["application/vnd.ms-excel.", FILE_TYPE_DOCUMENT],
	["application/vnd.ms-powerpoint.", FILE_TYPE_DOCUMENT],
	["application/vnd.oasis.opendocument.", FILE_TYPE_DOCUMENT],
];

// MIME types that are page/script assets — never intercept as downloads.
const REJECT_MIME_PREFIXES = [
	"text/html",
	"application/xhtml",
	"text/css",
	"application/javascript",
	"text/javascript",
	"application/x-javascript",
];

export function normalizeMime(mime) {
	if (!mime) return "";
	return String(mime).split(";", 1)[0].trim().toLowerCase();
}

export function classifyMime(mime) {
	const clean = normalizeMime(mime);
	if (!clean) return FILE_TYPE_OTHER;

	if (APPLICATION_EXACT.has(clean)) {
		return APPLICATION_EXACT.get(clean);
	}

	const slash = clean.indexOf("/");
	const major = slash >= 0 ? clean.slice(0, slash) : clean;
	const minor = slash >= 0 ? clean.slice(slash + 1) : "";

	if (major === "video") return FILE_TYPE_VIDEO;
	if (major === "audio") return FILE_TYPE_AUDIO;
	if (major === "image") return FILE_TYPE_IMAGE;
	if (major === "font") return FILE_TYPE_FONT;
	if (major === "text") {
		if (
			minor === "html" ||
			minor === "css" ||
			minor === "javascript" ||
			minor === "ecmascript"
		) {
			return FILE_TYPE_OTHER;
		}
		return FILE_TYPE_TEXT;
	}

	if (major === "application") {
		for (const [prefix, type] of APPLICATION_PREFIXES) {
			if (clean.startsWith(prefix)) return type;
		}
		return FILE_TYPE_OTHER;
	}

	return FILE_TYPE_OTHER;
}

export function isPageAssetMime(mime) {
	const clean = normalizeMime(mime);
	if (!clean) return false;
	return REJECT_MIME_PREFIXES.some((p) => clean.startsWith(p));
}

/**
 * True for HLS/DASH fragment/init URLs that must never be progressive MEDIA
 * and must never be treated as downloadable manifests.
 * Path/token based only — not a media-extension allowlist.
 * Manifests (.m3u8/.mpd) return false here; they are handled separately.
 */
export function isStreamSegmentUrl(url) {
	if (!url) return false;
	const lower = String(url).toLowerCase();
	// Actual manifests are not segments.
	if (lower.includes(".m3u8") || lower.includes(".mpd")) {
		return false;
	}
	let base = "";
	try {
		const path = new URL(url).pathname.toLowerCase();
		base = path.split("/").pop() || "";
	} catch {
		base = lower.split("?")[0].split("/").pop() || "";
	}
	// Common packager init/segment filenames.
	if (
		/^(init|segment|seg|chunk|media|fragment)[-._]/i.test(base) ||
		/\.(m4s|cmfv|cmfa)$/i.test(base)
	) {
		return true;
	}
	return (
		lower.includes("/init-") ||
		lower.includes("/init.") ||
		lower.includes("init-v") ||
		lower.includes(".m4s") ||
		lower.includes("/chunklist") ||
		lower.includes("frag(")
	);
}

/**
 * Classify a URL + optional Content-Type for the media sniffer.
 * Returns HLS | DASH | MEDIA | null.
 *
 * Order matters:
 *   1. Stream MIME
 *   2. Manifest path tokens (.m3u8 / .mpd) — even when Content-Type is video/*
 *   3. Init/segment fragments → drop (null)
 *   4. Weaker stream path tokens (/hls/, media=hls, /dash/)
 *   5. Progressive video/audio MIME
 */
export function classifyMediaCandidate(url, contentType = "") {
	const clean = normalizeMime(contentType);
	const lowerUrl = url ? String(url).toLowerCase() : "";

	if (HLS_MIME_TYPES.has(clean)) return STREAM_HLS;
	if (DASH_MIME_TYPES.has(clean)) return STREAM_DASH;

	// Manifests win over video/* MIME (e.g. file.mp4.m3u8).
	if (lowerUrl.includes(".m3u8") || lowerUrl.includes("/m3u8")) {
		return STREAM_HLS;
	}
	if (lowerUrl.includes(".mpd") || lowerUrl.includes("/manifest.mpd")) {
		return STREAM_DASH;
	}

	// Drop init/segment fragments — not downloadable progressive files or manifests.
	if (isStreamSegmentUrl(url)) {
		return null;
	}

	// Weaker stream directory tokens (only after dropping segments).
	if (lowerUrl.includes("/hls/") || lowerUrl.includes("media=hls")) {
		return STREAM_HLS;
	}
	if (lowerUrl.includes("/dash/")) {
		return STREAM_DASH;
	}

	const fileType = clean ? classifyMime(clean) : null;
	if (fileType === FILE_TYPE_VIDEO || fileType === FILE_TYPE_AUDIO) {
		return CANDIDATE_MEDIA;
	}
	if (fileType === FILE_TYPE_STREAM) {
		if (clean.includes("mpegurl")) return STREAM_HLS;
		if (clean.includes("dash")) return STREAM_DASH;
		return STREAM_HLS;
	}

	return null;
}

// Extensions that strongly indicate a page/script asset when the server did
// not send a clear MIME type.
const PAGE_ASSET_EXTENSIONS = new Set([
	"html",
	"htm",
	"css",
	"js",
	"mjs",
	"jsx",
	"ts",
	"tsx",
]);

function _getPageAssetExtension(url, filename) {
	if (filename) {
		const base = filename.split("/").pop().split("\\").pop();
		const dot = base.lastIndexOf(".");
		if (dot > 0) return base.slice(dot + 1).toLowerCase();
	}
	try {
		const base = new URL(url).pathname.split("/").pop() || "";
		const dot = base.lastIndexOf(".");
		if (dot > 0) return base.slice(dot + 1).toLowerCase();
	} catch {
		const base = url.split("?")[0].split("/").pop() || "";
		const dot = base.lastIndexOf(".");
		if (dot > 0) return base.slice(dot + 1).toLowerCase();
	}
	return "";
}

/**
 * Whether chrome.downloads should hand this item to DownloadAnything.
 * Intercepts everything except obvious page assets (HTML/CSS/JS).
 */
export function shouldInterceptDownload(url, mime, filename) {
	if (!url) return false;
	if (isPageAssetMime(mime)) return false;
	if (!mime || !normalizeMime(mime)) {
		const ext = _getPageAssetExtension(url, filename);
		return !PAGE_ASSET_EXTENSIONS.has(ext);
	}
	return true;
}

export function streamConfidence(url, contentType = "") {
	const clean = normalizeMime(contentType);
	if (HLS_MIME_TYPES.has(clean) || DASH_MIME_TYPES.has(clean)) {
		return 1.0;
	}
	const lower = String(url || "").toLowerCase();
	if (lower.includes("master.m3u8") || lower.includes("manifest.mpd")) {
		return 0.9;
	}
	if (lower.includes("/hls/") || lower.includes("/dash/")) {
		return 0.75;
	}
	if (lower.includes(".m3u8") || lower.includes(".mpd")) {
		return 0.7;
	}
	return 0.5;
}
