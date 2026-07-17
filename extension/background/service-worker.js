import { classifyDirectMedia, trackDirectMedia } from "./direct_media.js";
import { classifyStream, trackStream } from "./stream_extractor.js";

const OFFSCREEN_TAB_ID = -1;
const MAX_SNIFFED_STREAMS_PER_TAB = 100;

let backendAvailable = false;

async function pingBackend() {
	try {
		const response = await fetch("http://127.0.0.1:8765/ping", {
			method: "GET",
			cache: "no-cache",
			signal: AbortSignal.timeout(800),
		});
		if (response.ok) {
			const data = await response.json();
			return data && data.status === "ok";
		}
	} catch (e) {
		console.debug("Ping backend failed:", e.message);
	}
	return false;
}

async function updateBackendStatus(available) {
	if (backendAvailable === available) return;
	backendAvailable = available;
	console.log(`[Status] Backend availability changed: ${available}`);

	try {
		if (available) {
			await chrome.action.setBadgeText({ text: "" });
			await chrome.action.setTitle({ title: "DownloadAnything Downloader" });
			await chrome.action.enable();
			broadcastToAllTabs({ type: "BACKEND_STATUS", available: true });
		} else {
			await chrome.action.setBadgeText({ text: "OFF" });
			await chrome.action.setBadgeBackgroundColor({ color: "#ef4444" });
			await chrome.action.setTitle({
				title: "DownloadAnything (Backend Offline)",
			});
			await chrome.action.disable();
			broadcastToAllTabs({ type: "BACKEND_STATUS", available: false });
		}
	} catch (err) {
		console.warn("Failed to update extension action badge/state:", err);
	}
}

async function checkBackendAvailability() {
	const available = await pingBackend();
	await updateBackendStatus(available);
	return available;
}

const MAX_TRACKED_DOWNLOADS = 200;

const sniffedStreams = new Map();
const requestCandidates = new Map();
const downloadInitiators = new Map();
let storageMutationQueue = Promise.resolve();
let offscreenMessageQueue = Promise.resolve();

// Setup 1-min alarm keepalive to wake service worker
chrome.alarms.create("swKeepAliveAlarm", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
	if (alarm.name === "swKeepAliveAlarm") {
		console.debug("Service Worker keep-alive heartbeat alarm fired.");
	}
});

// Broadcast action clicked to content script
chrome.action.onClicked.addListener((tab) => {
	if (typeof tab.id === "number") {
		chrome.tabs
			.sendMessage(tab.id, { type: "EXTENSION_ACTIVATED" })
			.catch((err) => {
				console.warn("Failed to activate extension on click:", err);
			});
	}
});

// Offscreen document lifecycle management
let creatingOffscreen;
async function ensureOffscreenDocument() {
	const contexts = await chrome.runtime.getContexts({
		contextTypes: ["OFFSCREEN_DOCUMENT"],
	});
	if (contexts.length > 0) {
		return;
	}

	if (creatingOffscreen) {
		await creatingOffscreen;
		return;
	}

	creatingOffscreen = chrome.offscreen.createDocument({
		url: "background/offscreen.html",
		reasons: ["DOM_PARSER"],
		justification: "Keep WebSocket connection alive for downloading media",
	});
	try {
		await creatingOffscreen;
	} finally {
		creatingOffscreen = null;
	}
}

async function sendToWS(data) {
	try {
		await ensureOffscreenDocument();
		await chrome.runtime.sendMessage({
			target: "offscreen",
			type: "SEND_WS",
			payload: data,
		});
	} catch (error) {
		console.warn("Unable to send message to WebSocket client:", error);
	}
}

function isTrustedOffscreenMessage(message, sender) {
	return Boolean(
		message &&
			typeof message === "object" &&
			message.source === "offscreen" &&
			sender.id === chrome.runtime.id &&
			(sender.url === chrome.runtime.getURL("background/offscreen.html") ||
				sender.documentUrl ===
					chrome.runtime.getURL("background/offscreen.html")),
	);
}

function queueOffscreenMessage(message) {
	const operation = offscreenMessageQueue.then(() =>
		handleOffscreenMessage(message),
	);
	offscreenMessageQueue = operation.catch(() => undefined);
	return operation;
}

// Storage helpers for Job/Tab mappings
function mutateStorageMap(mapName, mutate) {
	const operation = storageMutationQueue.then(async () => {
		const data = await chrome.storage.local.get(mapName);
		const storedMap = data[mapName];
		const map =
			storedMap && typeof storedMap === "object" && !Array.isArray(storedMap)
				? storedMap
				: {};
		const result = mutate(map);
		await chrome.storage.local.set({ [mapName]: map });
		return result;
	});
	storageMutationQueue = operation.catch(() => undefined);
	return operation;
}

function registerJobTab(jobId, tabId) {
	return mutateStorageMap("jobTabMap", (map) => {
		map[jobId] = tabId;
	});
}

async function getTabForJob(jobId) {
	const data = await chrome.storage.local.get("jobTabMap");
	return data.jobTabMap?.[jobId] ?? null;
}

function removeJobTab(jobId) {
	return mutateStorageMap("jobTabMap", (map) => {
		delete map[jobId];
	});
}

// Storage helpers for URL/Tab mappings (used for probe mapping)
function registerUrlTab(url, tabId) {
	return mutateStorageMap("urlTabMap", (map) => {
		map[url] = tabId;
	});
}

async function getTabForUrl(url) {
	const data = await chrome.storage.local.get("urlTabMap");
	return data.urlTabMap?.[url] ?? null;
}

function removeUrlTab(url) {
	return mutateStorageMap("urlTabMap", (map) => {
		delete map[url];
	});
}

function broadcastToAllTabs(message) {
	chrome.tabs.query({}, (tabs) => {
		if (tabs) {
			tabs.forEach((tab) => {
				if (typeof tab.id === "number") {
					chrome.tabs.sendMessage(tab.id, message).catch(() => {});
				}
			});
		}
	});
}

function getHeaderValue(headers, name) {
	const target = name.toLowerCase();
	return (headers || []).find((header) => header.name?.toLowerCase() === target)
		?.value;
}

function classifyMediaCandidate(url, contentType = "") {
	return (
		classifyStream(url, contentType) || classifyDirectMedia(url, contentType)
	);
}

function rememberCandidate(details, type, contentType = "") {
	if (type === "HLS" || type === "DASH") {
		return trackStream(
			details,
			type,
			sniffedStreams,
			MAX_SNIFFED_STREAMS_PER_TAB,
			contentType,
		);
	}
	if (type === "MEDIA") {
		return trackDirectMedia(
			details,
			type,
			sniffedStreams,
			MAX_SNIFFED_STREAMS_PER_TAB,
			contentType,
		);
	}
	return null;
}

function notifyStreamCandidate(tabId, streamInfo) {
	if (typeof tabId !== "number" || tabId < 0) return;
	console.log(
		`[Sniffer] Tab ${tabId} detected ${streamInfo.type} candidate: ${streamInfo.url}`,
	);
	chrome.tabs
		.sendMessage(tabId, { type: "STREAM_SNIFFED", stream: streamInfo })
		.catch(() => {});
	if (!chrome.webNavigation) return;
	chrome.webNavigation.getAllFrames({ tabId }, (frames) => {
		if (!frames) return;
		frames.forEach((frame) => {
			if (frame.frameId !== 0) {
				chrome.tabs
					.sendMessage(
						tabId,
						{ type: "STREAM_SNIFFED", stream: streamInfo },
						{ frameId: frame.frameId },
					)
					.catch(() => {});
			}
		});
	});
}

function trackRequest(url, tabId) {
	if (
		typeof tabId === "number" &&
		tabId >= 0 &&
		url &&
		url.startsWith("http")
	) {
		downloadInitiators.set(url, tabId);
		if (downloadInitiators.size > MAX_TRACKED_DOWNLOADS) {
			const firstKey = downloadInitiators.keys().next().value;
			downloadInitiators.delete(firstKey);
		}
	}
}

// Listen to requests from content scripts (overlay / modal) and offscreen
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
	if (!message || typeof message !== "object") {
		return false;
	}

	if (message.type === "PING_BACKEND") {
		checkBackendAvailability().then((available) => {
			sendResponse({ available });
		});
		return true; // Keep channel open for async response
	}

	// Handle messages from offscreen
	if (message.source === "offscreen") {
		if (!isTrustedOffscreenMessage(message, sender)) {
			console.warn("Rejected untrusted offscreen message");
			return false;
		}
		queueOffscreenMessage(message).catch((error) => {
			console.warn("Failed to handle offscreen message:", error);
		});
		return false; // No async response
	}

	const tabId = sender.tab?.id;
	if (typeof tabId !== "number") {
		return false; // No async response for non-tab origin messages
	}

	if (message.type === "SHOW_MODAL_IN_TOP_FRAME") {
		chrome.tabs
			.sendMessage(
				tabId,
				{
					type: "SHOW_MODAL",
					url: message.url,
				},
				{ frameId: 0 },
			)
			.catch((err) => {
				console.warn("Failed to send SHOW_MODAL to top frame:", err);
			});
		sendResponse({ status: "forwarded" });
		return true;
	}

	if (message.type === "PROBE_MEDIA") {
		chrome.tabs.get(tabId, async (tab) => {
			if (chrome.runtime.lastError) {
				console.warn(
					"Failed to get tab info during probe:",
					chrome.runtime.lastError.message,
				);
				return;
			}
			if (!tab) return;

			let pageTitle = tab.title;
			if (pageTitle) {
				pageTitle = pageTitle.trim();
			}

			const finalTitle = message.title || pageTitle;

			if (message.jobId) {
				await registerJobTab(message.jobId, tabId);
			} else {
				await registerUrlTab(message.url, tabId);
			}
			// Page/tab URL as Referer is required for many embed-player CDNs.
			const referer = message.referer || message.referrer || tab.url || null;
			sendToWS({
				type: "probe",
				url: message.url,
				title: finalTitle || null,
				referer,
				jobId: message.jobId || null,
			});
		});
		sendResponse({ status: "probing" });
		return true;
	}

	if (message.type === "CANCEL_PROBE") {
		removeJobTab(message.jobId).catch(() => {});
		sendToWS({
			type: "cancel_probe",
			jobId: message.jobId,
		});
		sendResponse({ status: "probe_cancelled" });
		return true;
	}

	if (message.type === "CHECK_FILE_EXISTS") {
		registerJobTab(message.jobId, tabId).then(() => {
			sendToWS({
				type: "check_file_exists",
				path: message.path,
				filename: message.filename || null,
				jobId: message.jobId,
				title: message.title || null,
				ext: message.ext || null,
				url: message.url || null,
				mime: message.mime || null,
			});
		});
		sendResponse({ status: "checking" });
		return true;
	}

	if (message.type === "START_DOWNLOAD") {
		registerJobTab(message.jobId, tabId).then(() => {
			sendToWS({
				type: "choose",
				jobId: message.jobId,
				formatId: message.formatId,
				outputDir: message.outputDir,
				conflictResolution: message.conflictResolution || "replace",
				url: message.url,
				title: message.title,
				filename: message.filename,
				referer: message.referer,
				fileSize: message.fileSize,
				mime: message.mime,
			});
		});
		sendResponse({ status: "download_started" });
		return true;
	}

	if (message.type === "CANCEL_DOWNLOAD") {
		sendToWS({
			type: "cancel",
			jobId: message.jobId,
		});
		sendResponse({ status: "cancel_sent" });
		return true;
	}

	if (message.type === "GET_CATEGORIES") {
		chrome.storage.local.set({ lastCategoriesTabId: tabId }).then(() => {
			sendToWS({
				type: "get_categories",
			});
		});
		return false;
	}

	if (message.type === "REQUEST_BROWSE") {
		chrome.storage.local.set({ lastBrowseTabId: tabId }).then(() => {
			sendToWS({
				type: "browse_directory",
				path: message.path || message.initialDir || null,
			});
		});
		return false;
	}

	if (message.type === "GET_SNIFFED_STREAMS") {
		const streamsMap = sniffedStreams.get(tabId);
		const list = streamsMap ? Array.from(streamsMap.values()) : [];
		sendResponse({ streams: list });
		return true;
	}
});

async function handleOffscreenMessage(message) {
	if (message.type === "WS_OPEN") {
		updateBackendStatus(true);
		broadcastToAllTabs({ type: "SERVER_CONNECTED" });
	} else if (message.type === "WS_CLOSE") {
		checkBackendAvailability();
		broadcastToAllTabs({ type: "SERVER_DISCONNECTED" });
	} else if (message.type === "WS_MESSAGE") {
		const wsMsg = message.payload;

		// Broadcast updates
		if (wsMsg.type === "jobs_list" || wsMsg.type === "categories_list") {
			if (wsMsg.type === "categories_list") {
				const data = await chrome.storage.local.get("lastCategoriesTabId");
				const targetId = data.lastCategoriesTabId;
				if (typeof targetId === "number") {
					chrome.tabs.sendMessage(targetId, wsMsg).catch(() => {});
				}
			}
			broadcastToAllTabs(wsMsg);
			return;
		}

		if (wsMsg.type === "directory_selected" || wsMsg.type === "browse_failed") {
			const data = await chrome.storage.local.get("lastBrowseTabId");
			const targetId = data.lastBrowseTabId;
			if (typeof targetId === "number") {
				chrome.tabs.sendMessage(targetId, wsMsg).catch(() => {});
			}
			return;
		}

		if (wsMsg.type === "duplicate_job_alert") {
			const tabId = await getTabForUrl(wsMsg.url);
			if (typeof tabId === "number") {
				await removeUrlTab(wsMsg.url);
				chrome.tabs.sendMessage(tabId, wsMsg).catch(() => {});
			}
			return;
		}

		if (wsMsg.type === "probe_started") {
			let tabId = await getTabForJob(wsMsg.jobId);
			if (typeof tabId !== "number") {
				tabId = await getTabForUrl(wsMsg.url);
				if (typeof tabId === "number") {
					await registerJobTab(wsMsg.jobId, tabId);
					await removeUrlTab(wsMsg.url);
				}
			}
			if (typeof tabId === "number") {
				chrome.tabs.sendMessage(tabId, wsMsg).catch(() => {});
			}
			return;
		}

		if (wsMsg.jobId) {
			const tabId = await getTabForJob(wsMsg.jobId);
			if (typeof tabId === "number") {
				chrome.tabs.sendMessage(tabId, wsMsg).catch(() => {});

				if (
					[
						"probe_result",
						"probe_failed",
						"download_completed",
						"download_failed",
						"download_canceled",
					].includes(wsMsg.type)
				) {
					await removeJobTab(wsMsg.jobId);
				}
			}
		}
	}
}

// Clean up connections when a tab is closed
chrome.tabs.onRemoved.addListener((tabId) => {
	if (sniffedStreams.has(tabId)) {
		sniffedStreams.delete(tabId);
		console.log(`Cleaned up sniffed streams for tab ${tabId}`);
	}
	// Clear any storage job-to-tab mappings for this tab
	mutateStorageMap("jobTabMap", (map) => {
		for (const [jobId, mappedTabId] of Object.entries(map)) {
			if (mappedTabId === tabId) {
				delete map[jobId];
			}
		}
	}).catch((error) => {
		console.warn("Failed to clear closed-tab job mappings:", error);
	});
});

// Clear sniffed streams on navigation commit (main frame, non-same-document)
chrome.webNavigation.onCommitted.addListener((details) => {
	if (details.frameId === 0) {
		const tabId = details.tabId;
		if (sniffedStreams.has(tabId)) {
			sniffedStreams.delete(tabId);
			console.log(
				`Cleaned up sniffed streams for tab ${tabId} due to main-frame navigation`,
			);
		}
	}
});

// Observe media and manifest requests. URL patterns are only a hint; response
// MIME types catch opaque signed and extensionless manifest URLs.
chrome.webRequest.onBeforeRequest.addListener(
	(details) => {
		const tabId = details.tabId;
		if (tabId < 0) return;
		trackRequest(details.url, tabId);
		requestCandidates.set(details.requestId, {
			tabId,
			url: details.url,
			frameId: details.frameId,
			documentUrl: details.documentUrl || null,
			initiator: details.initiator || null,
			type: details.type || null,
		});
		const type = classifyMediaCandidate(details.url);
		const candidate = rememberCandidate(details, type);
		if (candidate?.isNew) notifyStreamCandidate(tabId, candidate.streamInfo);
	},
	{ urls: ["http://*/*", "https://*/*"] },
);

chrome.webRequest.onHeadersReceived.addListener(
	(details) => {
		const request = requestCandidates.get(details.requestId) || details;
		const contentType =
			getHeaderValue(details.responseHeaders, "content-type") || "";
		const type = classifyMediaCandidate(details.url, contentType);
		const candidate = rememberCandidate(
			{ ...request, ...details },
			type,
			contentType,
		);
		if (candidate?.changed) {
			notifyStreamCandidate(details.tabId, candidate.streamInfo);
		}
		requestCandidates.delete(details.requestId);
	},
	{ urls: ["http://*/*", "https://*/*"] },
	["responseHeaders"],
);

function clearRequestCandidate(details) {
	requestCandidates.delete(details.requestId);
}

chrome.webRequest.onCompleted.addListener(clearRequestCandidate, {
	urls: ["http://*/*", "https://*/*"],
});
chrome.webRequest.onErrorOccurred.addListener(clearRequestCandidate, {
	urls: ["http://*/*", "https://*/*"],
});

// Intercept browser downloads
if (chrome.downloads && chrome.downloads.onDeterminingFilename) {
	chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
		console.log(
			"[Interception] Download event captured. URL:",
			item.url,
			"Filename:",
			item.filename,
		);

		// Skip downloads triggered by this extension to prevent loop
		if (item.byExtensionId === chrome.runtime.id) {
			console.log(
				"[Interception] Download was triggered by this extension. Skipping.",
			);
			suggest();
			return;
		}

		const url = item.finalUrl || item.url;
		if (
			!url ||
			url.startsWith("http://localhost") ||
			url.startsWith("http://127.0.0.1") ||
			url.startsWith("ws://") ||
			url.startsWith("wss://") ||
			url.startsWith("chrome-extension://") ||
			url.startsWith("blob:") ||
			url.startsWith("data:")
		) {
			console.log("[Interception] Local or system URL. Skipping.");
			suggest();
			return;
		}

		// Perform live ping check to see if backend is available
		checkBackendAvailability().then((available) => {
			if (!available) {
				console.log(
					"[Interception] Backend is unavailable. Skipping interception.",
				);
				suggest();
				return;
			}

			console.log(
				"[Interception] Intercepting download! Cancelling Chrome native download...",
			);

			// Cancel Chrome's native download and catch any potential errors
			chrome.downloads.cancel(item.id, () => {
				const err = chrome.runtime.lastError;
				if (err) {
					console.log(
						"[Interception] Suppressed cancellation error:",
						err.message,
					);
				}
			});

			// Resolve suggest callback to release the download thread cleanly
			suggest();

			// Extract metadata. Keep the raw filename so the backend is the single
			// source of truth for filename/title extraction.
			const downloadData = {
				url: url,
				referrer: item.referrer || "",
				filename: item.filename || "downloaded_file",
				fileSize: item.fileSize,
				mime: item.mime,
			};

			console.log("[Interception] Extracted metadata:", downloadData);

			// Track initiating tabId using downloadInitiators mapping
			const tabId =
				downloadInitiators.get(url) ?? downloadInitiators.get(item.referrer);
			downloadInitiators.delete(url);
			if (item.referrer) {
				downloadInitiators.delete(item.referrer);
			}

			if (typeof tabId === "number") {
				if (!downloadData.referrer) {
					chrome.tabs.get(tabId, (t) => {
						if (!chrome.runtime.lastError && t && t.url) {
							downloadData.referrer = t.url;
						}
						sendIntercepted(tabId, downloadData);
					});
				} else {
					sendIntercepted(tabId, downloadData);
				}
			} else {
				// Fallback: search for active tab only when no better signal exists
				chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
					const tab = tabs && tabs[0];
					const activeTabId = tab?.id;
					if (
						typeof activeTabId === "number" &&
						tab.url &&
						tab.url.startsWith("http")
					) {
						if (!downloadData.referrer) {
							downloadData.referrer = tab.url;
						}
						sendIntercepted(activeTabId, downloadData);
					} else {
						console.log(
							"[Interception] No active webpage tab found. Routing directly to backend...",
						);
						routeDirectlyToBackend(OFFSCREEN_TAB_ID, downloadData);
					}
				});
			}
		});

		return true; // Keep channel open for asynchronous suggest() call
	});
}

function sendIntercepted(tabId, downloadData) {
	console.log(`[Interception] Sending INTERCEPTED_DOWNLOAD to tab ${tabId}...`);
	chrome.tabs
		.sendMessage(tabId, {
			type: "INTERCEPTED_DOWNLOAD",
			download: downloadData,
		})
		.catch((err) => {
			console.warn(
				"[Interception] Content script not responding in tab, routing directly to backend:",
				err,
			);
			routeDirectlyToBackend(tabId, downloadData);
		});
}

function routeDirectlyToBackend(tabId, downloadData) {
	const targetTabId =
		typeof tabId === "number" && tabId >= 0 ? tabId : OFFSCREEN_TAB_ID;
	const jobId = `job_intercept_${crypto.randomUUID()}`;
	const rawFilename = downloadData.filename || "downloaded_file";

	console.log(
		`[Interception] Routing direct download to backend: ${rawFilename}`,
	);
	registerJobTab(jobId, targetTabId).then(() => {
		// Pass the raw filename hint and let the backend resolve the clean
		// title/filename through title_extractor.
		sendToWS({
			type: "choose",
			jobId: jobId,
			formatId: "best",
			outputDir: "", // default dir
			conflictResolution: "rename",
			url: downloadData.url,
			filename: rawFilename,
			referer: downloadData.referrer,
			fileSize: downloadData.fileSize,
			mime: downloadData.mime,
		});
	});
}
