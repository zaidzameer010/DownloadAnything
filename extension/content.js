/**
 * content.js — Overlay buttons + extraction/download UI.
 *
 * Acquisition strategy (auto-fallback cascade):
 *   1. yt-dlp native  — probe the page URL; real extractors return real formats.
 *   2. stream         — probe each sniffed HLS (.m3u8) / DASH (.mpd) / MP4 URL.
 *   3. direct         — last resort: passthrough of the raw URL.
 *
 * All backend traffic is routed through the background service worker (avoids
 * mixed-content / CORS issues with the localhost engine). Long-running probes
 * open a "keepalive" Port so the MV3 service worker isn't terminated mid-call.
 */
(() => {
  "use strict";
  if (window.__DOWNLOADANYTHING_INJECTED__) return;
  window.__DOWNLOADANYTHING_INJECTED__ = true;

  /* ── Shared DOM helpers ──────────────────────────────────────────────── */

  const escapeHtml = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
    );

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "style") node.setAttribute("style", value);
      else if (key === "html") node.innerHTML = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) {
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  const errorMessage = (e) =>
    e instanceof Error ? e.message : typeof e === "string" ? e : JSON.stringify(e);

  const formatBytes = (bytes) => {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i++;
    }
    return `${value.toFixed(1)} ${units[i]}`;
  };

  const truncate = (str, max) => {
    const value = String(str ?? "");
    return value.length > max ? `${value.slice(0, max - 1)}…` : value;
  };

  const TOP_NODE = () =>
    document.fullscreenElement || document.body || document.documentElement;

  /* ── Background bridge ───────────────────────────────────────────────── */

  const keepalivePort = () => {
    try {
      return chrome.runtime.connect({ name: "keepalive" });
    } catch {
      return null;
    }
  };

  const sendToBackground = (type, payload = {}, timeoutMs = 100_000) =>
    new Promise((resolve) => {
      let done = false;
      const finish = (value) => {
        if (!done) {
          done = true;
          resolve(value);
        }
      };
      const timer = setTimeout(
        () => finish({ ok: false, error: "Background did not respond in time" }),
        timeoutMs,
      );
      try {
        chrome.runtime.sendMessage({ type, payload }, (response) => {
          clearTimeout(timer);
          if (chrome.runtime.lastError) {
            finish({ ok: false, error: chrome.runtime.lastError.message });
          } else {
            finish(response ?? { ok: false, error: "No response" });
          }
        });
      } catch (err) {
        clearTimeout(timer);
        finish({ ok: false, error: errorMessage(err) });
      }
    });

  /** Wrap an async op in a keepalive Port so the SW survives long extractions. */
  const withKeepalive = async (factory) => {
    const port = keepalivePort();
    try {
      return await factory();
    } finally {
      try {
        port?.disconnect();
      } catch {
        /* ignore */
      }
    }
  };

  /* ── Settings cache ──────────────────────────────────────────────────── */

  let cachedSettings = null;
  const fetchSettings = async () => {
    if (cachedSettings) return cachedSettings;
    const res = await sendToBackground("GET_SETTINGS");
    cachedSettings = res?.ok ? res.data : { categories: {} };
    return cachedSettings;
  };

  /* ── Media scanning (shadow-DOM aware) ───────────────────────────────── */

  const findAllMedia = (root = document) => {
    const found = [...(root.querySelectorAll?.("video, audio") || [])];
    const hosts = root.querySelectorAll?.("*") || [];
    for (const node of hosts) {
      if (node.shadowRoot) found.push(...findAllMedia(node.shadowRoot));
    }
    return found;
  };

  const PLAYER_HINTS = ["player", "video-container", "video-wrap", "video-player"];

  const findPlayerContainer = (mediaEl) => {
    if (!mediaEl?.parentElement) return document.body;
    if (mediaEl.parentElement.shadowRoot?.contains(mediaEl)) {
      return mediaEl.parentElement.shadowRoot;
    }
    let player = mediaEl.parentElement;
    let current = mediaEl.parentElement;
    while (current && current !== document.body) {
      const signature = `${current.className || ""} ${current.id || ""}`.toLowerCase();
      if (PLAYER_HINTS.some((hint) => signature.includes(hint))) player = current;
      current = current.parentElement;
    }
    return player;
  };

  /* ── Title extraction ────────────────────────────────────────────────── */

  const TITLE_SUFFIXES = [
    "YouTube", "Twitch", "Vimeo", "Netflix", "Disney+", "TikTok", "Twitter",
    "X", "Facebook", "Instagram", "Reddit", "Dailymotion", "Rumble", "Bilibili",
  ];

  const SITE_TITLE_SELECTOR = {
    "youtube.com": "ytd-watch-metadata h1, ytd-video-primary-info-renderer h1",
    "youtu.be": "ytd-watch-metadata h1, ytd-video-primary-info-renderer h1",
    "twitch.tv": '[data-a-target="stream-title"], [data-a-target="video-title"]',
  };

  const firstText = (selector, root = document) =>
    root.querySelector(selector)?.textContent?.trim() || "";

  function getMediaTitle(mediaEl) {
    const host = window.location.hostname.toLowerCase();

    // 1. Site-specific selectors (handle SPA dynamic titles)
    for (const [domain, selector] of Object.entries(SITE_TITLE_SELECTOR)) {
      if (host.includes(domain)) {
        const title = firstText(selector);
        if (title) return title;
      }
    }

    // 2. Element / player attributes
    if (mediaEl) {
      const direct = mediaEl.getAttribute("title") || mediaEl.getAttribute("aria-label");
      if (direct?.trim()) return direct.trim();

      const container = findPlayerContainer(mediaEl);
      if (container && container !== document.body) {
        const containerTitle =
          container.getAttribute?.("title") || container.getAttribute?.("aria-label");
        if (containerTitle?.trim()) return containerTitle.trim();

        for (const selector of [".video-title", ".title", ".media-title", ".caption", "figcaption"]) {
          const text = firstText(selector, container);
          if (text) return text;
        }
      }

      // 3. Nearest preceding heading
      let current = mediaEl;
      for (let depth = 0; current && current !== document.body && depth < 5; depth++) {
        let sibling = current.previousElementSibling;
        while (sibling) {
          const heading = sibling.matches("h1, h2, h3, h4")
            ? sibling
            : sibling.querySelector("h1, h2, h3, h4");
          if (heading?.textContent?.trim()) return heading.textContent.trim();
          sibling = sibling.previousElementSibling;
        }
        current = current.parentElement;
      }
    }

    // 4. Cleaned document.title
    let title = (document.title || "").trim();
    if (title) {
      const lowered = title.toLowerCase();
      for (const suffix of TITLE_SUFFIXES) {
        const token = ` - ${suffix}`.toLowerCase();
        if (lowered.endsWith(token)) {
          title = title.slice(0, -token.length).trim();
          break;
        }
      }
      title = title
        .replace(/\s*[-|·•–—]\s*(YouTube|Vimeo|Twitch|Dailymotion|Twitter|X|Facebook|Instagram|TikTok|Reddit|Bilibili|Rumble|Odysee|PeerTube|Niconico|SoundCloud|Spotify|Netflix|Prime Video|Disney\+|Apple TV)\s*$/i, "")
        .trim();
      if (title) return title;
    }

    // 5. Metadata fallbacks
    return (
      firstText("meta[property='og:title']") ||
      firstText("meta[name='twitter:title']") ||
      firstText("h1") ||
      document.title ||
      "Unknown media"
    );
  }

  /* ── Overlay buttons ─────────────────────────────────────────────────── */

  const overlays = new Map(); // active overlay descriptors (mediaEl -> overlay)
  let sharedListenersBound = false;

  const isFullscreen = () => !!document.fullscreenElement;

  function bindSharedListeners() {
    if (sharedListenersBound) return;
    sharedListenersBound = true;

    document.addEventListener("fullscreenchange", () =>
      overlays.forEach((o) => o.updateVisibility()),
    );

    // Single observer drives both pruning (detached media) and rescanning.
    let scanTimer = null;
    new MutationObserver(() => {
      for (const overlay of overlays.values()) {
        if (!overlay.mediaEl.isConnected || !overlay.host.isConnected) overlay.destroy();
      }
      if (!scanTimer) scanTimer = setTimeout(() => { scanTimer = null; scan(); }, 250);
    }).observe(document.documentElement, { childList: true, subtree: true });
  }

  function createOverlay(mediaEl) {
    if (overlays.has(mediaEl)) return;
    bindSharedListeners();

    const container = findPlayerContainer(mediaEl);
    if (window.getComputedStyle(container).position === "static") {
      container.style.position = "relative";
    }

    const host = el("div", {
      style: [
        "position:absolute", "top:10px", "left:10px", "z-index:2147483647",
        "pointer-events:auto", "opacity:0",
        "font-family:Inter,Segoe UI,-apple-system,sans-serif",
        "transition:opacity .25s ease-in-out",
      ].join(";"),
    });

    const shadow = host.attachShadow({ mode: "closed" });
    shadow.innerHTML = `
      <style>
        :host { all:initial; display:block; }
        .wrap { position:relative; display:inline-block; }
        .btn {
          display:flex; align-items:center; justify-content:center; gap:6px; width:108px; height:32px;
          cursor:pointer; box-sizing:border-box;
          background:rgba(0,0,0,.85); color:#fff; font-size:12px; font-weight:600;
          border:1px solid rgba(255,255,255,.15); border-radius:8px;
          backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
          box-shadow:0 4px 16px rgba(0,0,0,.6), inset 0 0 0 1px rgba(255,255,255,.05);
          transition:transform .2s cubic-bezier(.16,1,.3,1), background-color .2s, border-color .2s;
        }
        .btn:hover { transform:translateY(-1px); border-color:rgba(255,255,255,.35); }
        .btn:active { transform:scale(.96); }
        .btn svg { flex-shrink:0; }
        .tip {
          position:absolute; top:calc(100% + 8px); right:0; transform:translateY(4px);
          background:rgba(5,5,5,.95); color:#f0f0f0; font-size:11px; font-weight:500;
          line-height:1.5; padding:7px 11px; border-radius:7px;
          border:1px solid rgba(255,255,255,.15); max-width:320px; min-width:120px;
          box-shadow:0 8px 24px rgba(0,0,0,.7); backdrop-filter:blur(12px);
          pointer-events:none; opacity:0; transition:opacity .18s, transform .18s;
        }
        .wrap:hover .tip { opacity:1; transform:translateY(0); }
      </style>
      <div class="wrap">
        <div class="btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
          Download
        </div>
        <div class="tip"></div>
      </div>`;

    const tooltip = shadow.querySelector(".tip");
    tooltip.textContent = getMediaTitle(mediaEl);
    shadow.querySelector(".btn").addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openExtractor(mediaEl);
    });
    shadow.querySelector(".wrap").addEventListener("mouseenter", () => {
      tooltip.textContent = getMediaTitle(mediaEl);
    });

    const overlay = {
      host,
      mediaEl,
      _overContainer: false,
      _overHost: false,
      updateVisibility() {
        const visible = !isFullscreen() && (this._overContainer || this._overHost);
        host.style.opacity = visible ? "1" : "0";
        host.style.pointerEvents = visible ? "auto" : "none";
      },
      mount() {
        // Natively positioned inside the player container
      },
      destroy() {
        host.remove();
        overlays.delete(mediaEl);
      },
    };

    container.addEventListener("mouseenter", () => {
      overlay._overContainer = true;
      overlay.updateVisibility();
    });
    container.addEventListener("mouseleave", () => {
      overlay._overContainer = false;
      setTimeout(() => overlay.updateVisibility(), 100);
    });
    host.addEventListener("mouseenter", () => {
      overlay._overHost = true;
      overlay.updateVisibility();
    });
    host.addEventListener("mouseleave", () => {
      overlay._overHost = false;
      setTimeout(() => overlay.updateVisibility(), 100);
    });

    overlays.set(mediaEl, overlay);
    const targetContainer = container === document.body ? (mediaEl.parentElement || document.body) : container;
    if (window.getComputedStyle(targetContainer).position === "static") {
      targetContainer.style.position = "relative";
    }
    targetContainer.appendChild(host);
  }

  /* ── Shared dialog shell + styles ────────────────────────────────────── */

  const DIALOG_CSS = `
    :host {
      position:fixed; inset:0; z-index:2147483647;
      background:rgba(0,0,0,.8); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
      display:flex; align-items:center; justify-content:center;
      font-family:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; box-sizing:border-box;
    }
    .da-dialog {
      width:480px; max-width:92vw; background:rgba(10,10,10,.95);
      border:1px solid rgba(255,255,255,.1); border-radius:16px;
      box-shadow:0 24px 64px rgba(0,0,0,.9), inset 0 0 0 1px rgba(255,255,255,.05);
      color:#f5f5f7; padding:24px; display:flex; flex-direction:column; gap:16px;
      backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); box-sizing:border-box;
      animation:da-pop .3s cubic-bezier(.32,.72,0,1) forwards;
    }
    @keyframes da-pop { from{opacity:0;transform:scale(.96) translateY(8px)} to{opacity:1;transform:none} }
    .da-header { display:flex; justify-content:space-between; align-items:center; }
    .da-title { font-size:16px; font-weight:700; color:#fff; display:flex; align-items:center; gap:8px; }
    .da-close {
      cursor:pointer; color:#8e8e9c; font-size:12px; width:28px; height:28px;
      display:flex; align-items:center; justify-content:center; border-radius:50%;
      background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08);
      transition:transform .2s, background-color .2s, color .2s;
    }
    .da-close:hover { background:rgba(255,69,58,.15); color:#ff453a; border-color:rgba(255,69,58,.3); transform:rotate(90deg); }
    .da-close:active { transform:scale(.92); }
    .da-badge {
      align-self:flex-start; padding:4px 12px; border-radius:20px; font-size:9.5px;
      font-weight:700; letter-spacing:.8px; text-transform:uppercase; border:1px solid transparent;
    }
    .da-source, .da-url {
      font-size:10.5px; color:#a3a3a3; word-break:break-all; background:rgba(255,255,255,.03);
      padding:8px 12px; border-radius:8px; border:1px solid rgba(255,255,255,.06);
      font-family:monospace; max-height:60px; overflow-y:auto; box-sizing:border-box;
    }
    .da-status { font-size:12.5px; color:#a0a0b0; line-height:1.4; }
    .da-list { max-height:220px; overflow-y:auto; display:flex; flex-direction:column; gap:6px; }
    .da-list::-webkit-scrollbar { width:4px; }
    .da-list::-webkit-scrollbar-thumb { background:rgba(255,255,255,.1); border-radius:8px; }
    .da-row {
      padding:12px 16px; background:rgba(255,255,255,.02); border:1px solid rgba(255,255,255,.06);
      border-radius:8px; cursor:pointer; font-size:13px; display:flex; align-items:center;
      justify-content:space-between; color:#e2e2ec; font-variant-numeric:tabular-nums;
      transition:transform .2s, background-color .2s, border-color .2s, color .2s;
    }
    .da-row:hover { background:rgba(255,255,255,.05); border-color:rgba(255,255,255,.2); color:#fff; }
    .da-row:active { transform:scale(.98); }
    .da-row.selected { border-color:#fff !important; background:rgba(255,255,255,.08) !important; }
    .da-row small { color:#8e8e9c; font-size:11px; margin-left:6px; }
    .da-sep { font-size:9.5px; color:#8e8e9c; text-transform:uppercase; letter-spacing:1px; padding:12px 0 4px; font-weight:600; }
    .da-field { display:flex; flex-direction:column; gap:6px; }
    .da-field label { font-size:10px; color:#8e8e9c; text-transform:uppercase; letter-spacing:.8px; font-weight:600; }
    .da-input, .da-select {
      background:rgba(255,255,255,.04); color:#fff; border:1px solid rgba(255,255,255,.08);
      padding:11px 16px; border-radius:8px; font-size:13px; outline:none; font-family:inherit;
      width:100%; box-sizing:border-box; transition:border-color .2s, background-color .2s, box-shadow .2s;
    }
    .da-input:focus, .da-select:focus { border-color:rgba(255,255,255,.3); background:rgba(255,255,255,.08); box-shadow:0 0 0 3px rgba(255,255,255,.12); }
    .da-actions { display:flex; justify-content:flex-end; gap:8px; }
    .da-btn {
      font-family:inherit; font-size:12.5px; font-weight:600; padding:11px 18px; border-radius:8px;
      cursor:pointer; border:none; outline:none; display:inline-flex; align-items:center; justify-content:center;
      box-sizing:border-box; transition:transform .2s, background-color .2s, border-color .2s, box-shadow .2s;
    }
    .da-btn:active:not(:disabled) { transform:scale(.96); }
    .da-btn:disabled { opacity:.6; cursor:default; }
    .da-btn-ghost { background:transparent; color:#a0a0b0; border:1px solid rgba(255,255,255,.08); }
    .da-btn-ghost:hover { border-color:rgba(255,255,255,.2); background:rgba(255,255,255,.04); color:#fff; }
    .da-btn-soft { background:rgba(255,255,255,.08); color:#f5f5f7; border:1px solid rgba(255,255,255,.15); }
    .da-btn-soft:hover { background:rgba(255,255,255,.15); color:#fff; }
    .da-btn-solid { background:#fff; color:#000; border:1px solid #fff; }
    .da-btn-solid:hover { background:#e5e5e7; border-color:#e5e5e7; box-shadow:0 6px 18px rgba(255,255,255,.15); }
  `;

  function mountDialog({ titleText }) {
    const backdrop = el("div", {
      class: "da-backdrop",
      style: "position:fixed; inset:0; z-index:2147483647; display:flex; align-items:center; justify-content:center; background:rgba(0,0,0,.8); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; box-sizing:border-box;"
    });
    backdrop.attachShadow({ mode: "open" }).innerHTML = `
      <style>${DIALOG_CSS}</style>
      <div class="da-dialog">
        <div class="da-header">
          <div class="da-title">${titleText}</div>
          <span class="da-close">✕</span>
        </div>
        <div class="da-body" style="display:flex;flex-direction:column;gap:16px;"></div>
        <div class="da-actions"></div>
      </div>`;
    const root = backdrop.shadowRoot;
    const body = root.querySelector(".da-body");
    const actions = root.querySelector(".da-actions");
    const close = () => backdrop.remove();
    root.querySelector(".da-close").addEventListener("click", close);
    TOP_NODE().appendChild(backdrop);
    return { backdrop, root, body, actions, close };
  }

  const addButton = (container, { label, variant = "solid", onClick }) => {
    const btn = el("button", { class: `da-btn da-btn-${variant}`, html: escapeHtml(label) });
    btn.addEventListener("click", onClick);
    container.appendChild(btn);
    return btn;
  };

  /** Build the "Save to" field (into bodyEl) + Cancel/Confirm (into actionsEl). */
  const buildCategoryFields = async (bodyEl, actionsEl, onConfirm) => {
    const settings = await fetchSettings();
    const categories = settings.categories || {};

    const select = el("select", { class: "da-select" });
    for (const name of Object.keys(categories)) {
      select.append(el("option", { value: name, html: escapeHtml(name) }));
    }
    select.append(el("option", { value: "__custom", html: "Custom path…" }));

    const custom = el("input", {
      class: "da-input",
      type: "text",
      placeholder: "/absolute/path/optional",
      style: "display:none;",
    });
    select.addEventListener("change", () => {
      custom.style.display = select.value === "__custom" ? "block" : "none";
    });

    bodyEl.append(el("div", { class: "da-field" }, el("label", { html: "Save to" }), select, custom));

    const resolvePath = () =>
      select.value === "__custom"
        ? { category: null, customPath: custom.value.trim() }
        : { category: select.value, customPath: null };

    addButton(actionsEl, {
      label: "Cancel",
      variant: "ghost",
      onClick: () => onConfirm({ cancelled: true }),
    });
    const confirm = addButton(actionsEl, {
      label: "Confirm Download",
      variant: "solid",
      onClick: () => onConfirm({ ...resolvePath(), confirm }),
    });
    return { confirm };
  };

  /* ── Tiered probe ────────────────────────────────────────────────────── */

  const STREAM_PRIORITY = /\.(m3u8|mpd)(?:\?|#|$)/i;

  async function runProbe({ mediaEl, overrideUrl, overrideTitle, setStatus }) {
    return withKeepalive(async () => {
      const sniffed = (
        (await sendToBackground("GET_TAB_STREAMS"))?.data?.urls || []
      ).filter((url) => url && !url.startsWith("blob:"));

      const pageUrl = overrideUrl || window.location.href;
      const elementSrc = overrideUrl || (mediaEl?.currentSrc || mediaEl?.src || "").trim();
      const mediaTitle = overrideTitle || getMediaTitle(mediaEl);

      // Tier 1 — yt-dlp on the page URL
      setStatus("🔍 Probing page with yt-dlp…");
      const pageProbe = await sendToBackground("EXTRACT", { url: pageUrl, page_title: mediaTitle });
      if (pageProbe?.ok) {
        const data = pageProbe.data;
        const hasReal = data.formats?.some(
          (f) =>
            (f.vcodec && f.vcodec !== "none" && f.vcodec !== "direct") ||
            (f.acodec && f.acodec !== "none" && f.acodec !== "direct"),
        );
        if (hasReal && data.extraction_method !== "direct") {
          return { data, url: pageUrl, tier: data.extraction_method === "stream" ? "stream" : "native" };
        }
      }

      // Tier 2 — each sniffed stream + the element src
      const candidates = [
        ...sniffed.filter((url) => STREAM_PRIORITY.test(url)),
        ...sniffed.filter((url) => !STREAM_PRIORITY.test(url)),
      ];
      if (
        elementSrc &&
        /^https?:\/\//.test(elementSrc) &&
        !candidates.includes(elementSrc)
      ) {
        candidates.push(elementSrc);
      }

      for (const streamUrl of candidates) {
        setStatus(`🔗 Probing stream: ${truncate(streamUrl, 48)}…`);
        const probe = await sendToBackground("EXTRACT", { url: streamUrl, page_title: mediaTitle });
        if (probe?.ok && probe.data.formats?.length) {
          const method = probe.data.extraction_method;
          return {
            data: probe.data,
            url: streamUrl,
            tier: method === "direct" ? "direct" : method === "stream" ? "stream" : "native",
          };
        }
      }

      return null;
    });
  }

  /* ── Extractor modal ─────────────────────────────────────────────────── */

  const TIER_STYLES = {
    native: { bg: "rgba(255,255,255,.08)", color: "#ffffff", label: "⚡ yt-dlp Native" },
    stream: { bg: "rgba(48,209,88,.15)", color: "#30d158", label: "📡 Stream (HLS/DASH)" },
    direct: { bg: "rgba(255,159,10,.15)", color: "#ff9f0a", label: "⬇ Direct Download" },
  };

  let statusEl = null;
  const setStatus = (message) => {
    if (statusEl) statusEl.textContent = message;
  };

  function openExtractor(mediaEl) {
    const elementSrc = (mediaEl?.currentSrc || mediaEl?.src || "").trim();
    if (!elementSrc && window.location.href === "about:blank") {
      showToast("✗ No media URL detected on this element");
      return;
    }
    if (window !== window.top) {
      sendToBackground("SHOW_EXTRACTOR_MODAL_ON_TOP", {
        url: window.location.href,
        mediaSrc: elementSrc,
        mediaTitle: getMediaTitle(mediaEl),
      });
      return;
    }
    showExtractorDialog(mediaEl);
  }

  function showExtractorDialog(mediaEl, overrideUrl = null, overrideTitle = null) {
    const dialog = mountDialog({
      titleText:
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg> Download Media',
    });
    const { body, actions, close } = dialog;
    statusEl = el("div", { class: "da-status", html: "Analysing page…" });
    body.append(statusEl);

    const cleanup = () => {
      statusEl = null;
      close();
    };
    dialog.root.querySelector(".da-close").addEventListener("click", cleanup);

    (async () => {
      const result = await runProbe({
        mediaEl,
        overrideUrl,
        overrideTitle,
        setStatus,
      });
      if (!statusEl) return; // closed while probing
      if (!result) {
        setStatus("✗ No downloadable content found on this page or its streams.");
        addButton(actions, { label: "Close", variant: "ghost", onClick: cleanup });
        return;
      }
      renderProbeResult(dialog, result, mediaEl, cleanup);
    })();
  }

  function renderProbeResult(dialog, { data, url, tier }, mediaEl, closeDialog) {
    const { root, body, actions } = dialog;
    const style = TIER_STYLES[tier] || TIER_STYLES.direct;
    const mediaTitle = getMediaTitle(mediaEl);

    // Tier badge + source URL
    const badge = el("div", { class: "da-badge", html: escapeHtml(style.label) });
    badge.style.background = style.bg;
    badge.style.color = style.color;
    badge.style.borderColor = `${style.color}35`;
    const source = el("div", { class: "da-source" });
    source.textContent = url;
    body.prepend(badge, source);

    const count = data.formats.length;
    setStatus(
      `${data.title || "—"}${data.duration ? ` · ${Math.round(data.duration)}s` : ""} · ${count} format${count !== 1 ? "s" : ""}`,
    );

    const list = el("div", { class: "da-list" });
    body.append(list);

    const state = { selectedFormat: null, selectedUrl: url };
    let categoryFieldsBuilt = false;

    const revealCategory = async () => {
      if (categoryFieldsBuilt) return;
      categoryFieldsBuilt = true;
      await buildCategoryFields(body, actions, async (choice) => {
        if (choice.cancelled) return closeDialog();
        await submitDownload(dialog, state, choice, tier, mediaTitle, closeDialog);
      });
    };

    const selectRow = (row, format) => {
      [...list.children].forEach((child) => child.classList.remove("selected"));
      row.classList.add("selected");
      state.selectedFormat = format;
      revealCategory();
    };

    buildFormatRows(list, data, tier, style, selectRow, state);
  }

  function buildFormatRows(list, data, tier, style, selectRow, state) {
    if (tier === "direct") {
      const row = el("div", {
        class: "da-row",
        html: `<strong>Direct HTTP Download</strong><small>${escapeHtml((data.formats[0]?.ext || "FILE").toUpperCase())}</small>`,
      });
      row.addEventListener("click", () => selectRow(row, data.formats[0]));
      list.append(row);
      return;
    }

    const videoFormats = pickBestCodecs(
      data.formats.filter((f) => f.vcodec && f.vcodec !== "none" && f.vcodec !== "direct"),
    );
    const byResolution = new Map();
    for (const f of videoFormats) {
      const key = f.resolution || `${f.tbr || 0}k`;
      if (!byResolution.has(key) || (f.tbr || 0) > (byResolution.get(key).tbr || 0)) {
        byResolution.set(key, f);
      }
    }
    const sorted = [...byResolution.values()].sort(
      (a, b) => (b.height || b.tbr || 0) - (a.height || a.tbr || 0),
    );

    const audioFormats = data.formats
      .filter((f) => (f.vcodec === "none" || !f.vcodec) && f.acodec && f.acodec !== "none")
      .sort((a, b) => (b.abr || 0) - (a.abr || 0));
    const bestAudio = audioFormats[0] || null;
    const bestVideo = sorted[0] || null;

    const bestIsComposite = bestVideo?.format_id?.includes("+ba");
    const bestSize = bestVideo?.filesize || null;

    const best = el("div", {
      class: "da-row",
      html: `<strong>★ Best quality</strong><small>AV1 → VP9 → H.264${bestSize ? ` · ${bestIsComposite ? "~" : ""}${formatBytes(bestSize)}` : ""}</small>`,
    });
    best.style.color = style.color;
    best.addEventListener("click", () => selectRow(best, null)); // null → let yt-dlp pick
    list.append(best);

    for (const f of sorted) {
      const isComposite = f.format_id && f.format_id.includes("+ba");
      const total = f.filesize;
      const meta = [
        f.ext,
        f.fps ? `${f.fps}fps` : null,
        f.vcodec !== "none" ? f.vcodec : null,
        isComposite ? `+ Audio (${f.acodec})` : null,
        total ? (isComposite ? `~${formatBytes(total)}` : formatBytes(total)) : null,
      ]
        .filter(Boolean)
        .join(" · ");
      const row = el("div", {
        class: "da-row",
        html: `<strong>${escapeHtml(f.resolution || "unknown")}</strong><small>${escapeHtml(meta)}</small>`,
      });
      row.addEventListener("click", () => selectRow(row, f));
      list.append(row);
    }

    if (audioFormats.length) {
      list.append(el("div", { class: "da-sep", html: "Audio only" }));
      const byCodec = new Map();
      for (const f of audioFormats) {
        const key = `${f.acodec}@${Math.round(f.abr || 0)}`;
        byCodec.set(key, f);
      }
      for (const f of byCodec.values()) {
        const row = el("div", {
          class: "da-row",
          html: `<strong>${escapeHtml(f.acodec)}</strong><small>${escapeHtml(`${f.abr ? `${f.abr}kbps` : ""} · ${f.ext}`)}</small>`,
        });
        row.addEventListener("click", () => selectRow(row, f));
        list.append(row);
      }
    }

    // Stash best estimates for payload sizing.
    state.bestAudio = bestAudio;
    state.bestVideo = bestVideo;
  }

  /** AV1 → VP9 → H.264 codec waterfall, else keep everything. */
  function pickBestCodecs(formats) {
    const match = (codec, needles) => {
      const value = String(codec || "").toLowerCase();
      return needles.some((n) => value.includes(n));
    };
    if (formats.some((f) => match(f.vcodec, ["av01", "av1"]))) {
      return formats.filter((f) => match(f.vcodec, ["av01", "av1"]));
    }
    if (formats.some((f) => match(f.vcodec, ["vp09", "vp9"]))) {
      return formats.filter((f) => match(f.vcodec, ["vp09", "vp9"]));
    }
    if (formats.some((f) => match(f.vcodec, ["avc", "h264"]))) {
      return formats.filter((f) => match(f.vcodec, ["avc", "h264"]));
    }
    return formats;
  }

  async function submitDownload(dialog, state, choice, tier, mediaTitle, closeDialog) {
    const { selectedFormat, selectedUrl, bestAudio, bestVideo } = state;
    const isVideo = selectedFormat
      ? !!(selectedFormat.vcodec && selectedFormat.vcodec !== "none")
      : true;

    let estimatedTotalBytes = null;
    if (selectedFormat) {
      estimatedTotalBytes = selectedFormat.filesize || null;
    } else if (bestVideo?.filesize) {
      estimatedTotalBytes = bestVideo.filesize;
    }

    const payload = {
      url: selectedUrl,
      format_id: selectedFormat?.format_id || null,
      category: choice.category,
      custom_path: choice.customPath,
      is_video: isVideo,
      page_title: mediaTitle,
      is_stream: tier === "stream",
      estimated_total_bytes: estimatedTotalBytes,
    };

    choice.confirm.disabled = true;
    choice.confirm.textContent = "Queuing…";
    const result = await sendToBackground("DOWNLOAD", payload);
    if (result?.ok) {
      closeDialog();
      showToast("✓ Download queued");
    } else {
      choice.confirm.disabled = false;
      choice.confirm.textContent = "Confirm Download";
      showToast(`✗ Failed: ${result?.error || "unknown"}`);
    }
  }

  /* ── Standard-file "route download" popup ────────────────────────────── */

  function openRoutePopup(url, filename) {
    if (document.querySelector(".da-backdrop[data-route]")) return;
    const dialog = mountDialog({
      titleText:
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg> Route Download',
    });
    dialog.backdrop.dataset.route = "";
    const { body, actions, close } = dialog;

    const urlBox = el("div", { class: "da-url" });
    urlBox.textContent = url;
    const filenameInput = el("input", { class: "da-input", type: "text", value: filename || "" });
    const categoryField = el("div", { class: "da-field" }, el("label", { html: "Category" }));
    body.append(
      el("div", { class: "da-field" }, el("label", { html: "Source URL" }), urlBox),
      el("div", { class: "da-field" }, el("label", { html: "Save Filename" }), filenameInput),
      categoryField,
    );
    (async () => {
      const settings = await fetchSettings();
      const select = el("select", { class: "da-select" });
      for (const name of Object.keys(settings.categories || {})) {
        select.append(el("option", { value: name, html: escapeHtml(name) }));
      }
      select.append(el("option", { value: "__custom", html: "Custom path…" }));
      const custom = el("input", {
        class: "da-input",
        type: "text",
        placeholder: "/absolute/path/optional",
        style: "display:none;",
      });
      select.addEventListener("change", () => {
        custom.style.display = select.value === "__custom" ? "block" : "none";
      });
      categoryField.append(select, custom);

      addButton(actions, { label: "Cancel", variant: "ghost", onClick: close });
      const chromeBtn = addButton(actions, {
        label: "Download via Chrome",
        variant: "soft",
        onClick: async () => {
          chromeBtn.disabled = true;
          const name = filenameInput.value.trim() || filename;
          const result = await sendToBackground("BYPASS_DOWNLOAD", { url, filename: name });
          if (result?.ok) close();
          else {
            chromeBtn.disabled = false;
            showToast("✗ Failed to route to Chrome");
          }
        },
      });
      addButton(actions, {
        label: "Download with App",
        variant: "solid",
        onClick: async (event) => {
          const btn = event.currentTarget;
          btn.disabled = true;
          btn.textContent = "Queuing…";
          const payload = {
            url,
            format_id: "direct_stream",
            category: select.value === "__custom" ? null : select.value,
            custom_path: select.value === "__custom" ? custom.value.trim() : null,
            is_video: false,
            page_title: getMediaTitle(null),
            filename: filenameInput.value.trim() || filename || "download",
            is_stream: false,
          };
          const result = await sendToBackground("DOWNLOAD", payload);
          if (result?.ok) {
            close();
            showToast("✓ Routed to DownloadAnything");
          } else {
            btn.disabled = false;
            btn.textContent = "Download with App";
            showToast(`✗ Failed: ${result?.error || "unknown"}`);
          }
        },
      });
    })();
  }

  /* ── Toast (DOM-built, injection-safe) ───────────────────────────────── */

  function showToast(message) {
    const toast = el(
      "div",
      {
        style: [
          "position:fixed", "bottom:32px", "right:32px", "z-index:2147483647",
          "background:#0d0d0d", "color:#fff", "border:1px solid rgba(255,255,255,.15)",
          "padding:14px 24px", "border-radius:12px", "font-size:13.5px",
          "font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif",
          "box-shadow:0 10px 40px rgba(0,0,0,.8)", "backdrop-filter:blur(16px)",
          "display:flex", "align-items:center", "font-weight:500", "transition:all .3s ease",
        ].join(";"),
      },
      el("span", { style: "margin-right:8px;" }, document.createTextNode(message)),
    );
    TOP_NODE().appendChild(toast);
    setTimeout(() => toast.remove(), 2800);
  }

  /* ── Scan + observe ──────────────────────────────────────────────────── */

  const scan = () => findAllMedia(document).forEach(createOverlay);

  const init = () => {
    scan();
    bindSharedListeners();
    setTimeout(scan, 2000); // catch lazy-loaded iframes/players
  };

  if (document.readyState === "complete") {
    init();
  } else {
    window.addEventListener("load", init);
  }

  // Alt-click a link to let Chrome handle that download (bypass the engine).
  document.addEventListener(
    "click",
    (event) => {
      if (!event.altKey) return;
      const anchor = event.target.closest?.("a");
      if (anchor?.href) sendToBackground("ADD_ALT_BYPASS", { url: anchor.href });
    },
    { capture: true, passive: true },
  );

  /* ── Inbound messages ────────────────────────────────────────────────── */

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    switch (message?.type) {
      case "SHOW_ADD_DOWNLOAD_POPUP":
        openRoutePopup(message.url, message.filename);
        sendResponse({ ok: true });
        break;
      case "OPEN_EXTRACTOR_MODAL_IN_TOP": {
        const mock = {
          currentSrc: message.mediaSrc || "",
          src: message.mediaSrc || "",
          getAttribute: (attr) => (attr === "title" ? message.mediaTitle : null),
          parentElement: null,
          isConnected: true,
        };
        showExtractorDialog(mock, message.url, message.mediaTitle);
        sendResponse({ ok: true });
        break;
      }
      case "TRIGGER_EXTRACT": {
        const first = findAllMedia(document)[0];
        openExtractor(first || null);
        sendResponse({ ok: true });
        break;
      }
      default:
        return false;
    }
    return true;
  });
})();
