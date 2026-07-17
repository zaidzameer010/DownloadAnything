(() => {
	window.StreamExtractorFallback = {
		filterCandidates(currentSniffedStreams, fallbackUrlsTried) {
			const candidates = currentSniffedStreams.filter(
				(s) =>
					(s.type === "HLS" || s.type === "DASH") &&
					!fallbackUrlsTried.has(s.url),
			);

			const isMultiRes = (url) => {
				const lowerUrl = url.toLowerCase();
				return (
					lowerUrl.includes("multi=") ||
					lowerUrl.includes("master.m3u8") ||
					lowerUrl.includes("master.mpd") ||
					lowerUrl.includes("/master/") ||
					lowerUrl.includes("playlist_master") ||
					lowerUrl.includes("manifest.mpd")
				);
			};

			const multiResCandidates = candidates.filter((s) => isMultiRes(s.url));
			if (multiResCandidates.length > 0) {
				return multiResCandidates;
			}

			return candidates;
		},
		sortCandidates(candidates) {
			const getResolutionScore = (url) => {
				const lowerUrl = url.toLowerCase();
				const pMatch = lowerUrl.match(/(\d{3,4})p\b/);
				if (pMatch) {
					return parseInt(pMatch[1], 10);
				}
				const resMatch = lowerUrl.match(/\b(2160|1080|720|480|360|240)\b/);
				if (resMatch) {
					return parseInt(resMatch[1], 10);
				}
				if (lowerUrl.includes("4k")) {
					return 2160;
				}
				return 0;
			};

			const getCodecScore = (url) => {
				const lowerUrl = url.toLowerCase();
				if (lowerUrl.includes("av1")) return 3;
				if (lowerUrl.includes("h265") || lowerUrl.includes("hevc")) return 2;
				if (lowerUrl.includes("h264") || lowerUrl.includes("avc")) return 1;
				return 0;
			};

			return [...candidates].sort((a, b) => {
				const resA = getResolutionScore(a.url);
				const resB = getResolutionScore(b.url);
				if (resA !== resB) {
					return resB - resA;
				}

				const codecA = getCodecScore(a.url);
				const codecB = getCodecScore(b.url);
				if (codecA !== codecB) {
					return codecB - codecA;
				}

				return 0;
			});
		},
	};
})();
