/**
 * streams.js — Stream sniffing, caching, and webRequest pipeline observers
 */
"use strict";

const STREAM_REGEX = new RegExp(`\\.(${MEDIA_EXTS}|${FILE_EXTS})(?:\\?|#|$)`, "i");

// Sequential HLS/DASH segments — filtered so a stream list isn't flooded.
const SEGMENT_REGEX =
  /(?:^|[-_])(?:chunk|seg(?:ment)?|fragment|part)[-_0-9]*\.(?:ts|m4s|aac|mp4)|(?:^|[-_])\d+\.(?:ts|m4s)/i;

const CACHE_CAP = 200;        // max entries per cache map
const MAX_STREAMS_PER_TAB = 50;

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
 * (oldest dropped by insertion order).
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

/* Temporary request headers cache in session storage to capture headers for extensionless dynamic streams. */
const TEMP_HEADERS_CAP = 300;

async function cacheTempHeaders(url, headers) {
  const map = await sessionGet("tempHeaders", {});
  if (!Object.prototype.hasOwnProperty.call(map, url)) {
    const keys = Object.keys(map);
    if (keys.length >= TEMP_HEADERS_CAP) {
      delete map[keys[0]];
    }
  }
  map[url] = headers;
  await sessionSet("tempHeaders", map);
}

async function getTempHeaders(url) {
  const map = await sessionGet("tempHeaders", {});
  return map[url] || null;
}

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
    const next = list.filter((bypassedUrl) => bypassedUrl !== url);
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
    if (!details.requestHeaders) return;
    if (details.tabId >= 0) {
      void cacheUrlTab(details.url, details.tabId);
    }

    const headers = {};
    for (const header of details.requestHeaders) {
      switch (header.name.toLowerCase()) {
        case "referer":
        case "user-agent":
        case "origin":
          headers[header.name] = header.value;
          break;
      }
    }
    cacheTempHeaders(details.url, headers);
    void cacheRequestHeaders(details.url, headers);
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["requestHeaders", "extraHeaders"],
);

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    (async () => {
      const contentTypeHeader = details.responseHeaders?.find(
        (header) => header.name.toLowerCase() === "content-type",
      );
      const contentTypeValue = contentTypeHeader?.value || "";
      if (MEDIA_MIME.test(contentTypeValue) || FILE_MIME.test(contentTypeValue)) {
        void recordStream(details.tabId, details.url, { force: true });
        
        // Promote cached temp request headers to session storage
        const headers = await getTempHeaders(details.url);
        if (headers) {
          void mapInsert("requestHeaders", details.url, headers);
        }
        // Save url→tab mapping for the dynamic stream
        void mapInsert("urlTab", details.url, details.tabId);
      }
    })();
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["responseHeaders"],
);
