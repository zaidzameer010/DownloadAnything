importScripts('constants.js');
importScripts('extractor.js');
importScripts('logger.js');

const STORE_PREFIX = 'media:';
const MAX_ITEMS = 200;
const MEDIA_FLUSH_DELAY_MS = 100;
const mediaWriteChains = new Map();
const mediaFlushTimers = new Map();
const mediaGenerations = new Map();
const mediaCache = new Map();
const hydratedMediaTabs = new Set();

// Frame & header tracking: tabId -> Map<frameId, { url, referer, origin }>
const tabFrameUrls = new Map();

function storeKey(tabId) {
  return `${STORE_PREFIX}${tabId}`;
}

function getFrameContext(tabId, frameId) {
  if (tabId < 0) return null;
  const frames = tabFrameUrls.get(tabId);
  if (!frames) return null;
  return frames.get(frameId) || frames.get(0) || null;
}

function queueMediaOperation(tabId, operation) {
  const key = storeKey(tabId);
  const previous = mediaWriteChains.get(key) || Promise.resolve();
  const next = previous
    .catch(err => DownloadAnythingLogger.error('media chain error', { tabId, error: err.message }))
    .then(operation)
    .catch(err => {
      DownloadAnythingLogger.error('media operation error', { tabId, error: err.message });
      throw err;
    })
    .finally(() => {
      if (mediaWriteChains.get(key) === next) mediaWriteChains.delete(key);
    });
  mediaWriteChains.set(key, next);
  return next;
}

function invalidateMedia(tabId) {
  if (tabId < 0) return;
  const prev = mediaGenerations.get(tabId);
  mediaGenerations.set(tabId, {
    id: (prev?.id || 0) + 1,
    invalidatedAt: Date.now(),
  });
  const flushTimer = mediaFlushTimers.get(tabId);
  if (flushTimer !== undefined) clearTimeout(flushTimer);
  mediaFlushTimers.delete(tabId);
  // Prevent a navigation from briefly exposing the previous page's streams.
  mediaCache.delete(tabId);
  hydratedMediaTabs.delete(tabId);
  tabFrameUrls.delete(tabId);
  queueMediaOperation(tabId, () => chrome.storage.session.remove(storeKey(tabId)));
}

function cacheMedia(tabId, entry) {
  if (tabId < 0 || !entry || !entry.url) return;
  const canonicalUrl = DownloadAnything.normalizeMediaUrl(entry.url);
  const normalizedEntry = { ...entry, url: canonicalUrl };

  let list = mediaCache.get(tabId) || [];
  list = list.filter(item => item.url !== normalizedEntry.url);
  list.unshift(normalizedEntry);
  if (list.length > MAX_ITEMS) list = list.slice(0, MAX_ITEMS);
  mediaCache.set(tabId, list);
}

async function hydrateMediaCache(tabId) {
  if (hydratedMediaTabs.has(tabId)) return mediaCache.get(tabId) || [];
  const generationId = mediaGenerations.get(tabId)?.id || 0;
  const pending = mediaCache.get(tabId) || [];
  const result = await chrome.storage.session.get([storeKey(tabId)]);
  if ((mediaGenerations.get(tabId)?.id || 0) !== generationId) {
    return mediaCache.get(tabId) || [];
  }
  const invalidatedAt = mediaGenerations.get(tabId)?.invalidatedAt || 0;
  const stored = Array.isArray(result[storeKey(tabId)])
    ? result[storeKey(tabId)].filter(entry => entry.timestamp >= invalidatedAt)
    : [];
  mediaCache.set(tabId, stored);
  for (const entry of [...pending].reverse()) cacheMedia(tabId, entry);
  hydratedMediaTabs.add(tabId);
  return mediaCache.get(tabId) || [];
}

function flushMedia(tabId, generationId) {
  queueMediaOperation(tabId, async () => {
    if (mediaGenerations.get(tabId)?.id !== generationId) return;
    const list = await hydrateMediaCache(tabId);
    if (mediaGenerations.get(tabId)?.id !== generationId) return;
    await chrome.storage.session.set({ [storeKey(tabId)]: list });
  });
}

function scheduleMediaFlush(tabId, generationId) {
  if (mediaFlushTimers.has(tabId)) return;
  mediaFlushTimers.set(tabId, setTimeout(() => {
    mediaFlushTimers.delete(tabId);
    flushMedia(tabId, generationId);
  }, MEDIA_FLUSH_DELAY_MS));
}

function persistMedia(tabId, entry) {
  if (tabId < 0 || !entry || !entry.url) return;
  if (DownloadAnything.isSegmentUrl(entry.url)) return;

  let generation = mediaGenerations.get(tabId);
  if (!generation) {
    generation = { id: 0, invalidatedAt: 0 };
    mediaGenerations.set(tabId, generation);
  }
  if (entry.timestamp < generation.invalidatedAt) return;
  queueMediaOperation(tabId, async () => {
    if (mediaGenerations.get(tabId)?.id !== generation.id) return;
    await hydrateMediaCache(tabId);
    if (mediaGenerations.get(tabId)?.id !== generation.id) return;
    cacheMedia(tabId, entry);
    scheduleMediaFlush(tabId, generation.id);
  });
}

function mediaEntry(details, type, contentType, sizeInfo = null) {
  const frameCtx = getFrameContext(details.tabId, details.frameId);
  const normalizedUrl = DownloadAnything.normalizeMediaUrl(details.url);
  const isMaster = DownloadAnything.isMasterUrl(normalizedUrl);

  return {
    url: normalizedUrl,
    type,
    contentType: contentType || '',
    statusCode: details.statusCode,
    resourceType: details.type,
    timestamp: details.timeStamp || Date.now(),
    isMaster,
    size: sizeInfo?.size || null,
    sizeIsEstimate: sizeInfo?.isEstimate || false,
    referer: frameCtx?.referer || frameCtx?.url || '',
    origin: frameCtx?.origin || ''
  };
}

function captureMediaRequest(details, contentType, sizeInfo = null) {
  if (!details.url || DownloadAnything.isSegmentUrl(details.url)) return;
  const type = DownloadAnything.classifyMedia(details.url, contentType);
  if (!type) return;
  persistMedia(details.tabId, mediaEntry(details, type, contentType, sizeInfo));
}

// 1. Early capture onBeforeRequest
chrome.webRequest.onBeforeRequest.addListener(
  details => captureMediaRequest(details, ''),
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] }
);

// 2. Frame URL & Header tracking onBeforeSendHeaders
chrome.webRequest.onBeforeSendHeaders.addListener(
  details => {
    if (details.tabId < 0) return;
    let frames = tabFrameUrls.get(details.tabId);
    if (!frames) {
      frames = new Map();
      tabFrameUrls.set(details.tabId, frames);
    }

    const referer = DownloadAnything.getHeader(details.requestHeaders, 'referer') || '';
    const origin = DownloadAnything.getHeader(details.requestHeaders, 'origin') || '';

    if (details.type === 'main_frame' || details.type === 'sub_frame') {
      frames.set(details.frameId, { url: details.url, referer, origin });
    }
  },
  { urls: ['<all_urls>'] },
  ['requestHeaders', 'extraHeaders']
);

// 3. Header inspection & size calculation onHeadersReceived
chrome.webRequest.onHeadersReceived.addListener(
  details => {
    const contentType = DownloadAnything.getHeader(details.responseHeaders, 'content-type');
    const sizeInfo = DownloadAnything.calculateMediaSize(details.url, details.responseHeaders);
    captureMediaRequest(details, contentType, sizeInfo);
  },
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] },
  ['responseHeaders', 'extraHeaders']
);

chrome.tabs.onRemoved.addListener(tabId => {
  invalidateMedia(tabId);
  queueMediaOperation(tabId, () => {
    mediaGenerations.delete(tabId);
  });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url || changeInfo.status === 'loading') invalidateMedia(tabId);
});

function getMediaForTab(tabId) {
  return queueMediaOperation(tabId, () => hydrateMediaCache(tabId));
}

/* --------------------------------------------------------------------------
 * Mobile Safari UA Fallback Prober
 * Scans page HTML with iOS Safari User-Agent to bypass desktop DRM/obfuscation.
 * ------------------------------------------------------------------------ */
const MOBILE_SAFARI_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1';

async function scanPageWithMobileUA(pageUrl) {
  if (!pageUrl || !/^https?:\/\//i.test(pageUrl)) return [];
  try {
    const resp = await fetch(pageUrl, {
      headers: {
        'User-Agent': MOBILE_SAFARI_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
      }
    });
    if (!resp.ok) return [];
    const text = await resp.text();
    const results = [];
    const seen = new Set();

    // Regex for video/source/track/meta media attributes
    const tagPattern = /<(?:video|source|track|meta)\s+[^>]*(?:src|content)=['"]([^'"]+)['"][^>]*>/gi;
    let match;
    while ((match = tagPattern.exec(text)) !== null) {
      const rawUrl = match[1];
      const type = DownloadAnything.classifyMedia(rawUrl, null);
      if (type && !seen.has(rawUrl)) {
        seen.add(rawUrl);
        results.push({
          url: DownloadAnything.normalizeMediaUrl(rawUrl),
          type,
          isMaster: DownloadAnything.isMasterUrl(rawUrl),
          source: 'mobile_ua:tag'
        });
      }
    }

    // Regex for direct stream/media URLs in scripts/data
    const streamPattern = /(?:https?:\/\/|\/\/)[^'"\s<>\\]+\.(?:m3u8|mpd|mp4|webm|mkv|mov|m4v|flv|m4a|mp3|aac|flac|wav|ogg|opus)(?:\?[^'"\s<>\\]+)?/gi;
    while ((match = streamPattern.exec(text)) !== null) {
      const rawUrl = match[0];
      const type = DownloadAnything.classifyMedia(rawUrl, null);
      if (type && !seen.has(rawUrl)) {
        seen.add(rawUrl);
        results.push({
          url: DownloadAnything.normalizeMediaUrl(rawUrl),
          type,
          isMaster: DownloadAnything.isMasterUrl(rawUrl),
          source: 'mobile_ua:regex'
        });
      }
    }

    return results;
  } catch (err) {
    DownloadAnythingLogger.debug('Mobile UA probe failed', { pageUrl, error: err.message });
    return [];
  }
}

/* --------------------------------------------------------------------------
 * yt-dlp backend websocket bridge
 * ------------------------------------------------------------------------ */
const BACKEND_URL = 'ws://127.0.0.1:8765';
const REQUEST_TIMEOUT_MS = 120000;

let backendWs = null;
let backendConnected = false;
let requestCounter = 0;
let backendConnecting = false;
let backendHeartbeatTimer = null;
const pendingRequests = new Map();
const backendConnectionWaiters = new Set();

function isBackendOpen() {
  return backendConnected && backendWs?.readyState === WebSocket.OPEN;
}

function resolveConnectionWaiters(connected) {
  const waiters = [...backendConnectionWaiters];
  backendConnectionWaiters.clear();
  for (const resolve of waiters) resolve(connected);
}

function waitForBackendConnection(timeoutMs = 3000) {
  if (isBackendOpen()) return Promise.resolve(true);
  connectBackend();
  return new Promise(resolve => {
    let settled = false;
    let timeout;
    const waiter = connected => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      backendConnectionWaiters.delete(waiter);
      resolve(connected);
    };
    timeout = setTimeout(() => waiter(false), timeoutMs);
    backendConnectionWaiters.add(waiter);
  });
}

function nextReqId() {
  requestCounter += 1;
  return `ext-${Date.now()}-${requestCounter}`;
}

function failPendingRequests(reason, socket) {
  for (const [reqId, pending] of pendingRequests) {
    if (socket && pending.socket !== socket) continue;
    clearTimeout(pending.timer);
    pendingRequests.delete(reqId);
    pending.sendResponse({ ok: false, error: reason });
  }
}

function onBackendMessage(event) {
  let message;
  try {
    message = JSON.parse(event.data);
  } catch {
    return;
  }
  if (!message || typeof message !== 'object') return;

  if (message.reqId && pendingRequests.has(message.reqId)) {
    const pending = pendingRequests.get(message.reqId);
    pendingRequests.delete(message.reqId);
    clearTimeout(pending.timer);
    pending.sendResponse(message);
  }
}

function connectBackend() {
  if (backendWs && backendWs.readyState !== WebSocket.CLOSED) return;
  if (backendConnecting) return;
  backendConnecting = true;

  // Probe with a silently-catchable HTTP HEAD before opening the WebSocket.
  // `new WebSocket` logs an uncatchable ERR_CONNECTION_REFUSED to the console
  // when nothing is listening, so we avoid it until we know a server is there.
  const probeUrl = BACKEND_URL.replace(/^ws/, 'http');
  fetch(probeUrl, { method: 'HEAD', mode: 'no-cors' })
    .then(() => {
      if (backendWs && backendWs.readyState !== WebSocket.CLOSED) {
        backendConnecting = false;
        return;
      }

      let ws;
      try {
        ws = new WebSocket(BACKEND_URL);
      } catch {
        backendConnecting = false;
        resolveConnectionWaiters(false);
        return;
      }
      backendWs = ws;
      backendConnecting = false;

      ws.onopen = () => {
        if (backendWs !== ws) return;
        backendConnected = true;
        DownloadAnythingLogger.info('backend connected');
        ws.send(JSON.stringify({ type: 'hello', client: 'extension' }));
        // Keep the service worker (and thus the websocket) alive. Chrome
        // extends the SW lifetime while websocket messages flow at least
        // once per 30-second window; 20s is the documented safe interval.
        if (backendHeartbeatTimer) clearInterval(backendHeartbeatTimer);
        backendHeartbeatTimer = setInterval(() => {
          if (backendWs === ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, 20000);
        resolveConnectionWaiters(true);
      };

      ws.onmessage = event => {
        if (backendWs === ws) onBackendMessage(event);
      };

      ws.onclose = event => {
        const reason = event.wasClean
          ? `Backend connection closed (code ${event.code})`
          : `Backend connection lost (code ${event.code})`;
        failPendingRequests(reason, ws);
        if (backendWs !== ws) return;
        backendWs = null;
        backendConnected = false;
        if (backendHeartbeatTimer) {
          clearInterval(backendHeartbeatTimer);
          backendHeartbeatTimer = null;
        }
        DownloadAnythingLogger.info(reason);
        resolveConnectionWaiters(false);
      };

      ws.onerror = () => {
        DownloadAnythingLogger.debug('backend websocket error');
      };
    })
    .catch(() => {
      backendConnecting = false;
      resolveConnectionWaiters(false);
    });
}

function backendRequest(payload, sendResponse) {
  const ws = backendWs;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    sendResponse({ ok: false, error: 'yt-dlp backend is not connected' });
    return;
  }
  const reqId = payload.reqId || nextReqId();
  const timer = setTimeout(() => {
    pendingRequests.delete(reqId);
    sendResponse({ ok: false, error: 'Backend request timed out' });
  }, REQUEST_TIMEOUT_MS);
  pendingRequests.set(reqId, { sendResponse, timer, socket: ws });
  try {
    ws.send(JSON.stringify({ ...payload, reqId }));
    DownloadAnythingLogger.debug('backend request sent', { type: payload.type, reqId });
  } catch (error) {
    clearTimeout(timer);
    pendingRequests.delete(reqId);
    DownloadAnythingLogger.warning('could not send backend request', { type: payload.type, error: error instanceof Error ? error.message : String(error) });
    sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : 'Could not send backend request',
    });
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'log') {
    if (backendWs && backendWs.readyState === WebSocket.OPEN) {
      try {
        backendWs.send(JSON.stringify({
          type: 'log',
          level: message.level,
          message: message.message,
          context: message.context,
          client: message.client || 'extension/unknown',
          ts: message.ts,
        }));
      } catch (err) {
        DownloadAnythingLogger.warning('dropping log message', { error: err.message });
      }
    }
    return false;
  }

  if (message.type === 'da:openProbe') {
    const tabId = sender.tab?.id;
    if (tabId == null) {
      sendResponse({ ok: false, error: 'Download request has no tab context' });
      return false;
    }
    chrome.tabs.sendMessage(tabId, message, { frameId: 0 })
      .then(response => sendResponse(response || { ok: false, error: 'Top frame did not handle the download request' }))
      .catch(error => {
        DownloadAnythingLogger.warning('could not route download request to top frame', { tabId, error: error.message });
        sendResponse({ ok: false, error: 'Top frame is not reachable' });
      });
    return true;
  }

  if (message.type === 'backendStatus') {
    if (isBackendOpen()) {
      sendResponse({ connected: true });
      return false;
    }
    waitForBackendConnection().then(connected => sendResponse({ connected }));
    return true;
  }

  if (message.type === 'backend') {
    backendRequest(message.payload || {}, sendResponse);
    return true;
  }

  if (message.type === 'getMediaForTab') {
    const tabId = message.tabId ?? sender.tab?.id ?? -1;
    getMediaForTab(tabId)
      .then(media => sendResponse({ media }))
      .catch(error => sendResponse({ media: [], error: error.message }));
    return true;
  }

  // Handle media links sent from content script (sniffed or extracted)
  if (message.type === 'da:addMediaLinks') {
    const tabId = sender.tab?.id ?? message.tabId ?? -1;
    const links = Array.isArray(message.media) ? message.media : (message.entry ? [message.entry] : []);
    for (const link of links) {
      if (link && link.url) {
        const frameCtx = getFrameContext(tabId, sender.frameId || 0);
        persistMedia(tabId, {
          url: DownloadAnything.normalizeMediaUrl(link.url),
          type: link.type || DownloadAnything.classifyMedia(link.url, link.contentType) || 'video',
          contentType: link.contentType || '',
          timestamp: link.timestamp || Date.now(),
          isMaster: DownloadAnything.isMasterUrl(link.url),
          size: link.size || null,
          sizeIsEstimate: link.sizeIsEstimate || false,
          label: link.label || '',
          source: link.source || 'content',
          referer: link.referer || frameCtx?.referer || frameCtx?.url || '',
          origin: link.origin || frameCtx?.origin || ''
        });
      }
    }
    sendResponse({ ok: true, count: links.length });
    return false;
  }

  // Handle Mobile UA fallback probe request
  if (message.type === 'da:probeMobileUA') {
    const pageUrl = message.pageUrl || sender.tab?.url || '';
    scanPageWithMobileUA(pageUrl)
      .then(sources => sendResponse({ ok: true, sources }))
      .catch(err => sendResponse({ ok: false, error: err.message, sources: [] }));
    return true;
  }

  return false;
});

/* --------------------------------------------------------------------------
 * Chrome download interception
 * -------------------------------------------------------------------------- */

function downloadBasename(filename) {
  if (!filename) return '';
  const parts = filename.split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

async function sendToActiveTab(message) {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) return null;
    try {
      return await chrome.tabs.sendMessage(tab.id, message);
    } catch (err) {
      DownloadAnythingLogger.debug('tab not reachable for intercepted download', { tabId: tab.id, error: err.message });
      return null;
    }
  } catch (err) {
    DownloadAnythingLogger.debug('tabs query failed for intercepted download', { error: err.message });
    return null;
  }
}

function onDeterminingFilename(item, suggest) {
  let suggestionSent = false;

  // Release helper that always checks runtime.lastError so Chrome does not log
  // an unchecked error when a suggestion is invalid or the download has already
  // finished.
  function safeSuggest(suggestion) {
    if (suggestionSent) return true;
    suggestionSent = true;
    try {
      suggest(suggestion);
    } catch (err) {
      DownloadAnythingLogger.debug('suggest threw', { id: item.id, error: err?.message });
      return false;
    }
    const lastError = chrome.runtime.lastError;
    if (lastError) {
      DownloadAnythingLogger.debug('suggest runtime error', { id: item.id, error: lastError.message });
      return false;
    }
    return true;
  }

  function emitInterceptedDownload() {
    const rawUrl = item.finalUrl || item.url;
    const url = DownloadAnything.normalizeMediaUrl(rawUrl);
    const filename = downloadBasename(item.filename);
    const mediaType = DownloadAnything.classifyDownload(url, item.mime, filename);
    const size = (Number.isFinite(item.fileSize) && item.fileSize > 0)
      ? item.fileSize
      : (Number.isFinite(item.totalBytes) && item.totalBytes > 0 ? item.totalBytes : null);

    return sendToActiveTab({
      type: 'da:interceptedDownload',
      url,
      pageUrl: item.referrer || url,
      filename,
      mediaType,
      mime: item.mime,
      size,
      sizeIsEstimate: false
    });
  }

  if (item.byExtensionId === chrome.runtime.id) {
    safeSuggest();
    return;
  }
  const scheme = (item.url || '').split(':')[0].toLowerCase();
  if (!['http', 'https'].includes(scheme)) {
    safeSuggest();
    return;
  }

  if (item.state !== 'in_progress') {
    safeSuggest();
    return;
  }

  async function resumeBrowserDownload() {
    safeSuggest();
    try {
      await chrome.downloads.resume(item.id);
    } catch (err) {
      DownloadAnythingLogger.debug('resume failed', { id: item.id, error: err.message });
    }
  }

  async function eraseAndEmit() {
    if (!isBackendOpen()) {
      await resumeBrowserDownload();
      return;
    }
    try {
      const response = await emitInterceptedDownload();
      if (!response?.ok) {
        await resumeBrowserDownload();
        return;
      }
      try {
        await chrome.downloads.erase({ id: item.id });
      } catch (err) {
        DownloadAnythingLogger.debug('erase failed', { id: item.id, error: err.message });
      }
      // Do NOT call suggest() here. The download was cancelled; releasing the
      // filename determiner makes Chrome resume it natively, which would show
      // both the native download and the extension modal. suggest() calls are
      // ignored for cancelled downloads, so leaving it uncalled is safe.
    } catch (error) {
      DownloadAnythingLogger.debug('emit intercepted download failed', { id: item.id, error: error?.message });
      await resumeBrowserDownload();
    }
  }

  async function inspectAfterCancel(attempt) {
    let download;
    try {
      [download] = await chrome.downloads.search({ id: item.id });
    } catch (err) {
      DownloadAnythingLogger.debug('search after cancel failed', { id: item.id, error: err.message });
      safeSuggest();
      return;
    }

    if (!download) {
      await eraseAndEmit();
      return;
    }

    if (download.state === 'complete') {
      safeSuggest();
      return;
    }

    if (download.state === 'in_progress') {
      if (attempt < 2) await cancelAndInspect(attempt + 1);
      else safeSuggest();
      return;
    }

    await eraseAndEmit();
  }

  async function cancelAndInspect(attempt) {
    try {
      await chrome.downloads.cancel(item.id);
    } catch (err) {
      DownloadAnythingLogger.debug('cancel failed', { id: item.id, attempt, error: err.message });
    }
    await inspectAfterCancel(attempt);
  }

  // Cancel while Chrome is still blocked on filename determination. Calling
  // suggest() first releases the download and can open the native Save As dialog
  // before this asynchronous cancellation runs.
  //
  // The listener MUST return true synchronously: per the downloads API docs,
  // if an async listener neither calls suggest() synchronously nor returns
  // true, Chrome calls suggest() automatically — which would release the
  // download natively before our cancel runs, showing both the native
  // download and the extension modal.
  void (async () => {
    const connected = await waitForBackendConnection();
    if (connected) await cancelAndInspect(1);
    else safeSuggest();
  })();

  return true;
}

chrome.downloads.onDeterminingFilename.addListener(onDeterminingFilename);
