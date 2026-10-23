// dax extension service worker (MV3)
// Handles RPC communication with dax backend, network stream sniffing,
// context menus, and tab badge feedback.

const DEFAULT_CONFIG = {
  apiPort: 16801,
  apiToken: '',
};

let cachedConfig = null;

async function getConfig() {
  if (cachedConfig) return cachedConfig;
  const stored = await chrome.storage.local.get(DEFAULT_CONFIG);
  cachedConfig = {
    apiPort: Number(stored.apiPort) || DEFAULT_CONFIG.apiPort,
    apiToken: (stored.apiToken || '').trim(),
  };
  return cachedConfig;
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local') {
    cachedConfig = null;
    if (changes.apiPort || changes.apiToken) {
      closeActiveSocket('config changed');
    }
  }
});

// ---- Multiplexed RPC Client ----
let activeWs = null;
let activeWsPromise = null;
let idleTimer = null;
let nextReqId = 1;
const pending = new Map(); // id -> { resolve, reject, timer }

function closeActiveSocket(reason = 'idle') {
  if (idleTimer) {
    clearTimeout(idleTimer);
    idleTimer = null;
  }
  if (activeWs) {
    try {
      activeWs.close(1000, reason);
    } catch {}
    activeWs = null;
    activeWsPromise = null;
  }
  for (const [id, req] of pending) {
    clearTimeout(req.timer);
    req.reject(new Error(`socket closed (${reason})`));
  }
  pending.clear();
}

function resetIdleTimer() {
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (pending.size === 0) closeActiveSocket('idle timeout');
  }, 12_000);
}

async function getConnectedSocket() {
  const config = await getConfig();
  if (!config.apiToken) {
    throw new Error('No API token configured — paste it in the extension options');
  }

  if (activeWs && activeWs.readyState === WebSocket.OPEN) {
    resetIdleTimer();
    return activeWs;
  }

  if (activeWsPromise) return activeWsPromise;

  activeWsPromise = new Promise((resolve, reject) => {
    const wsUrl = `ws://127.0.0.1:${config.apiPort}/ws?token=${encodeURIComponent(config.apiToken)}`;
    const ws = new WebSocket(wsUrl);
    let settled = false;

    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { ws.close(); } catch {}
      reject(new Error('Connection timed out — is dax running?'));
    }, 10_000);

    ws.onopen = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      activeWs = ws;
      activeWsPromise = null;
      resetIdleTimer();
      resolve(ws);
    };

    ws.onclose = (ev) => {
      const isBadToken = ev.code === 4401;
      const errMsg = isBadToken
        ? 'Bad API token — check it in the extension options'
        : `Connection closed (${ev.code})`;

      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(errMsg));
      }

      for (const [id, req] of pending) {
        clearTimeout(req.timer);
        req.reject(new Error(errMsg));
      }
      pending.clear();
      activeWs = null;
      activeWsPromise = null;
    };

    ws.onerror = () => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(`Failed to connect to dax on port ${config.apiPort} — is dax running?`));
      }
    };

    ws.onmessage = (ev) => {
      resetIdleTimer();
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }

      // Ignore broadcast messages (they have no id)
      if (typeof data.id !== 'number') return;

      const req = pending.get(data.id);
      if (!req) return;

      pending.delete(data.id);
      clearTimeout(req.timer);

      if (data.error !== undefined) {
        req.reject(new Error(data.error));
      } else {
        req.resolve(data.result);
      }
    };
  });

  return activeWsPromise;
}

async function rpc(method, params = {}) {
  const ws = await getConnectedSocket();
  const id = nextReqId++;

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`RPC timeout for '${method}'`));
    }, 30_000);

    pending.set(id, { resolve, reject, timer });
    try {
      ws.send(JSON.stringify({ id, method, params }));
    } catch (e) {
      pending.delete(id);
      clearTimeout(timer);
      reject(e);
    }
  });
}

// ---- Media Sniffing & Stream Capture ----
const CAPTURE_MAX = 12;
const HEADER_ALLOW = /^(?:cookie|referer|user-agent|accept-language|authorization|x-)/;

function filterHeaders(raw) {
  return raw
    .filter((h) => h.name && HEADER_ALLOW.test(h.name.toLowerCase()) && h.value)
    .map((h) => ({ name: h.name.toLowerCase(), value: h.value }));
}

const pendingReq = new Map(); // requestId -> { headers, at }
const headersByUrl = new Map(); // url -> { headers, at } — download requests
// carry no requestId the downloads API exposes, so URL is the correlation key.
function noteReqHeaders(id, raw) {
  if (pendingReq.size > 500) {
    const now = Date.now();
    for (const [k, v] of pendingReq) {
      if (now - v.at > 60_000) pendingReq.delete(k);
    }
  }
  pendingReq.set(id, { headers: filterHeaders(raw), at: Date.now() });
}

function noteUrlHeaders(url, headers) {
  if (!headers.length) return;
  if (headersByUrl.size > 500) {
    const now = Date.now();
    for (const [k, v] of headersByUrl) {
      if (now - v.at > 60_000) headersByUrl.delete(k);
    }
  }
  headersByUrl.set(url, { headers, at: Date.now() });
}

function findUrlHeaders(...urls) {
  for (const u of urls) {
    if (!u) continue;
    const e = headersByUrl.get(u);
    if (e) return e.headers;
  }
  return [];
}

function takeReqHeaders(id) {
  const e = pendingReq.get(id);
  pendingReq.delete(id);
  return e?.headers ?? [];
}

const captured = new Map(); // tabId -> [{ url, headers, at, manifest, kind, mime }]
let hydrated = null;
let persistTimer = null;

async function hydrate() {
  if (hydrated) return hydrated;
  hydrated = chrome.storage.session
    .get('mediaCapture')
    .then((v) => {
      for (const [tabId, list] of Object.entries(v.mediaCapture ?? {})) {
        captured.set(Number(tabId), list);
      }
    })
    .catch(() => {});
  return hydrated;
}

function queuePersist() {
  if (persistTimer) return;
  persistTimer = setTimeout(() => {
    persistTimer = null;
    const obj = {};
    for (const [tabId, list] of captured) {
      if (list.length) obj[tabId] = list;
    }
    chrome.storage.session.set({ mediaCapture: obj }).catch(() => {});
  }, 800);
}

function mediaKindOf(url, mime) {
  const lowerUrl = url.toLowerCase();
  const path = (new URL(url, 'http://localhost')).pathname.toLowerCase();

  if (/\.m3u8(?:[?#]|$)/.test(lowerUrl) || /mpegurl/i.test(mime)) {
    const isMaster = lowerUrl.includes('master');
    return { kind: isMaster ? 'hls-master' : 'hls', manifest: true, label: isMaster ? 'HLS Master' : 'HLS Stream' };
  }
  if (/\.mpd(?:[?#]|$)/.test(lowerUrl) || /dash\+xml/i.test(mime)) {
    return { kind: 'dash', manifest: true, label: 'DASH Manifest' };
  }
  if (/^(?:video|audio)\//i.test(mime) && !mime.includes('mp2t')) {
    const ext = path.split('.').pop() || 'media';
    return { kind: 'file', manifest: false, label: ext.toUpperCase() };
  }
  if (/\.(?:mp4|webm|mkv|mov|m4v|flv|mp3|m4a|ogg|opus|wav|flac|aac)(?:[/?#]|$)/.test(path)) {
    const ext = path.split('.').pop() || 'media';
    return { kind: 'file', manifest: false, label: ext.toUpperCase() };
  }
  return null;
}

function isSegment(url) {
  try {
    const path = new URL(url).pathname;
    const base = path.split('/').filter(Boolean).pop() ?? '';
    const dot = base.lastIndexOf('.');
    if (dot < 0) return false;
    const ext = base.slice(dot + 1).toLowerCase();
    if (ext === 'm3u8' || ext === 'mpd') return false;
    const stem = base.slice(0, dot).toLowerCase();
    return /^(?:seg|segment|chunk|init|frag|part|range|piece)/.test(stem) || /^\d+$/.test(stem);
  } catch {
    return false;
  }
}

function mimeOf(rawHeaders) {
  try {
    return (rawHeaders.find((h) => h.name.toLowerCase() === 'content-type')?.value ?? '')
      .split(';')[0]
      .trim()
      .toLowerCase();
  } catch {
    return '';
  }
}

async function updateBadge(tabId) {
  if (tabId <= 0) return;
  const count = (captured.get(tabId) ?? []).length;
  if (count > 0) {
    chrome.action.setBadgeText({ text: String(count), tabId }).catch(() => {});
    chrome.action.setBadgeBackgroundColor({ color: '#10b981', tabId }).catch(() => {});
  } else {
    chrome.action.setBadgeText({ text: '', tabId }).catch(() => {});
  }
}

async function rememberMedia(tabId, url, headers, mime) {
  await hydrate();
  if (!tabId || tabId < 0) return;
  const match = mediaKindOf(url, mime);
  if (!match || isSegment(url)) return;

  const list = captured.get(tabId) ?? [];
  const existing = list.find((m) => m.url === url);
  if (existing) {
    existing.at = Date.now();
    if (headers.length) existing.headers = headers;
    if (match.kind) existing.kind = match.kind;
    if (match.label) existing.label = match.label;
  } else {
    list.push({
      url,
      headers,
      at: Date.now(),
      manifest: match.manifest,
      kind: match.kind,
      label: match.label,
      mime: mime || '',
    });
  }

  captured.set(tabId, list.slice(-CAPTURE_MAX));
  queuePersist();
  updateBadge(tabId).catch(() => {});
}

function manifestTier(url) {
  try {
    const stem = new URL(url).pathname.split('/').pop()?.toLowerCase() ?? '';
    return stem.includes('master') ? 2 : stem.includes('index') ? 1 : 0;
  } catch {
    return 0;
  }
}

async function captureContexts(tabId, title = '') {
  await hydrate();
  const list = captured.get(tabId) ?? [];
  const manifests = list
    .filter((m) => m.manifest)
    .sort((a, b) => manifestTier(b.url) - manifestTier(a.url) || b.at - a.at);
  const files = list.filter((m) => !m.manifest).sort((a, b) => b.at - a.at);
  return [...manifests, ...files].map((m) => ({
    url: m.url,
    headers: m.headers,
    kind: m.kind,
    label: m.label,
    // Page title of the tab the request was sniffed on — names the
    // extension-fallback item when no extractor metadata exists.
    ...(title ? { title } : {}),
  }));
}

// Prefer the page's own semantic title asked of the content script
// (og:title/h1/document.title with site chrome stripped). When no content
// script can answer, the raw tab title is the last resort.
async function domTitle(tabId, fallback = '') {
  try {
    const t = await chrome.tabs.sendMessage(tabId, { type: 'dax-dom-title' });
    return (typeof t === 'string' ? t.trim() : '') || fallback;
  } catch {
    return fallback;
  }
}

// WebRequest interceptors
chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    const headers = filterHeaders(details.requestHeaders ?? []);
    noteReqHeaders(details.requestId, details.requestHeaders ?? []);
    noteUrlHeaders(details.url, headers);
    rememberMedia(details.tabId, details.url, headers, '').catch(() => {});
  },
  { urls: ['http://*/*', 'https://*/*'] },
  ['requestHeaders', 'extraHeaders']
);

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    rememberMedia(
      details.tabId,
      details.url,
      takeReqHeaders(details.requestId),
      mimeOf(details.responseHeaders ?? [])
    ).catch(() => {});
  },
  { urls: ['http://*/*', 'https://*/*'] },
  ['responseHeaders']
);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => takeReqHeaders(details.requestId),
  { urls: ['http://*/*', 'https://*/*'] }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  captured.delete(tabId);
  queuePersist();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) {
    captured.delete(tabId);
    queuePersist();
    updateBadge(tabId).catch(() => {});
  }
});

// ---- Chrome Download Interception ----
// onDeterminingFilename is the takeover point: the full DownloadItem
// (filename, mime, sizes, finalUrl after redirects, referrer, danger) is
// resolved there and the file hasn't hit disk yet. We let Chrome keep its
// filename (suggest() with no args), cancel the browser download, and push
// the URL + all metadata to dax for probing instead.
const interceptedIds = new Set(); // download ids we cancelled — erased on interrupted

const baseName = (p) => (p || '').split(/[\\/]/).pop() || '';
const extOf = (name) => {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(i + 1).toLowerCase() : '';
};

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  // Required exactly once per listener — we never rename, we take over.
  suggest();
  handleIntercept(item).catch(() => {});
});

async function handleIntercept(item) {
  if (!(await getConfig()).apiToken) return;
  // Any extension-initiated download is programmatic work, not a user file
  // grab — skipping them also breaks our own intercept→restore loop below.
  if (item.byExtensionId) return;
  const url = item.finalUrl || item.url || '';
  // blob:/data:/file:/chrome: URLs can't be re-fetched by the app.
  if (!/^https?:\/\//i.test(url)) return;

  interceptedIds.add(item.id);
  try {
    await chrome.downloads.cancel(item.id);
  } catch {
    // Already finished or gone — leave Chrome's download alone.
    interceptedIds.delete(item.id);
    return;
  }

  const name = baseName(item.filename);
  const headers = findUrlHeaders(item.finalUrl, item.url);
  try {
    await rpc('push', {
      url,
      referer: item.referrer || '',
      title: name || 'download',
      contexts: headers.length ? [{ url, headers }] : [],
      download: {
        id: item.id,
        url: item.url,
        finalUrl: item.finalUrl,
        referrer: item.referrer,
        filename: item.filename,
        name,
        ext: extOf(name),
        mime: item.mime,
        fileSize: item.fileSize,
        totalBytes: item.totalBytes,
        bytesReceived: item.bytesReceived,
        startTime: item.startTime,
        endTime: item.endTime,
        estimatedEndTime: item.estimatedEndTime,
        danger: item.danger,
        incognito: item.incognito,
        paused: item.paused,
        canResume: item.canResume,
        state: item.state,
        error: item.error,
        exists: item.exists,
        byExtensionId: item.byExtensionId,
        byExtensionName: item.byExtensionName,
      },
    });
  } catch {
    // dax unreachable — hand the download back to Chrome rather than
    // silently dropping what the user asked for.
    interceptedIds.delete(item.id);
    chrome.downloads
      .download({ url: item.url, filename: name || undefined })
      .catch(() => {});
  }
}

// The cancelled download lands as 'interrupted' — erase it so the shelf
// and history don't show a phantom failed download.
chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state?.current === 'interrupted' && interceptedIds.delete(delta.id)) {
    chrome.downloads.erase({ id: delta.id }).catch(() => {});
  }
});

// ---- Context Menus ----
function updateContextMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'dax-download-page',
      title: 'Send page to dax',
      contexts: ['page'],
    });

    chrome.contextMenus.create({
      id: 'dax-download-link',
      title: 'Download link with dax',
      contexts: ['link'],
    });

    chrome.contextMenus.create({
      id: 'dax-download-media',
      title: 'Download media with dax',
      contexts: ['video', 'audio', 'image'],
    });
  });
}

chrome.runtime.onInstalled.addListener(() => {
  updateContextMenus();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const tabId = tab?.id;
  const pageUrl = tab?.url || info.pageUrl || '';
  const title = tabId != null ? await domTitle(tabId, tab?.title) : (tab?.title || '');
  const contexts = tabId ? await captureContexts(tabId, title) : [];

  let targetUrl = pageUrl;
  if (info.menuItemId === 'dax-download-link' && info.linkUrl) {
    targetUrl = info.linkUrl;
  } else if (info.menuItemId === 'dax-download-media' && (info.srcUrl || info.linkUrl)) {
    targetUrl = info.srcUrl || info.linkUrl;
  }

  try {
    await rpc('push', {
      url: targetUrl,
      referer: pageUrl,
      title,
      contexts: contexts.slice(0, 8),
    });
    if (tabId) {
      chrome.action.setBadgeText({ text: '✓', tabId }).catch(() => {});
      chrome.action.setBadgeBackgroundColor({ color: '#10b981', tabId }).catch(() => {});
      setTimeout(() => updateBadge(tabId).catch(() => {}), 2000);
    }
  } catch (err) {
    if (tabId) {
      chrome.action.setBadgeText({ text: '!', tabId }).catch(() => {});
      chrome.action.setBadgeBackgroundColor({ color: '#ef4444', tabId }).catch(() => {});
      setTimeout(() => updateBadge(tabId).catch(() => {}), 3000);
    }
  }
});

// ---- Toolbar Action Click (1-Click Push or Open Options) ----
chrome.action.onClicked.addListener(async (tab) => {
  const config = await getConfig();
  if (!config.apiToken) {
    chrome.runtime.openOptionsPage();
    return;
  }

  const tabId = tab?.id;
  const pageUrl = tab?.url || '';
  const title = tabId != null ? await domTitle(tabId, tab?.title) : (tab?.title || '');
  if (!pageUrl || pageUrl.startsWith('chrome://')) {
    chrome.runtime.openOptionsPage();
    return;
  }

  const contexts = tabId ? await captureContexts(tabId, title) : [];

  if (tabId) {
    chrome.action.setBadgeText({ text: '…', tabId }).catch(() => {});
    chrome.action.setBadgeBackgroundColor({ color: '#f59e0b', tabId }).catch(() => {});
  }

  try {
    await rpc('push', {
      url: pageUrl,
      referer: pageUrl,
      title,
      contexts: contexts.slice(0, 8),
    });
    if (tabId) {
      chrome.action.setBadgeText({ text: '✓', tabId }).catch(() => {});
      chrome.action.setBadgeBackgroundColor({ color: '#10b981', tabId }).catch(() => {});
      setTimeout(() => updateBadge(tabId).catch(() => {}), 2000);
    }
  } catch (err) {
    const isBadToken = String(err?.message || err).toLowerCase().includes('token');
    if (isBadToken) {
      chrome.runtime.openOptionsPage();
    }
    if (tabId) {
      chrome.action.setBadgeText({ text: '!', tabId }).catch(() => {});
      chrome.action.setBadgeBackgroundColor({ color: '#ef4444', tabId }).catch(() => {});
      setTimeout(() => updateBadge(tabId).catch(() => {}), 3000);
    }
  }
});

// ---- Message Dispatcher ----
async function handlePush({ pageUrl, referer, title, tabId: explicitTabId }, sender) {
  const tabId = explicitTabId ?? sender?.tab?.id;
  // Overlay pushes from embed iframes carry that frame's title (often junk
  // like "c") — the top frame's title is what the user sees, so it wins;
  // the passed title is only a fallback when no top frame answers.
  const resolvedTitle =
    (tabId != null ? await domTitle(tabId) : '') || title || sender?.tab?.title || '';
  const contexts = tabId != null ? await captureContexts(tabId, resolvedTitle) : [];
  const flash = (text, color) => {
    if (tabId == null || tabId < 0) return;
    chrome.action.setBadgeText({ text, tabId }).catch(() => {});
    chrome.action.setBadgeBackgroundColor({ color, tabId }).catch(() => {});
    setTimeout(() => updateBadge(tabId).catch(() => {}), 2000);
  };
  try {
    const res = await rpc('push', {
      url: pageUrl,
      referer: referer || sender?.tab?.url || pageUrl,
      title: resolvedTitle,
      contexts: contexts.slice(0, 8),
    });
    // Magnet clicks and overlay buttons push through here — flash the tab
    // badge so the user sees the URI landed even without a dialog focus.
    flash('✓', '#10b981');
    return res;
  } catch (e) {
    flash('!', '#ef4444');
    throw e;
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const run = (p) =>
    p.then((r) => sendResponse({ ok: true, data: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));

  switch (msg?.type) {
    case 'push':
      run(handlePush(msg, sender));
      return true;

    case 'testConnection':
      run((async () => {
        const start = Date.now();
        const res = await rpc('ping');
        const latencyMs = Date.now() - start;
        return { ...res, latencyMs };
      })());
      return true;

    default:
      return false;
  }
});
