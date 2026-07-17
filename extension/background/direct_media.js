export function classifyDirectMedia(url, contentType = "") {
	const lowerUrl = url.toLowerCase();
	const lowerType = (contentType || "").toLowerCase().split(";", 1)[0].trim();
	const isMedia =
		lowerType.startsWith("video/") || lowerType.startsWith("audio/");

	if (isMedia) return "MEDIA";
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

export function trackDirectMedia(
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
		confidence: 0.7,
	};

	streamsMap.set(url, streamInfo);
	while (streamsMap.size > maxSniffedStreams) {
		const oldestUrl = streamsMap.keys().next().value;
		streamsMap.delete(oldestUrl);
	}

	return { streamInfo, isNew: !existing, changed };
}
