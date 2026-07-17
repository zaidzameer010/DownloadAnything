export function classifyStream(url, contentType = "") {
	const lowerUrl = url.toLowerCase();
	const lowerType = (contentType || "").toLowerCase().split(";", 1)[0].trim();
	const isHls =
		lowerType === "application/vnd.apple.mpegurl" ||
		lowerType === "application/x-mpegurl" ||
		lowerUrl.includes(".m3u8") ||
		lowerUrl.includes("/m3u8") ||
		lowerUrl.includes("/hls/");
	const isDash =
		lowerType === "application/dash+xml" ||
		lowerUrl.includes(".mpd") ||
		lowerUrl.includes("/manifest.mpd") ||
		lowerUrl.includes("/dash/");

	if (isHls) return "HLS";
	if (isDash) return "DASH";
	return null;
}

function canonicalizeCandidateUrl(url) {
	try {
		const parsed = new URL(url);
		parsed.hash = "";
		return parsed.toString();
	} catch {
		return url;
	}
}

export function trackStream(
	details,
	type,
	sniffedStreamsMap,
	maxSniffedStreams,
	contentType = "",
) {
	const tabId = details.tabId;
	if (tabId < 0 || !type) return null;
	const url = canonicalizeCandidateUrl(details.url);

	if (!sniffedStreamsMap.has(tabId)) {
		sniffedStreamsMap.set(tabId, new Map());
	}
	const streamsMap = sniffedStreamsMap.get(tabId);
	const existing = streamsMap.get(url);
	const changed =
		!existing ||
		existing.type !== type ||
		(contentType && existing.contentType !== contentType);

	const streamInfo = {
		...(existing || {}),
		url,
		type,
		contentType: contentType || existing?.contentType || null,
		timestamp: Date.now(),
		frameId: details.frameId,
		documentUrl: details.documentUrl || existing?.documentUrl || null,
		initiator: details.initiator || existing?.initiator || null,
		requestType: details.type || existing?.requestType || null,
		confidence: 1.0,
	};

	streamsMap.set(url, streamInfo);
	while (streamsMap.size > maxSniffedStreams) {
		const oldestUrl = streamsMap.keys().next().value;
		streamsMap.delete(oldestUrl);
	}

	return { streamInfo, isNew: !existing, changed };
}
