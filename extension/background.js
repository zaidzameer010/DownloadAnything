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

// Rolling caches to prevent memory leaks
const requestHeadersCache = new Map(); // url -> headers object
const urlTabMap = new Map();           // url -> tabId
const MAX_CACHE_ENTRIES = 200;

function cacheRequestHeaders(url, headers) {
  if (requestHeadersCache.size >= MAX_CACHE_ENTRIES) {
    const oldestKey = requestHeadersCache.keys().next().value;
    requestHeadersCache.delete(oldestKey);
  }
  requestHeadersCache.set(url, headers);
}

function cacheUrlTab(url, tabId) {
  if (tabId < 0) return;
  if (urlTabMap.size >= MAX_CACHE_ENTRIES) {
    const oldestKey = urlTabMap.keys().next().value;
    urlTabMap.delete(oldestKey);
  }
  urlTabMap.set(url, tabId);
}

// In-memory per-tab stream registry.
// chrome.storage.session is the persistent backing store so streams survive SW restarts.
const tabStreams = new Map(); // tabId -> Set<url>

// ── Restore state from session storage on SW startup ─────────────────────
async function restoreFromSession() {
  try {
    const all = await chrome.storage.session.get(null);
    for (const [key, urls] of Object.entries(all)) {
      if (!key.startsWith("streams_")) continue;
      const tabId = parseInt(key.slice(8), 10);
      if (!isNaN(tabId) && Array.isArray(urls)) {
        tabStreams.set(tabId, new Set(urls));
      }
    }
  } catch {
    // Storage may not be available in all contexts; ignore
  }
}
restoreFromSession();

// ── Record a stream URL for a tab ─────────────────────────────────────────
async function recordStream(tabId, url, forceRecord = false) {
  if (!forceRecord) {
    if (!STREAM_REGEX.test(url)) return;
  }

  // Filter out sequential chunk segments (like chunk_5.ts or segment-12.m4s)
  // to avoid flooding tab stream lists when the main stream is already captured
  const isSegment = /[-_]chunk|[-_]seg|fragment|[-_]\d+\.(ts|m4s)/i.test(url);
  if (isSegment) return;
  if (!tabStreams.has(tabId)) tabStreams.set(tabId, new Set());
  const set = tabStreams.get(tabId);
  if (set.has(url)) return;

  // Enforce a maximum cap of 50 streams per tab to prevent memory/storage bloat
  if (set.size >= 50) {
    const first = set.values().next().value;
    set.delete(first);
  }

  set.add(url);
  try {
    await chrome.storage.session.set({ [`streams_${tabId}`]: Array.from(set) });
  } catch { /* ignore storage errors */ }

  // Notify content script if present
  try {
    await chrome.tabs.sendMessage(tabId, { type: "STREAM_FOUND", url });
  } catch { /* content script may not be loaded yet */ }
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
//
// Service workers are ephemeral. We do NOT cache `ws` across event handler
// invocations — a cached socket will be stale after a SW restart. Instead,
// each backend call opens a fresh connection (or reuses the current one if
// it happens to still be open within the same SW activation), sends the
// request, and awaits the response. The promise always settles (resolve or
// reject) so there are no leaks.
//
// requestCounter is local to each SW activation; because a restarted SW
// also gets a fresh WS connection, ID collisions across sessions cannot occur.

let _ws = null;
let _requestCounter = 0;
// Map<requestId, { resolve, reject, timeoutId }>
const _pending = new Map();

const WS_REQUEST_TIMEOUT_MS = 30_000; // 30 s — matches typical backend timeout

function _getOrCreateWS() {
  if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
    return _ws;
  }

  const wsUrl = BACKEND_BASE.replace(/^http/, "ws") + "/ws/progress";
  _ws = new WebSocket(wsUrl);

  _ws.onmessage = (event) => {
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

  _ws.onclose = () => {
    // Reject all in-flight requests so callers never hang
    for (const [id, pending] of _pending) {
      clearTimeout(pending.timeoutId);
      pending.reject(new Error("WebSocket closed before response"));
    }
    _pending.clear();
    _ws = null;
  };

  _ws.onerror = () => {
    // onclose fires immediately after onerror; cleanup happens there
    if (_ws) _ws.close();
  };

  return _ws;
}

function sendWSRequest(action, payload = {}) {
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
      socket.send(JSON.stringify({ action, request_id: requestId, payload }));
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
    const set = tabStreams.get(tabId);
    sendResponse({ urls: set ? Array.from(set) : [] });
    return; // synchronous — no need to return true
  }

  if (msg.type === "EXTRACT") {
    (async () => {
      try {
        const headers = requestHeadersCache.get(msg.url) || null;
        const data = await sendWSRequest("extract", {
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
        const headers = requestHeadersCache.get(msg.payload.url) || null;
        const payload = { ...msg.payload, headers };
        const data = await sendWSRequest("download", payload);
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
        const data = await sendWSRequest("get_settings", {});
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (msg.type === "ADD_ALT_BYPASS") {
    bypassedDownloads.add(msg.url);
    sendResponse({ ok: true });
    return;
  }

  if (msg.type === "BYPASS_DOWNLOAD") {
    bypassedDownloads.add(msg.url);
    const downloadOpts = { url: msg.url };
    if (msg.filename) {
      // Chrome downloads API expects filename relative to user's download directory
      // Strip any absolute path separators just in case
      const cleanName = msg.filename.split(/[/\\]/).pop();
      if (cleanName) downloadOpts.filename = cleanName;
    }
    chrome.downloads.download(downloadOpts);
    sendResponse({ ok: true });
    return; // synchronous response
  }
});

// ── Chrome Downloads Interception ────────────────────────────────────────
const bypassedDownloads = new Set();

function extractFilename(item) {
  // 1. Try Chrome's suggested filename
  if (item.filename) {
    const name = item.filename.split(/[/\\]/).pop();
    // Exclude generic browser temporary filename stubs if possible
    if (name && name !== "download" && !name.endsWith(".crdownload")) {
      return name;
    }
  }

  // 2. Try to parse from the URL
  if (item.url) {
    try {
      const urlObj = new URL(item.url);
      
      // Look in query parameters for any value containing a file extension
      for (const [, value] of urlObj.searchParams.entries()) {
        if (value && /\.[a-zA-Z0-9]{2,5}$/.test(value)) {
          const name = value.split(/[/\\]/).pop();
          if (name) return decodeURIComponent(name);
        }
      }

      // Fallback to URL pathname segment
      const segment = urlObj.pathname.split("/").pop();
      if (segment) {
        return decodeURIComponent(segment);
      }
    } catch (e) {
      // ignore
    }
  }

  // 3. Absolute fallback
  return "download";
}

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  // Ignore downloads explicitly requested to bypass (i.e. user wants to download via Chrome)
  if (bypassedDownloads.has(item.url)) {
    bypassedDownloads.delete(item.url);
    suggest();
    return;
  }

  // Cancel the standard Chrome download immediately
  try {
    chrome.downloads.cancel(item.id);
    chrome.downloads.erase({ id: item.id });
  } catch (err) {
    console.error("[StreamSnatcher] Failed to cancel/erase download:", err);
  }

  // Find the exact tab that triggered the download, or fallback to the active tab
  (async () => {
    try {
      let targetTabId = urlTabMap.get(item.url) || null;
      
      // Check if any redirects or referrer url mapped to it
      if (!targetTabId && item.referrer) {
        targetTabId = urlTabMap.get(item.referrer) || null;
      }

      if (!targetTabId) {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) targetTabId = tab.id;
      }

      if (targetTabId) {
        // item.filename now contains the correct name resolved from Content-Disposition
        const cleanFilename = item.filename ? item.filename.split(/[/\\]/).pop() : extractFilename(item);
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
  })();

  // Tell Chrome to stop processing (ignored since we cancelled, but resolves pending state)
  suggest();
});

// ── Clean up when a tab is closed ────────────────────────────────────────
chrome.tabs.onRemoved.addListener(async (tabId) => {
  tabStreams.delete(tabId);
  try {
    await chrome.storage.session.remove(`streams_${tabId}`);
  } catch { /* ignore */ }
});

// ── Service worker lifecycle ──────────────────────────────────────────────
chrome.runtime.onStartup.addListener(() => {
  console.info("[StreamSnatcher] Service worker initialised");
});

// ── Context Menus Creation & Handling (IDM-style) ────────────────────────
function createContextMenus() {
  chrome.contextMenus.removeAll(() => {
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
  });
}

chrome.runtime.onInstalled.addListener(createContextMenus);
chrome.runtime.onStartup.addListener(createContextMenus);

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;

  // Extract filename
  let suggestedFilename = "";
  if (info.srcUrl) {
    suggestedFilename = info.srcUrl.split("/").pop().split("?")[0];
  } else if (info.linkUrl) {
    suggestedFilename = info.linkUrl.split("/").pop().split("?")[0];
  }

  if (suggestedFilename && suggestedFilename.includes(".")) {
    suggestedFilename = decodeURIComponent(suggestedFilename);
  } else {
    suggestedFilename = "download";
  }

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