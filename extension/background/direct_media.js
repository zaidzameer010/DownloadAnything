import {
	CANDIDATE_MEDIA,
	classifyMime,
	FILE_TYPE_AUDIO,
	FILE_TYPE_VIDEO,
	isStreamSegmentUrl,
	normalizeMime,
} from "../lib/file_types.js";
import { trackCandidate } from "./sniff_common.js";

/**
 * Classify progressive video/audio candidates from MIME only.
 * Rejects HLS/DASH init/segment URLs even when Content-Type is video/*.
 */
export function classifyDirectMedia(url, contentType = "") {
	const lowerType = normalizeMime(contentType);
	if (!lowerType) return null;

	// Never treat stream manifests or init/segment fragments as progressive MEDIA.
	const lowerUrl = String(url || "").toLowerCase();
	if (
		isStreamSegmentUrl(url) ||
		lowerUrl.includes(".m3u8") ||
		lowerUrl.includes(".mpd") ||
		lowerUrl.includes("/hls/") ||
		lowerUrl.includes("/dash/") ||
		lowerUrl.includes("media=hls")
	) {
		return null;
	}

	if (lowerType.startsWith("video/") || lowerType.startsWith("audio/")) {
		return CANDIDATE_MEDIA;
	}

	const fileType = classifyMime(lowerType);
	if (fileType === FILE_TYPE_VIDEO || fileType === FILE_TYPE_AUDIO) {
		return CANDIDATE_MEDIA;
	}

	return null;
}

export function trackDirectMedia(
	details,
	type,
	sniffedStreamsMap,
	maxSniffedStreams,
	contentType = "",
) {
	return trackCandidate(
		details,
		type,
		sniffedStreamsMap,
		maxSniffedStreams,
		contentType,
		0.7,
	);
}
