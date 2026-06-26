/**
 * background.js — MV3 Service Worker
 * Responsibilities:
 *  - Observe webRequest traffic for HLS/DASH/media URLs
 *  - Cache discovered streams per-tab (backed by chrome.storage.session for SW resilience)
 *  - Relay sniffed URLs to content scripts on demand
 *  - Bridge content-script ⇆ FastAPI backend via WebSocket
 *
 * NOTE: WebSocket connections and in-flight requests are intentionally re-created
 * on every SW activation. The SW can be terminated at any time; chrome.storage.session
 * backs the only state that must survive a restart (tab stream lists).
 */

const STREAM_REGEX = /\.(m3u8|mpd|mp4|webm|mkv|avi|mov|wmv|flv|mpg|mpeg|3gp|ts|mp3|aac|m4a|flac|wav|ogg|opus|wma|vid|zip|rar|7z|tar|gz|bz2|xz|dmg|iso|bin|img|pdf|epub|doc|docx|xls|xlsx|ppt|pptx|exe|msi|apk|pkg)(\?|#|$)/i;
const BACKEND_BASE = "http://127.0.0.1:8000";

// ── Session-Backed Caches for MV3 resilience ─────────────────────────────
const MAX_CACHE_ENTRIES = 200;

async function getSessionData(key, defaultValue) {
  try {
    const result = await chrome.storage.session.get(key);
    return result[key] !== undefined ? result[key] : defaultValue;
  } catch (err) {
    console.error(`[StreamSnatcher] Error reading session key ${key}:`, err);
    return defaultValue;
  }
}

async function setSessionData(key, value) {
  try {
    await chrome.storage.session.set({ [key]: value });
  } catch (err) {
    console.error(`[StreamSnatcher] Failed to set ${key} in session storage:`, err);
  }
}

// 1. Request Headers Cache (url -> headers object)
async function getCachedRequestHeaders(url) {
  try {
    const cache = await getSessionData("requestHeadersCache", {});
    if (cache[url]) {
      const val = cache[url];
      delete cache[url];
      cache[url] = val;
      await setSessionData("requestHeadersCache", cache);
      return val;
    }
  } catch (err) {
    console.error("[StreamSnatcher] Error reading/updating request headers cache:", err);
  }
  return null;
}

async function cacheRequestHeaders(url, headers) {
  try {
    const cache = await getSessionData("requestHeadersCache", {});
    const keys = Object.keys(cache);
    if (cache[url] !== undefined) {
      delete cache[url];
    } else if (keys.length >= MAX_CACHE_ENTRIES) {
      delete cache[keys[0]];
    }
    cache[url] = headers;
    await setSessionData("requestHeadersCache", cache);
  } catch (err) {
    console.error("[StreamSnatcher] Error caching request headers:", err);
  }
}

// 2. URL -> Tab ID Map (url -> tabId)
async function getCachedUrlTab(url) {
  try {
    const cache = await getSessionData("urlTabMap", {});
    if (cache[url] !== undefined) {
      const val = cache[url];
      delete cache[url];
      cache[url] = val;
      await setSessionData("urlTabMap", cache);
      return val;
    }
  } catch (err) {
    console.error("[StreamSnatcher] Error reading/updating URL tab map:", err);
  }
  return null;
}

async function cacheUrlTab(url, tabId) {
  if (tabId < 0) return;
  try {
    const cache = await getSessionData("urlTabMap", {});
    const keys = Object.keys(cache);
    if (cache[url] !== undefined) {
      delete cache[url];
    } else if (keys.length >= MAX_CACHE_ENTRIES) {
      delete cache[keys[0]];
    }
    cache[url] = tabId;
    await setSessionData("urlTabMap", cache);
  } catch (err) {
    console.error("[StreamSnatcher] Error caching URL tab mapping:", err);
  }
}

// 3. Bypassed Downloads Set
async function isDownloadBypassed(url) {
  const bypassed = await getSessionData("bypassedDownloads", []);
  return bypassed.includes(url);
}

async function addBypassDownload(url) {
  try {
    const bypassed = await getSessionData("bypassedDownloads", []);
    if (!bypassed.includes(url)) {
      bypassed.push(url);
      await setSessionData("bypassedDownloads", bypassed);
    }
  } catch (err) {
    console.error("[StreamSnatcher] Error adding bypass URL:", err);
  }
}

async function removeBypassDownload(url) {
  try {
    const bypassed = await getSessionData("bypassedDownloads", []);
    const index = bypassed.indexOf(url);
    if (index !== -1) {
      bypassed.splice(index, 1);
      await setSessionData("bypassedDownloads", bypassed);
    }
  } catch (err) {
    console.error("[StreamSnatcher] Error removing bypass URL:", err);
  }
}

// ── Record a stream URL for a tab ─────────────────────────────────────────
async function recordStream(tabId, url, forceRecord = false) {
  if (!forceRecord) {
    if (!STREAM_REGEX.test(url)) return;
  }

  // Filter out sequential chunk segments (like chunk_5.ts or segment-12.m4s)
  // to avoid flooding tab stream lists when the main stream is already captured
  const isSegment = /[-_]chunk|[-_]seg|fragment|[-_]\d+\.(ts|m4s)/i.test(url);
  if (isSegment) return;

  try {
    const streamsList = await getSessionData(`streams_${tabId}`, []);
    const set = new Set(streamsList);
    if (set.has(url)) return;

    // Enforce a maximum cap of 50 streams per tab to prevent memory/storage bloat
    if (set.size >= 50) {
      const first = set.values().next().value;
      set.delete(first);
    }

    set.add(url);
    await setSessionData(`streams_${tabId}`, Array.from(set));

    // Notify content script if present
    try {
      await chrome.tabs.sendMessage(tabId, { type: "STREAM_FOUND", url });
    } catch { /* content script may not be loaded yet */ }
  } catch (err) {
    console.error("[StreamSnatcher] Error recording stream:", err);
  }
}

// ── MV3 webRequest — non-blocking, read-only ──────────────────────────────
// Observes URL patterns to detect HLS/DASH/media requests and capture metadata.
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;
    // Map URL to the tab initiating the request
    cacheUrlTab(details.url, details.tabId);
    
    if (STREAM_REGEX.test(details.url)) {
      recordStream(details.tabId, details.url, false);
    }
  },
  { urls: ["http://*/*", "https://*/*"] },
  []
);

// Capture cookies, referer, user-agent, and origin for all tab requests
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0) return;
    
    cacheUrlTab(details.url, details.tabId);

    const headers = {};
    if (details.requestHeaders) {
      for (const h of details.requestHeaders) {
        const nameLower = h.name.toLowerCase();
        if (
          nameLower === "cookie" ||
          nameLower === "referer" ||
          nameLower === "user-agent" ||
          nameLower === "origin"
        ) {
          headers[h.name] = h.value;
        }
      }
    }
    // Cache headers to use them for stream extraction or route verification
    cacheRequestHeaders(details.url, headers);
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["requestHeaders", "extraHeaders"]
);

// Also catch MediaSource/XHR-loaded manifests via Content-Type header.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const ct = details.responseHeaders?.find(
      (h) => h.name.toLowerCase() === "content-type"
    );
    if (ct) {
      const val = ct.value || "";
      // If it's explicitly a media stream MIME type, force record it even without file extension
      if (/video\/|audio\/|mpegurl|dash\+xml/i.test(val)) {
        recordStream(details.tabId, details.url, true);
      } else if (/(application\/pdf|application\/zip|application\/x-7z-compressed|application\/x-rar-compressed)/i.test(val)) {
        recordStream(details.tabId, details.url, true);
      }
    }
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["responseHeaders"]
);

// ── WebSocket helper ──────────────────────────────────────────────────────
let activeWebSocket = null;
let _requestCounter = 0;
// Map<requestId, { resolve, reject, timeoutId }>
const _pending = new Map();

const WS_REQUEST_TIMEOUT_MS = 30_000; // 30 s — matches typical backend timeout

function _getOrCreateWS() {
  if (activeWebSocket && (activeWebSocket.readyState === WebSocket.OPEN || activeWebSocket.readyState === WebSocket.CONNECTING)) {
    return activeWebSocket;
  }

  const wsUrl = BACKEND_BASE.replace(/^http/, "ws") + "/ws/progress";
  activeWebSocket = new WebSocket(wsUrl);

  activeWebSocket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "response") {
        const { request_id, ok, data, error } = msg;
        const pending = _pending.get(request_id);
        if (pending) {
          clearTimeout(pending.timeoutId);
          _pending.delete(request_id);
          if (ok) {
            pending.resolve(data);
          } else {
            pending.reject(new Error(error || "Request failed"));
          }
        }
      }
    } catch (e) {
      console.error("[StreamSnatcher] Error parsing WS message:", e);
    }
  };

  activeWebSocket.onclose = () => {
    // Reject all in-flight requests so callers never hang
    for (const [id, pending] of _pending) {
      clearTimeout(pending.timeoutId);
      pending.reject(new Error("WebSocket closed before response"));
    }
    _pending.clear();
    activeWebSocket = null;
  };

  activeWebSocket.onerror = () => {
    // onclose fires immediately after onerror; cleanup happens there
    if (activeWebSocket) activeWebSocket.close();
  };

  return activeWebSocket;
}

function sendWebSocketRequest(action, payload = {}) {
  return new Promise((resolve, reject) => {
    const socket = _getOrCreateWS();

    const requestId = `ext-${Date.now()}-${_requestCounter++}`;

    // Per-request timeout so promises never leak on silent server failures
    const timeoutId = setTimeout(() => {
      if (_pending.has(requestId)) {
        _pending.delete(requestId);
        reject(new Error("WebSocket request timed out"));
      }
    }, WS_REQUEST_TIMEOUT_MS);

    _pending.set(requestId, { resolve, reject, timeoutId });

    const doSend = () => {
      try {
        socket.send(JSON.stringify({ action, request_id: requestId, payload }));
      } catch (err) {
        clearTimeout(timeoutId);
        _pending.delete(requestId);
        reject(err);
      }
    };

    if (socket.readyState === WebSocket.OPEN) {
      doSend();
    } else if (socket.readyState === WebSocket.CONNECTING) {
      socket.addEventListener("open", doSend, { once: true });
      socket.addEventListener("error", () => {
        clearTimeout(timeoutId);
        _pending.delete(requestId);
        reject(new Error("WebSocket server is offline or unreachable"));
      }, { once: true });
    } else {
      clearTimeout(timeoutId);
      _pending.delete(requestId);
      reject(new Error("WebSocket server is offline or unreachable"));
    }
  });
}

// ── Message relay from content scripts ────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GET_TAB_STREAMS") {
    const tabId = sender.tab?.id ?? msg.tabId;
    getSessionData(`streams_${tabId}`, []).then((urls) => {
      sendResponse({ urls });
    }).catch((err) => {
      console.error("[StreamSnatcher] Error fetching streams from session storage:", err);
      sendResponse({ urls: [] });
    });
    return true; // async response
  }

  if (msg.type === "SHOW_EXTRACTOR_MODAL_ON_TOP") {
    if (sender.tab && sender.tab.id) {
      chrome.tabs.sendMessage(sender.tab.id, {
        type: "OPEN_EXTRACTOR_MODAL_IN_TOP",
        url: msg.url,
        mediaSrc: msg.mediaSrc,
        mediaTitle: msg.mediaTitle
      }, { frameId: 0 }).catch(err => {
        console.warn("[StreamSnatcher] Failed to send OPEN_EXTRACTOR_MODAL_IN_TOP to main frame:", err);
      });
    }
    sendResponse({ ok: true });
    return;
  }

  if (msg.type === "EXTRACT") {
    (async () => {
      try {
        const headers = await getCachedRequestHeaders(msg.url);
        const data = await sendWebSocketRequest("extract", {
          url: msg.url,
          page_title: msg.page_title,
          headers
        });
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true; // keep channel open for async response
  }

  if (msg.type === "DOWNLOAD") {
    (async () => {
      try {
        const headers = await getCachedRequestHeaders(msg.payload.url);
        const payload = { ...msg.payload, headers };
        const data = await sendWebSocketRequest("download", payload);
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (msg.type === "GET_SETTINGS") {
    (async () => {
      try {
        const data = await sendWebSocketRequest("get_settings", {});
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (msg.type === "ADD_ALT_BYPASS") {
    addBypassDownload(msg.url).then(() => {
      sendResponse({ ok: true });
    }).catch(err => {
      sendResponse({ ok: false, error: err.message });
    });
    return true;
  }

  if (msg.type === "BYPASS_DOWNLOAD") {
    (async () => {
      try {
        await addBypassDownload(msg.url);
        const downloadOpts = { url: msg.url };
        if (msg.filename) {
          // Chrome downloads API expects filename relative to user's download directory
          // Strip any absolute path separators just in case
          const cleanName = msg.filename.split(/[/\\]/).pop();
          if (cleanName) downloadOpts.filename = cleanName;
        }
        await chrome.downloads.download(downloadOpts);
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }
});

// ── Chrome Downloads Interception ────────────────────────────────────────
function extractFilename(itemOrUrl, suggestedName = "") {
  let url = "";
  let filename = suggestedName;

  if (itemOrUrl && typeof itemOrUrl === "object") {
    url = itemOrUrl.url || "";
    filename = filename || itemOrUrl.filename || "";
  } else if (typeof itemOrUrl === "string") {
    url = itemOrUrl;
  }

  // 1. Clean suggested filename from path
  if (filename) {
    const name = filename.split(/[/\\]/).pop().split("?")[0];
    if (name && name !== "download" && !name.endsWith(".crdownload") && name.includes(".")) {
      return decodeURIComponent(name);
    }
  }

  // 2. Parse from URL query parameters (e.g. ?file=CCleaner.exe)
  if (url) {
    try {
      const urlObj = new URL(url);
      for (const [, value] of urlObj.searchParams.entries()) {
        if (value && /\.[a-zA-Z0-9]{2,5}$/.test(value)) {
          const name = value.split(/[/\\]/).pop();
          if (name && !name.endsWith(".crdownload")) {
            return decodeURIComponent(name);
          }
        }
      }

      // 3. Parse from URL pathname segment
      const segment = urlObj.pathname.split("/").pop();
      if (segment && segment.includes(".")) {
        return decodeURIComponent(segment);
      }
    } catch (e) {
      // ignore
    }
  }

  return "download";
}

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  (async () => {
    let cancelled = false;
    try {
      const isBypassed = await isDownloadBypassed(item.url);
      if (isBypassed) {
        await removeBypassDownload(item.url);
        suggest();
        return;
      }

      // Cancel the standard Chrome download immediately
      try {
        await chrome.downloads.cancel(item.id);
        await chrome.downloads.erase({ id: item.id });
        cancelled = true;
      } catch (err) {
        console.error("[StreamSnatcher] Failed to cancel/erase download:", err);
      }

      let targetTabId = await getCachedUrlTab(item.url);
      
      // Check if any redirects or referrer url mapped to it
      if (!targetTabId && item.referrer) {
        targetTabId = await getCachedUrlTab(item.referrer);
      }

      if (!targetTabId) {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) targetTabId = tab.id;
      }

      if (targetTabId) {
        // item.filename now contains the correct name resolved from Content-Disposition
        const cleanFilename = extractFilename(item);
        await chrome.tabs.sendMessage(targetTabId, {
          type: "SHOW_ADD_DOWNLOAD_POPUP",
          url: item.url,
          filename: cleanFilename
        });
      } else {
        console.warn("[StreamSnatcher] No tab context to show the download popup");
      }
    } catch (err) {
      console.warn("[StreamSnatcher] Could not display popup on tab (injecting context script might be disabled on this tab/page):", err);
    }
    
    if (!cancelled) {
      suggest();
    }
  })();

  return true; // Tells Chrome we will call suggest() asynchronously
});

// ── Clean up when a tab is closed ────────────────────────────────────────
chrome.tabs.onRemoved.addListener(async (tabId) => {
  try {
    await chrome.storage.session.remove(`streams_${tabId}`);
  } catch (err) {
    console.error("[StreamSnatcher] Error removing streams for tab:", err);
  }
});

// ── Service worker lifecycle ──────────────────────────────────────────────
chrome.runtime.onStartup.addListener(() => {
  console.info("[StreamSnatcher] Service worker initialised");
});

// ── Context Menus Creation & Handling (IDM-style) ────────────────────────
async function createContextMenus() {
  try {
    await chrome.contextMenus.removeAll();
    chrome.contextMenus.create({
      id: "download-link",
      title: "Download Link with DownloadAnything",
      contexts: ["link"]
    });
    chrome.contextMenus.create({
      id: "download-media",
      title: "Download Media with DownloadAnything",
      contexts: ["video", "audio"]
    });
  } catch (err) {
    console.error("[StreamSnatcher] Error setting up context menus:", err);
  }
}

chrome.runtime.onInstalled.addListener(createContextMenus);
chrome.runtime.onStartup.addListener(createContextMenus);

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;

  const suggestedFilename = extractFilename(url);

  if (tab && tab.id) {
    try {
      await chrome.tabs.sendMessage(tab.id, {
        type: "SHOW_ADD_DOWNLOAD_POPUP",
        url: url,
        filename: suggestedFilename
      });
    } catch (err) {
      console.warn("[StreamSnatcher] Failed to display popup on context menu click:", err);
    }
  }
});