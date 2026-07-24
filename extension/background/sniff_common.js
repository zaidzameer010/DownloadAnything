export function canonicalizeCandidateIdentity(url) {
	try {
		const parsed = new URL(url);
		parsed.hash = "";
		parsed.search = "";
		return parsed.toString();
	} catch {
		return url;
	}
}

// Kept for backward compatibility until service-worker refresh matching is updated.
export function canonicalizeCandidateUrl(url) {
	return canonicalizeCandidateIdentity(url);
}

export function trackCandidate(
	details,
	type,
	sniffedStreamsMap,
	maxSniffedStreams,
	contentType = "",
	confidence = 0.7,
) {
	const tabId = details.tabId;
	if (tabId < 0 || !type) return null;
	const url = details.url;
	const identityKey = canonicalizeCandidateIdentity(url);

	if (!sniffedStreamsMap.has(tabId)) {
		sniffedStreamsMap.set(tabId, new Map());
	}
	const streamsMap = sniffedStreamsMap.get(tabId);
	const existing = streamsMap.get(identityKey);
	const changed =
		!existing ||
		existing.type !== type ||
		(contentType && existing.contentType !== contentType);

	const streamInfo = {
		...(existing || {}),
		key: identityKey,
		url,
		type,
		contentType: contentType || existing?.contentType || null,
		timestamp: Date.now(),
		frameId: details.frameId,
		documentUrl: details.documentUrl || existing?.documentUrl || null,
		initiator: details.initiator || existing?.initiator || null,
		requestType: details.type || existing?.requestType || null,
		confidence,
	};

	streamsMap.set(identityKey, streamInfo);
	while (streamsMap.size > maxSniffedStreams) {
		const oldestUrl = streamsMap.keys().next().value;
		streamsMap.delete(oldestUrl);
	}

	return { streamInfo, isNew: !existing, changed };
}
