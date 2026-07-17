(() => {
	window.DirectMediaFallback = {
		filterCandidates(currentSniffedStreams, fallbackUrlsTried) {
			return currentSniffedStreams.filter(
				(s) => s.type === "MEDIA" && !fallbackUrlsTried.has(s.url),
			);
		},
		sortCandidates(candidates) {
			return [...candidates].sort((a, b) => b.timestamp - a.timestamp);
		},
	};
})();
