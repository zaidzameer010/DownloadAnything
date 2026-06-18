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
  } catch { /* content script may not be loaded yet */ }
}

// ── MV3 webRequest — non-blocking, read-only ──────────────────────────────
// Observes URL patterns to detect HLS/DASH/media requests.
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;
    recordStream(details.tabId, details.url);
  },
  { urls: ["http://*/*", "https://*/*"] },
  []
);

// Also catch MediaSource/XHR-loaded manifests via Content-Type header.
// "responseHeaders" alone is sufficient for Content-Type sniffing;
// "extraHeaders" is not needed here and adds unnecessary overhead.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const ct = details.responseHeaders?.find(
      (h) => h.name.toLowerCase() === "content-type"
    );
    if (
      ct &&
      /(mpegurl|dash\+xml|video\/|audio\/|application\/pdf|application\/zip|application\/x-7z-compressed|application\/x-rar-compressed|application\/octet-stream)/i.test(
        ct.value || ""
      )
    ) {
      recordStream(details.tabId, details.url);
    }
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["responseHeaders"] // removed "extraHeaders" — not needed for Content-Type
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
        const data = await sendWSRequest("extract", { url: msg.url, page_title: msg.page_title });
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
        const data = await sendWSRequest("download", msg.payload);
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