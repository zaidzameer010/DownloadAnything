(function () {
  const DOWNLOAD_ICON = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjIiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTIxIDE1djRhMiAyIDAgMCAxLTIgMkg1YTIgMiAwIDAgMS0yLTJ2LTQiLz48cG9seWxpbmUgcG9pbnRzPSI3IDEwIDEyIDE1IDE3IDEwIi8+PGxpbmUgeDE9IjEyIiB5MT0iMTUiIHgyPSIxMiIgeTI9IjMiLz48L3N2Zz4=';

  const { getVideoUrl, isStream, isMasterUrl } = DownloadAnything;

  const knownVideos = new Set();
  const observedShadowRoots = new Set();
  let scanRaf = null;

  let activeOverlay = null;
  let currentVideo = null;
  let overlayRaf = null;
  let overlayVisible = false;
  let hideTimeout = null;
  let pendingPointer = null;
  let pendingPointerX = 0;
  let pendingPointerY = 0;
  let resizeObserver = null;

  const OVERLAY_SIZE = 36;
  const OVERLAY_MARGIN = 6;
  const HIDE_DELAY = 250;

  function isFullscreen() {
    return Boolean(
      document.fullscreenElement ||
      document.webkitFullscreenElement ||
      document.mozFullScreenElement ||
      document.msFullscreenElement ||
      (window.fullScreen !== undefined && window.fullScreen)
    );
  }

  function createOverlay() {
    const overlay = document.createElement('button');
    overlay.type = 'button';
    overlay.setAttribute('aria-label', 'Download video');
    if ('showPopover' in HTMLElement.prototype) {
      overlay.setAttribute('popover', 'manual');
    }
    Object.assign(overlay.style, {
      position: 'fixed',
      zIndex: '2147483647',
      top: 'auto',
      right: 'auto',
      bottom: 'auto',
      left: 'auto',
      margin: '0',
      width: `${OVERLAY_SIZE}px`,
      height: `${OVERLAY_SIZE}px`,
      border: 'none',
      borderRadius: '50%',
      background: 'rgba(0, 0, 0, 0.75)',
      cursor: 'pointer',
      padding: '6px',
      boxSizing: 'border-box',
      pointerEvents: 'auto',
      boxShadow: '0 2px 6px rgba(0, 0, 0, 0.35)',
      backdropFilter: 'blur(4px)',
      transition: 'background-color 150ms ease, transform 150ms ease'
    });
    if (!overlay.popover) overlay.style.display = 'none';

    const icon = document.createElement('img');
    icon.src = DOWNLOAD_ICON;
    icon.style.width = '100%';
    icon.style.height = '100%';
    icon.style.display = 'block';
    icon.alt = '';
    overlay.appendChild(icon);

    overlay.addEventListener('mouseenter', () => {
      overlay.style.background = 'rgba(0, 0, 0, 0.95)';
      overlay.style.transform = 'scale(1.06)';
    });
    overlay.addEventListener('mouseleave', () => {
      overlay.style.background = 'rgba(0, 0, 0, 0.75)';
      overlay.style.transform = 'scale(1)';
    });
    overlay.addEventListener('click', event => {
      event.stopPropagation();
      event.preventDefault();
      if (!currentVideo) return;

      const directUrl = getVideoUrl(currentVideo);
      const rect = overlay.getBoundingClientRect();
      const anchorRect = {
        left: rect.left,
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height
      };
      hideOverlay();

      void openProbeInTopFrame({ pageUrl: window.location.href, directUrl, anchorRect });
    });

    document.body.appendChild(overlay);
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
      if (!isVideoVisible(currentVideo)) {
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

    if ('showPopover' in activeOverlay) {
      try { activeOverlay.showPopover(); } catch { /* no-op */ }
    }
    activeOverlay.style.display = 'flex';
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
    if (activeOverlay) {
      if ('hidePopover' in activeOverlay) {
        try { activeOverlay.hidePopover(); } catch { /* no-op */ }
      }
      activeOverlay.style.display = 'none';
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
    if (typeof video.checkVisibility === 'function') {
      try {
        return video.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
      } catch { /* no-op */ }
    }
    return true;
  }

  function findVideoAtPoint(x, y) {
    let best = null;
    let bestArea = Infinity;
    for (const video of knownVideos) {
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
    if (isFullscreen()) {
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
    } else if (isOverOverlay(x, y)) {
      // keep the overlay visible
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
    showOverlay(video);
  }

  function onVideoLeave() {
    scheduleHideOverlay();
  }

  document.addEventListener('pointermove', onPointerMove, { passive: true, capture: true });
  document.addEventListener('pointerleave', onPointerLeave, { capture: true });
  document.addEventListener('pointerdown', onPointerDown, { capture: true });
  ['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'].forEach(eventType => {
    document.addEventListener(eventType, () => {
      if (isFullscreen()) hideOverlay();
    }, { passive: true, capture: true });
  });

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

  function sendRuntimeMessage(message, defaultError) {
    return new Promise(resolve => {
      let response;
      try {
        response = chrome.runtime.sendMessage(message, reply => {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, error: chrome.runtime.lastError.message });
          } else {
            resolve(reply || { ok: false, error: defaultError });
          }
        });
      } catch (error) {
        resolve({ ok: false, error: error.message });
        return;
      }
      // Some Chrome builds return a promise instead of using the callback.
      if (response && typeof response.then === 'function') {
        response.then(reply => resolve(reply || { ok: false, error: defaultError }))
          .catch(error => resolve({ ok: false, error: error.message }));
      }
    });
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
      if (/\bmaster\b/i.test(item.url)) return 0;
      return isMasterUrl(item.url) ? 1 : 2;
    };
    const sortedCaptured = captured
      .slice()
      .sort((a, b) => captureRank(a) - captureRank(b) || a.timestamp - b.timestamp);

    const sources = [];
    const seen = new Set([pageUrl]);
    const add = (url, label = '') => {
      if (!url) return;
      if (seen.has(url)) {
        const existing = sources.find(source => source.url === url);
        if (existing && !existing.label && label) existing.label = label;
        return;
      }
      seen.add(url);
      sources.push({ url, label });
    };
    add(directUrl, DownloadAnything.extractResolutionFromSources(directUrl));
    for (const source of sortedPageSources) add(source.url, source.label);
    for (const item of sortedCaptured) add(item.url);
    return sources.slice(0, 12);
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
      handleInterceptedDownload(message);
      sendResponse({ ok: true });
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
   * Toast notifications (shared shadow-DOM host)
   * ---------------------------------------------------------------------- */
  let toastHost = null;
  let toastShadow = null;

  function ensureToastHost() {
    if (toastHost && toastHost.isConnected) return;
    toastHost = document.createElement('div');
    toastShadow = toastHost.attachShadow({ mode: 'open' });
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = chrome.runtime.getURL('src/styles.css');
    toastShadow.appendChild(link);
    document.documentElement.appendChild(toastHost);
  }

  function showToast(text, isError) {
    ensureToastHost();
    const toast = document.createElement('div');
    toast.className = `dl-toast ${isError ? 'dl-toast-error' : 'dl-toast-success'}`;
    toast.textContent = text;
    toastShadow.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('show'));
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 300);
    }, 3200);
  }

  /* ------------------------------------------------------------------------
   * Probe modal (shadow DOM, styled by src/styles.css)
   * ---------------------------------------------------------------------- */
  let modalHost = null;
  let modalCleanup = null;

  function h(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function closeProbeModal() {
    if (modalCleanup) {
      modalCleanup();
      modalCleanup = null;
    }
    if (modalHost) {
      modalHost.remove();
      modalHost = null;
    }
  }

  function buildModalShell(titleText) {
    closeProbeModal();
    modalHost = document.createElement('div');
    const shadow = modalHost.attachShadow({ mode: 'open' });
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = chrome.runtime.getURL('src/styles.css');
    shadow.appendChild(link);

    const backdrop = h('div', 'modal-backdrop');
    const box = h('div', 'modal-box');

    const header = h('div', 'modal-header');
    header.appendChild(h('h2', '', titleText));
    const closeBtn = h('button', 'close-btn');
    closeBtn.type = 'button';
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

    backdrop.addEventListener('click', event => {
      if (event.target === backdrop) closeProbeModal();
    });

    function onKeyDown(event) {
      if (event.key === 'Escape') closeProbeModal();
    }
    document.addEventListener('keydown', onKeyDown);
    modalCleanup = () => document.removeEventListener('keydown', onKeyDown);

    return { shadow, content, footer };
  }

  function renderProbing(content) {
    const wrap = h('div', 'progress-container');
    const labelRow = h('div', 'progress-label-row');
    labelRow.appendChild(h('span', '', 'Probing with yt-dlp'));
    const stage = h('span', '');
    stage.id = 'progressStage';
    stage.textContent = 'extracting media info…';
    labelRow.appendChild(stage);
    const track = h('div', 'progress-track');
    const fill = h('div', 'progress-fill indeterminate');
    track.appendChild(fill);
    wrap.appendChild(labelRow);
    wrap.appendChild(track);
    content.appendChild(wrap);
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
    content.innerHTML = '';
    footer.innerHTML = '';
    footer.style.display = 'flex';

    const wrap = h('div', 'duplicate-prompt');
    wrap.appendChild(h('div', 'duplicate-title', 'Duplicate found'));

    const message = duplicate.type === 'job'
      ? 'A download for this item is already active.'
      : `${duplicate.filename || 'This file'} already exists in the download location.`;
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

    const overrideBtn = h('button', 'footer-btn download', 'Override');
    overrideBtn.type = 'button';
    overrideBtn.addEventListener('click', () => onChoose('override'));

    const renameBtn = h('button', 'footer-btn download',
      `Rename to ${duplicate.suggestedName || 'unique name'}`);
    renameBtn.type = 'button';
    renameBtn.addEventListener('click', () => onChoose('rename'));

    footer.appendChild(skipBtn);
    footer.appendChild(overrideBtn);
    footer.appendChild(renameBtn);
  }

  function renderProbeResult(content, footer, probe, settings) {
    content.innerHTML = '';
    footer.style.display = 'flex';
    footer.innerHTML = '';
    const result = probe.result;

    // yt-dlp names extractor-fallback streams after the manifest ("master",
    // "index-v1-a1"); derive a meaningful name from the page instead.
    // Intercepted generic files already have a reliable filename from the browser.
    let filename = result.filename;
    let title = result.title;
    if (probe.engine === 'file') {
      filename = result.filename;
      title = (filename ? filename.replace(/\.[^.]+$/, '') : '') || result.webpageUrl;
    } else if (probe.engine !== 'ytdlp') {
      filename = DownloadAnything.suggestFilename(
        probe.url,
        DownloadAnything.classifyMedia(probe.url, null),
        null,
        settings?.mergeOutputFormat
      );
      title = filename.replace(/\.[^.]+$/, '') || title;
    }

    // For Chrome-intercepted media, prefer the filename Chrome already resolved
    // from the response (Content-Disposition or URL path) over the page title.
    if (probe.interceptedFilename && probe.engine !== 'file') {
      const intercepted = probe.interceptedFilename;
      const interceptedBase = intercepted.replace(/\.[^.]+$/, '');
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
    const hasVideo = result.formats?.length > 0;
    const hasPlaylist = result.isPlaylist && (result.entries?.length ?? 0) > 0;
    let activeTab = hasVideo ? 'video' : 'playlist';

    const tabBtns = {};
    let videoPanel = null;
    let playlistPanel = null;
    let selectedFormat = null;
    let selectedUrls = new Set();

    if (hasVideo) {
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

      const formats = result.formats;
      const baseResolutionSources = [probe.directUrl, filename, result.filename, probe.url, result.webpageUrl].filter(Boolean);
      function isPlaceholderLabel(label) {
        return !label || /^(best quality|direct|video)$/i.test(label);
      }
      function sameResource(left, right) {
        if (!left || !right) return false;
        try {
          return new URL(left).pathname === new URL(right).pathname;
        } catch {
          return left.split('?')[0] === right.split('?')[0];
        }
      }

      const directResolution = DownloadAnything.extractResolutionFromSources(probe.directUrl);
      selectedFormat = formats.find(fmt => sameResource(fmt.url, probe.directUrl))
        || formats.find(fmt => directResolution && (fmt.resolution === directResolution || fmt.label === directResolution))
        || formats[0];
      const radios = [];
      for (const fmt of formats) {
        if (!fmt.resolution && fmt.kind === 'video' && isPlaceholderLabel(fmt.label)) {
          const fmtSources = [fmt.url, ...baseResolutionSources].filter(Boolean);
          const extracted = DownloadAnything.extractResolutionFromSources(...fmtSources);
          if (extracted) {
            fmt.resolution = extracted;
            fmt.label = extracted;
          }
        }
        const row = h('div', `format-list-row${fmt === selectedFormat ? ' selected' : ''}`);
        const radioCol = h('span', 'format-list-radio-col');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'da-format';
        radio.checked = fmt === selectedFormat;
        radioCol.appendChild(radio);
        const labelCol = h('span', 'format-list-label-col');
        labelCol.appendChild(h('span', 'format-list-label', fmt.label + (fmt.kind === 'audio' ? ' (audio)' : '')));
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
          for (const r of radios) r.row.classList.remove('selected');
          row.classList.add('selected');
          radio.checked = true;
        };
        radios.push({ row, radio });
        row.addEventListener('click', pick);
        list.appendChild(row);
      }
      formatSection.appendChild(list);
      videoPanel = h('div', 'tab-panel video-tab');
      videoPanel.appendChild(formatSection);
    }

    if (hasPlaylist) {
      const entries = result.entries || [];
      selectedUrls = new Set(entries.map(e => e.url));
      playlistPanel = h('div', 'tab-panel playlist-tab');

      const header = h('div', 'playlist-header');
      header.appendChild(h('span', 'section-label', 'Videos to download'));
      const selectAllWrap = h('label', 'playlist-select-all');
      const selectAllCb = document.createElement('input');
      selectAllCb.type = 'checkbox';
      selectAllCb.checked = true;
      selectAllWrap.appendChild(selectAllCb);
      selectAllWrap.appendChild(document.createTextNode('Select all'));
      header.appendChild(selectAllWrap);
      playlistPanel.appendChild(header);

      const list = h('div', 'format-list playlist-list');
      const entryCheckboxes = [];
      for (const entry of entries) {
        const isCurrent = result.currentEntryId && entry.id === result.currentEntryId;
        const row = h('div', 'playlist-entry-row');
        const radioCol = h('span', 'format-list-radio-col');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = true;
        cb.setAttribute('aria-label', `Download ${entry.title}`);
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

        const toggle = (checked) => {
          cb.checked = checked;
          if (checked) selectedUrls.add(entry.url);
          else selectedUrls.delete(entry.url);
          updateSelectAll();
          updateDownloadButton();
        };
        cb.addEventListener('change', () => toggle(cb.checked));
        row.addEventListener('click', (event) => {
          if (event.target === cb) return;
          toggle(!cb.checked);
        });
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
          const url = cb.closest('.playlist-entry-row')?.dataset.url;
          if (!url) continue;
          if (checked) selectedUrls.add(url);
          else selectedUrls.delete(url);
        }
        updateDownloadButton();
      });
    }

    const panels = h('div', 'tab-panels');
    if (videoPanel) panels.appendChild(videoPanel);
    if (playlistPanel) panels.appendChild(playlistPanel);

    if (hasVideo && hasPlaylist) {
      const tabContainer = h('div', 'modal-tabs');
      const makeTab = (id, label) => {
        const btn = h('button', `modal-tab-btn${activeTab === id ? ' active' : ''}`, label);
        btn.type = 'button';
        btn.addEventListener('click', () => {
          activeTab = id;
          for (const b of Object.values(tabBtns)) b.classList.toggle('active', b === btn);
          if (videoPanel) videoPanel.style.display = activeTab === 'video' ? '' : 'none';
          if (playlistPanel) playlistPanel.style.display = activeTab === 'playlist' ? '' : 'none';
          updateDownloadButton();
        });
        tabContainer.appendChild(btn);
        tabBtns[id] = btn;
      };
      makeTab('video', 'Video');
      makeTab('playlist', 'Playlist');
      content.appendChild(tabContainer);
      if (videoPanel) videoPanel.style.display = activeTab === 'video' ? '' : 'none';
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
    output.appendChild(h('label', '', 'Download Location'));
    const select = h('select', 'category-select');
    for (const option of locationOptions(settings)) {
      const opt = document.createElement('option');
      opt.value = option.path;
      opt.textContent = option.name;
      select.appendChild(opt);
    }
    output.appendChild(select);
    content.appendChild(output);

    // Filename preview: yt-dlp-provided name when supported
    if (filename) {
      const nameRow = h('div', 'filename-preview', filename);
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
        DownloadAnythingLogger.info('starting playlist download', { url: probe.url, selectedCount: urls.length, directory: select.value, duplicateAction });
        payload = {
          type: 'download',
          url: probe.url,
          directory: select.value,
          downloadPlaylist: true,
          selectedEntryUrls: urls,
          title: result.playlistTitle || result.title,
          thumbnail: result.thumbnail,
          engine: probe.engine,
          mediaType: probe.mediaType
        };
      } else {
        const selectedUrl = selectedFormat?.url || probe.url;
        const selectedFormatId = selectedFormat?.url ? undefined : selectedFormat?.selector;
        DownloadAnythingLogger.info('starting download', { url: selectedUrl, formatId: selectedFormatId, directory: select.value, duplicateAction });
        payload = {
          type: 'download',
          url: selectedUrl,
          formatId: selectedFormatId || undefined,
          directory: select.value,
          filename: filename || undefined,
          title,
          thumbnail: result.thumbnail,
          engine: probe.engine,
          mediaType: probe.mediaType
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
        content.appendChild(h('div', 'error-message', response.error || 'Failed to start download'));
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
    content.innerHTML = '';
    footer.style.display = 'none';
    const wrap = h('div', 'progress-container');
    wrap.appendChild(h('div', 'error-message',
      errorMessage(probe.error, 'Could not read download metadata')));
    content.appendChild(wrap);
  }

  function renderTorrentResult(content, footer, probe, settings) {
    content.innerHTML = '';
    footer.style.display = 'flex';
    footer.innerHTML = '';
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
      if (downloadBtn) downloadBtn.disabled = selectedFiles.size === 0;
    };
    for (const file of files) {
      const row = h('label', 'torrent-file-row');
      const checkboxCol = h('span', 'format-list-radio-col');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = true;
      checkbox.setAttribute('aria-label', `Download ${file.path}`);
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
    output.appendChild(h('label', '', 'Download Location'));
    const select = h('select', 'category-select');
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
    downloadBtn = h('button', 'footer-btn download', 'Download Torrent');
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
        mediaType: 'torrent'
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
        downloadBtn.textContent = 'Download Torrent';
        content.appendChild(h('div', 'error-message', response.error || 'Failed to start torrent'));
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
    renderProbing(content);

    try {
      const settingsPromise = sendBackend({ type: 'settings_get' });
      const fallbackSources = magnet ? [] : await collectFallbackSources(context.pageUrl, context.directUrl);
      DownloadAnythingLogger.debug('probe fallback sources collected', { count: fallbackSources.length, sources: fallbackSources });
      const probePayload = {
        type: 'probe',
        url: magnet ? context.directUrl : context.pageUrl,
        fallbackSources
      };
      if (context.mediaType || magnet) probePayload.mediaType = magnet ? 'torrent' : context.mediaType;
      const [probe, settingsResponse] = await Promise.all([
        sendBackend(probePayload),
        settingsPromise
      ]);

      if (!modalHost) return; // user closed while probing

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

      // If this probe came from a Chrome intercepted download, prefer Chrome's
      // resolved filename over yt-dlp's title-derived one.
      if (context.filename) {
        probe.interceptedFilename = context.filename;
      }

      DownloadAnythingLogger.info('probe succeeded', { pageUrl: context.pageUrl, engine: probe.engine, title: probe.result?.title || probe.torrent?.name, interceptedFilename: probe.interceptedFilename });
      const settings = settingsResponse.settings || { downloadDir: '', presetPaths: [] };
      if (probe.engine === 'torrent' && probe.torrent) {
        renderTorrentResult(content, footer, probe.torrent, settings);
      } else {
        renderProbeResult(content, footer, probe, settings);
      }
    } catch (error) {
      if (!modalHost) return;
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
    DownloadAnythingLogger.info('overlay clicked', { pageUrl: context.pageUrl, directUrl: context.directUrl });
    const connected = await checkBackendStatus();
    if (!connected) {
      DownloadAnythingLogger.warning('overlay click ignored, backend not connected');
      showToast('yt-dlp backend is not connected', true);
      return;
    }
    openProbeModal(context);
  }

  async function handleInterceptedDownload(message) {
    const connected = await checkBackendStatus();
    if (!connected) {
      showToast('yt-dlp backend is not connected', true);
      return;
    }

    if (message.mediaType === 'file') {
      openInterceptedModal(message);
    } else {
      // Stream or direct video: let the normal probe path resolve formats/sizes.
      openProbeModal({
        pageUrl: message.pageUrl,
        directUrl: message.url,
        mediaType: message.mediaType,
        filename: message.filename
      });
    }
  }

  async function openInterceptedModal(context) {
    DownloadAnythingLogger.info('opening intercepted file modal', { url: context.url, filename: context.filename });
    const { content, footer } = buildModalShell('Download File');
    renderProbing(content);

    const settingsResponse = await sendBackend({ type: 'settings_get' });
    const settings = settingsResponse.settings || { downloadDir: '', presetPaths: [] };

    let filename = context.filename || DownloadAnything.pathFilename(context.url) || 'download';
    filename = DownloadAnything.sanitizeFilename(filename) || filename;
    let ext = DownloadAnything.extFromName(filename) || DownloadAnything.extFromName(DownloadAnything.pathFilename(context.url)) || 'bin';
    if (!DownloadAnything.extFromName(filename)) {
      filename = `${filename}.${ext}`;
    }
    const title = filename.replace(/\.[^.]+$/, '');

    const size = context.size;
    const probe = {
      ok: true,
      engine: 'file',
      mediaType: 'file',
      url: context.url,
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
          showToast(response?.error || 'Could not open the download options', true);
        }
      });
  }

  function attachToVideo(video) {
    if (video.__daAttached) return;
    video.__daAttached = true;
    knownVideos.add(video);
    video.addEventListener('mouseenter', () => onVideoEnter(video));
    video.addEventListener('mouseleave', onVideoLeave);
  }

  function collectFromRoot(root, videos, roots) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.tagName === 'VIDEO') videos.push(node);
      if (node.shadowRoot && node.shadowRoot !== root) {
        roots.push(node.shadowRoot);
        collectFromRoot(node.shadowRoot, videos, roots);
      }
    }
  }

  function getAllVideos() {
    const videos = [];
    const roots = [];
    collectFromRoot(document, videos, roots);
    return { videos, roots };
  }

  function scanVideos() {
    if (scanRaf) cancelAnimationFrame(scanRaf);
    scanRaf = requestAnimationFrame(() => {
      scanRaf = null;
      for (const video of Array.from(knownVideos)) {
        if (!video.isConnected) knownVideos.delete(video);
      }
      const { videos, roots } = getAllVideos();
      for (const video of videos) attachToVideo(video);
      for (const root of roots) {
        if (!observedShadowRoots.has(root)) {
          observedShadowRoots.add(root);
          observer.observe(root, { childList: true, subtree: true });
        }
      }
    });
  }

  const observer = new MutationObserver(scanVideos);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  scanVideos();
})();
