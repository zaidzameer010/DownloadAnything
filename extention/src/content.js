(function () {
  const DOWNLOAD_ICON = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjIiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTIxIDE1djRhMiAyIDAgMCAxLTIgMkg1YTIgMiAwIDAgMS0yLTJ2LTQiLz48cG9seWxpbmUgcG9pbnRzPSI3IDEwIDEyIDE1IDE3IDEwIi8+PGxpbmUgeDE9IjEyIiB5MT0iMTUiIHgyPSIxMiIgeTI9IjMiLz48L3N2Zz4=';

  const {
    getVideoUrl,
    isStream,
    isMasterUrl,
    isSegmentUrl,
    normalizeMediaUrl,
    extractPerformanceResources
  } = DownloadAnything;

  const sniffedMediaItems = new Map();

  window.addEventListener('message', event => {
    if (event.source !== window) return;
    if (event.data?.type === '__DA_SNIFFED_MEDIA__' && event.data.detail) {
      const item = event.data.detail;
      if (!item.url || isSegmentUrl(item.url)) return;
      const canonicalUrl = normalizeMediaUrl ? normalizeMediaUrl(item.url) : item.url;
      sniffedMediaItems.set(canonicalUrl, { ...item, url: canonicalUrl });
      // The pointer may already be hovering a video whose stream just became
      // probeable — re-run the hover check so the button appears now instead
      // of waiting for the next mouse move.
      checkPointerPosition(pendingPointerX, pendingPointerY);
      try {
        chrome.runtime.sendMessage({
          type: 'da:addMediaLinks',
          media: [{ ...item, url: canonicalUrl }]
        });
      } catch {}
    }
  });

  const knownVideos = new Set();
  const attachedVideos = new WeakSet();
  const observedShadowRoots = new WeakSet();
  let scanRaf = null;

  let activeOverlay = null;
  let overlayHost = null;
  let currentVideo = null;
  let overlayRaf = null;
  let overlayVisible = false;
  let hideTimeout = null;
  let pendingPointer = null;
  let pendingPointerX = 0;
  let pendingPointerY = 0;
  let resizeObserver = null;

  const OVERLAY_SIZE = 40;
  const OVERLAY_MARGIN = 8;
  const HIDE_DELAY = 250;

  function isFullscreen() {
    return Boolean(document.fullscreenElement);
  }

  function createOverlay() {
    overlayHost = document.createElement('div');
    overlayHost.id = 'download-anything-overlay-host';
    if ('showPopover' in HTMLElement.prototype) {
      overlayHost.setAttribute('popover', 'manual');
    }
    Object.assign(overlayHost.style, {
      position: 'fixed',
      zIndex: '2147483647',
      top: '0px',
      left: '0px',
      width: '0px',
      height: '0px',
      margin: '0',
      padding: '0',
      border: 'none',
      background: 'transparent',
      overflow: 'visible',
      pointerEvents: 'none'
    });
    if (!overlayHost.popover) overlayHost.style.display = 'none';

    const shadow = overlayHost.attachShadow({ mode: 'open' });
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = chrome.runtime.getURL('src/styles.css');
    shadow.appendChild(link);

    const overlay = document.createElement('button');
    overlay.type = 'button';
    overlay.className = 'da-video-overlay';
    overlay.setAttribute('aria-label', 'Download video');
    overlay.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';

    let activated = false;
    const activate = event => {
      if (activated) return;
      activated = true;
      setTimeout(() => { activated = false; }, 400);
      if (!currentVideo) return;

      // Video embed iframes have no stream sources; their src (e.g. a
      // YouTube embed URL) is exactly what yt-dlp should probe.
      let directUrl = currentVideo instanceof HTMLIFrameElement
        ? currentVideo.src
        : getVideoUrl(currentVideo);

      if (!directUrl || directUrl.startsWith('blob:')) {
        for (const item of sniffedMediaItems.values()) {
          if (isMasterUrl(item.url) || isStream(item.type)) {
            directUrl = item.url;
            break;
          }
        }
      }
      hideOverlay();

      void openProbeInTopFrame({
        pageUrl: window.location.href,
        directUrl,
        poster: currentVideo instanceof HTMLVideoElement ? (currentVideo.poster || '') : '',
        // The playing element knows the real rendered resolution — the most
        // accurate source for rows yt-dlp and URL mining cannot label.
        videoWidth: currentVideo instanceof HTMLVideoElement ? (currentVideo.videoWidth || 0) : 0,
        videoHeight: currentVideo instanceof HTMLVideoElement ? (currentVideo.videoHeight || 0) : 0
      });
    };
    const pressActivates = event => {
      if (event.button !== 0 && event.pointerType === 'mouse') return;
      if (!overlayVisible || !activeOverlay) return;
      if (!isOverOverlay(event.clientX, event.clientY)) return;
      activate(event);
    };
    // Window capture runs before any site handler can retarget, cancel, or
    // stop the event, so the press on the button can never be bypassed.
    window.addEventListener('pointerdown', pressActivates, true);

    overlay.addEventListener('pointerup', event => {
      if (event.button !== 0 && event.pointerType === 'mouse') return;
      activate(event);
    });
    overlay.addEventListener('click', event => activate(event));

    shadow.appendChild(overlay);
    (document.body || document.documentElement).appendChild(overlayHost);
    return overlay;
  }

  function positionOverlay(video) {
    if (!activeOverlay) return;
    const rect = video.getBoundingClientRect();

    let top = rect.top + OVERLAY_MARGIN;
    if (top + OVERLAY_SIZE > window.innerHeight - OVERLAY_MARGIN) {
      top = rect.bottom - OVERLAY_SIZE - OVERLAY_MARGIN;
    }
    top = Math.max(OVERLAY_MARGIN, Math.min(window.innerHeight - OVERLAY_SIZE, top));

    let left = rect.right - OVERLAY_SIZE - OVERLAY_MARGIN;
    if (left < OVERLAY_MARGIN) {
      left = rect.left + OVERLAY_MARGIN;
    }
    left = Math.max(0, Math.min(window.innerWidth - OVERLAY_SIZE, left));

    activeOverlay.style.top = `${top}px`;
    activeOverlay.style.left = `${left}px`;
  }

  function trackOverlay() {
    overlayRaf = null;
    if (isFullscreen()) {
      hideOverlay();
      return;
    }
    if (activeOverlay && overlayVisible && currentVideo) {
      // Re-evaluate every frame: a player that is removed, hidden, or whose
      // stream disappeared must take the button with it immediately.
      if (!isVideoVisible(currentVideo) || !hasDownloadableMedia(currentVideo)) {
        hideOverlay();
        return;
      }
      const rect = currentVideo.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) {
        hideOverlay();
        return;
      }
      positionOverlay(currentVideo);
      overlayRaf = requestAnimationFrame(trackOverlay);
    }
  }

  function showOverlay(video) {
    if (isFullscreen()) {
      hideOverlay();
      return;
    }
    if (!video || !isVideoVisible(video)) return;
    if (!hasDownloadableMedia(video)) return;
    if (currentVideo === video && overlayVisible) {
      positionOverlay(video);
      return;
    }
    currentVideo = video;
    cancelHideOverlay();

    if (!activeOverlay) activeOverlay = createOverlay();

    if (!resizeObserver) {
      resizeObserver = new ResizeObserver(() => {
        if (currentVideo && overlayVisible) positionOverlay(currentVideo);
      });
    }
    resizeObserver.disconnect();
    resizeObserver.observe(video);

    if (overlayHost && 'showPopover' in overlayHost) {
      try { overlayHost.showPopover(); } catch { /* no-op */ }
    }
    activeOverlay.style.display = 'inline-flex';
    if (overlayHost) overlayHost.style.display = 'block';
    overlayVisible = true;
    positionOverlay(video);

    if (!overlayRaf) overlayRaf = requestAnimationFrame(trackOverlay);
  }

  function hideOverlay() {
    if (overlayRaf) {
      cancelAnimationFrame(overlayRaf);
      overlayRaf = null;
    }
    if (pendingPointer) {
      cancelAnimationFrame(pendingPointer);
      pendingPointer = null;
    }
    cancelHideOverlay();
    if (resizeObserver) resizeObserver.disconnect();
    if (overlayHost && 'hidePopover' in overlayHost) {
      try { overlayHost.hidePopover(); } catch { /* no-op */ }
    }
    if (activeOverlay) {
      activeOverlay.style.display = 'none';
    }
    if (overlayHost) {
      overlayHost.style.display = 'none';
    }
    overlayVisible = false;
    currentVideo = null;
  }

  function scheduleHideOverlay() {
    if (hideTimeout) return;
    hideTimeout = setTimeout(() => {
      hideTimeout = null;
      hideOverlay();
    }, HIDE_DELAY);
  }

  function cancelHideOverlay() {
    if (hideTimeout) {
      clearTimeout(hideTimeout);
      hideTimeout = null;
    }
  }

  function isVideoVisible(video) {
    if (!video.isConnected) return false;
    try {
      if (video.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return true;
    } catch { /* no-op */ }
    // Some players keep the <video> at opacity 0 behind a custom poster/skin.
    // A playing video with a real on-screen rect is visible regardless.
    if (video.readyState >= 2 && !video.paused) {
      const rect = video.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }
    return false;
  }

  // Only offer the download once there is something to probe. MSE/blob
  // players render the <video> and poster immediately, but the manifest —
  // and with it the sniffed stream URL the probe needs — only exists after
  // the player fetches it, so a too-early click probes a dead URL.
  let capturedMediaReady = false;
  let capturedMediaCheckedAt = 0;
  let capturedMediaChecking = false;

  function refreshCapturedMedia() {
    if (capturedMediaChecking || Date.now() - capturedMediaCheckedAt < 250) return;
    capturedMediaChecking = true;
    getCapturedMedia().then(media => {
      capturedMediaChecking = false;
      capturedMediaCheckedAt = Date.now();
      const wasReady = capturedMediaReady;
      capturedMediaReady = media.some(
        item => isStream(item.type) || isMasterUrl(item.url)
          || item.type === 'video' || item.type === 'audio'
      );
      // The pointer may be hovering a video whose stream just became
      // probeable — re-run the hover check so the button appears now
      // instead of waiting for the next mouse move.
      if (capturedMediaReady && !wasReady) {
        checkPointerPosition(pendingPointerX, pendingPointerY);
      }
    }).catch(() => {
      capturedMediaChecking = false;
    });
  }

  function hasDownloadableMedia(video) {
    if (video instanceof HTMLIFrameElement) {
      // An embed URL is only worth probing once the player inside actually
      // fetched a stream (tab-wide capture); before that the probe dies with
      // "Unsupported URL" (e.g. hgcloud.to embeds).
      if (!video.src) return false;
      if (sniffedMediaItems.size > 0 || capturedMediaReady) return true;
      refreshCapturedMedia();
      return false;
    }
    const url = getVideoUrl(video);
    if (url && !url.startsWith('blob:')) return true;
    if (video.readyState >= 2) return true;
    if (sniffedMediaItems.size > 0 || capturedMediaReady) return true;
    refreshCapturedMedia();
    return false;
  }

  // Generic video-embed heuristics — no site allowlist. An iframe counts as a
  // video player when its src looks like an embed/player/video URL, or when it
  // declares fullscreen/autoplay permissions (all major video embeds do).
  const { EMBED_URL_HINT, EMBED_HOST_HINTS } = globalThis.DownloadAnythingConstants;

  function isVideoEmbed(iframe) {
    let src = '';
    try {
      src = iframe.src || '';
      if (!src.startsWith('http')) return false;
      if (iframe.allowFullscreen) return true;
      const allow = iframe.getAttribute('allow') || '';
      if (/\bautoplay\b/i.test(allow) && /fullscreen|encrypted-media|picture-in-picture/i.test(allow)) return true;
      const url = new URL(src);
      const path = `${url.hostname}${url.pathname}`;
      if (EMBED_URL_HINT.test(path) && !/\.(?:html?|php|pdf|jpe?g|png|webp|gif|svg)(?:[?#]|$)/i.test(url.pathname)) return true;
      if (EMBED_HOST_HINTS.test(url.hostname)) return true;
    } catch { /* no-op */ }
    return false;
  }

  function findVideoAtPoint(x, y) {
    let best = null;
    let bestArea = Infinity;
    for (const video of [...knownVideos, ...knownEmbeds]) {
      if (!isVideoVisible(video)) continue;
      const rect = video.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        const area = rect.width * rect.height;
        if (area < bestArea) {
          best = video;
          bestArea = area;
        }
      }
    }
    return best;
  }

  function isOverOverlay(x, y) {
    if (!activeOverlay || !overlayVisible) return false;
    const rect = activeOverlay.getBoundingClientRect();
    return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  }

  function checkPointerPosition(x, y) {
    if (isFullscreen() || modalHost) {
      hideOverlay();
      return;
    }
    if (hideTimeout) {
      clearTimeout(hideTimeout);
      hideTimeout = null;
    }

    const video = findVideoAtPoint(x, y);
    if (video) {
      showOverlay(video);
    } else if (
      isOverOverlay(x, y)
      && currentVideo
      && isVideoVisible(currentVideo)
      && hasDownloadableMedia(currentVideo)
    ) {
      // keep the overlay visible only while its video is still alive
    } else {
      scheduleHideOverlay();
    }
  }

  function onPointerMove(event) {
    pendingPointerX = event.clientX;
    pendingPointerY = event.clientY;
    if (pendingPointer) return;
    pendingPointer = requestAnimationFrame(() => {
      pendingPointer = null;
      checkPointerPosition(pendingPointerX, pendingPointerY);
    });
  }

  function onPointerLeave(event) {
    if (event.target === document) {
      cancelHideOverlay();
      hideOverlay();
    }
  }

  function onPointerDown(event) {
    if (!activeOverlay || !overlayVisible) return;
    if (activeOverlay.contains(event.target)) return;
    hideOverlay();
  }

  function onVideoEnter(video) {
    if (modalHost) return;
    showOverlay(video);
  }

  function onVideoLeave() {
    scheduleHideOverlay();
  }

  document.addEventListener('pointermove', onPointerMove, { passive: true, capture: true });
  document.addEventListener('pointerleave', onPointerLeave, { capture: true });
  document.addEventListener('pointerdown', onPointerDown, { capture: true });
  document.addEventListener('fullscreenchange', () => {
    if (isFullscreen()) hideOverlay();
  }, { passive: true, capture: true });

  function handleMagnetAnchorClick(event) {
    const target = event.target instanceof Element ? event.target.closest('a[href]') : null;
    const url = target?.href || '';
    if (!/^magnet:\?/i.test(url)) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    void openProbeInTopFrame({ pageUrl: window.location.href, directUrl: url });
  }

  document.addEventListener('click', handleMagnetAnchorClick, { capture: true });

  /* ------------------------------------------------------------------------
   * yt-dlp backend client helpers
   * ---------------------------------------------------------------------- */

  async function sendRuntimeMessage(message, defaultError) {
    try {
      const reply = await chrome.runtime.sendMessage(message);
      return reply || { ok: false, error: defaultError };
    } catch (error) {
      return { ok: false, error: error.message };
    }
  }

  function errorMessage(error, fallback) {
    if (typeof error === 'string' && error) return error;
    if (error instanceof Error && error.message) return error.message;
    if (error && typeof error === 'object') {
      try {
        return JSON.stringify(error);
      } catch {
        return Object.prototype.toString.call(error);
      }
    }
    return fallback;
  }

  function makeReqId() {
    return crypto.randomUUID();
  }

  function sendBackend(payload) {
    return sendRuntimeMessage({ type: 'backend', payload }, 'No response from backend');
  }

  function checkBackendStatus() {
    return sendRuntimeMessage({ type: 'backendStatus' }, 'No response from backend')
      .then(reply => !!reply?.connected);
  }

  function getCapturedMedia() {
    return sendRuntimeMessage({ type: 'getMediaForTab' }, 'No response from backend')
      .then(reply => (Array.isArray(reply?.media) ? reply.media : []));
  }

  // Candidate stream URLs for the yt-dlp fallback: DOM-extracted URL first,
  // then network-captured media (master playlists, streams, direct videos).
  async function collectFallbackSources(pageUrl, directUrl) {
    const captured = await getCapturedMedia();
    const pageSources = DownloadAnything.collectPageMediaVariants();
    const perfSources = extractPerformanceResources ? extractPerformanceResources() : [];

    try {
      window.postMessage({ type: '__DA_TRIGGER_PLAYER_SCAN__' }, '*');
    } catch {}

    const directPath = (() => {
      try { return new URL(directUrl).pathname; } catch { return ''; }
    })();
    const directDirectory = directPath.slice(0, directPath.lastIndexOf('/'));
    const pageRank = source => {
      try {
        const path = new URL(source.url).pathname;
        if (directDirectory && path.startsWith(`${directDirectory}/`)) return 0;
        return 1;
      } catch {
        return 2;
      }
    };
    const sortedPageSources = pageSources
      .filter(source => source.url !== directUrl)
      .sort((a, b) => pageRank(a) - pageRank(b));
    const captureRank = item => {
      if (!isStream(item.type)) return 3;
      if (DownloadAnything.isMultiUrl(item.url) || /\bmaster\b/i.test(item.url)) return 0;
      return isMasterUrl(item.url) ? 1 : 2;
    };
    const sortedCaptured = captured
      .slice()
      .sort((a, b) => captureRank(a) - captureRank(b) || (a.timestamp || 0) - (b.timestamp || 0));

    const sources = [];
    const seen = new Set([pageUrl]);
    const add = (url, label = '', referer = '', origin = '') => {
      if (!url || isSegmentUrl(url)) return;
      let resolvedUrl = url;
      try {
        const base = referer || pageUrl || (typeof window !== 'undefined' ? window.location.href : '');
        resolvedUrl = base ? new URL(url, base).href : new URL(url).href;
      } catch {}
      const normalizedUrl = normalizeMediaUrl ? normalizeMediaUrl(resolvedUrl) : resolvedUrl;
      const normalizedLabel = String(label || '').trim()
        || (DownloadAnything.extractResolutionFromSources ? DownloadAnything.extractResolutionFromSources(normalizedUrl) : '');
      if (seen.has(normalizedUrl)) {
        const existing = sources.find(source => source.url === normalizedUrl);
        if (existing) {
          if (!existing.label && normalizedLabel) existing.label = normalizedLabel;
          if (!existing.referer && referer) existing.referer = referer;
          if (!existing.origin && origin) existing.origin = origin;
        }
        return;
      }
      seen.add(normalizedUrl);
      const entry = { url: normalizedUrl, label: normalizedLabel };
      if (referer) entry.referer = referer;
      if (origin) entry.origin = origin;
      sources.push(entry);
    };

    if (directUrl && !directUrl.startsWith('blob:')) {
      add(directUrl, DownloadAnything.extractResolutionFromSources ? DownloadAnything.extractResolutionFromSources(directUrl) : '', pageUrl);
    }

    // 1. Master manifests from sniffed items & captured media
    for (const item of sniffedMediaItems.values()) {
      if (isMasterUrl(item.url)) add(item.url, item.label, item.referer || pageUrl, item.origin);
    }
    for (const item of sortedCaptured) {
      if (isMasterUrl(item.url)) add(item.url, '', item.referer || pageUrl, item.origin);
    }

    // 2. Page DOM sources
    for (const source of sortedPageSources) add(source.url, source.label, pageUrl);

    // 3. Sniffed & captured media
    for (const item of sniffedMediaItems.values()) add(item.url, item.label, item.referer || pageUrl, item.origin);
    for (const item of sortedCaptured) add(item.url, '', item.referer || pageUrl, item.origin);

    // 4. Performance resources
    for (const item of perfSources) add(item.url, '', pageUrl);

    // 5. Mobile Safari UA fallback probe if no viable stream found
    if (sources.length <= 1 && pageUrl && /^https?:\/\//i.test(pageUrl)) {
      try {
        const mobileResp = await new Promise(resolve => {
          chrome.runtime.sendMessage({ type: 'da:probeMobileUA', pageUrl }, resp => {
            resolve(chrome.runtime.lastError ? null : resp);
          });
        });
        if (mobileResp?.ok && Array.isArray(mobileResp.sources)) {
          for (const s of mobileResp.sources) add(s.url, s.label);
        }
      } catch {}
    }

    // A multi-rendition manifest encodes every variant in one URL, so probing
    // it lists all qualities; keep the first plain master as a fallback
    // candidate in case the multi URL is dead.
    const multiSource = sources.find(source => DownloadAnything.isMultiUrl(source.url));
    const masterSource = sources.find(source => isMasterUrl(source.url));
    if (multiSource) {
      return masterSource && masterSource.url !== multiSource.url
        ? [multiSource, masterSource]
        : [multiSource];
    }
    return masterSource ? [masterSource] : sources.slice(0, 16);
  }

  // Service worker -> content script: show the DownloadAnything modal for an
  // intercepted Chrome download.
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'da:openProbe') {
      const context = message.context;
      if (
        window !== window.top ||
        !context ||
        typeof context.pageUrl !== 'string' ||
        typeof context.directUrl !== 'string'
      ) {
        sendResponse({ ok: false, error: 'Invalid download request' });
        return false;
      }
      void handleOverlayClick({ pageUrl: context.pageUrl, directUrl: context.directUrl });
      sendResponse({ ok: true });
      return true;
    }
    if (message?.type === 'da:interceptedDownload') {
      DownloadAnythingLogger.info('intercepted download received', { url: message.url, filename: message.filename, mediaType: message.mediaType });
      handleInterceptedDownload(message)
        .then(ok => sendResponse({ ok, error: ok ? undefined : 'Backend is not connected' }))
        .catch(error => sendResponse({ ok: false, error: errorMessage(error, 'Could not open download options') }));
      return true;
    }
    return false;
  });

  /* ------------------------------------------------------------------------
   * Formatting helpers
   * ---------------------------------------------------------------------- */
  function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '';
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
    return `${h > 0 ? h + ':' : ''}${mm}:${String(s).padStart(2, '0')}`;
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1000 && unit < units.length - 1) {
      value /= 1000;
      unit += 1;
    }
    return `${value.toFixed(unit === 0 ? 0 : 2)} ${units[unit]}`;
  }

  function shortCodec(vcodec) {
    if (!vcodec || vcodec === 'none') return '';
    const v = vcodec.toLowerCase();
    if (v.includes('av1') || v.includes('av01')) return 'AV1';
    if (v.includes('vp9') || v.includes('vp09')) return 'VP9';
    if (v.includes('avc') || v.includes('h264') || v.includes('264')) return 'AVC';
    if (v.includes('hevc') || v.includes('h265') || v.includes('265')) return 'HEVC';
    return vcodec.split('.')[0].toUpperCase();
  }

  /* ------------------------------------------------------------------------
   * Probe modal (shadow DOM, styled by src/styles.css)
   * ---------------------------------------------------------------------- */
  let modalHost = null;
  let modalCleanup = null;
  let pendingProbeReqId = null;

  function h(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function closeProbeModal() {
    const probeReqId = pendingProbeReqId;
    if (probeReqId) {
      pendingProbeReqId = null;
      void sendBackend({ type: 'probe_cancel', probeReqId });
    }
    if (modalCleanup) {
      modalCleanup();
      modalCleanup = null;
    }
    if (modalHost) {
      modalHost.remove();
      modalHost = null;
    }
  }

  let previousActiveElement = null;

  function buildModalShell(titleText) {
    closeProbeModal();
    hideOverlay();
    previousActiveElement = document.activeElement;

    modalHost = document.createElement('div');
    modalHost.id = 'download-anything-modal-host';
    const shadow = modalHost.attachShadow({ mode: 'open' });
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = chrome.runtime.getURL('src/styles.css');
    shadow.appendChild(link);

    const backdrop = h('div', 'modal-backdrop');
    const box = h('div', 'modal-box');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'da-modal-title');

    const header = h('div', 'modal-header');
    const title = h('h2', '', titleText);
    title.id = 'da-modal-title';
    header.appendChild(title);

    const closeBtn = h('button', 'close-btn');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Close dialog');
    closeBtn.setAttribute('data-slot', 'close-button');
    closeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    closeBtn.addEventListener('click', closeProbeModal);
    header.appendChild(closeBtn);

    const content = h('div', 'modal-content');
    const footer = h('div', 'modal-footer');
    footer.style.display = 'none';

    box.appendChild(header);
    box.appendChild(content);
    box.appendChild(footer);
    backdrop.appendChild(box);
    shadow.appendChild(backdrop);
    document.documentElement.appendChild(modalHost);

    // Trap focus and set inert on outside background content
    const inertElements = [];
    try {
      if (document.body) {
        for (const el of document.body.children) {
          if (el !== modalHost && !el.inert) {
            el.inert = true;
            inertElements.push(el);
          }
        }
      }
    } catch {}

    // Prevent scrolling and interaction with background page
    const prevHtmlOverflow = document.documentElement.style.overflow;
    const prevBodyOverflow = document.body ? document.body.style.overflow : '';
    document.documentElement.style.overflow = 'hidden';
    if (document.body) {
      document.body.style.overflow = 'hidden';
    }

    const blockBackdropScroll = event => {
      if (event.target === backdrop) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    backdrop.addEventListener('wheel', blockBackdropScroll, { passive: false });
    backdrop.addEventListener('touchmove', blockBackdropScroll, { passive: false });

    // Disable closing modal on backdrop click and prevent interaction bleed
    backdrop.addEventListener('click', event => {
      if (event.target === backdrop) {
        event.preventDefault();
        event.stopPropagation();
      }
    });

    function onKeyDown(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeProbeModal();
        return;
      }
      if (event.key === 'Tab') {
        const focusable = Array.from(
          shadow.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')
        ).filter(el => el.offsetParent !== null || el.offsetWidth > 0 || el.offsetHeight > 0);
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = shadow.activeElement;
        if (event.shiftKey) {
          if (active === first || !shadow.contains(active)) {
            event.preventDefault();
            last.focus();
          }
        } else {
          if (active === last || !shadow.contains(active)) {
            event.preventDefault();
            first.focus();
          }
        }
      }
    }
    document.addEventListener('keydown', onKeyDown, true);

    modalCleanup = () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.documentElement.style.overflow = prevHtmlOverflow;
      if (document.body) {
        document.body.style.overflow = prevBodyOverflow;
      }
      for (const el of inertElements) {
        try { el.inert = false; } catch {}
      }
      if (previousActiveElement && typeof previousActiveElement.focus === 'function') {
        try { previousActiveElement.focus(); } catch {}
        previousActiveElement = null;
      }
    };

    // Auto-focus initial interactive element
    requestAnimationFrame(() => {
      closeBtn.focus();
    });

    return { shadow, content, footer };
  }

  function renderProbing(content, footer) {
    content.replaceChildren();
    footer.style.display = 'flex';
    footer.replaceChildren();
    const wrap = h('div', 'progress-container');
    wrap.setAttribute('role', 'status');
    wrap.setAttribute('aria-live', 'polite');
    const labelRow = h('div', 'progress-label-row');
    labelRow.appendChild(h('span', '', 'Probing media'));
    const stage = h('span', '', 'Extracting stream details…');
    labelRow.appendChild(stage);
    const track = h('div', 'progress-track');
    const fill = h('div', 'progress-fill indeterminate');
    track.appendChild(fill);
    wrap.appendChild(labelRow);
    wrap.appendChild(track);
    content.appendChild(wrap);

    const cancelBtn = h('button', 'footer-btn cancel', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeProbeModal);
    footer.appendChild(cancelBtn);
  }


  function locationOptions(settings) {
    const options = [];
    const seen = new Set();
    const add = (name, path) => {
      if (!path || seen.has(path)) return;
      seen.add(path);
      options.push({ name, path });
    };
    add(`Default (${settings.downloadDir})`, settings.downloadDir);
    for (const preset of settings.presetPaths || []) add(preset.name, preset.path);
    return options;
  }

  function renderDuplicatePrompt(content, footer, duplicate, onChoose) {
    content.replaceChildren();
    footer.replaceChildren();
    footer.style.display = 'flex';

    const wrap = h('div', 'duplicate-prompt');
    wrap.appendChild(h('div', 'duplicate-title', 'Duplicate file found'));

    const message = duplicate.type === 'job'
      ? 'A download for this item is currently active.'
      : duplicate.type === 'torrent'
        ? 'Torrent data already exists in the destination folder.'
        : `${duplicate.filename || 'This file'} already exists in the destination folder.`;
    wrap.appendChild(h('div', 'duplicate-message', message));

    if (duplicate.existing?.filename || duplicate.filename) {
      wrap.appendChild(h('div', 'duplicate-existing',
        `Existing: ${duplicate.existing?.filename || duplicate.filename}`));
    }
    if (duplicate.suggestedName) {
      wrap.appendChild(h('div', 'duplicate-suggested',
        `Suggested name: ${duplicate.suggestedName}`));
    }
    content.appendChild(wrap);

    const skipBtn = h('button', 'footer-btn cancel', 'Skip');
    skipBtn.type = 'button';
    skipBtn.addEventListener('click', () => onChoose('skip'));

    const overwriteBtn = h('button', 'footer-btn cancel', 'Overwrite');
    overwriteBtn.type = 'button';
    overwriteBtn.addEventListener('click', () => onChoose('override'));

    footer.appendChild(skipBtn);
    footer.appendChild(overwriteBtn);

    if (duplicate.type === 'torrent') {
      const resumeBtn = h('button', 'footer-btn download', 'Resume');
      resumeBtn.type = 'button';
      resumeBtn.addEventListener('click', () => onChoose('resume'));
      footer.appendChild(resumeBtn);
    } else {
      const renameBtn = h('button', 'footer-btn download',
        `Rename to ${duplicate.suggestedName || 'unique name'}`);
      renameBtn.type = 'button';
      renameBtn.addEventListener('click', () => onChoose('rename'));
      footer.appendChild(renameBtn);
    }
  }

  function renderProbeResult(content, footer, probe, settings) {
    content.replaceChildren();
    footer.style.display = 'flex';
    footer.replaceChildren();
    const result = probe.result;

    // yt-dlp names extractor-fallback streams after the manifest ("master",
    // "index-v1-a1"); derive a meaningful name from the page instead. The
    // same happens when yt-dlp's generic extractor probes a page whose HLS
    // manifest has a generic name, so check for it on every engine.
    // Intercepted generic files already have a reliable filename from the browser.
    let filename = result.filename;
    let title = result.title;
    if (probe.engine === 'file') {
      filename = result.filename;
      title = (filename ? DownloadAnything.stemFromName(filename) : '') || result.webpageUrl;
    } else if (probe.engine !== 'ytdlp' || DownloadAnything.isGenericName(DownloadAnything.stemFromName(result.filename || ''))) {
      filename = DownloadAnything.suggestFilename(
        probe.url,
        DownloadAnything.classifyMedia(probe.url, null),
        null,
        settings?.mergeOutputFormat
      );
      title = DownloadAnything.stemFromName(filename) || title;
    }

    // For Chrome-intercepted media, prefer the filename Chrome already resolved
    // from the response (Content-Disposition or URL path) over the page title.
    if (probe.interceptedFilename && probe.engine !== 'file') {
      const intercepted = probe.interceptedFilename;
      const interceptedBase = DownloadAnything.stemFromName(intercepted);
      const targetExt = DownloadAnything.extFromName(result.filename)
        || settings?.mergeOutputFormat
        || 'mp4';
      filename = targetExt ? `${interceptedBase}.${targetExt}` : interceptedBase;
      title = interceptedBase || title || result.webpageUrl;
    }

    // Media info segment
    const info = h('div', 'media-info');
    if (result.thumbnail) {
      const thumb = h('img', 'media-thumb');
      thumb.src = result.thumbnail;
      thumb.alt = '';
      info.appendChild(thumb);
    } else {
      info.appendChild(h('div', 'media-thumb'));
    }
    const details = h('div', 'media-details');
    details.appendChild(h('div', 'media-title', title || result.webpageUrl));
    const badges = h('div', 'meta-badges-row');
    const engineClass = probe.engine === 'ytdlp' ? 'ytdlp' : probe.engine === 'file' ? 'file' : 'stream';
    badges.appendChild(h('span', `meta-badge-chip engine ${engineClass}`, probe.engine));
    if (result.extractor) badges.appendChild(h('span', 'meta-badge-chip', result.extractor));
    if (result.duration) badges.appendChild(h('span', 'meta-badge-chip', formatDuration(result.duration)));
    if (result.isPlaylist && result.playlistCount) {
      badges.appendChild(h('span', 'meta-badge-chip', `${result.playlistCount} videos`));
    }
    if (result.uploader) badges.appendChild(h('span', 'meta-badge-chip', result.uploader));
    details.appendChild(badges);
    info.appendChild(details);
    content.appendChild(info);

    // Tab state
    const allFormats = result.formats || [];
    const hasPlaylist = result.isPlaylist && (result.entries?.length ?? 0) > 0;
    let videoFormats = [];
    let audioFormats = [];
    let hasVideo = false;
    let hasAudio = false;
    let selectedFormat = null;
    let selectedUrls = new Set();

    const tabBtns = {};
    let videoPanel = null;
    let audioPanel = null;
    let playlistPanel = null;

    if (allFormats.length > 0) {
      const baseResolutionSources = [probe.directUrl, filename, result.filename, probe.url, result.webpageUrl].filter(Boolean);
      function isPlaceholderLabel(label) {
        return !label || /^(best quality|direct|video|original)$/i.test(label) || /^https?:\/\//i.test(label);
      }
      function sameResource(left, right) {
        if (!left || !right) return false;
        try {
          return new URL(left).pathname === new URL(right).pathname;
        } catch {
          return left.split('?')[0] === right.split('?')[0];
        }
      }
      function getFormatHeight(fmt) {
        if (fmt.kind === 'audio') return null;
        if (typeof fmt.height === 'number' && !Number.isNaN(fmt.height)) return fmt.height;
        const text = [fmt.resolution, fmt.label].filter(Boolean).join(' ');
        let m = text.match(/(\d{2,4})p(?:\d{2,3})?/i);
        if (m) return parseInt(m[1], 10);
        m = text.match(/(\d{3,5})x(\d{3,5})/i);
        if (m) return parseInt(m[2], 10);
        m = text.match(/\b(4k|uhd|8k|fhd|qhd|2k|hd)\b/i);
        if (m) {
          const known = { '4k': 2160, 'uhd': 2160, '8k': 4320, 'fhd': 1080, 'qhd': 1440, '2k': 1440, 'hd': 720 };
          return known[m[1].toLowerCase()] || null;
        }
        return null;
      }
      function isResolutionBelow240p(fmt) {
        if (fmt.kind === 'audio') return false;
        const h = getFormatHeight(fmt);
        return h !== null && h < 240;
      }

      // Resolve placeholder labels to resolutions so the <240p filter can see them.
      for (const fmt of allFormats) {
        if (!fmt.resolution && fmt.kind === 'video' && isPlaceholderLabel(fmt.label)) {
          const fmtSources = [fmt.resolution, fmt.url, ...baseResolutionSources].filter(Boolean);
          const extracted = DownloadAnything.extractResolutionFromSources(...fmtSources);
          if (extracted) {
            fmt.resolution = extracted;
            fmt.label = extracted;
          }
        }
      }

      const visibleFormats = allFormats.filter(fmt => fmt.kind === 'audio' || !isResolutionBelow240p(fmt));
      videoFormats = visibleFormats.filter(fmt => fmt.kind !== 'audio');
      audioFormats = visibleFormats.filter(fmt => fmt.kind === 'audio');
      hasVideo = videoFormats.length > 0;
      hasAudio = audioFormats.length > 0 && !hasPlaylist;

      // The clicked <video> element reports the resolution actually playing.
      // With exactly one video row it can only be that row's rendition — a
      // ground truth yt-dlp and URL mining often lack (MSE, tokenized MP4s).
      const contextHeight = (probe.videoHeight > 0 && probe.videoWidth > 0
        && probe.videoHeight >= 144 && probe.videoHeight <= 8640)
        ? probe.videoHeight
        : 0;
      if (contextHeight && videoFormats.length === 1 && !videoFormats[0].height) {
        const onlyFormat = videoFormats[0];
        onlyFormat.height = contextHeight;
        if (!onlyFormat.resolution) onlyFormat.resolution = `${contextHeight}p`;
        if (isPlaceholderLabel(onlyFormat.label)) onlyFormat.label = onlyFormat.resolution;
      }

      const directResolution = DownloadAnything.extractResolutionFromSources(probe.directUrl);
      // Sniff actual dimensions for direct files with no resolution metadata
      // so the picker shows a real resolution instead of "Best".
      const labelSpans = new Map();
      const displayLabel = fmt => isPlaceholderLabel(fmt.label)
        ? (fmt.height
            ? `${fmt.height}p`
            : (contextHeight
                ? `${contextHeight}p`
                : (fmt.kind === 'audio' ? 'Audio' : 'Best')))
        : fmt.label;
      const updateFormatLabel = (fmt) => {
        const span = labelSpans.get(fmt);
        if (!span) return;
        span.textContent = displayLabel(fmt);
      };
      const sniffVideoDimensions = (fmt) => {
        if (fmt.height || fmt.kind !== 'video' || !fmt.url || !/^https?:/i.test(fmt.url)) return;
        try {
          const video = document.createElement('video');
          video.muted = true;
          video.preload = 'metadata';
          video.style.display = 'none';
          const cleanup = () => { video.removeAttribute('src'); video.load(); video.remove(); };
          video.addEventListener('loadedmetadata', () => {
            if (video.videoHeight > 0 && video.videoWidth > 0) {
              fmt.height = video.videoHeight;
              fmt.resolution = `${video.videoHeight}p`;
              if (isPlaceholderLabel(fmt.label)) fmt.label = fmt.resolution;
              updateFormatLabel(fmt);
            }
            cleanup();
          }, { once: true });
          video.addEventListener('error', cleanup, { once: true });
          setTimeout(cleanup, 10000);
          video.src = fmt.url;
          document.body.appendChild(video);
        } catch {}
      };
      for (const fmt of allFormats) {
        if (fmt.kind === 'video' && !fmt.height && isPlaceholderLabel(fmt.label)) sniffVideoDimensions(fmt);
      }
      selectedFormat = visibleFormats.find(fmt => sameResource(fmt.url, probe.directUrl))
        || visibleFormats.find(fmt => directResolution && (fmt.resolution === directResolution || fmt.label === directResolution))
        || visibleFormats[0];
      const radios = [];

      const buildFormatPanel = (formats, tabClass) => {
        const formatSection = h('div', '');
        formatSection.appendChild(h('div', 'section-label', probe.engine === 'file' ? 'File' : 'Quality'));
        const list = h('div', 'format-list');
        const listHeader = h('div', 'format-list-header');
        listHeader.appendChild(h('span', 'format-list-radio-col', ''));
        const headLabel = h('span', 'format-list-label-col', 'Resolution');
        const headExt = h('span', 'format-list-ext-col', 'Format');
        const headSize = h('span', 'format-list-size-col', 'Size');
        listHeader.appendChild(headLabel);
        listHeader.appendChild(headExt);
        listHeader.appendChild(headSize);
        list.appendChild(listHeader);

        for (const fmt of formats) {
          // Last resort: never render a URL or raw placeholder in the
          // Resolution column — derive a clean label from what is known.
          const rowLabel = displayLabel(fmt);
          const isSelected = fmt === selectedFormat;
          const row = h('label', `format-list-row${isSelected ? ' selected' : ''}`);
          const radioCol = h('span', 'format-list-radio-col');
          const radio = document.createElement('input');
          radio.type = 'radio';
          radio.name = `da-format-${tabClass}`;
          radio.checked = isSelected;
          radio.setAttribute('aria-label', `${rowLabel} ${fmt.ext || ''} ${fmt.size != null ? formatBytes(fmt.size) : ''}`);
          radioCol.appendChild(radio);
          const labelCol = h('span', 'format-list-label-col');
          const labelSpan = h('span', 'format-list-label', rowLabel);
          labelCol.appendChild(labelSpan);
          labelSpans.set(fmt, labelSpan);
          if (fmt.vcodec) {
            labelCol.appendChild(h('span', 'format-list-codec', shortCodec(fmt.vcodec)));
          }
          const extCol = h('span', 'format-list-ext-col', fmt.ext || '—');
          const sizeCol = h(
            'span',
            'format-list-size-col',
            fmt.size == null ? '—' : (fmt.sizeIsEstimate ? '~ ' : '') + formatBytes(fmt.size)
          );
          row.appendChild(radioCol);
          row.appendChild(labelCol);
          row.appendChild(extCol);
          row.appendChild(sizeCol);

          const pick = () => {
            selectedFormat = fmt;
            for (const r of radios) {
              r.row.classList.remove('selected');
              r.radio.checked = false;
            }
            row.classList.add('selected');
            radio.checked = true;
            if (nameRow) {
              const stem = DownloadAnything.stemFromName(filename) || filename;
              nameRow.textContent = fmt.kind === 'audio' ? `${stem}.mp3` : filename;
            }
          };
          radios.push({ row, radio });
          radio.addEventListener('change', pick);
          row.addEventListener('click', pick);
          list.appendChild(row);
        }
        formatSection.appendChild(list);
        const panel = h('div', `tab-panel ${tabClass}`);
        panel.appendChild(formatSection);
        return panel;
      };

      if (hasVideo) videoPanel = buildFormatPanel(videoFormats, 'video-tab');
      if (hasAudio) audioPanel = buildFormatPanel(audioFormats, 'audio-tab');
    }

    const tabOrder = [
      ...(hasVideo ? ['video'] : []),
      ...(hasAudio ? ['audio'] : []),
      ...(hasPlaylist ? ['playlist'] : []),
    ];
    let activeTab = tabOrder[0] || 'video';

    if (hasPlaylist) {
      const entries = result.entries || [];
      selectedUrls = new Set(entries.map(e => e.url));
      playlistPanel = h('div', 'tab-panel playlist-tab');

      const header = h('div', 'playlist-header');
      header.appendChild(h('span', 'section-label', 'Videos to download'));
      const selectAllWrap = h('label', 'playlist-select-all');
      selectAllWrap.htmlFor = 'da-select-all-playlist';
      const selectAllCb = document.createElement('input');
      selectAllCb.type = 'checkbox';
      selectAllCb.id = 'da-select-all-playlist';
      selectAllCb.checked = true;
      selectAllWrap.appendChild(selectAllCb);
      selectAllWrap.appendChild(document.createTextNode('Select all'));
      header.appendChild(selectAllWrap);
      playlistPanel.appendChild(header);

      const list = h('div', 'format-list playlist-list');
      const entryCheckboxes = [];
      for (const entry of entries) {
        const isCurrent = result.currentEntryId && entry.id === result.currentEntryId;
        const row = h('label', 'playlist-entry-row');
        const radioCol = h('span', 'format-list-radio-col');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = true;
        cb.setAttribute('aria-label', `Select ${entry.title || entry.url}`);
        radioCol.appendChild(cb);
        row.appendChild(radioCol);

        if (entry.thumbnail) {
          const thumb = h('img', 'playlist-entry-thumb');
          thumb.src = entry.thumbnail;
          thumb.alt = '';
          row.appendChild(thumb);
        }

        const meta = h('div', 'playlist-entry-meta');
        const titleWrap = h('div', 'playlist-entry-title-wrap');
        titleWrap.appendChild(h('span', 'playlist-entry-title', entry.title || entry.url));
        if (isCurrent) titleWrap.appendChild(h('span', 'playlist-entry-current', 'Current'));
        meta.appendChild(titleWrap);
        const sub = h('div', 'playlist-entry-sub');
        if (entry.uploader) sub.appendChild(h('span', '', entry.uploader));
        if (entry.uploader && entry.duration) sub.appendChild(h('span', '', ' · '));
        if (entry.duration) sub.appendChild(h('span', '', formatDuration(entry.duration)));
        meta.appendChild(sub);
        row.appendChild(meta);

        row.dataset.url = entry.url;
        if (cb.checked) row.classList.add('selected');

        const toggle = (checked) => {
          cb.checked = checked;
          row.classList.toggle('selected', checked);
          if (checked) selectedUrls.add(entry.url);
          else selectedUrls.delete(entry.url);
          updateSelectAll();
          updateDownloadButton();
        };
        cb.addEventListener('change', () => toggle(cb.checked));
        entryCheckboxes.push(cb);
        list.appendChild(row);
      }
      playlistPanel.appendChild(list);

      const updateSelectAll = () => {
        const all = entryCheckboxes.every(cb => cb.checked);
        selectAllCb.checked = all;
      };
      selectAllCb.addEventListener('change', () => {
        const checked = selectAllCb.checked;
        for (const cb of entryCheckboxes) {
          cb.checked = checked;
          const row = cb.closest('.playlist-entry-row');
          if (row) row.classList.toggle('selected', checked);
          const url = row?.dataset.url;
          if (!url) continue;
          if (checked) selectedUrls.add(url);
          else selectedUrls.delete(url);
        }
        updateDownloadButton();
      });
    }

    const panels = h('div', 'tab-panels');
    if (videoPanel) {
      videoPanel.setAttribute('role', 'tabpanel');
      videoPanel.id = 'da-panel-video';
      videoPanel.setAttribute('aria-labelledby', 'da-tab-video');
      videoPanel.tabIndex = 0;
      panels.appendChild(videoPanel);
    }
    if (audioPanel) {
      audioPanel.setAttribute('role', 'tabpanel');
      audioPanel.id = 'da-panel-audio';
      audioPanel.setAttribute('aria-labelledby', 'da-tab-audio');
      audioPanel.tabIndex = 0;
      panels.appendChild(audioPanel);
    }
    if (playlistPanel) {
      playlistPanel.setAttribute('role', 'tabpanel');
      playlistPanel.id = 'da-panel-playlist';
      playlistPanel.setAttribute('aria-labelledby', 'da-tab-playlist');
      playlistPanel.tabIndex = 0;
      panels.appendChild(playlistPanel);
    }

    if (tabOrder.length > 1) {
      const tabContainer = h('div', 'modal-tabs');
      tabContainer.setAttribute('role', 'tablist');
      tabContainer.setAttribute('aria-label', 'Media format options');

      const selectTab = (id) => {
        activeTab = id;
        for (const [key, b] of Object.entries(tabBtns)) {
          const isCurrent = key === id;
          b.classList.toggle('active', isCurrent);
          b.setAttribute('aria-selected', isCurrent ? 'true' : 'false');
          b.tabIndex = isCurrent ? 0 : -1;
        }
        if (videoPanel) videoPanel.style.display = activeTab === 'video' ? '' : 'none';
        if (audioPanel) audioPanel.style.display = activeTab === 'audio' ? '' : 'none';
        if (playlistPanel) playlistPanel.style.display = activeTab === 'playlist' ? '' : 'none';
        updateDownloadButton();
      };

      const makeTab = (id, label) => {
        const isActive = activeTab === id;
        const btn = h('button', `modal-tab-btn${isActive ? ' active' : ''}`, label);
        btn.type = 'button';
        btn.setAttribute('role', 'tab');
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        btn.setAttribute('aria-controls', `da-panel-${id}`);
        btn.id = `da-tab-${id}`;
        btn.tabIndex = isActive ? 0 : -1;

        btn.addEventListener('click', () => selectTab(id));
        tabContainer.appendChild(btn);
        tabBtns[id] = btn;
      };

      // APG keyboard navigation for tabs: ArrowLeft/ArrowRight, Home, End
      tabContainer.addEventListener('keydown', (event) => {
        const tabs = tabOrder.map(id => tabBtns[id]).filter(Boolean);
        const currentIndex = tabOrder.indexOf(activeTab);
        let nextIndex = -1;

        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
          nextIndex = (currentIndex + 1) % tabs.length;
        } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
          nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
        } else if (event.key === 'Home') {
          nextIndex = 0;
        } else if (event.key === 'End') {
          nextIndex = tabs.length - 1;
        }

        if (nextIndex !== -1) {
          event.preventDefault();
          const targetId = tabOrder[nextIndex];
          selectTab(targetId);
          tabBtns[targetId]?.focus();
        }
      });

      if (videoPanel) makeTab('video', 'Video');
      if (audioPanel) makeTab('audio', 'Audio');
      if (playlistPanel) makeTab('playlist', 'Playlist');
      content.appendChild(tabContainer);
      if (videoPanel) videoPanel.style.display = activeTab === 'video' ? '' : 'none';
      if (audioPanel) audioPanel.style.display = activeTab === 'audio' ? '' : 'none';
      if (playlistPanel) playlistPanel.style.display = activeTab === 'playlist' ? '' : 'none';
    }

    content.appendChild(panels);

    function updateDownloadButton() {
      if (activeTab === 'playlist' && hasPlaylist) {
        const count = selectedUrls.size;
        downloadBtn.textContent = count ? `Download ${count} video${count === 1 ? '' : 's'}` : 'No videos selected';
        downloadBtn.disabled = count === 0;
      } else {
        downloadBtn.textContent = 'Download';
        downloadBtn.disabled = false;
      }
    }

    // Download location
    const output = h('div', 'output-row');
    const locLabel = h('label', '', 'Download location');
    locLabel.htmlFor = 'da-download-dir';
    output.appendChild(locLabel);
    const select = h('select', 'category-select');
    select.id = 'da-download-dir';
    for (const option of locationOptions(settings)) {
      const opt = document.createElement('option');
      opt.value = option.path;
      opt.textContent = option.name;
      select.appendChild(opt);
    }
    output.appendChild(select);
    content.appendChild(output);

    // Filename preview: yt-dlp-provided name when supported
    let nameRow = null;
    if (filename) {
      nameRow = h('div', 'filename-preview', filename);
      content.appendChild(nameRow);
    }

    // Footer actions
    const cancelBtn = h('button', 'footer-btn cancel', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeProbeModal);
    const downloadBtn = h('button', 'footer-btn download', 'Download');
    downloadBtn.type = 'button';
    footer.appendChild(cancelBtn);
    footer.appendChild(downloadBtn);
    updateDownloadButton();

    async function startDownload(duplicateAction) {
      downloadBtn.disabled = true;
      downloadBtn.textContent = 'Starting…';

      let payload;
      if (activeTab === 'playlist' && hasPlaylist) {
        const urls = [...selectedUrls];
        DownloadAnythingLogger.info('starting playlist download', { url: probe.url, selectedCount: urls.length, directory: select.value, duplicateAction, referer: probe.referer });
        payload = {
          type: 'download',
          url: probe.url,
          directory: select.value,
          downloadPlaylist: true,
          selectedEntryUrls: urls,
          title: result.playlistTitle || result.title,
          thumbnail: result.thumbnail,
          engine: probe.engine,
          mediaType: probe.mediaType,
          referer: probe.referer
        };
      } else {
        const useFormatSelector = probe.engine === 'ytdlp' || !selectedFormat?.url;
        const selectedUrl = useFormatSelector ? probe.url : selectedFormat.url;
        const selectedFormatId = useFormatSelector ? selectedFormat?.selector : undefined;
        const isAudio = selectedFormat?.kind === 'audio';
        DownloadAnythingLogger.info('starting download', { url: selectedUrl, formatId: selectedFormatId, directory: select.value, duplicateAction, referer: probe.referer });
        payload = {
          type: 'download',
          url: selectedUrl,
          formatId: selectedFormatId || undefined,
          directory: select.value,
          filename: filename || undefined,
          title,
          thumbnail: result.thumbnail,
          engine: probe.engine,
          mediaType: isAudio ? 'audio' : probe.mediaType,
          referer: probe.referer
        };
      }
      if (duplicateAction) {
        payload.duplicateAction = duplicateAction;
      }
      const response = await sendBackend(payload);
      if (!modalHost) return;
      if (!response.ok && response.error === 'duplicate' && !duplicateAction) {
        DownloadAnythingLogger.info('duplicate download detected', { url: probe.url, duplicate: response.duplicate });
        renderDuplicatePrompt(content, footer, response.duplicate, (action) => {
          if (action === 'skip') {
            closeProbeModal();
            return;
          }
          startDownload(action);
        });
        return;
      }
      if (!response.ok) {
        DownloadAnythingLogger.error('download start failed', { url: probe.url, error: response.error });
        downloadBtn.disabled = false;
        downloadBtn.textContent = 'Download';
        const error = h('div', 'error-message', response.error || 'Failed to start download');
        error.setAttribute('role', 'alert');
        content.insertBefore(error, output.nextSibling);
        return;
      }
      if (response.skipped) {
        DownloadAnythingLogger.info('download skipped by user', { url: probe.url });
        closeProbeModal();
        return;
      }
      DownloadAnythingLogger.info('download started', { jobId: response.jobId, url: probe.url });
      closeProbeModal();
    }

    downloadBtn.addEventListener('click', () => startDownload(null));
  }


  function renderProbeError(content, footer, probe) {
    content.replaceChildren();
    footer.style.display = 'flex';
    footer.replaceChildren();
    const error = h('div', 'error-message',
      errorMessage(probe.error, 'Could not read download metadata. Please check the media URL or your network connection and try again.'));
    error.setAttribute('role', 'alert');
    content.appendChild(error);

    const closeBtn = h('button', 'footer-btn cancel', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeProbeModal);
    footer.appendChild(closeBtn);
  }

  function renderTorrentResult(content, footer, probe, settings) {
    content.replaceChildren();
    footer.style.display = 'flex';
    footer.replaceChildren();
    const files = probe.files || [];
    const selectedFiles = new Set(files.map(file => file.path));

    const info = h('div', 'media-info');
    info.appendChild(h('div', 'media-thumb', 'TORRENT'));
    const details = h('div', 'media-details');
    details.appendChild(h('div', 'filename-preview', probe.name));
    const badges = h('div', 'meta-badges-row');
    badges.appendChild(h('span', 'meta-badge-chip engine torrent', 'libtorrent'));
    badges.appendChild(h('span', 'meta-badge-chip', formatBytes(probe.totalSize)));
    badges.appendChild(h('span', 'meta-badge-chip', `${probe.fileCount} file${probe.fileCount === 1 ? '' : 's'}`));
    details.appendChild(badges);
    info.appendChild(details);
    content.appendChild(info);

    const filesSection = h('div', '');
    filesSection.appendChild(h('div', 'section-label', 'Files to download'));
    const list = h('div', 'torrent-file-list');
    let downloadBtn;
    const updateDownloadState = () => {
      if (downloadBtn) {
        const count = selectedFiles.size;
        downloadBtn.textContent = count ? `Download torrent (${count})` : 'No files selected';
        downloadBtn.disabled = count === 0;
      }
    };
    for (const file of files) {
      const row = h('label', 'torrent-file-row');
      const checkboxCol = h('span', 'format-list-radio-col');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = true;
      checkbox.setAttribute('aria-label', `Select ${file.path}`);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selectedFiles.add(file.path);
        else selectedFiles.delete(file.path);
        updateDownloadState();
      });
      checkboxCol.appendChild(checkbox);
      row.appendChild(checkboxCol);
      row.appendChild(h('span', '', file.path));
      row.appendChild(h('small', '', formatBytes(file.size)));
      list.appendChild(row);
    }
    filesSection.appendChild(list);
    content.appendChild(filesSection);

    if (probe.trackers?.length) {
      content.appendChild(h('div', 'filename-preview', `${probe.trackers.length} tracker${probe.trackers.length === 1 ? '' : 's'} discovered`));
    }

    const output = h('div', 'output-row');
    const locLabel = h('label', '', 'Download location');
    locLabel.htmlFor = 'da-torrent-download-dir';
    output.appendChild(locLabel);
    const select = h('select', 'category-select');
    select.id = 'da-torrent-download-dir';
    for (const option of locationOptions(settings)) {
      const opt = document.createElement('option');
      opt.value = option.path;
      opt.textContent = option.name;
      select.appendChild(opt);
    }
    output.appendChild(select);
    content.appendChild(output);

    const cancelBtn = h('button', 'footer-btn cancel', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeProbeModal);
    downloadBtn = h('button', 'footer-btn download', 'Download torrent');
    downloadBtn.type = 'button';
    updateDownloadState();
    footer.appendChild(cancelBtn);
    footer.appendChild(downloadBtn);

    async function startDownload(duplicateAction) {
      downloadBtn.disabled = true;
      downloadBtn.textContent = 'Starting…';
      const payload = {
        type: 'download',
        url: probe.magnet,
        directory: select.value,
        selectedFiles: [...selectedFiles],
        filename: probe.name,
        title: probe.name,
        engine: 'torrent',
        mediaType: 'torrent',
        referer: probe.referer
      };
      if (duplicateAction) payload.duplicateAction = duplicateAction;
      const response = await sendBackend(payload);
      if (!modalHost) return;
      if (!response.ok && response.error === 'duplicate' && !duplicateAction) {
        renderDuplicatePrompt(content, footer, response.duplicate, action => {
          if (action === 'skip') closeProbeModal();
          else void startDownload(action);
        });
        return;
      }
      if (!response.ok) {
        downloadBtn.disabled = false;
        downloadBtn.textContent = 'Download torrent';
        const error = h('div', 'error-message', response.error || 'Failed to start torrent');
        error.setAttribute('role', 'alert');
        content.insertBefore(error, output.nextSibling);
        return;
      }
      closeProbeModal();
    }

    downloadBtn.addEventListener('click', () => void startDownload(null));
  }


  async function openProbeModal(context) {
    DownloadAnythingLogger.info('opening probe modal', { pageUrl: context.pageUrl, directUrl: context.directUrl });
    const magnet = /^magnet:\?/i.test(context.directUrl || '');
    const { content, footer } = buildModalShell(magnet ? 'Download Torrent' : 'Download Media');
    renderProbing(content, footer);
    let probeReqId;

    try {
      probeReqId = makeReqId();
      pendingProbeReqId = probeReqId;

      const settingsPromise = sendBackend({ type: 'settings_get' });
      const fallbackSources = magnet ? [] : await collectFallbackSources(context.pageUrl, context.directUrl);
      DownloadAnythingLogger.debug('probe fallback sources collected', { count: fallbackSources.length, sources: fallbackSources });
      const probePayload = {
        type: 'probe',
        reqId: probeReqId,
        url: magnet ? context.directUrl : context.pageUrl,
        fallbackSources
      };
      if (context.mediaType || magnet) probePayload.mediaType = magnet ? 'torrent' : context.mediaType;
      const [probe, settingsResponse] = await Promise.all([
        sendBackend(probePayload),
        settingsPromise
      ]);

      // If the user cancelled/closed this probe (or opened a new one), ignore.
      if (pendingProbeReqId !== probeReqId) return;
      if (!modalHost) return; // user closed while probing

      // The probe completed; we no longer need a pending cancel token.
      pendingProbeReqId = null;

      if (!probe.ok) {
        const message = errorMessage(probe.error, 'Could not read download metadata');
        DownloadAnythingLogger.warning('probe failed', { pageUrl: context.pageUrl, error: message });
        renderProbeError(content, footer, { ...probe, error: message });
        return;
      }

      // Carry the original media type through so the download button can tell the
      // backend whether to remux (video/hls) or download as-is (file).
      if (context.mediaType) probe.mediaType = context.mediaType;
      if (!magnet) probe.directUrl = context.directUrl;
      probe.referer = context.pageUrl;
      if (context.videoWidth > 0 && context.videoHeight > 0) {
        probe.videoWidth = context.videoWidth;
        probe.videoHeight = context.videoHeight;
      }

      // If this probe came from a Chrome intercepted download, prefer Chrome's
      // resolved filename over yt-dlp's title-derived one.
      if (context.filename) {
        probe.interceptedFilename = context.filename;
      }

      // yt-dlp-supported sites return a thumbnail in the probe result; for
      // everything else derive one from the page (poster, OG/JSON-LD meta).
      // Only do this for actual video/audio pages; files and playlists should
      // not pull a site logo or unrelated image into cover art.
      if (
        probe.result &&
        !probe.result.thumbnail &&
        probe.engine !== 'file' &&
        !probe.result.isPlaylist
      ) {
        probe.result.thumbnail = context.poster || DownloadAnything.extractThumbnail();
      }

      DownloadAnythingLogger.info('probe succeeded', { pageUrl: context.pageUrl, engine: probe.engine, title: probe.result?.title || probe.torrent?.name, interceptedFilename: probe.interceptedFilename });
      const settings = settingsResponse.settings || { downloadDir: '', presetPaths: [] };
      if (probe.engine === 'torrent' && probe.torrent) {
        probe.torrent.referer = probe.referer;
        renderTorrentResult(content, footer, probe.torrent, settings);
      } else {
        renderProbeResult(content, footer, probe, settings);
      }
    } catch (error) {
      if (pendingProbeReqId !== probeReqId) return;
      if (!modalHost) return;
      pendingProbeReqId = null;
      const message = errorMessage(error, 'Probe could not be completed');
      const probe = {
        ok: false,
        engine: 'none',
        error: message
      };
      DownloadAnythingLogger.error('probe request failed', { pageUrl: context.pageUrl, error: probe.error });
      renderProbeError(content, footer, probe);
    }
  }

  async function handleOverlayClick(context) {
    const connected = await checkBackendStatus();
    if (!connected) {
      DownloadAnythingLogger.info('overlay click ignored, backend not connected');
      window.alert('yt-dlp backend is not connected');
      return;
    }
    openProbeModal(context);
  }

  async function handleInterceptedDownload(message) {
    if (window !== window.top) return false;
    const connected = await checkBackendStatus();
    if (!connected) return false;

    if (message.mediaType === 'file') {
      void openInterceptedModal(message);
    } else {
      // Stream or direct video: let the normal probe path resolve formats/sizes.
      void openProbeModal({
        pageUrl: message.pageUrl,
        directUrl: message.url,
        mediaType: message.mediaType,
        filename: message.filename
      });
    }
    return true;
  }

  async function openInterceptedModal(context) {
    DownloadAnythingLogger.info('opening intercepted file modal', { url: context.url, filename: context.filename, referer: context.pageUrl });
    const { content, footer } = buildModalShell('Download File');
    renderProbing(content, footer);

    const settingsResponse = await sendBackend({ type: 'settings_get' });
    const settings = settingsResponse.settings || { downloadDir: '', presetPaths: [] };

    let filename = context.filename || DownloadAnything.pathFilename(context.url) || 'download';
    filename = DownloadAnything.sanitizeFilename(filename) || filename;
    const ext = DownloadAnything.extFromName(filename) || DownloadAnything.extFromName(DownloadAnything.pathFilename(context.url)) || '';
    if (ext && !DownloadAnything.extFromName(filename)) {
      filename = `${filename}.${ext}`;
    }
    const title = DownloadAnything.stemFromName(filename);

    const size = context.size;
    const probe = {
      ok: true,
      engine: 'file',
      mediaType: 'file',
      url: context.url,
      referer: context.pageUrl,
      result: {
        id: '',
        title,
        uploader: '',
        duration: null,
        thumbnail: '',
        webpageUrl: context.pageUrl,
        extractor: '',
        isPlaylist: false,
        playlistCount: null,
        filename,
        formats: [{
          id: 'best',
          selector: '',
          label: 'Original',
          resolution: '',
          ext,
          tbr: null,
          fps: null,
          vcodec: '',
          acodec: '',
          kind: 'file',
          size: size == null || size <= 0 ? null : size,
          sizeIsEstimate: context.sizeIsEstimate || false
        }]
      }
    };

    if (!modalHost) return;
    renderProbeResult(content, footer, probe, settings);
  }

  function openProbeInTopFrame(context) {
    if (window === window.top) {
      handleOverlayClick(context);
      return;
    }
    sendRuntimeMessage({ type: 'da:openProbe', context }, 'No response from top frame')
      .then(response => {
        if (!response?.ok) {
          window.alert(response?.error || 'Could not open the download options');
        }
      });
  }

  function attachToVideo(video) {
    knownVideos.add(video);
    if (attachedVideos.has(video)) return;
    attachedVideos.add(video);
    video.addEventListener('mouseenter', () => onVideoEnter(video));
    video.addEventListener('mouseleave', onVideoLeave);
  }

  // Media events catch videos the MutationObserver scan can miss: players
  // that reuse detached <video> nodes, swap sources without DOM mutations,
  // or were created before the content script ran.
  for (const eventType of ['play', 'playing', 'loadstart']) {
    document.addEventListener(eventType, event => {
      const video = event.target;
      if (video instanceof HTMLVideoElement) attachToVideo(video);
    }, { capture: true, passive: true });
  }

  const knownEmbeds = new Set();

  function attachToEmbed(iframe) {
    knownEmbeds.add(iframe);
    if (attachedVideos.has(iframe)) return;
    attachedVideos.add(iframe);
    iframe.addEventListener('mouseenter', () => onVideoEnter(iframe));
    iframe.addEventListener('mouseleave', onVideoLeave);
  }

  const pendingVideoRoots = new Set([document]);

  function collectFromRoot(root, videos, embeds, roots) {
    const visit = node => {
      if (node.tagName === 'VIDEO') videos.add(node);
      if (node.tagName === 'IFRAME' && isVideoEmbed(node)) embeds.add(node);
      if (node.shadowRoot && node.shadowRoot !== root) {
        roots.add(node.shadowRoot);
        collectFromRoot(node.shadowRoot, videos, embeds, roots);
      }
    };
    if (root.nodeType === Node.ELEMENT_NODE) visit(root);

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) visit(walker.currentNode);
  }

  function scanVideos() {
    if (scanRaf) return;
    scanRaf = requestAnimationFrame(() => {
      scanRaf = null;
      for (const video of knownVideos) {
        if (!video.isConnected) knownVideos.delete(video);
      }
      for (const iframe of knownEmbeds) {
        if (!iframe.isConnected || !isVideoEmbed(iframe)) knownEmbeds.delete(iframe);
      }

      const rootsToScan = [...pendingVideoRoots];
      pendingVideoRoots.clear();
      const videos = new Set();
      const embeds = new Set();
      const roots = new Set();
      for (const root of rootsToScan) {
        if (root === document || root.isConnected) collectFromRoot(root, videos, embeds, roots);
      }
      for (const video of videos) attachToVideo(video);
      for (const iframe of embeds) attachToEmbed(iframe);
      for (const root of roots) {
        if (!observedShadowRoots.has(root)) {
          observedShadowRoots.add(root);
          observer.observe(root, { childList: true, subtree: true });
        }
      }
    });
  }

  function queueVideoScan(mutations) {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
          pendingVideoRoots.add(node);
        }
      }
    }
    scanVideos();
  }

  const observer = new MutationObserver(queueVideoScan);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  scanVideos();
})();
