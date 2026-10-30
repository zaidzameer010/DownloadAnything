// DownloadAnything extension service worker (MV3)
// Handles RPC communication with DownloadAnything backend, network stream sniffing,
// context menus, and tab badge feedback.

const DEFAULT_CONFIG = {
  apiPort: 16801,
  apiToken: '',
};

const BADGE_OK = '#10b981';
const BADGE_ERR = '#ef4444';
const BADGE_BUSY = '#f59e0b';

/** Set (or clear) the per-tab action badge. */
function setBadge(tabId, text, color) {
  if (tabId == null || tabId < 0) return;
  chrome.action.setBadgeText({ text, tabId }).catch(() => {});
  if (color) chrome.action.setBadgeBackgroundColor({ color, tabId }).catch(() => {});
}

/** Flash a result on the tab badge, then restore the capture count. */
function flashBadge(tabId, text, color, delay = 2000) {
  setBadge(tabId, text, color);
  setTimeout(() => updateBadge(tabId).catch(() => {}), delay);
}

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
      reject(new Error('Connection timed out — is DownloadAnything running?'));
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
        reject(new Error(`Failed to connect to DownloadAnything on port ${config.apiPort} — is DownloadAnything running?`));
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
const CAPTURE_MAX = 32;
// A player that stopped requesting media an hour ago is stale — its signed
// URLs are dead and would only add failed probes to the next push. Kept long
// enough that a still-playing long video's manifest capture survives.
const CAPTURE_TTL_MS = 60 * 60 * 1000;
const HEADER_ALLOW = /^(?:cookie|referer|user-agent|accept-language|x-)/;
const MEDIA_EXT_RE =
  /^(?:mp4|webm|mkv|mov|m4v|ts|mp3|m4a|opus|wav|flac|aac)$/;
const AUDIO_FILE_RE = /^(?:mp3|m4a|opus|wav|flac|aac)$/;
const RETIRED_EXT = new Set([
  'ogg', 'oga', 'avi', '3gp', 'wmv', 'flv', 'mpg', 'mpeg', 'wma', 'aiff', 'm2ts', 'alac',
]);
const SEGMENT_PREFIX_TS_RE = /^(?:seg|segment|chunk|init|frag|part|range|piece)/;
const SEGMENT_PREFIX_RE = /^(?:seg|segment|chunk|init|frag)/;

function urlParts(url) {
  try {
    const u = new URL(url, 'http://localhost');
    const path = u.pathname;
    const base = path.split('/').filter(Boolean).pop() || '';
    const dot = base.lastIndexOf('.');
    return {
      path,
      stem: (dot > 0 ? base.slice(0, dot) : base).toLowerCase(),
      ext: dot > 0 ? base.slice(dot + 1).toLowerCase() : '',
      query: u.search.toLowerCase().replace(/^\?/, ''),
    };
  } catch {
    return { path: '', stem: '', ext: '', query: '' };
  }
}

const MASTER_STEMS = ['master', 'manifest', 'multivariant', 'multi', 'playlist'];
function isMasterPlaylist(stem, query) {
  return MASTER_STEMS.some((t) => stem.includes(t)) || /(?:^|&)multi=/.test(query);
}

// Resolution encoded in the URL stem (`…-720p.mp4`, `…_1080p_av1.mp4`).
function heightFromUrl(url) {
  const { stem } = urlParts(url);
  const m = stem.match(/(?:^|[^0-9])(\d{3,4})p(?:[^0-9]|$)/);
  const h = m ? Number(m[1]) : 0;
  return h >= 144 && h <= 4320 ? h : 0;
}

// Height from a player label (`720p`, `1080p60`).
function heightFromLabel(label) {
  const m = /^(\d{3,4})p/i.exec(label || '');
  const h = m ? Number(m[1]) : 0;
  return h >= 144 && h <= 4320 ? h : 0;
}

// Dimension sanity for DOM-provided values — a `<source size>` may be bytes.
function clampHeight(value) {
  const h = Number(value) || 0;
  return h >= 144 && h <= 4320 ? h : 0;
}

function clampWidth(value) {
  const w = Number(value) || 0;
  return w >= 16 && w <= 16384 ? w : 0;
}

// Codecs named by a mime type (`video/mp4; codecs="avc1.64001F, mp4a.40.2"`).
function codecsFromMime(mime) {
  const m = /codecs\s*=\s*"?([^";]+)/i.exec(mime || '');
  if (!m) return '';
  return m[1]
    .split(',')
    .map((codec) => codec.trim())
    .filter((codec) => codec && !/^(?:none|unknown)$/i.test(codec))
    .join(', ');
}

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
  mime = (mime || '').toLowerCase();
  const { stem, ext, query } = urlParts(url);
  if (RETIRED_EXT.has(ext)) return null;

  if (ext === 'm3u8' || ext === 'm3u' || /mpegurl/i.test(mime)) {
    const master = isMasterPlaylist(stem, query);
    return { kind: master ? 'hls-master' : 'hls', manifest: true, label: master ? 'HLS Master' : 'HLS Stream' };
  }
  if (ext === 'mpd' || /dash\+xml/i.test(mime)) {
    return { kind: 'dash', manifest: true, label: 'DASH Manifest' };
  }
  if (/^(?:video|audio)\//i.test(mime) && !mime.includes('mp2t')) {
    return { kind: 'file', manifest: false, label: (ext || 'media').toUpperCase() };
  }
  if (MEDIA_EXT_RE.test(ext)) {
    return { kind: 'file', manifest: false, label: ext.toUpperCase() };
  }
  return null;
}

function isSegment(url) {
  const { stem, ext } = urlParts(url);
  if (!ext || ext === 'm3u8' || ext === 'm3u' || ext === 'mpd') return false;
  if (/^\d+$/.test(stem)) return true;
  // "part" is a segment prefix only for transport-stream chunks — a real
  // file named `part1.mp4` must stay capturable.
  const prefixes =
    ext === 'ts' || ext === 'm4s' ? SEGMENT_PREFIX_TS_RE : SEGMENT_PREFIX_RE;
  return prefixes.test(stem);
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
  setBadge(tabId, count > 0 ? String(count) : '', count > 0 ? BADGE_OK : undefined);
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
    if (!existing.height) existing.height = heightFromUrl(url);
    // The entry is created on request headers, where no response mime exists
    // yet — the response pass is the only one that can fill it.
    if (mime && !existing.mime) existing.mime = mime;
  } else {
    list.push({
      url,
      headers,
      at: Date.now(),
      manifest: match.manifest,
      kind: match.kind,
      label: match.label,
      mime: mime || '',
      height: heightFromUrl(url),
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

/** Drop captures older than the TTL; returns the tab's live list. */
function pruneCaptured(tabId, now = Date.now()) {
  const list = captured.get(tabId) ?? [];
  const fresh = list.filter((m) => now - (m.at || 0) <= CAPTURE_TTL_MS);
  if (fresh.length !== list.length) {
    captured.set(tabId, fresh);
    queuePersist();
    updateBadge(tabId).catch(() => {});
  }
  return fresh;
}

/** Origin + directory of a media URL — one player's ladder lives in one
 *  directory, so sibling players never share a scope. */
function urlScope(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname.slice(0, u.pathname.lastIndexOf('/') + 1)}`;
  } catch {
    return '';
  }
}

/** Keep only captures in the selected player's scopes — switching players
 *  drops the previous player's URLs. No-op when no scoped URL is given. */
function scopeCaptured(tabId, urls) {
  const scopes = new Set(
    urls
      .filter((u) => typeof u === 'string' && /^https?:\/\//i.test(u))
      .map(urlScope)
      .filter(Boolean)
  );
  if (tabId == null || !scopes.size) return;
  const live = pruneCaptured(tabId);
  const kept = live.filter((m) => scopes.has(urlScope(m.url)));
  if (kept.length !== live.length) {
    captured.set(tabId, kept);
    queuePersist();
    updateBadge(tabId).catch(() => {});
  }
}

async function captureContexts(tabId) {
  await hydrate();
  const list = pruneCaptured(tabId);
  const manifests = list
    .filter((m) => m.manifest)
    .sort((a, b) => manifestTier(b.url) - manifestTier(a.url) || b.at - a.at);
  const files = list.filter((m) => !m.manifest).sort((a, b) => b.at - a.at);
  return [...manifests, ...files].map((m) => ({
    url: m.url,
    headers: m.headers,
    kind: m.kind,
    label: m.label,
    mime: m.mime || '',
    ...(m.height ? { height: m.height } : {}),
  }));
}

// Merge the DOM source ladder into captured contexts — each <source> carries
// its own resolution/mime codecs, which the request sniffer cannot see.
function mergeSources(contexts, sources) {
  const dom = new Set((sources || []).map((s) => s?.url).filter(Boolean));
  // A site's own UI sounds are ordinary audio/mpeg requests; only audio a
  // media element references is page content (YouTube's /s/search/audio/*.mp3
  // otherwise became the pushed page's only "media").
  const audioOnly = (c) =>
    !dom.has(c.url) &&
    (/^audio\//i.test(c.mime || '') || AUDIO_FILE_RE.test(urlParts(c.url).ext));
  contexts = contexts.filter((c) => !audioOnly(c));
  for (const source of sources || []) {
    const url = source?.url || '';
    if (!/^https?:\/\//i.test(url)) continue;
    const mime = source.mime || '';
    const match = mediaKindOf(url, mime);
    const height = clampHeight(source.height) || heightFromLabel(source.label) || heightFromUrl(url);
    const width = clampWidth(source.width);
    const codecs = codecsFromMime(mime);
    const existing = contexts.find((context) => context.url === url);
    if (existing) {
      if (!existing.height && height) existing.height = height;
      if (!existing.width && width) existing.width = width;
      if (!existing.codecs && codecs) existing.codecs = codecs;
      if (!existing.label && source.label) existing.label = source.label;
      if (!existing.mime && mime) existing.mime = mime;
      if (!existing.kind && match?.kind) existing.kind = match.kind;
      continue;
    }
    contexts.push({
      url,
      headers: findUrlHeaders(url),
      kind: match?.kind,
      label: source.label || match?.label,
      mime,
      ...(height ? { height } : {}),
      ...(width ? { width } : {}),
      ...(codecs ? { codecs } : {}),
    });
  }
  return contexts;
}

// Top-frame source ladders (toolbar/context-menu pushes).
async function domSources(tabId) {
  try {
    const sources = await chrome.tabs.sendMessage(tabId, { type: 'downloadanything-dom-sources' });
    return Array.isArray(sources) ? sources : [];
  } catch {
    return [];
  }
}

// Fallback title asked of the content script (og:title/h1/document.title with
// site chrome stripped). Rust's extracted page title always wins in DownloadAnything; this
// is only the last resort when Rust found none. When no content script can
// answer, the raw tab title is the last resort.
async function domTitle(tabId, fallback = '') {
  try {
    const t = await chrome.tabs.sendMessage(tabId, { type: 'downloadanything-dom-title' });
    return (typeof t === 'string' ? t.trim() : '') || fallback;
  } catch {
    return fallback;
  }
}

// Top-frame poster, used only when the overlay's own frame had none.
async function domThumbnail(tabId, fallback = '') {
  try {
    const t = await chrome.tabs.sendMessage(tabId, { type: 'downloadanything-dom-thumbnail' });
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
// the URL + all metadata to DownloadAnything for probing instead.
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
    // DownloadAnything unreachable — hand the download back to Chrome rather than
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
      id: 'downloadanything-download-page',
      title: 'Send page to DownloadAnything',
      contexts: ['page'],
    });

    chrome.contextMenus.create({
      id: 'downloadanything-download-link',
      title: 'Download link with DownloadAnything',
      contexts: ['link'],
    });

    chrome.contextMenus.create({
      id: 'downloadanything-download-media',
      title: 'Download media with DownloadAnything',
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
  const thumbnail = tabId != null ? await domThumbnail(tabId) : '';

  let targetUrl = pageUrl;
  if (info.menuItemId === 'downloadanything-download-link' && info.linkUrl) {
    targetUrl = info.linkUrl;
  } else if (info.menuItemId === 'downloadanything-download-media' && (info.srcUrl || info.linkUrl)) {
    targetUrl = info.srcUrl || info.linkUrl;
    // Picking one media element selects its player — drop the previous one.
    scopeCaptured(tabId, [targetUrl]);
  }
  const contexts =
    tabId != null ? mergeSources(await captureContexts(tabId), await domSources(tabId)) : [];

  try {
    await rpc('push', {
      url: targetUrl,
      referer: pageUrl,
      title,
      thumbnail,
      contexts: contexts.slice(0, 16),
    });
    flashBadge(tabId, '✓', BADGE_OK);
  } catch (err) {
    flashBadge(tabId, '!', BADGE_ERR, 3000);
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
  const thumbnail = tabId != null ? await domThumbnail(tabId) : '';
  if (!pageUrl || pageUrl.startsWith('chrome://')) {
    chrome.runtime.openOptionsPage();
    return;
  }

  const contexts =
    tabId != null ? mergeSources(await captureContexts(tabId), await domSources(tabId)) : [];

  setBadge(tabId, '…', BADGE_BUSY);

  try {
    await rpc('push', {
      url: pageUrl,
      referer: pageUrl,
      title,
      thumbnail,
      contexts: contexts.slice(0, 16),
    });
    flashBadge(tabId, '✓', BADGE_OK);
  } catch (err) {
    const isBadToken = String(err?.message || err).toLowerCase().includes('token');
    if (isBadToken) {
      chrome.runtime.openOptionsPage();
    }
    flashBadge(tabId, '!', BADGE_ERR, 3000);
  }
});

// ---- Message Dispatcher ----
async function handlePush(
  { pageUrl, mediaUrl, referer, title, thumbnail, sources, tabId: explicitTabId },
  sender
) {
  const tabId = explicitTabId ?? sender?.tab?.id;
  const tabUrl = sender?.tab?.url || '';
  // The embed player is the media/thumbnail target; the top page is only
  // the referer (and the title source, resolved below).
  const subframe = sender?.frameId != null && sender.frameId !== 0;
  const targetReferer =
    subframe && tabUrl ? tabUrl : (referer || tabUrl || pageUrl);
  // Overlay pushes from embed iframes carry that frame's title (often junk
  // like "c") — the top frame's title is what the user sees, so it wins;
  // the passed title is only a fallback when no top frame answers.
  const resolvedTitle =
    (tabId != null ? await domTitle(tabId) : '') || title || sender?.tab?.title || '';
  // Prefer the overlay frame's own poster (the embed player), then the top
  // frame's page image.
  const resolvedThumbnail =
    thumbnail || (tabId != null ? await domThumbnail(tabId) : '') || '';
  // A player selection scopes the tab's captures to that player: switching
  // players keeps the current ladder and drops the previous one's URLs.
  scopeCaptured(tabId, [mediaUrl, ...(sources || []).map((s) => s?.url)]);
  const contexts = mergeSources(tabId != null ? await captureContexts(tabId) : [], sources);
  if (
    mediaUrl &&
    /^https?:\/\//i.test(mediaUrl) &&
    !contexts.some((context) => context.url === mediaUrl)
  ) {
    contexts.unshift({ url: mediaUrl, headers: findUrlHeaders(mediaUrl) });
  }
  try {
    const res = await rpc('push', {
      url: pageUrl,
      referer: targetReferer,
      title: resolvedTitle,
      thumbnail: resolvedThumbnail,
      contexts: contexts.slice(0, 16),
    });
    // Magnet clicks and overlay buttons push through here — flash the tab
    // badge so the user sees the URI landed even without a dialog focus.
    flashBadge(tabId, '✓', BADGE_OK);
    return res;
  } catch (e) {
    flashBadge(tabId, '!', BADGE_ERR);
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
