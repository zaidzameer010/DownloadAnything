// DownloadAnything content script — intelligent in-player download overlay
// Detects web video and audio players, injecting a sleek, non-obtrusive
// download trigger that links directly to the DownloadAnything desktop backend.

(() => {
  const ICON_DOWNLOAD = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v13"/><path d="m6.5 10.5 5.5 5.5 5.5-5.5"/><path d="M4 21h16"/></svg>`;
  const ICON_CHECK = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
  const ICON_ALERT = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;

  const BTN_SIZE = 34;
  const MIN_W = 140;
  const MIN_H = 80;

  // Fallback title only: DownloadAnything prefers the title its Rust extractor found on
  // the page; this runs when Rust found none (JS-rendered/SPA pages).
  // The page's own semantic title names the media: og/twitter → h1 → the
  // longest text node that is a substring of document.title → document.title.
  // Whatever wins gets trailing site chrome stripped (" - Beeg",
  function pageTitle() {
    const meta = document.querySelector(
      'meta[property="og:title"], meta[name="twitter:title"]'
    )?.content;
    const direct = meta?.trim() || document.querySelector('h1')?.textContent?.trim() || '';
    let raw = (direct || document.title || '').trim();
    // Composite titles are rendered as separate text nodes — performer,
    // title, site name. The node holding the title is a substring of
    // document.title; performer/site pieces lose on length, and unrelated
    // page text is never a substring. Longest match wins.
    let best = '';
    if (!direct && raw) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) {
        const t = n.textContent.replace(/\s+/g, ' ').trim();
        if (t.length > best.length && raw.includes(t)) best = t;
      }
      if (best) raw = best;
    }
    const site = location.hostname.split('.').filter((p) => !/^www\d*$/.test(p))[0] || '';
    const m = site && raw.match(/^(.*?)\s*[-–—|·:]\s*([^-–—|·:]*)$/);
    if (m && m[1]) {
      const tail = m[2].toLowerCase().replace(/[^a-z0-9]/g, '');
      if (!tail || tail.startsWith(site)) return m[1].trim();
    }
    return raw;
  }

  // Player poster/preview for the embed frame — the page og:image often
  // describes site chrome, while the player's own poster is the cover art.
  function pageThumbnail() {
    const abs = (u) => {
      try {
        return new URL(u, location.href).href;
      } catch {
        return '';
      }
    };
    const junk = /(?:^|[\/_.-])(logo|favicon|avatar|blank|spacer|pixel|placeholder)(?:[\/_.-]|$)/i;
    const pick = (u) => {
      const v = abs(u);
      return v && !junk.test(v) ? v : '';
    };
    const attrs = [
      'poster',
      'data-poster',
      'data-thumb',
      'data-thumbnail',
      'data-preview',
      'data-preview-url',
      'data-poster-url',
      'data-image',
      'data-cover',
      'data-splash',
    ];
    const roots = document.querySelectorAll(
      'video, [class*="player"], [class*="poster"], [class*="preview"], [class*="thumb"], [class*="video"]'
    );
    for (const el of roots) {
      for (const a of attrs) {
        const v = el.getAttribute?.(a);
        if (v && pick(v)) return pick(v);
      }
      const bg = getComputedStyle(el).backgroundImage;
      const m = bg && bg.match(/url\(["']?([^"')]+)["']?\)/);
      if (m && pick(m[1])) return pick(m[1]);
    }
    for (const img of document.querySelectorAll('img')) {
      const cls = `${img.className} ${img.id} ${img.alt}`.toLowerCase();
      if (/(poster|preview|thumb|cover|splash)/.test(cls)) {
        const v = pick(img.currentSrc || img.src);
        if (v) return v;
      }
    }
    const meta = document.querySelector(
      'meta[property="og:image"], meta[property="og:image:secure_url"], meta[name="twitter:image"]'
    );
    return meta?.content ? pick(meta.content) : '';
  }

  const tracked = new Map(); // el -> { host, io, hideTimer, mouseOver }
  const scanned = new WeakSet(); // roots already walked — prevents O(n²) rescans

  // Every resolution/codec a player advertises for one media element. The
  // network sniffer only sees the stream the player actually requested; the
  // <source> ladder names the rest, with the mime codecs on each entry.
  function mediaSources(el) {
    const abs = (u) => {
      try {
        return new URL(u, location.href).href;
      } catch {
        return '';
      }
    };
    const out = new Map();
    const add = (node, src) => {
      const url = abs(src || '');
      if (!/^https?:\/\//i.test(url) || out.has(url)) return;
      const attr = (name) => node.getAttribute?.(name) || '';
      const rawHeight =
        Number(attr('height') || attr('data-height') || attr('size') || attr('data-size')) || 0;
      const height = rawHeight >= 144 && rawHeight <= 4320 ? rawHeight : 0;
      const rawWidth = Number(attr('width') || attr('data-width')) || 0;
      const width = rawWidth >= 16 && rawWidth <= 16384 ? rawWidth : 0;
      out.set(url, {
        url,
        mime: attr('type'),
        label: attr('label') || attr('res') || attr('data-res') || attr('data-quality'),
        height,
        width,
      });
    };
    el.querySelectorAll?.('source').forEach((source) => {
      add(source, source.src || source.getAttribute('src') || source.getAttribute('data-src'));
    });
    add(el, el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src'));
    return [...out.values()];
  }

  // All media ladders on the top document — toolbar/context-menu pushes have
  // no single overlay element to read from. `tracked` adds media inside
  // shadow roots, which a plain document query cannot reach.
  function pageMediaSources() {
    const out = [];
    const seen = new Set();
    const elements = new Set([...document.querySelectorAll('video, audio'), ...tracked.keys()]);
    for (const el of elements) {
      for (const source of mediaSources(el)) {
        if (seen.has(source.url)) continue;
        seen.add(source.url);
        out.push(source);
      }
    }
    return out.slice(0, 32);
  }

  // Background asks the top frame for the DOM title (context-menu/toolbar
  // pushes have no sender frame to read it from).
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'downloadanything-dom-title' && window.top === window) {
      sendResponse(pageTitle());
    }
    if (msg?.type === 'downloadanything-dom-thumbnail' && window.top === window) {
      sendResponse(pageThumbnail());
    }
    if (msg?.type === 'downloadanything-dom-sources' && window.top === window) {
      sendResponse(pageMediaSources());
    }
  });

  // Composed-tree parent: climbs out of open shadow roots so clipping and
  // hit-tests work for media inside shadow-DOM players.
  const up = (n) => n.parentElement || n.getRootNode?.().host || null;

  // getBoundingClientRect ignores ancestor clipping — intersect the media
  // rect with the padding box of every non-visible-overflow ancestor, then
  // the viewport. Returns the genuinely visible rect or null.
  function visibleRect(el) {
    const r = el.getBoundingClientRect();
    let { top, left, right, bottom } = r;
    // Only ancestors on the containing-block chain can clip: fixed → none
    // (viewport only); absolute → from offsetParent up; everything else →
    // every ancestor.
    const pos = getComputedStyle(el).position;
    let p =
      pos === 'fixed' ? null
      : pos === 'absolute' ? (el.offsetParent || up(el))
      : up(el);
    for (; p && p !== document.documentElement; p = up(p)) {
      const s = getComputedStyle(p);
      const clipX = s.overflowX !== 'visible';
      const clipY = s.overflowY !== 'visible';
      if (!clipX && !clipY) continue;
      // A degenerate box clips nothing real — e.g. body on YouTube reports
      // clientHeight 0 (the document element is the scroller) while its
      // children render fine.
      if (p.clientWidth <= 0 || p.clientHeight <= 0) continue;
      const pr = p.getBoundingClientRect();
      if (clipX) {
        left = Math.max(left, pr.left + p.clientLeft);
        right = Math.min(right, pr.left + p.clientLeft + p.clientWidth);
      }
      if (clipY) {
        top = Math.max(top, pr.top + p.clientTop);
        bottom = Math.min(bottom, pr.top + p.clientTop + p.clientHeight);
      }
      if (right <= left || bottom <= top) return null;
    }
    left = Math.max(left, 0);
    top = Math.max(top, 0);
    right = Math.min(right, window.innerWidth);
    bottom = Math.min(bottom, window.innerHeight);
    if (right <= left || bottom <= top) return null;
    return { top, left, right, bottom, width: right - left, height: bottom - top };
  }

  // Genuinely on screen: big enough, not clipped away, and not hidden by
  // display:none / visibility:hidden on itself or an ancestor. (opacity:0 is
  // deliberately allowed — canvas/poster players keep the <video> invisible
  // while the player surface is what the user sees.)
  function mediaVisible(el) {
    if (el.checkVisibility && !el.checkVisibility({ checkVisibilityCSS: true })) {
      return null;
    }
    const v = visibleRect(el);
    return v && v.width >= MIN_W && v.height >= MIN_H ? v : null;
  }

  // The anchor corner may sit under page UI the media rect doesn't know
  // about — fixed/sticky chrome (navbars, modals, cookie banners) or another
  // media/iframe stacked on top. elementFromPoint skips pointer-events:none
  // layers, so transparent click-shields and player gradients don't wrongly
  // hide the button, and the check walks the hit's ancestors (a navbar's
  // static-positioned children still count as chrome). When top chrome
  // covers the corner the anchor slides below it if the media's visible
  
  function resolveAnchor(el, host, v, x, y) {
    for (let tries = 0; tries < 2; tries++) {
      const hit = document.elementFromPoint(x + BTN_SIZE / 2, y + BTN_SIZE / 2);
      if (!hit || hit === el || hit === host || hit.contains(el)) return { show: true, y };
      if (hit.closest?.('[data-downloadanything-overlay]')) return { show: true, y };
      if (hit.matches?.('video, audio, iframe')) return { show: false, y };

      let chrome = null;
      for (let n = hit; n; n = up(n)) {
        const pos = getComputedStyle(n).position;
        if (pos === 'fixed' || pos === 'sticky') {
          chrome = n;
          break;
        }
      }
      // Player-internal overlays (absolute, no fixed ancestor) are allowed —
      // hiding on them once meant the button never came back.
      if (!chrome || chrome.contains(el)) return { show: true, y };

      const cr = chrome.getBoundingClientRect();
      // Chrome covering from the top: slide the anchor below it if the
      // visible region still has room, then re-test once.
      if (cr.height > 0 && cr.bottom + BTN_SIZE + 8 < v.bottom) {
        y = cr.bottom + 8;
        continue;
      }
      return { show: false, y };
    }
    return { show: false, y };
  }

  // Fullscreen promotes the player into the top layer — the fixed-position
  // button would either paint over it or float detached under it. For a
  // frame embedded inside a fullscreened element of the top document,
  // document.fullscreenElement stays null — its viewport then equals the
  // screen, which is the only signal reachable cross-origin. (Skipped for
  // the top frame: a fullscreened browser window would false-positive.)
  function inFullscreen() {
    if (document.fullscreenElement) return true;
    return (
      window !== window.top &&
      window.innerWidth === screen.width &&
      window.innerHeight === screen.height
    );
  }

  function reposition() {
    const fs = inFullscreen();
    for (const [el, rec] of tracked) {
      if (!el.isConnected) {
        rec.io.disconnect();
        rec.host.remove();
        tracked.delete(el);
        continue;
      }

      // Pages occasionally wipe our node when they rebuild the player —
      // reattach rather than leaving the tracked record orphaned.
      if (!rec.host.isConnected) document.documentElement.appendChild(rec.host);

      const v = fs ? null : mediaVisible(el);
      let show = false;
      let x = 0;
      let y = 0;
      if (v) {
        x = Math.min(Math.max(8, v.right - BTN_SIZE - 10), window.innerWidth - BTN_SIZE - 8);
        y = Math.min(Math.max(8, v.top + 8), window.innerHeight - BTN_SIZE - 8);
        const a = resolveAnchor(el, rec.host, v, x, y);
        show = a.show;
        y = a.y;
      }
      rec.host.style.display = show ? 'block' : 'none';
      if (show) {
        rec.host.style.top = `${y}px`;
        rec.host.style.left = `${x}px`;
      }
    }
  }

  function track(el) {
    if (tracked.has(el)) return;

    const host = document.createElement('div');
    const shadow = host.attachShadow({ mode: 'open' });

    shadow.innerHTML = `
      <style>
        :host {
          position: fixed;
          z-index: 2147483647;
          display: none;
          pointer-events: auto;
          font-family: "Geist Variable", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
        }

        .downloadanything-btn-wrapper {
          position: relative;
          display: flex;
          align-items: center;
        }

        button {
          all: unset;
          box-sizing: border-box;
          cursor: pointer;
          width: 34px;
          height: 34px;
          border-radius: 10px;
          display: grid;
          place-items: center;
          background: oklch(0.16 0 0 / 0.85);
          color: oklch(0.905 0 0);
          border: 1px solid oklch(0.30 0 0 / 0.6);
          backdrop-filter: blur(10px);
          -webkit-backdrop-filter: blur(10px);
          box-shadow: 0 2px 10px rgb(0 0 0 / 0.35);
          transition:
            background-color 120ms cubic-bezier(0.23, 1, 0.32, 1),
            border-color 120ms cubic-bezier(0.23, 1, 0.32, 1),
            color 120ms cubic-bezier(0.23, 1, 0.32, 1),
            opacity 220ms ease;
          opacity: 0.92;
        }

        button:hover {
          background: oklch(0.24 0 0 / 0.95);
          border-color: oklch(0.42 0 0 / 0.8);
          color: oklch(0.985 0 0);
          opacity: 1;
        }

        button:active {
          background: oklch(0.28 0 0 / 0.95);
        }

        button.busy {
          cursor: wait;
          border-color: oklch(0.70 0.10 240 / 0.7);
          color: oklch(0.70 0.10 240);
          animation: downloadanything-pulse 0.9s ease-in-out infinite alternate;
        }

        button.ok {
          border-color: oklch(0.71 0.115 155 / 0.55);
          color: oklch(0.79 0.085 155);
          background: oklch(0.71 0.115 155 / 0.18);
        }

        button.err {
          border-color: oklch(0.63 0.15 25 / 0.6);
          color: oklch(0.78 0.09 25);
          background: oklch(0.63 0.15 25 / 0.18);
        }

        .tooltip {
          position: absolute;
          right: calc(100% + 8px);
          white-space: nowrap;
          background: oklch(0.24 0 0);
          border: 1px solid oklch(0.175 0 0);
          color: oklch(0.905 0 0);
          font-size: 11px;
          font-weight: 500;
          letter-spacing: -0.005em;
          padding: 4px 8px;
          border-radius: 6px;
          pointer-events: none;
          opacity: 0;
          transform: translateX(4px);
          transition:
            opacity 120ms cubic-bezier(0.23, 1, 0.32, 1),
            transform 120ms cubic-bezier(0.23, 1, 0.32, 1);
          box-shadow: 0 8px 24px -8px rgb(0 0 0 / 0.5);
        }

        .downloadanything-btn-wrapper:hover .tooltip {
          opacity: 1;
          transform: translateX(0);
        }

        @keyframes downloadanything-pulse {
          0% { opacity: 0.6; }
          100% { opacity: 1; }
        }
      </style>
      <div class="downloadanything-btn-wrapper">
        <span class="tooltip">Download with DownloadAnything</span>
        <button title="Send media to DownloadAnything">${ICON_DOWNLOAD}</button>
      </div>
    `;

    // Inline styles beat both the shadow :host rule and any page CSS that
    // might target this div — the host lives in the main document's tree.
    host.setAttribute('data-downloadanything-overlay', '');
    host.style.cssText = 'position:fixed;z-index:2147483647;display:none;';
    const btn = shadow.querySelector('button');
    const tooltip = shadow.querySelector('.tooltip');

    const resetBtn = () => {
      btn.className = '';
      btn.innerHTML = ICON_DOWNLOAD;
      tooltip.textContent = 'Download with DownloadAnything';
    };

    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();

      if (btn.classList.contains('busy')) return;
      btn.classList.add('busy');
      tooltip.textContent = 'Sending to DownloadAnything…';

      // Prefer the element's own source when it is a plain fetchable file —
      // blob:/mediasource srcs can't be re-fetched. The page/embed URL stays
      // the extraction target; the direct media URL rides as a context so
      // title/thumbnail still come from the page.
      const src =
        el.currentSrc ||
        el.src ||
        el.getAttribute?.('data-src') ||
        el.querySelector?.('source[src^="http"]')?.src ||
        '';
      const mediaUrl = /^https?:\/\//i.test(src) ? src : '';

      try {
        const res = await chrome.runtime.sendMessage({
          type: 'push',
          pageUrl: location.href,
          mediaUrl,
          referer: location.href,
          title: pageTitle(),
          thumbnail: pageThumbnail(),
          sources: mediaSources(el),
        });

        btn.classList.remove('busy');
        if (res?.ok) {
          btn.innerHTML = ICON_CHECK;
          btn.classList.add('ok');
          tooltip.textContent = 'Sent to DownloadAnything!';
        } else {
          btn.innerHTML = ICON_ALERT;
          btn.classList.add('err');
          tooltip.textContent = res?.error || 'Failed — is DownloadAnything running?';
        }
        setTimeout(resetBtn, 2200);
      } catch {
        btn.classList.remove('busy');
        btn.innerHTML = ICON_ALERT;
        btn.classList.add('err');
        tooltip.textContent = 'Connection error';
        setTimeout(resetBtn, 2200);
      }
    });

    // Auto-fade during playback when idle
    const rec = { host, io: null, hideTimer: null, mouseOver: false };

    const resetFade = () => {
      btn.style.opacity = '1';
      if (rec.hideTimer) clearTimeout(rec.hideTimer);
      if (!el.paused && !rec.mouseOver) {
        rec.hideTimer = setTimeout(() => {
          if (!el.paused && !rec.mouseOver) btn.style.opacity = '0.25';
        }, 2500);
      }
    };

    el.addEventListener('mousemove', resetFade, { passive: true });
    el.addEventListener('play', resetFade);
    el.addEventListener('pause', resetFade);

    host.addEventListener('mouseenter', () => {
      rec.mouseOver = true;
      btn.style.opacity = '1';
      if (rec.hideTimer) clearTimeout(rec.hideTimer);
    });

    host.addEventListener('mouseleave', () => {
      rec.mouseOver = false;
      resetFade();
    });

    document.documentElement.appendChild(host);

    const io = new IntersectionObserver(reposition, { threshold: 0.1 });
    io.observe(el);
    rec.io = io;

    tracked.set(el, rec);
    reposition();
  }

  const MO_OPTS = {
    childList: true,
    subtree: true,
    // class/style/hidden flips are how players toggle their chrome — each
    // flip is a reason to re-evaluate visibility and occlusion.
    attributes: true,
    attributeFilter: ['class', 'style', 'hidden'],
  };

  const mo = new MutationObserver((mutations) => {
    if (scanQueued) return;
    scanQueued = true;
    queueMicrotask(() => {
      scanQueued = false;
      for (const m of mutations) {
        if (m.type === 'childList') m.addedNodes.forEach(scan);
      }
      scheduleReposition();
    });
  });

  // The document observer can't see mutations inside a shadow root, so each
  // discovered root gets observed too — players that attach <video> into
  // their shadow DOM after initial load would otherwise never be tracked.
  function watchShadow(shadowRoot) {
    if (scanned.has(shadowRoot)) return;
    mo.observe(shadowRoot, MO_OPTS);
    scan(shadowRoot);
  }

  function scan(root) {
    if (!root || scanned.has(root)) return;
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1 && root.hasAttribute('data-downloadanything-overlay')) return;
    scanned.add(root);
    if (root.matches?.('video, audio')) track(root);
    if (root.shadowRoot) watchShadow(root.shadowRoot);
    root.querySelectorAll?.('video, audio').forEach(track);
    root.querySelectorAll?.('*').forEach((n) => {
      if (n.shadowRoot) watchShadow(n.shadowRoot);
    });
  }

  let scanQueued = false;
  mo.observe(document.documentElement, MO_OPTS);
  scan(document);
  reposition();

  let rafPending = false;
  const scheduleReposition = () => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      reposition();
    });
  };

  window.addEventListener('scroll', scheduleReposition, { capture: true, passive: true });
  window.addEventListener('resize', scheduleReposition);
  document.addEventListener('fullscreenchange', scheduleReposition);
  // Player chrome fades in/out without DOM mutations we can rely on — CSS
  // transitions and pointer movement are the last-resort triggers that let
  // a once-occluded button come back.
  document.addEventListener('transitionend', scheduleReposition, true);
  document.addEventListener('animationend', scheduleReposition, true);

  // Pointer moves re-check occlusion so the button recovers when covering
  // UI fades — throttled separately from the rAF path (~8Hz is plenty).
  let lastMoveCheck = 0;
  document.addEventListener(
    'pointermove',
    () => {
      const now = Date.now();
      if (now - lastMoveCheck < 120) return;
      lastMoveCheck = now;
      scheduleReposition();
    },
    { capture: true, passive: true }
  );

  // Magnet links are not downloads Chrome can hand over — catch the click
  // and push the URI so DownloadAnything adds it as a torrent.
  document.addEventListener(
    'click',
    (e) => {
      if (e.defaultPrevented || e.button !== 0) return;
      const el = e.target instanceof Element ? e.target : null;
      const href = el?.closest('a[href]')?.getAttribute('href') || '';
      if (!/^magnet:/i.test(href)) return;
      e.preventDefault();
      e.stopPropagation();
      chrome.runtime
        .sendMessage({
          type: 'push',
          pageUrl: href,
          referer: location.href,
          title: pageTitle(),
        })
        .catch(() => {});
    },
    true
  );
})();
