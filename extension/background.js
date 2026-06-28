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

/* ── Config ─────────────────────────────────────────────────────────────── */

const BACKEND_BASE = "http://127.0.0.1:8000";
const WS_URL = `${BACKEND_BASE.replace(/^http/, "ws")}/ws/progress`;

// Media container/codecs + common downloadable file extensions.
const MEDIA_EXTS =
  "m3u8|mpd|mp4|webm|mkv|avi|mov|wmv|flv|mpg|mpeg|3gp|ts|mp3|aac|m4a|flac|wav|ogg|opus|wma|vid";
const FILE_EXTS =
  "zip|rar|7z|tar|gz|bz2|xz|dmg|iso|bin|img|pdf|epub|doc|docx|xls|xlsx|ppt|pptx|exe|msi|apk|pkg";
const STREAM_REGEX = new RegExp(`\\.(${MEDIA_EXTS}|${FILE_EXTS})(?:\\?|#|$)`, "i");

// Sequential HLS/DASH segments — filtered so a stream list isn't flooded.
const SEGMENT_REGEX =
  /(?:^|[-_])(?:chunk|seg(?:ment)?|fragment|part)[-_0-9]*\.(?:ts|m4s|aac|mp4)|(?:^|[-_])\d+\.(?:ts|m4s)/i;

const MEDIA_MIME = /video\/|audio\/|mpegurl|dash\+xml/i;
const FILE_MIME = /application\/(?:pdf|zip|x-7z-compressed|x-rar-compressed)/i;

const CACHE_CAP = 200;        // max entries per cache map
const MAX_STREAMS_PER_TAB = 50;
const REQUEST_TIMEOUT_MS = 90_000; // yt-dlp extraction + stream-size probing can be slow

/* ── Tiny helpers ───────────────────────────────────────────────────────── */

const errorMessage = (e) =>
  e instanceof Error ? e.message : typeof e === "string" ? e : JSON.stringify(e);

const sessionGet = async (key, fallback) => {
  try {
    const result = await chrome.storage.session.get(key);
    return result[key] !== undefined ? result[key] : fallback;
  } catch (err) {
    console.error("[DownloadAnything] session.get(%s):", key, err);
    return fallback;
  }
};

const sessionSet = async (key, value) => {
  try {
    await chrome.storage.session.set({ [key]: value });
  } catch (err) {
    console.error("[DownloadAnything] session.set(%s):", key, err);
  }
};

/**
 * Insert into a JSON map stored under `mapKey`, capped at `cap` entries
 * (oldest dropped by insertion order). NOTE: deliberately does NOT rewrite
 * storage on read — the old code did a wasteful read-modify-write on every
 * access just to "touch" recency, which is pointless for short-lived data.
 */
async function mapInsert(mapKey, subKey, value, cap = CACHE_CAP) {
  const map = await sessionGet(mapKey, {});
  if (!Object.prototype.hasOwnProperty.call(map, subKey)) {
    const keys = Object.keys(map);
    if (keys.length >= cap) delete map[keys[0]];
  }
  map[subKey] = value;
  await sessionSet(mapKey, map);
}

const mapLookup = async (mapKey, subKey) => {
  const map = await sessionGet(mapKey, {});
  return Object.prototype.hasOwnProperty.call(map, subKey) ? map[subKey] : null;
};

const mapRemove = async (mapKey, subKey) => {
  const map = await sessionGet(mapKey, {});
  if (Object.prototype.hasOwnProperty.call(map, subKey)) {
    delete map[subKey];
    await sessionSet(mapKey, map);
  }
};

/* Headers / url→tab caches. Only persisted for media/file URLs (gated by
   STREAM_REGEX) so we don't hammer storage.session on every CSS/JS/XHR. */
const isCacheable = (url) => STREAM_REGEX.test(url) && !SEGMENT_REGEX.test(url);

const getRequestHeaders = (url) => mapLookup("requestHeaders", url);
const cacheRequestHeaders = (url, headers) =>
  isCacheable(url) ? mapInsert("requestHeaders", url, headers) : Promise.resolve();

const getUrlTab = (url) => mapLookup("urlTab", url);
const cacheUrlTab = (url, tabId) =>
  tabId >= 0 && isCacheable(url)
    ? mapInsert("urlTab", url, tabId)
    : Promise.resolve();

/* Bypass set: URLs the user explicitly wants Chrome (not the engine) to handle. */
const addBypass = (url) => {
  if (!url) return Promise.resolve();
  return sessionGet("bypassed", []).then(async (list) => {
    const set = new Set(list);
    if (set.has(url)) return;
    set.add(url);
    await sessionSet("bypassed", [...set]);
  });
};

const isBypassed = async (url) => {
  const list = await sessionGet("bypassed", []);
  return list.includes(url);
};

const removeBypass = (url) =>
  sessionGet("bypassed", []).then(async (list) => {
    const next = list.filter((u) => u !== url);
    if (next.length !== list.length) await sessionSet("bypassed", next);
  });

/* ── Stream recording ───────────────────────────────────────────────────── */

async function recordStream(tabId, url, { force = false } = {}) {
  if (tabId < 0) return;
  if (!force && (!STREAM_REGEX.test(url) || SEGMENT_REGEX.test(url))) return;

  const key = `streams_${tabId}`;
  const list = await sessionGet(key, []);
  if (list.includes(url)) return;

  const next = list.length >= MAX_STREAMS_PER_TAB
    ? [...list.slice(1), url]   // drop oldest, append newest
    : [...list, url];
  await sessionSet(key, next);
  // No per-stream broadcast: content scripts pull the list on demand via
  // GET_TAB_STREAMS, so a push notification would be dead traffic.
}

/* ── webRequest observers (read-only; no webRequestBlocking needed) ─────── */

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;
    void cacheUrlTab(details.url, details.tabId);
    if (STREAM_REGEX.test(details.url)) void recordStream(details.tabId, details.url);
  },
  { urls: ["http://*/*", "https://*/*"] },
);

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0 || !details.requestHeaders || !isCacheable(details.url)) return;
    void cacheUrlTab(details.url, details.tabId);

    const headers = {};
    for (const h of details.requestHeaders) {
      switch (h.name.toLowerCase()) {
        case "cookie":
        case "referer":
        case "user-agent":
        case "origin":
          headers[h.name] = h.value;
          break;
      }
    }
    void cacheRequestHeaders(details.url, headers);
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["requestHeaders", "extraHeaders"],
);

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const ct = details.responseHeaders?.find(
      (h) => h.name.toLowerCase() === "content-type",
    );
    const value = ct?.value || "";
    if (MEDIA_MIME.test(value) || FILE_MIME.test(value)) {
      void recordStream(details.tabId, details.url, { force: true });
    }
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["responseHeaders"],
);

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
      for (const w of waiters) w.resolve();
    });
    const teardown = (reason) => {
      this._rejectAll(reason);
      const waiters = this.openWaiters.splice(0);
      for (const w of waiters) w.reject(new Error(reason));
      if (this.socket === ws) this.socket = null;
    };
    ws.addEventListener("close", () => teardown("Backend connection closed"));
    ws.addEventListener("error", () => {
      try {
        ws.close();
      } catch {
        /* ignore */
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
    } catch {
      return; // malformed frame
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
    const headers = (await getRequestHeaders(payload.url)) || undefined;
    return backend.request("extract", {
      url: payload.url,
      page_title: payload.page_title,
      headers,
    });
  },

  DOWNLOAD: async (payload) => {
    const headers = payload.headers ?? (await getRequestHeaders(payload.url)) ?? undefined;
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
        .catch(() => {
          /* top frame may not have the content script */
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
    (data) => sendResponse({ ok: true, data }),
    (error) => sendResponse({ ok: false, error: errorMessage(error) }),
  );
  return true; // keep the message channel open for the async response
});

/* ── Native-download interception ───────────────────────────────────────── */

/** Best-effort human filename from a DownloadItem or URL. Never throws. */
function extractFilename(source, suggested = "") {
  let url = "";
  let filename = suggested;
  if (source && typeof source === "object") {
    url = source.url || "";
    filename = filename || source.filename || "";
  } else if (typeof source === "string") {
    url = source;
  }

  const clean = (value) => {
    if (!value) return "";
    const base = value.split(/[/\\]/).pop().split("?")[0];
    return base && base !== "download" && !base.endsWith(".crdownload") && base.includes(".")
      ? safeDecode(base)
      : "";
  };

  if (clean(filename)) return clean(filename);

  if (url) {
    try {
      const parsed = new URL(url);
      for (const value of parsed.searchParams.values()) {
        if (value && /\.[a-z0-9]{2,5}$/i.test(value)) {
          const name = clean(value);
          if (name) return name;
        }
      }
      const segment = parsed.pathname.split("/").pop();
      if (segment && segment.includes(".")) return safeDecode(segment);
    } catch {
      /* not a URL */
    }
  }
  return "download";
}

const safeDecode = (value) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  (async () => {
    // Always resolve filename determination first: a download can only be
    // cancelled once it reaches the IN_PROGRESS state, which requires suggest().
    suggest();

    if (await isBypassed(item.url)) {
      await removeBypass(item.url);
      return; // let Chrome handle it normally
    }

    try {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    } catch (err) {
      console.warn("[DownloadAnything] Could not cancel native download:", err);
    }

    let tabId = await getUrlTab(item.url);
    if (!tabId && item.referrer) tabId = await getUrlTab(item.referrer);
    if (!tabId) {
      const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
      tabId = active?.id;
    }

    if (tabId != null) {
      await chrome.tabs
        .sendMessage(tabId, {
          type: "SHOW_ADD_DOWNLOAD_POPUP",
          url: item.url,
          filename: extractFilename(item),
        })
        .catch(() => {
          /* content script unavailable on this page */
        });
    }
  })();
  return true; // we'll call suggest() asynchronously above
});

/* ── Toolbar action: acquire media on the active tab ────────────────────── */

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab?.id) return;
  await chrome.tabs
    .sendMessage(tab.id, { type: "TRIGGER_EXTRACT" }, { frameId: 0 })
    .catch(() => {
      console.warn("[DownloadAnything] Content script not available on this tab.");
    });
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
    .catch(() => {});
});

chrome.runtime.onInstalled.addListener(createContextMenus);
chrome.runtime.onStartup.addListener(createContextMenus);

/* ── Tab cleanup ────────────────────────────────────────────────────────── */

chrome.tabs.onRemoved.addListener((tabId) => {
  void chrome.storage.session.remove(`streams_${tabId}`);
});

/* ── Keepalive port: a connected content-script Port keeps the SW alive
   for the duration of a long-running operation (extraction/download). ──── */

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "keepalive") return;
  // The port is held open by the content script while it awaits a result;
  // we simply acknowledge it. When the script disconnects, the SW is free
  // to terminate again — which is exactly what we want.
  port.onDisconnect.addListener(() => {});
});
