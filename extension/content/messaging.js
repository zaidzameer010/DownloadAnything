(() => {
	const logger = window.__DMA_LOGGER__;

	if (window.DownloadAnythingMessaging) return;

	let extensionContextInvalidated = false;

	function isExtensionContextValid() {
		if (extensionContextInvalidated) return false;
		return (
			typeof chrome !== "undefined" && chrome.runtime && !!chrome.runtime.id
		);
	}

	function markContextInvalidated(source) {
		if (extensionContextInvalidated) return;
		extensionContextInvalidated = true;
		logger.warn(
			`[${source}] Extension context invalidated. Reload the extension or refresh the page.`,
		);
		if (
			window === window.top &&
			window.DownloadAnythingModal &&
			window.DownloadAnythingModal.showToast
		) {
			window.DownloadAnythingModal.showToast(
				"Extension was updated or disabled. Please reload it or refresh the page.",
				true,
			);
		}
	}

	function safeSendMessage(source, message, callback) {
		if (!isExtensionContextValid()) {
			if (typeof callback === "function") callback(null);
			return;
		}
		const expectsResponse = typeof callback === "function";
		try {
			if (expectsResponse) {
				chrome.runtime.sendMessage(message, (response) => {
					if (chrome.runtime.lastError) {
						const msg = chrome.runtime.lastError.message || "";
						if (msg.toLowerCase().includes("invalidated")) {
							markContextInvalidated(source);
						} else if (!msg.toLowerCase().includes("port closed")) {
							logger.warn(`[${source}] sendMessage error:`, msg);
						}
						callback(null);
						return;
					}
					callback(response);
				});
			} else {
				chrome.runtime.sendMessage(message).catch((err) => {
					const msg = err?.message || "";
					if (msg.toLowerCase().includes("invalidated")) {
						markContextInvalidated(source);
					} else if (!msg.toLowerCase().includes("port closed")) {
						logger.warn(`[${source}] sendMessage error:`, err);
					}
				});
			}
		} catch (err) {
			const msg = err?.message || "";
			if (msg.toLowerCase().includes("invalidated")) {
				markContextInvalidated(source);
			} else if (!msg.toLowerCase().includes("port closed")) {
				logger.warn(`[${source}] sendMessage threw:`, err);
			}
			if (expectsResponse) callback(null);
		}
	}

	window.DownloadAnythingMessaging = {
		isExtensionContextValid,
		markContextInvalidated,
		safeSendMessage,
	};
})();
