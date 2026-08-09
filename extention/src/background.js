importScripts('extractor.js');
importScripts('logger.js');

const STORE_PREFIX = 'media:';
const MAX_ITEMS = 200;
const mediaWriteChains = new Map();
const mediaGenerations = new Map();
const mediaCache = new Map();

function storeKey(tabId) {
  return `${STORE_PREFIX}${tabId}`;
}

function contentTypeFromHeaders(headers) {
  return DownloadAnything.getHeader(headers, 'content-type');
}

function queueMediaOperation(tabId, operation) {
  const key = storeKey(tabId);
  const previous = mediaWriteChains.get(key) || Promise.resolve();
  const next = previous
    .catch(err => DownloadAnythingLogger.error('media chain error', { tabId, error: err.message }))
    .then(operation)
    .catch(err => DownloadAnythingLogger.error('media operation error', { tabId, error: err.message }))
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
  // Prevent a navigation from briefly exposing the previous page's streams.
  mediaCache.delete(tabId);
  queueMediaOperation(tabId, () => chrome.storage.session.remove(storeKey(tabId)));
}

function cacheMedia(tabId, entry) {
  if (tabId < 0) return;
  let list = mediaCache.get(tabId) || [];
  list = list.filter(item => item.url !== entry.url);
  list.unshift(entry);
  if (list.length > MAX_ITEMS) list = list.slice(0, MAX_ITEMS);
  mediaCache.set(tabId, list);
}

function persistMedia(tabId, entry) {
  if (tabId < 0) return;
  let generation = mediaGenerations.get(tabId);
  if (!generation) {
    generation = { id: 0, invalidatedAt: 0 };
    mediaGenerations.set(tabId, generation);
  }
  if (entry.timestamp < generation.invalidatedAt) return;
  cacheMedia(tabId, entry);
  const key = storeKey(tabId);
  queueMediaOperation(tabId, async () => {
    if (mediaGenerations.get(tabId)?.id !== generation.id) return;
    const result = await chrome.storage.session.get([key]);
    if (mediaGenerations.get(tabId)?.id !== generation.id) return;
    let list = result[key] || [];
    list = list.filter(item => item.url !== entry.url);
    list.unshift(entry);
    if (list.length > MAX_ITEMS) list = list.slice(0, MAX_ITEMS);
    await chrome.storage.session.set({ [key]: list });
  });
}

function mediaEntry(details, type, contentType) {
  return {
    url: details.url,
    type,
    contentType: contentType || '',
    statusCode: details.statusCode,
    resourceType: details.type,
    timestamp: details.timeStamp
  };
}

function captureMediaRequest(details, contentType) {
  const type = DownloadAnything.classifyMedia(details.url, contentType);
  if (!type) return;
  DownloadAnythingLogger.debug('captured media request', { url: details.url, type, tabId: details.tabId });
  persistMedia(details.tabId, mediaEntry(details, type, contentType));
}

// Capture manifest/direct-media URLs before their response completes. This is
// important for short-lived HLS requests and avoids waiting behind segment
// writes before the content script asks for fallback URLs.
chrome.webRequest.onBeforeRequest.addListener(
  details => captureMediaRequest(details, ''),
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] }
);

chrome.webRequest.onCompleted.addListener(
  details => captureMediaRequest(details, contentTypeFromHeaders(details.responseHeaders)),
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] },
  ['responseHeaders']
);

chrome.tabs.onRemoved.addListener(tabId => invalidateMedia(tabId));
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) invalidateMedia(tabId);
});

function getMediaForTab(tabId) {
  const cached = mediaCache.get(tabId);
  if (cached) return Promise.resolve(cached);

  const key = storeKey(tabId);
  return chrome.storage.session.get([key]).then(result => {
    const list = result[key] || [];
    mediaCache.set(tabId, list);
    return list;
  });
}

/* --------------------------------------------------------------------------
 * yt-dlp backend websocket bridge
 * ------------------------------------------------------------------------ */
const BACKEND_URL = 'ws://127.0.0.1:8765';
const REQUEST_TIMEOUT_MS = 120000;

let backendWs = null;
let backendConnected = false;
let reconnectTimer = null;
let reconnectDelay = 1000;
let requestCounter = 0;
const pendingRequests = new Map();

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

function pushBackendEvent(event) {
  // Reserved hook for broadcasting backend events.
  // Currently the content script does not listen for these messages, so the
  // extension does not forward them to tabs/runtime. Re-enable here once a
  // consumer (popup, badge, or content UI) is added.
  void event;
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
    return;
  }

  if (message.type === 'job_update' || message.type === 'job_removed' || message.type === 'settings') {
    pushBackendEvent(message);
  }
}

function connectBackend() {
  if (backendWs && backendWs.readyState !== WebSocket.CLOSED) return;
  let ws;
  try {
    ws = new WebSocket(BACKEND_URL);
  } catch {
    scheduleReconnect();
    return;
  }
  backendWs = ws;

  ws.onopen = () => {
    if (backendWs !== ws) return;
    backendConnected = true;
    reconnectDelay = 1000;
    DownloadAnythingLogger.info('backend connected');
    ws.send(JSON.stringify({ type: 'hello', client: 'extension' }));
    pushBackendEvent({ type: 'backend_status', connected: true });
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
    if (backendConnected) {
      backendConnected = false;
      DownloadAnythingLogger.warning('backend disconnected', { code: event.code, wasClean: event.wasClean });
      pushBackendEvent({ type: 'backend_status', connected: false });
    }
    scheduleReconnect();
  };

  ws.onerror = () => {
    DownloadAnythingLogger.warning('backend websocket error');
    // onclose follows and handles reconnect.
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  DownloadAnythingLogger.info('scheduling backend reconnect', { delayMs: reconnectDelay });
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    connectBackend();
  }, reconnectDelay);
}

function backendRequest(payload, sendResponse) {
  const ws = backendWs;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    sendResponse({ ok: false, error: 'yt-dlp backend is not connected' });
    return;
  }
  const reqId = nextReqId();
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
    DownloadAnythingLogger.error('could not send backend request', { type: payload.type, error: error instanceof Error ? error.message : String(error) });
    sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : 'Could not send backend request',
    });
  }
}

connectBackend();

// Keep the service worker (and thus the websocket) alive.
chrome.alarms.create('da-keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== 'da-keepalive') return;
  if (backendWs && backendWs.readyState === WebSocket.OPEN) {
    backendWs.send(JSON.stringify({ type: 'ping' }));
  } else {
    connectBackend();
  }
});

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

  if (message.type === 'backend:event') return false;

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
    sendResponse({ connected: backendConnected });
    return false;
  }

  if (message.type === 'backend') {
    // Relay a request/response exchange to the yt-dlp backend.
    backendRequest(message.payload || {}, sendResponse);
    return true;
  }

  if (message.type === 'getMediaForTab') {
    const tabId = message.tabId ?? sender.tab?.id ?? -1;
    getMediaForTab(tabId).then(media => sendResponse({ media }));
    return true;
  }

  if (message.type === 'getMediaForActiveTab') {
    chrome.tabs.query({ active: true, currentWindow: true }).then(tabs => {
      const tabId = tabs[0]?.id ?? -1;
      getMediaForTab(tabId).then(media => sendResponse({ tabId, media }));
    });
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

function sendToActiveTab(message) {
  return chrome.tabs.query({ active: true, currentWindow: true }).then(tabs => {
    const tab = tabs[0];
    if (!tab?.id) return null;
    return chrome.tabs.sendMessage(tab.id, message).catch(err => {
      DownloadAnythingLogger.debug('tab not reachable for intercepted download', { tabId: tab.id, error: err.message });
      return null;
    });
  }).catch(err => {
    DownloadAnythingLogger.debug('tabs query failed for intercepted download', { error: err.message });
    return null;
  });
}

function onDeterminingFilename(item, suggest) {
  // Release helper that always checks runtime.lastError so Chrome does not log
  // an unchecked error when a suggestion is invalid or the download has already
  // finished.
  function safeSuggest(suggestion) {
    try {
      if (suggestion) {
        suggest(suggestion);
      } else {
        suggest();
      }
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
    const url = item.finalUrl || item.url;
    const filename = downloadBasename(item.filename);
    const mediaType = DownloadAnything.classifyDownload(url, item.mime, filename);
    const size = (Number.isFinite(item.fileSize) && item.fileSize > 0)
      ? item.fileSize
      : (Number.isFinite(item.totalBytes) && item.totalBytes > 0 ? item.totalBytes : null);

    sendToActiveTab({
      type: 'da:interceptedDownload',
      url,
      pageUrl: item.referrer || url,
      filename,
      mediaType,
      mime: item.mime,
      size,
      sizeIsEstimate: false
    }).catch(err => DownloadAnythingLogger.debug('emit intercepted download failed', { id: item.id, error: err?.message }));
  }

  // Ignore our own downloads and non-HTTP(S) schemes.
  if (item.byExtensionId === chrome.runtime.id) {
    safeSuggest();
    return false;
  }
  const scheme = (item.url || '').split(':')[0].toLowerCase();
  if (!['http', 'https'].includes(scheme)) {
    safeSuggest();
    return false;
  }

  // If the download has already completed before we got the determination
  // event, there is nothing to intercept; just release the determiner.
  if (item.state !== 'in_progress') {
    return false;
  }

  function eraseAndEmit() {
    chrome.downloads.erase({ id: item.id }, () => {
      if (chrome.runtime.lastError) {
        DownloadAnythingLogger.debug('erase failed', { id: item.id, error: chrome.runtime.lastError.message });
      }
      emitInterceptedDownload();
    });
  }

  // Cancel while Chrome is still blocked on filename determination. Calling
  // suggest() first releases the download and can open the native Save As dialog
  // before this asynchronous cancellation runs.
  chrome.downloads.cancel(item.id, () => {
    if (chrome.runtime.lastError) {
      DownloadAnythingLogger.debug('cancel runtime error', { id: item.id, error: chrome.runtime.lastError.message });
    }

    chrome.downloads.search({ id: item.id }, (results) => {
      if (chrome.runtime.lastError) {
        DownloadAnythingLogger.debug('search after cancel failed', { id: item.id, error: chrome.runtime.lastError.message });
        eraseAndEmit();
        return;
      }

      const [download] = (results || []);
      if (!download) {
        // The item is gone from the manager, so cancel took effect. Erase any
        // lingering shelf/history entry and hand off to the backend modal.
        eraseAndEmit();
        return;
      }

      if (download.state === 'complete') {
        // The file finished before cancel could stop it. Remove it from disk
        // and erase the history entry so the backend download is the only copy.
        chrome.downloads.removeFile(item.id, () => {
          if (chrome.runtime.lastError) {
            DownloadAnythingLogger.debug('removeFile failed', { id: item.id, error: chrome.runtime.lastError.message });
          }
          eraseAndEmit();
        });
        return;
      }

      if (download.state === 'in_progress') {
        // cancel() did not take effect yet. Try once more.
        chrome.downloads.cancel(item.id, () => {
          if (chrome.runtime.lastError) {
            DownloadAnythingLogger.debug('second cancel runtime error', { id: item.id, error: chrome.runtime.lastError.message });
          }
          eraseAndEmit();
        });
        return;
      }

      // Cancelled or interrupted — the browser did not finish the file. Erase
      // the history/shelf entry and open the download modal.
      eraseAndEmit();
    });
  });

  return false;
}

chrome.downloads.onDeterminingFilename.addListener(onDeterminingFilename);
