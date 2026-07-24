import { PING_URL } from "../lib/constants.js";
import {
	classifyMediaCandidate as classifyByMime,
	shouldInterceptDownload,
} from "../lib/file_types.js";
import { createLogger } from "../lib/logger.js";
import { classifyDirectMedia, trackDirectMedia } from "./direct_media.js";
import { canonicalizeCandidateUrl } from "./sniff_common.js";
import { classifyStream, trackStream } from "./stream_extractor.js";

const logger = createLogger("service-worker");

const OFFSCREEN_TAB_ID = -1;
const MAX_SNIFFED_STREAMS_PER_TAB = 100;

let backendAvailable = false;

async function pingBackend() {
	try {
		const response = await fetch(PING_URL, {
			method: "GET",
			cache: "no-cache",
			signal: AbortSignal.timeout(800),
		});
		if (response.ok) {
			const data = await response.json();
			return data && data.status === "ok";
		}
	} catch (e) {
		logger.debug("Ping backend failed:", e.message);
	}
	return false;
}

async function updateBackendStatus(available) {
	if (backendAvailable === available) return;
	backendAvailable = available;
	logger.info(`[Status] Backend availability changed: ${available}`);

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
		logger.warn("Failed to update extension action badge/state:", err);
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
const pendingRefreshByTab = new Map();
const pendingRefreshByPageUrl = new Map();
const REFRESH_TTL_MS = 60_000;
let pendingRefreshCleanup = null;
let storageMutationQueue = Promise.resolve();
let offscreenMessageQueue = Promise.resolve();

// Setup 1-min alarm keepalive to wake service worker
chrome.alarms.create("swKeepAliveAlarm", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
	if (alarm.name === "swKeepAliveAlarm") {
		logger.debug("Service Worker keep-alive heartbeat alarm fired.");
	}
});

// Broadcast action clicked to content script
chrome.action.onClicked.addListener((tab) => {
	if (typeof tab.id === "number") {
		chrome.tabs
			.sendMessage(tab.id, { type: "EXTENSION_ACTIVATED" })
			.catch((err) => {
				logger.warn("Failed to activate extension on click:", err);
			});
	}
});

// Expired URL refresh tracking
function _isRefreshExpired(entry) {
	return !entry || Date.now() > entry.expiresAt;
}

function _prunePendingRefreshEntries() {
	const now = Date.now();
	for (const [tabId, entry] of pendingRefreshByTab.entries()) {
		if (!entry || now > entry.expiresAt || entry.consumed) {
			pendingRefreshByTab.delete(tabId);
		}
	}
	for (const [pageUrl, entries] of pendingRefreshByPageUrl.entries()) {
		const alive = entries.filter(
			(entry) => entry && now <= entry.expiresAt && !entry.consumed,
		);
		if (alive.length === 0) {
			pendingRefreshByPageUrl.delete(pageUrl);
		} else if (alive.length !== entries.length) {
			pendingRefreshByPageUrl.set(pageUrl, alive);
		}
	}
	if (
		pendingRefreshByTab.size === 0 &&
		pendingRefreshByPageUrl.size === 0 &&
		pendingRefreshCleanup
	) {
		clearTimeout(pendingRefreshCleanup);
		pendingRefreshCleanup = null;
	}
}

function _scheduleRefreshCleanup() {
	if (pendingRefreshCleanup) return;
	pendingRefreshCleanup = setTimeout(() => {
		pendingRefreshCleanup = null;
		_prunePendingRefreshEntries();
	}, REFRESH_TTL_MS);
}

function _findPendingRefresh(item) {
	if (typeof item.tabId === "number" && item.tabId >= 0) {
		const refresh = pendingRefreshByTab.get(item.tabId);
		if (refresh && !_isRefreshExpired(refresh) && !refresh.consumed) {
			return {
				refresh,
				key: canonicalizeCandidateUrl(refresh.pageUrl),
				byTab: true,
			};
		}
	}
	if (item.referrer) {
		const key = canonicalizeCandidateUrl(item.referrer);
		const entries = pendingRefreshByPageUrl.get(key);
		if (Array.isArray(entries)) {
			const refresh = entries.find(
				(entry) => entry && !_isRefreshExpired(entry) && !entry.consumed,
			);
			if (refresh) {
				return { refresh, key, byTab: false };
			}
		}
	}
	return null;
}

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
		logger.warn("Unable to send message to WebSocket client:", error);
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

// Storage helpers for Job/Tab mappings. Reads are also chained through the
// mutation queue so a read that races a pending write always observes the
// most recent persisted state.
function readStorageMap(mapName) {
	return storageMutationQueue.then(async () => {
		const data = await chrome.storage.local.get(mapName);
		const storedMap = data[mapName];
		return storedMap &&
			typeof storedMap === "object" &&
			!Array.isArray(storedMap)
			? storedMap
			: {};
	});
}

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
	const map = await readStorageMap("jobTabMap");
	return map[jobId] ?? null;
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
	const map = await readStorageMap("urlTabMap");
	return map[url] ?? null;
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
		classifyByMime(url, contentType) ||
		classifyStream(url, contentType) ||
		classifyDirectMedia(url, contentType)
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
	logger.info(
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
	if (typeof tabId === "number" && tabId >= 0 && url?.startsWith("http")) {
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
			logger.warn("Rejected untrusted offscreen message");
			return false;
		}
		queueOffscreenMessage(message).catch((error) => {
			logger.warn("Failed to handle offscreen message:", error);
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
				logger.warn("Failed to send SHOW_MODAL to top frame:", err);
			});
		sendResponse({ status: "forwarded" });
		return true;
	}

	if (message.type === "PROBE_MEDIA") {
		chrome.tabs.get(tabId, async (tab) => {
			if (chrome.runtime.lastError) {
				logger.warn(
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
		(async () => {
			try {
				await registerJobTab(message.jobId, tabId);
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
			} catch (error) {
				logger.warn("Failed to register job tab for file check:", error);
			}
		})();
		sendResponse({ status: "checking" });
		return true;
	}

	if (message.type === "START_DOWNLOAD") {
		(async () => {
			try {
				await registerJobTab(message.jobId, tabId);

				const sendChoose = (pageUrl) => {
					sendToWS({
						type: "choose",
						jobId: message.jobId,
						formatId: message.formatId,
						outputDir: message.outputDir,
						conflictResolution: message.conflictResolution || "replace",
						torrentSelectedFileIndices: message.torrentSelectedFileIndices,
						url: message.url,
						title: message.title,
						filename: message.filename,
						referer: message.referer,
						pageUrl,
						fileSize: message.fileSize,
						mime: message.mime,
					});
				};

				if (typeof tabId === "number" && tabId >= 0) {
					chrome.tabs.get(tabId, (tab) => {
						const pageUrl =
							!chrome.runtime.lastError && tab?.url ? tab.url : message.pageUrl;
						sendChoose(pageUrl);
					});
				} else {
					sendChoose(message.pageUrl);
				}
			} catch (error) {
				logger.warn("Failed to register job tab for download:", error);
			}
		})();
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
		sendResponse({ status: "sent" });
		return true;
	}

	if (message.type === "GET_SETTINGS") {
		chrome.storage.local.get("settings").then((data) => {
			sendResponse({ settings: data.settings || null });
		});
		return true;
	}

	if (message.type === "REQUEST_BROWSE") {
		chrome.storage.local.set({ lastBrowseTabId: tabId }).then(() => {
			sendToWS({
				type: "browse_directory",
				path: message.path || message.initialDir || null,
			});
		});
		sendResponse({ status: "sent" });
		return true;
	}

	if (message.type === "SHOW_TOAST_IN_TOP_FRAME") {
		chrome.tabs
			.sendMessage(
				tabId,
				{
					type: "SHOW_TOAST",
					message: message.message,
					isError: message.isError,
				},
				{ frameId: 0 },
			)
			.catch((err) => {
				logger.warn("Failed to forward SHOW_TOAST to top frame:", err);
			});
		sendResponse({ status: "forwarded" });
		return true;
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
		sendToWS({ type: "get_settings" });
	} else if (message.type === "WS_CLOSE") {
		checkBackendAvailability();
		broadcastToAllTabs({ type: "SERVER_DISCONNECTED" });
	} else if (message.type === "WS_MESSAGE") {
		const wsMsg = message.payload;

		if (wsMsg.type === "settings_data" && wsMsg.settings) {
			chrome.storage.local.set({ settings: wsMsg.settings }).catch(() => {});
			broadcastToAllTabs(wsMsg);
			return;
		}

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

		if (wsMsg.type === "needs_refresh") {
			const pageUrl = wsMsg.pageUrl;
			const jobId = wsMsg.jobId;
			if (!pageUrl) {
				logger.warn(`[Refresh] No pageUrl for job ${jobId}`);
				return;
			}
			const canonicalPageUrl = canonicalizeCandidateUrl(pageUrl);
			logger.info(
				`[Refresh] Registered source page for job ${jobId}: ${canonicalPageUrl}`,
			);
			_prunePendingRefreshEntries();
			const entry = {
				jobId,
				pageUrl,
				expiresAt: Date.now() + REFRESH_TTL_MS,
				consumed: false,
			};
			const existing = pendingRefreshByPageUrl.get(canonicalPageUrl) || [];
			existing.push(entry);
			pendingRefreshByPageUrl.set(canonicalPageUrl, existing);
			_scheduleRefreshCleanup();
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
			} else {
				logger.warn(
					`[Routing] No tab for probe_started ${wsMsg.jobId}; broadcasting`,
				);
				broadcastToAllTabs(wsMsg);
			}
			return;
		}

		if (wsMsg.jobId) {
			const tabId = await getTabForJob(wsMsg.jobId);
			if (typeof tabId === "number") {
				chrome.tabs.sendMessage(tabId, wsMsg).catch(() => {});

				// Keep job→tab across probe_result: START_DOWNLOAD reuses the
				// probe jobId, so download_progress still needs this mapping.
				// Clear only on terminal download/probe-failure events.
				if (
					[
						"probe_failed",
						"download_completed",
						"download_failed",
						"download_canceled",
					].includes(wsMsg.type)
				) {
					await removeJobTab(wsMsg.jobId);
				}
			} else {
				// Desktop-app jobs have no extension tab — don't spam broadcast.
				if (wsMsg.type === "download_progress") {
					return;
				}
				logger.warn(
					`[Routing] No tab for job ${wsMsg.jobId}; broadcasting ${wsMsg.type}`,
				);
				broadcastToAllTabs(wsMsg);
			}
		}
	}
}

// Clean up connections when a tab is closed
chrome.tabs.onRemoved.addListener((tabId) => {
	if (sniffedStreams.has(tabId)) {
		sniffedStreams.delete(tabId);
		logger.info(`Cleaned up sniffed streams for tab ${tabId}`);
	}
	if (pendingRefreshByTab.has(tabId)) {
		pendingRefreshByTab.delete(tabId);
		logger.info(`Cleaned up pending refresh for tab ${tabId}`);
	}
	// Clear any storage job-to-tab mappings for this tab
	mutateStorageMap("jobTabMap", (map) => {
		for (const [jobId, mappedTabId] of Object.entries(map)) {
			if (mappedTabId === tabId) {
				delete map[jobId];
			}
		}
	}).catch((error) => {
		logger.warn("Failed to clear closed-tab job mappings:", error);
	});
});

// Map tabs that navigate to a pending refresh source page. This lets the user
// open the page manually (or reuse an already-open tab) instead of the
// extension creating a new tab.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
	if (!tab.url) return;
	if (changeInfo.url || changeInfo.status === "complete") {
		const canonical = canonicalizeCandidateUrl(tab.url);
		const entries = pendingRefreshByPageUrl.get(canonical);
		if (Array.isArray(entries)) {
			const refresh = entries.find(
				(entry) => entry && !entry.consumed && !_isRefreshExpired(entry),
			);
			if (refresh) {
				pendingRefreshByTab.set(tabId, refresh);
				logger.debug(
					`[Refresh] Tab ${tabId} matched source page ${tab.url} for job ${refresh.jobId}`,
				);
			}
		}
	}
});

// Clear sniffed streams on navigation commit (main frame, non-same-document)
chrome.webNavigation.onCommitted.addListener((details) => {
	if (details.frameId === 0) {
		const tabId = details.tabId;
		if (sniffedStreams.has(tabId)) {
			sniffedStreams.delete(tabId);
			logger.info(
				`Cleaned up sniffed streams for tab ${tabId} due to main-frame navigation`,
			);
		}
		if (pendingRefreshByTab.has(tabId)) {
			const entry = pendingRefreshByTab.get(tabId);
			const canonical = canonicalizeCandidateUrl(entry?.pageUrl || "");
			const entries = pendingRefreshByPageUrl.get(canonical);
			if (
				!Array.isArray(entries) ||
				!entries.includes(entry)
			) {
				pendingRefreshByTab.delete(tabId);
			}
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

// Intercept browser downloads. Any non-local, non-system URL triggered by
// the user is handed to the backend, except HTML/CSS/JS page assets.
if (chrome.downloads?.onDeterminingFilename) {
	chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
		logger.info(
			"[Interception] Download event captured. URL:",
			item.url,
			"Filename:",
			item.filename,
			"MIME:",
			item.mime,
		);

		// Skip downloads triggered by this extension to prevent loop
		if (item.byExtensionId === chrome.runtime.id) {
			logger.info(
				"[Interception] Download was triggered by this extension. Skipping.",
			);
			suggest();
			return;
		}

		// If this download is from a tab the user opened for a URL refresh, route
		// it back to the existing job instead of creating a new one. Stale or
		// already consumed entries are ignored and pruned.
		const refreshMatch = _findPendingRefresh(item);
		if (refreshMatch) {
			const { refresh, key, byTab } = refreshMatch;
			refresh.consumed = true;
			const entries = pendingRefreshByPageUrl.get(key);
			if (Array.isArray(entries)) {
				const idx = entries.indexOf(refresh);
				if (idx >= 0) {
					entries.splice(idx, 1);
				}
				if (entries.length === 0) {
					pendingRefreshByPageUrl.delete(key);
				}
			}
			if (byTab) {
				pendingRefreshByTab.delete(item.tabId);
			}
			logger.info(
				`[Interception] Refresh download from tab ${item.tabId ?? "(referrer)"} for job ${refresh.jobId}`,
			);
			const refreshUrl = item.finalUrl || item.url;
			const refreshReferrer = item.referrer || refresh.pageUrl;
			chrome.downloads.cancel(item.id, () => {
				if (chrome.runtime.lastError) {
					logger.info(
						"[Interception] Suppressed cancellation error:",
						chrome.runtime.lastError.message,
					);
				}
				suggest();
				sendDownloadUrlToBackend(refresh.jobId, {
					url: refreshUrl,
					referrer: refreshReferrer,
				});
			});
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
			logger.info("[Interception] Local or system URL. Skipping.");
			suggest();
			return;
		}

		if (!shouldInterceptDownload(url, item.mime, item.filename)) {
			logger.info(
				"[Interception] Page asset (HTML/CSS/JS) or empty URL. Skipping.",
			);
			suggest();
			return;
		}

		// Perform live ping check to see if backend is available
		checkBackendAvailability().then((available) => {
			if (!available) {
				logger.info(
					"[Interception] Backend is unavailable. Skipping interception.",
				);
				suggest();
				return;
			}

			logger.info(
				"[Interception] Intercepting download! Cancelling Chrome native download...",
			);

			// Extract metadata. Keep the raw filename so the backend is the single
			// source of truth for filename/title extraction.
			const downloadData = {
				url: url,
				referrer: item.referrer || "",
				filename: item.filename || "downloaded_file",
				fileSize: item.fileSize,
				mime: item.mime,
			};

			logger.info("[Interception] Extracted metadata:", downloadData);

			// Track initiating tabId using downloadInitiators mapping
			const tabId =
				downloadInitiators.get(url) ?? downloadInitiators.get(item.referrer);
			downloadInitiators.delete(url);
			if (item.referrer) {
				downloadInitiators.delete(item.referrer);
			}

			// Cancel the native download. Resolve the filename event immediately so
			// Chrome does not time it out. Send the intercepted data from inside the
			// cancel callback so runtime.lastError is consumed before any new API call.
			const proceed = (targetId) => {
				chrome.tabs.get(targetId, (t) => {
					if (
						!chrome.runtime.lastError &&
						t &&
						t.url &&
						!downloadData.referrer
					) {
						downloadData.referrer = t.url;
					}
					sendIntercepted(targetId, downloadData);
				});
			};
			const fallback = () => {
				chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
					if (chrome.runtime.lastError) {
						logger.info(
							"[Interception] Tab query error:",
							chrome.runtime.lastError.message,
						);
					}
					const tab = tabs?.[0];
					const activeTabId = tab?.id;
					if (typeof activeTabId === "number" && tab.url?.startsWith("http")) {
						if (!downloadData.referrer) {
							downloadData.referrer = tab.url;
						}
						sendIntercepted(activeTabId, downloadData);
					} else {
						logger.info(
							"[Interception] No active webpage tab found. Routing directly to backend...",
						);
						routeDirectlyToBackend(OFFSCREEN_TAB_ID, downloadData);
					}
				});
			};

			// Cancel the native download. The callback wrapper forces us to read
			// runtime.lastError so Chrome does not log an unchecked warning.
			new Promise((resolve) => {
				chrome.downloads.cancel(item.id, () => {
					const err = chrome.runtime.lastError;
					if (err) {
						logger.info(
							"[Interception] Suppressed cancellation error:",
							err.message,
						);
					}
					resolve(undefined);
				});
			}).then(() => {
				// Release the filename event and then start the intercepted workflow.
				suggest();
				if (typeof tabId === "number") {
					proceed(tabId);
				} else {
					fallback();
				}
			});
		});

		return true; // Keep channel open for asynchronous suggest() call
	});
}

function sendDownloadUrlToBackend(jobId, downloadData) {
	logger.info(`[Interception] Sending download_url for job ${jobId}`);
	sendToWS({
		type: "download_url",
		jobId,
		url: downloadData.url,
		referer: downloadData.referrer,
	});
}

function sendIntercepted(tabId, downloadData) {
	logger.info(`[Interception] Sending INTERCEPTED_DOWNLOAD to tab ${tabId}...`);
	chrome.tabs
		.sendMessage(tabId, {
			type: "INTERCEPTED_DOWNLOAD",
			download: downloadData,
		})
		.catch((err) => {
			logger.warn(
				"[Interception] Content script not responding in tab, routing directly to backend:",
				err,
			);
			routeDirectlyToBackend(tabId, downloadData);
		});
}

async function routeDirectlyToBackend(tabId, downloadData) {
	const targetTabId =
		typeof tabId === "number" && tabId >= 0 ? tabId : OFFSCREEN_TAB_ID;
	const jobId = `job_intercept_${crypto.randomUUID()}`;
	const rawFilename = downloadData.filename || "downloaded_file";

	logger.info(
		`[Interception] Routing direct download to backend: ${rawFilename}`,
	);
	try {
		await registerJobTab(jobId, targetTabId);
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
	} catch (error) {
		logger.warn("[Interception] Failed to route direct download:", error);
	}
}
