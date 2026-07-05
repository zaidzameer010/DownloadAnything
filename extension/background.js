/**
 * background.js — MV3 Service Worker
 * ==================================
 * Responsibilities:
 *   • Observe webRequest traffic for HLS / DASH / media / file URLs
 *   • Cache per-tab sniffed streams + request headers (chrome.storage.session
 *     so state survives SW termination — the only durable state we need)
 *   • Bridge content scripts ⇆ the local FastAPI engine over WebSocket
 *   • Intercept native downloads and route them through the engine
 *
 * MV3 notes (2026):
 *   The SW is terminated after ~30s of inactivity; a WebSocket does NOT keep
 *   it alive. While a content script is waiting on a long-running action
 *   (e.g. yt-dlp extraction), it opens a "keepalive" Port, which DOES keep
 *   the SW alive for the operation's duration. All other state is ephemeral
 *   and rebuilt on demand.
 */
"use strict";

importScripts("constants.js", "filename_extractor.js", "streams.js");

/* ── Config ─────────────────────────────────────────────────────────────── */

/* ── Tiny helpers ───────────────────────────────────────────────────────── */

const errorMessage = (err) =>
  err instanceof Error ? err.message : typeof err === "string" ? err : JSON.stringify(err);

const cloneHeaders = (headers = {}) => ({ ...headers });

/* ── Backend WebSocket client (lazy connect, auto-reconnect) ────────────── */

class BackendClient {
  constructor(url, timeoutMs) {
    this.url = url;
    this.timeoutMs = timeoutMs;
    this.socket = null;
    this.seq = 0;
    this.pending = new Map(); // requestId -> { resolve, reject, timer }
    this.openWaiters = []; // queued { resolve, reject } waiting for OPEN
  }

  _ensureSocket() {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) {
      return this.socket; // CONNECTING(0) or OPEN(1)
    }
    const ws = new WebSocket(this.url);
    this.socket = ws;

    ws.addEventListener("message", (event) => this._onMessage(event));
    ws.addEventListener("open", () => {
      const waiters = this.openWaiters.splice(0);
      for (const waiter of waiters) waiter.resolve();
    });
    const teardown = (reason) => {
      this._rejectAll(reason);
      const waiters = this.openWaiters.splice(0);
      for (const waiter of waiters) waiter.reject(new Error(reason));
      if (this.socket === ws) this.socket = null;
    };
    ws.addEventListener("close", () => teardown("Backend connection closed"));
    ws.addEventListener("error", () => {
      try {
        ws.close();
      } catch (err) {
        console.debug("[DownloadAnything] Socket close on error ignored:", err);
      }
    });
    return ws;
  }

  _onceOpen() {
    const ws = this._ensureSocket();
    if (ws.readyState === WebSocket.OPEN) return Promise.resolve();
    return new Promise((resolve, reject) =>
      this.openWaiters.push({ resolve, reject }),
    );
  }

  _onMessage(event) {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (err) {
      console.warn("[DownloadAnything] Failed to parse message frame:", err);
      return;
    }
    if (message?.type !== "response") return; // ignore async task broadcasts here
    const entry = this.pending.get(message.request_id);
    if (!entry) return;
    clearTimeout(entry.timer);
    this.pending.delete(message.request_id);
    if (message.ok) entry.resolve(message.data);
    else entry.reject(new Error(message.error || "Backend request failed"));
  }

  _rejectAll(reason) {
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(new Error(reason));
    }
    this.pending.clear();
  }

  /** Send an action; resolves with the backend `data`, rejects on error/timeout. */
  request(action, payload = {}) {
    const requestId = `ext-${Date.now()}-${this.seq++}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.delete(requestId)) {
          reject(new Error("Backend request timed out"));
        }
      }, this.timeoutMs);
      this.pending.set(requestId, { resolve, reject, timer });

      this._onceOpen()
        .then(() => {
          try {
            this.socket.send(
              JSON.stringify({ action, request_id: requestId, payload }),
            );
          } catch (err) {
            if (this.pending.delete(requestId)) {
              clearTimeout(timer);
              reject(new Error("Failed to send request to backend"));
            }
          }
        })
        .catch((err) => {
          if (this.pending.delete(requestId)) {
            clearTimeout(timer);
            reject(err);
          }
        });
    });
  }
}

const backend = new BackendClient(WS_URL, REQUEST_TIMEOUT_MS);

/* ── Content-script message router ──────────────────────────────────────── */

const ROUTERS = {
  GET_TAB_STREAMS: async (_payload, sender) => {
    const tabId = sender.tab?.id ?? -1;
    const urls = await sessionGet(`streams_${tabId}`, []);
    return { urls: Array.isArray(urls) ? urls : [] };
  },

  GET_SETTINGS: async () => backend.request("get_settings"),
  GET_HEALTH: async () => backend.request("get_health"),

  EXTRACT: async (payload) => {
    let headers = cloneHeaders(
      payload.headers ?? (await getRequestHeaders(payload.url)) ?? (await getTempHeaders(payload.url)) ?? {},
    );
    if (!headers["User-Agent"]) {
      headers["User-Agent"] = navigator.userAgent;
    }
    return backend.request("extract", {
      url: payload.url,
      page_title: payload.page_title,
      headers,
    });
  },

  DOWNLOAD: async (payload) => {
    let headers = cloneHeaders(
      payload.headers ?? (await getRequestHeaders(payload.url)) ?? (await getTempHeaders(payload.url)) ?? {},
    );
    if (Object.keys(headers).length === 0 && payload.referrer) {
      headers = cloneHeaders((await getRequestHeaders(payload.referrer)) ?? (await getTempHeaders(payload.referrer)) ?? {});
    }
    if (!headers["User-Agent"]) {
      headers["User-Agent"] = navigator.userAgent;
    }
    if (payload.referrer && !headers.Referer) {
      headers.Referer = payload.referrer;
    }
    return backend.request("download", { ...payload, headers });
  },

  // Iframe → top-frame modal relay (kept here because content scripts can't
  // message across frames directly with a target).
  SHOW_EXTRACTOR_MODAL_ON_TOP: async (payload, sender) => {
    if (sender.tab?.id != null) {
      await chrome.tabs
        .sendMessage(
          sender.tab.id,
          {
            type: "OPEN_EXTRACTOR_MODAL_IN_TOP",
            url: payload.url,
            mediaSrc: payload.mediaSrc,
            mediaTitle: payload.mediaTitle,
          },
          { frameId: 0 },
        )
        .catch((err) => {
          console.debug("[DownloadAnything] Top frame modal relay omitted:", err);
        });
    }
    return { ok: true };
  },

  ADD_ALT_BYPASS: async (payload) => {
    await addBypass(payload.url);
    return { ok: true };
  },

  BYPASS_DOWNLOAD: async (payload) => {
    await addBypass(payload.url);
    const options = { url: payload.url };
    const name = (payload.filename || "").split(/[/\\]/).pop();
    if (name) options.filename = name; // relative to the user's download dir
    await chrome.downloads.download(options);
    return { ok: true };
  },
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const type = message?.type;
  const handler = type ? ROUTERS[type] : undefined;
  if (!handler) return; // not one of ours → let another listener handle it

  handler(message.payload ?? {}, sender).then(
    (result) => sendResponse({ ok: true, data: result }),
    (error) => sendResponse({ ok: false, error: errorMessage(error) }),
  );
  return true; // keep the message channel open for the async response
});

/* ── Native-download interception ───────────────────────────────────────── */

let cachedSettings = null;
let lastSettingsFetch = 0;

async function getBackendSettings() {
  const now = Date.now();
  if (cachedSettings && (now - lastSettingsFetch < SETTINGS_CACHE_TTL)) {
    return cachedSettings;
  }

  try {
    await Promise.race([
      backend._onceOpen(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("WS Connection timeout")), 1000))
    ]);
    const response = await backend.request("get_settings");
    cachedSettings = response || {};
    lastSettingsFetch = now;
    return cachedSettings;
  } catch (err) {
    console.debug("[DownloadAnything] Failed to fetch settings from backend:", err);
    return null;
  }
}

function isMediaDownload(item) {
  const mime = item.mime || "";
  if (mime && (mime.startsWith("video/") || mime.startsWith("audio/") || /mpegurl|dash\+xml/i.test(mime))) {
    return true;
  }
  const filename = item.filename || "";
  const ext = filename.split(".").pop().split(/[?#]/)[0].toLowerCase();
  const mediaExts = new Set(MEDIA_EXTS.split("|"));
  if (ext && mediaExts.has(ext)) {
    return true;
  }
  try {
    const pathname = new URL(item.url).pathname;
    const urlExt = pathname.split(".").pop().toLowerCase();
    if (urlExt && mediaExts.has(urlExt)) {
      return true;
    }
  } catch (e) {}
  return false;
}




function shouldBypassEngineForDirectVid(item) {
  const mime = (item.mime || "").split(";")[0].trim().toLowerCase();
  if (!mime || /mpegurl|dash\+xml/i.test(mime)) {
    return false;
  }

  try {
    const pathname = new URL(item.url).pathname.toLowerCase();
    if (!pathname.endsWith(".vid")) {
      return false;
    }
  } catch (err) {
    return false;
  }

  return mime.startsWith("video/") || mime.startsWith("audio/");
}
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  (async () => {
    try {
      // 1. Check if bypassed by Alt-click or manual selection
      if (await isBypassed(item.url)) {
        await removeBypass(item.url);
        suggest();
        return;
      }

      // 2. Fetch settings and check if core engine is online/ready
      const settings = await getBackendSettings();
      if (!settings) {
        console.debug("[DownloadAnything] Engine offline. Bypassing interception.");
        suggest();
        return;
      }

      // 3. Respect user's enable_download_interception setting
      if (settings.enable_download_interception === false) {
        console.debug("[DownloadAnything] Interception disabled in settings. Bypassing.");
        suggest();
        return;
      }

      // 4. Respect user's intercept_media_only setting
      if (settings.intercept_media_only && !isMediaDownload(item)) {
        console.debug("[DownloadAnything] Bypassing non-media download (media-only mode enabled).");
        suggest();
        return;
      }

      // 4b. Direct .vid links that already resolve to normal media should stay native.
      if (shouldBypassEngineForDirectVid(item)) {
        console.debug("[DownloadAnything] Bypassing engine for direct .vid media download.");
        suggest();
        return;
      }

      // 5. Find target tab to display prompt
      let tabId = await getUrlTab(item.url);
      if (!tabId && item.referrer) tabId = await getUrlTab(item.referrer);
      if (!tabId) {
        const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
        tabId = active?.id;
      }

      // 6. Ping the tab's content script to make sure it is responsive
      let canPrompt = false;
      if (tabId != null) {
        try {
          const pingResult = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("Ping timeout")), 400);
            chrome.tabs.sendMessage(tabId, { type: "PING" }, (resp) => {
              clearTimeout(timer);
              if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
              else resolve(resp);
            });
          });
          if (pingResult && pingResult.ok) {
            canPrompt = true;
          }
        } catch (err) {
          console.debug("[DownloadAnything] Ping failed for tab", tabId, err);
        }
      }

      // 7. Intercept if content script is active; otherwise, let Chrome download natively
      if (canPrompt) {
        // Always call suggest first to complete filename determination and transition to "in progress" state
        suggest();

        try {
          chrome.downloads.cancel(item.id, () => {
            const err = chrome.runtime.lastError;
            if (err) {
              console.debug("[DownloadAnything] cancel error:", err.message);
            }
            chrome.downloads.erase({ id: item.id }, () => {
              const eraseErr = chrome.runtime.lastError;
              if (eraseErr) {
                console.debug("[DownloadAnything] erase error:", eraseErr.message);
              }
            });
          });
        } catch (err) {
          console.warn("[DownloadAnything] Could not cancel native download:", err);
        }

        await chrome.tabs
          .sendMessage(tabId, {
            type: "SHOW_ADD_DOWNLOAD_POPUP",
            url: item.url,
            filename: extractFilename(item),
            referrer: item.referrer,
          })
          .catch((err) => {
            console.debug("[DownloadAnything] Failed sending SHOW_ADD_DOWNLOAD_POPUP message:", err);
          });
      } else {
        console.debug("[DownloadAnything] Content script unresponsive or not loaded. Letting Chrome handle download.");
        suggest();
      }
    } catch (err) {
      console.error("[DownloadAnything] Error in onDeterminingFilename:", err);
      suggest();
    }
  })();
  return true; // We resolve suggest() asynchronously
});

/* ── Toolbar action: acquire media on the active tab ────────────────────── */

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab?.id) return;
  const triggerInFrame = async (frameId) => {
    try {
      const response = await chrome.tabs.sendMessage(
        tab.id,
        { type: "TRIGGER_EXTRACT" },
        { frameId },
      );
      return !!response?.found;
    } catch {
      return false;
    }
  };

  try {
    const frames = await chrome.webNavigation.getAllFrames({ tabId: tab.id });
    const orderedFrameIds = [0, ...frames.map((frame) => frame.frameId).filter((frameId) => frameId !== 0)];
    for (const frameId of orderedFrameIds) {
      if (await triggerInFrame(frameId)) {
        return;
      }
    }
  } catch (err) {
    console.warn("[DownloadAnything] Frame enumeration failed; falling back to top frame only.", err);
    await triggerInFrame(0);
  }
});

/* ── Context menus ──────────────────────────────────────────────────────── */

async function createContextMenus() {
  await chrome.contextMenus.removeAll();
  chrome.contextMenus.create({
    id: "download-link",
    title: "Download link with DownloadAnything",
    contexts: ["link"],
  });
  chrome.contextMenus.create({
    id: "download-media",
    title: "Download media with DownloadAnything",
    contexts: ["video", "audio"],
  });
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url || !tab?.id) return;
  chrome.tabs
    .sendMessage(tab.id, {
      type: "SHOW_ADD_DOWNLOAD_POPUP",
      url,
      filename: extractFilename(url),
    })
    .catch((err) => {
      console.debug("[DownloadAnything] Context menu message dispatch failed:", err);
    });
});

chrome.runtime.onInstalled.addListener(createContextMenus);
chrome.runtime.onStartup.addListener(createContextMenus);

/* ── Tab cleanup ────────────────────────────────────────────────────────── */

chrome.tabs.onRemoved.addListener((tabId) => {
  void chrome.storage.session.remove(`streams_${tabId}`);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url) {
    void chrome.storage.session.remove(`streams_${tabId}`);
    chrome.tabs.sendMessage(tabId, { type: "URL_CHANGED", url: changeInfo.url }).catch((err) => {
      // Content script may not be loaded/active yet; ignore
    });
  }
});

/* ── Keepalive port: a connected content-script Port keeps the SW alive
   for the duration of a long-running operation (extraction/download). ──── */

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "keepalive") return;
  // The port is held open by the content script while it awaits a result;
  // we simply acknowledge it. When the script disconnects, the SW is free
  // to terminate again — which is exactly what we want.
  port.onDisconnect.addListener(() => {
    console.debug("[DownloadAnything] Keepalive port disconnected.");
  });
});
