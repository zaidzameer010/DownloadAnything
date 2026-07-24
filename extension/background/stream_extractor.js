import {
	isStreamSegmentUrl,
	normalizeMime,
	STREAM_DASH,
	STREAM_HLS,
	streamConfidence as sharedStreamConfidence,
} from "../lib/file_types.js";
import { trackCandidate } from "./sniff_common.js";

const HLS_MIME_TYPES = new Set([
	"application/vnd.apple.mpegurl",
	"application/x-mpegurl",
]);
const DASH_MIME_TYPES = new Set(["application/dash+xml"]);

/**
 * Classify HLS/DASH stream candidates.
 * Path tokens (including ".mp4.m3u8") beat progressive video/* MIME.
 * Init/segment fragments are not tracked as candidates.
 */
export function classifyStream(url, contentType = "") {
	const lowerType = normalizeMime(contentType);
	const lowerUrl = String(url || "").toLowerCase();

	if (HLS_MIME_TYPES.has(lowerType)) return STREAM_HLS;
	if (DASH_MIME_TYPES.has(lowerType)) return STREAM_DASH;

	// Manifests first (handles disguised names like file.mp4.m3u8).
	if (lowerUrl.includes(".m3u8") || lowerUrl.includes("/m3u8")) {
		return STREAM_HLS;
	}
	if (lowerUrl.includes(".mpd") || lowerUrl.includes("/manifest.mpd")) {
		return STREAM_DASH;
	}

	// Never promote init/segment fragments as stream manifests.
	if (isStreamSegmentUrl(url)) {
		return null;
	}

	// Weaker stream directory tokens.
	if (lowerUrl.includes("/hls/") || lowerUrl.includes("media=hls")) {
		return STREAM_HLS;
	}
	if (lowerUrl.includes("/dash/")) {
		return STREAM_DASH;
	}

	return null;
}

export function streamConfidence(url, contentType = "") {
	return sharedStreamConfidence(url, contentType);
}

export function trackStream(
	details,
	type,
	sniffedStreamsMap,
	maxSniffedStreams,
	contentType = "",
) {
	const confidence = streamConfidence(details.url, contentType);
	return trackCandidate(
		details,
		type,
		sniffedStreamsMap,
		maxSniffedStreams,
		contentType,
		confidence,
	);
}
