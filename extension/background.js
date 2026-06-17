/**
 * background.js — MV3 Service Worker
 * Responsibilities:
 *  - Observe webRequest traffic for HLS/DASH/media URLs
 *  - Cache discovered streams per-tab (backed by chrome.storage.session for SW resilience)
 *  - Relay sniffed URLs to content scripts on demand
 *  - Bridge content-script ⇆ FastAPI backend
 */

const STREAM_REGEX = /\.(m3u8|mpd|mp4|webm|mov|ogg|m4a|ts|mp3|aac|flac|wav|opus|zip|pdf|rar|7z|tar|gz|dmg|exe)(\?|#|$)/i;
const BACKEND_BASE = "http://127.0.0.1:8000";

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
async function recordStream(tabId, url) {
  if (!STREAM_REGEX.test(url)) return;
  if (!tabStreams.has(tabId)) tabStreams.set(tabId, new Set());
  const set = tabStreams.get(tabId);
  if (set.has(url)) return;

  set.add(url);
  try {
    await chrome.storage.session.set({ [`streams_${tabId}`]: Array.from(set) });
  } catch { /* ignore storage errors */ }

  // Notify content script if present
  try {
    await chrome.tabs.sendMessage(tabId, { type: "STREAM_FOUND", url });
  } catch { /* content script may not be loaded */ }
}

// ── MV3 webRequest — non-blocking, read-only ──────────────────────────────
// Observes URL patterns to detect HLS/DASH/media requests
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;
    recordStream(details.tabId, details.url);
  },
  { urls: ["http://*/*", "https://*/*"] },
  []
);

// Also catch MediaSource/XHR-loaded manifests via Content-Type header.
// 'extraHeaders' is required in MV3 to access response headers that are
// sent with CORS or other protective mechanisms.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const ct = details.responseHeaders?.find(
      (h) => h.name.toLowerCase() === "content-type"
    );
    if (ct && /(mpegurl|dash\+xml|video\/|audio\/|application\/pdf|application\/zip|application\/x-7z-compressed|application\/x-rar-compressed|application\/octet-stream)/i.test(ct.value || "")) {
      recordStream(details.tabId, details.url);
    }
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["responseHeaders", "extraHeaders"]
);

// ── WebSocket Connection for Service Worker ──────────────────────────────
let ws = null;
const pendingRequests = new Map();
let requestCounter = 0;

function connectWS() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }
  const wsUrl = BACKEND_BASE.replace(/^http/, "ws") + "/ws/progress";
  ws = new WebSocket(wsUrl);

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "response") {
        const { request_id, ok, data, error } = msg;
        if (pendingRequests.has(request_id)) {
          const { resolve, reject } = pendingRequests.get(request_id);
          pendingRequests.delete(request_id);
          if (ok) {
            resolve(data);
          } else {
            reject(new Error(error || "Request failed"));
          }
        }
      }
    } catch (e) {
      console.error("[StreamSnatcher] Error parsing WS message:", e);
    }
  };

  ws.onclose = () => {
    ws = null;
  };

  ws.onerror = () => {
    if (ws) ws.close();
    ws = null;
  };
}

function sendWSRequest(action, payload = {}) {
  return new Promise((resolve, reject) => {
    connectWS();

    let attempts = 0;
    const checkAndSend = () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        const requestId = `ext-${Date.now()}-${requestCounter++}`;
        pendingRequests.set(requestId, { resolve, reject });
        ws.send(JSON.stringify({ action, request_id: requestId, payload }));
      } else if (ws && ws.readyState === WebSocket.CONNECTING && attempts < 50) {
        attempts++;
        setTimeout(checkAndSend, 100);
      } else {
        reject(new Error("WebSocket server is offline or unreachable"));
      }
    };

    checkAndSend();
  });
}

// ── Message relay from content scripts ────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GET_TAB_STREAMS") {
    const tabId = sender.tab?.id ?? msg.tabId;
    const set = tabStreams.get(tabId);
    sendResponse({ urls: set ? Array.from(set) : [] });
    return true;
  }

  if (msg.type === "EXTRACT") {
    sendWSRequest("extract", { url: msg.url, page_title: msg.page_title })
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // keep channel open for async response
  }

  if (msg.type === "DOWNLOAD") {
    sendWSRequest("download", msg.payload)
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.type === "GET_SETTINGS") {
    sendWSRequest("get_settings", {})
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
});

// ── Clean up when a tab is closed ────────────────────────────────────────
chrome.tabs.onRemoved.addListener(async (tabId) => {
  tabStreams.delete(tabId);
  try {
    await chrome.storage.session.remove(`streams_${tabId}`);
  } catch { /* ignore */ }
});

// ── Service worker lifecycle ──────────────────────────────────────────────
chrome.runtime.onStartup?.addListener?.(() => {
  console.info("[StreamSnatcher] Service worker initialised");
});