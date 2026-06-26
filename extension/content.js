/**
 * content.js — Injects floating download button on media players.
 *
 * Download strategy (two-tier with auto-fallback):
 *
 *   Tier 1 — yt-dlp Native (preferred)
 *     Probes window.location.href (the page URL) first.
 *     If yt-dlp has a site extractor for this page, it returns real formats
 *     (YouTube, Vimeo, Twitch, Dailymotion, …). The user picks a resolution
 *     and yt-dlp handles the full mux + merge pipeline.
 *
 *   Tier 2 — Stream Extraction (HLS / DASH / MP4)
 *     If yt-dlp doesn't know the site, the background service worker has
 *     already sniffed all HLS (.m3u8), DASH (.mpd), and MP4 URLs from the
 *     tab's network traffic. We probe each in order until one succeeds.
 *     yt-dlp can still download raw HLS/DASH manifests natively.
 *
 *   Tier 3 — Direct HTTP Download (last resort)
 *     If every probe fails (private CDN, auth-gated segments, etc.) we
 *     offer the raw stream URL as a passthrough download.
 *
 * All backend calls go through the background service worker to avoid CORS
 * errors on restrictive origins.
 */
(() => {
  "use strict";
  if (window.__STREAM_SNATCHER_INJECTED__) return;
  window.__STREAM_SNATCHER_INJECTED__ = true;

  const injectedSet = new WeakSet();
  let cachedSettings = null;

  // ── Settings ─────────────────────────────────────────────────────────────
  async function fetchSettings() {
    if (cachedSettings) return cachedSettings;
    try {
      const res = await sendToBackground({ type: "GET_SETTINGS" });
      cachedSettings = res?.ok ? res.data : { categories: {} };
    } catch {
      cachedSettings = { categories: {} };
    }
    return cachedSettings;
  }

  // ── Background bridge (avoids direct-to-localhost CORS) ──────────────────
  function sendToBackground(msg) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(msg, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          resolve(response);
        }
      });
    });
  }

  // ── Shadow-DOM-aware media element scanner ───────────────────────────────
  function findAllMedia(root = document) {
    const found = [];
    (root.querySelectorAll?.("video, audio") || []).forEach((el) =>
      found.push(el)
    );
    (root.querySelectorAll?.("*") || []).forEach((node) => {
      if (node.shadowRoot) found.push(...findAllMedia(node.shadowRoot));
    });
    return found;
  }

  // ── Helper to find the actual player container ───────────────────────────
  function findPlayerContainer(mediaEl) {
    const parent = mediaEl.parentElement || document.body;
    if (parent.shadowRoot?.contains(mediaEl)) {
      return parent.shadowRoot;
    }

    let current = mediaEl.parentElement;
    let player = mediaEl.parentElement || document.body;

    while (current && current !== document.body) {
      const className = current.className ? String(current.className).toLowerCase() : "";
      const idName = current.id ? String(current.id).toLowerCase() : "";

      if (
        className.includes("player") ||
        className.includes("video-container") ||
        className.includes("video-wrap") ||
        className.includes("video-player") ||
        idName.includes("player") ||
        idName.includes("video-container") ||
        idName.includes("video-wrap")
      ) {
        player = current;
      }
      current = current.parentElement;
    }
    return player;
  }

  // ── Overlay button injected onto each media element ──────────────────────
  function getMediaTitle(mediaEl) {
    const host = window.location.hostname.toLowerCase();
    
    // 1. YouTube specific (handles dynamic watch transitions)
    if (host.includes("youtube.com") || host.includes("youtu.be")) {
      const ytTitleEl = document.querySelector(
        "ytd-watch-metadata h1, ytd-video-primary-info-renderer h1, #container h1.ytd-video-primary-info-renderer"
      );
      if (ytTitleEl?.textContent?.trim()) {
        return ytTitleEl.textContent.trim();
      }
    }
    
    // 2. Twitch specific
    if (host.includes("twitch.tv")) {
      const twitchTitleEl = document.querySelector(
        '[data-a-target="stream-title"], [data-a-target="video-title"]'
      );
      if (twitchTitleEl?.textContent?.trim()) {
        return twitchTitleEl.textContent.trim();
      }
    }

    // 3. Element-specific context (very useful for custom platforms & pages with multiple videos)
    if (mediaEl) {
      // Direct attributes
      const direct = mediaEl.getAttribute("title") || mediaEl.getAttribute("aria-label");
      if (direct?.trim()) return direct.trim();

      // Parent container attributes / titles
      const container = findPlayerContainer(mediaEl);
      if (container && container !== document.body) {
        const containerTitle = container.getAttribute?.("title") || container.getAttribute?.("aria-label");
        if (containerTitle?.trim()) return containerTitle.trim();

        // Check for common title classes inside the player
        const titleClasses = [
          ".video-title", ".title", ".media-title", 
          "[class*='title']", "[id*='title']", ".caption", "figcaption"
        ];
        for (const cls of titleClasses) {
          try {
            const el = container.querySelector(cls);
            if (el?.textContent?.trim()) return el.textContent.trim();
          } catch (e) {
            // ignore selector parsing errors
          }
        }
      }

      // Check preceding siblings / headings in the document tree
      try {
        let current = mediaEl;
        let depth = 0;
        while (current && current !== document.body && depth < 5) {
          let prev = current.previousElementSibling;
          while (prev) {
            const heading = prev.querySelector("h1, h2, h3, h4") || 
                            (prev.matches("h1, h2, h3, h4") ? prev : null);
            if (heading?.textContent?.trim()) {
              return heading.textContent.trim();
            }
            prev = prev.previousElementSibling;
          }
          current = current.parentElement;
          depth++;
        }
      } catch (err) {
        // ignore navigation errors
      }
    }

    // 4. Cleaned document.title (dynamic on most modern SPAs)
    const raw = document.title || "";
    let cleanTitle = raw;
    
    const suffixes = [
      " - YouTube", " | YouTube",
      " - Twitch", " | Twitch",
      " - Vimeo", " | Vimeo",
      " - Netflix", " | Netflix",
      " - Disney+", " | Disney+",
      " - TikTok", " | TikTok",
      " - Twitter", " | Twitter", " - X", " | X",
      " - Facebook", " | Facebook",
      " - Instagram", " | Instagram",
      " - Reddit", " | Reddit",
      " - Dailymotion", " | Dailymotion",
      " - Rumble", " | Rumble",
      " - Bilibili", " | Bilibili",
    ];
    
    for (const suffix of suffixes) {
      if (cleanTitle.toLowerCase().endsWith(suffix.toLowerCase())) {
        cleanTitle = cleanTitle.slice(0, -suffix.length).trim();
        break;
      }
    }
    
    cleanTitle = cleanTitle
      .replace(/\s*[-|·•–—]\s*(YouTube|Vimeo|Twitch|Dailymotion|Twitter|X|Facebook|Instagram|TikTok|Reddit|Bilibili|Rumble|Odysee|PeerTube|Niconico|SoundCloud|Spotify|Netflix|Prime Video|Disney\+|Apple TV)\s*$/i, "")
      .trim();
      
    if (cleanTitle) return cleanTitle;

    // 5. Open Graph / Metadata fallbacks
    const og = document.querySelector("meta[property='og:title']");
    if (og?.content?.trim()) return og.content.trim();

    const tw = document.querySelector("meta[name='twitter:title']");
    if (tw?.content?.trim()) return tw.content.trim();

    const h1 = document.querySelector("h1");
    if (h1?.textContent?.trim()) return h1.textContent.trim();

    return raw || "Unknown media";
  }

  function createOverlay(mediaEl) {
    if (injectedSet.has(mediaEl)) return;
    injectedSet.add(mediaEl);

    const container = findPlayerContainer(mediaEl);
    const mediaTitle = getMediaTitle(mediaEl);

    // Ensure the container is positioned so our absolute host anchors to it
    const containerStyle = window.getComputedStyle(container);
    if (containerStyle.position === "static") {
      container.style.position = "relative";
    }

    const host = document.createElement("div");
    host.id = "ss-overlay-host-" + Math.random().toString(36).slice(2, 8);
    host.style.cssText = [
      "position:absolute", "top:10px", "left:10px",
      "width:max-content", "height:max-content", "display:block",
      "z-index:2147483647", "pointer-events:auto",
      "font-family:Inter,Segoe UI,-apple-system,sans-serif",
      "opacity:0", "transition:opacity 0.25s ease-in-out"
    ].join(";");

    const shadow = host.attachShadow({ mode: "closed" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; display: block; width: max-content; height: max-content; }
        .wrapper { position: relative; display: inline-block; }
        .btn {
          display:flex; align-items:center; justify-content:center; gap:6px;
          width: 100px; height: 32px; cursor:pointer;
          box-sizing: border-box;
          background:rgba(0, 0, 0, 0.85);
          color:#ffffff; font-size:12px; font-weight:600;
          border:1px solid rgba(255, 255, 255, 0.15);
          border-radius:8px; backdrop-filter:blur(12px);
          -webkit-backdrop-filter:blur(12px);
          box-shadow:0 4px 16px rgba(0,0,0,0.6), 0 0 0 1px rgba(255, 255, 255, 0.05) inset;
          transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .btn:hover { 
          transform:translateY(-1px); 
          box-shadow:0 6px 20px rgba(0,0,0,0.8), 0 0 0 1px rgba(255, 255, 255, 0.05) inset; 
          border-color:rgba(255, 255, 255, 0.35);
        }
        .btn:active {
          transform: scale(0.96) !important;
        }
        .btn svg { transition: transform 0.25s ease; flex-shrink:0; }
        .btn:hover svg { transform: translateY(0.5px); }
 
        .tooltip {
          position: absolute;
          top: calc(100% + 8px);
          right: 0;
          transform: translateY(4px);
          background: rgba(5, 5, 5, 0.95);
          color: #f0f0f0;
          font-size: 11px;
          font-weight: 500;
          line-height: 1.5;
          padding: 7px 11px;
          border-radius: 7px;
          border: 1px solid rgba(255, 255, 255, 0.15);
          box-shadow: 0 8px 24px rgba(0,0,0,0.7), 0 0 0 1px rgba(255, 255, 255, 0.05) inset;
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
          white-space: normal;
          word-break: break-word;
          max-width: 320px;
          min-width: 120px;
          pointer-events: none;
          opacity: 0;
          transition: opacity 0.18s ease, transform 0.18s cubic-bezier(0.16, 1, 0.3, 1);
          z-index: 1;
        }
        .tooltip::before {
          content: "";
          position: absolute;
          bottom: 100%;
          right: 14px;
          border: 5px solid transparent;
          border-bottom-color: rgba(255, 255, 255, 0.15);
        }
        .tooltip::after {
          content: "";
          position: absolute;
          bottom: calc(100% - 1px);
          right: 14px;
          border: 5px solid transparent;
          border-bottom-color: rgba(5, 5, 5, 0.95);
        }
        .wrapper:hover .tooltip {
          opacity: 1;
          transform: translateY(0);
        }
      </style>
      <div class="wrapper">
        <div class="btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
          </svg>
          Download
        </div>
        <div class="tooltip">${mediaTitle.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")}</div>
      </div>
    `;
    shadow.querySelector(".btn").addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openExtractorModal(mediaEl);
    });
 
    // Dynamically update tooltip to show current media title on hover (handles page navigation like on YouTube)
    const wrapper = shadow.querySelector(".wrapper");
    const tooltip = shadow.querySelector(".tooltip");
    wrapper.addEventListener("mouseenter", () => {
      tooltip.textContent = getMediaTitle(mediaEl);
    });
 
    let isOverContainer = false;
    let isOverHost = false;

    const updateVisibility = () => {
      const isFs = !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement);
      if (isFs) {
        host.style.opacity = "0";
        host.style.pointerEvents = "none";
        return;
      }

      if (isOverContainer || isOverHost) {
        host.style.opacity = "1";
        host.style.pointerEvents = "auto";
      } else {
        host.style.opacity = "0";
        host.style.pointerEvents = "none";
      }
    };

    const mount = () => {
      if (!mediaEl.isConnected || !container.isConnected) return;
      const cr = container.getBoundingClientRect();
      const left = cr.left + window.scrollX;
      const top = cr.top + window.scrollY;
      host.style.left = left + 10 + "px";
      host.style.top = top + 10 + "px";
    };

    document.body.appendChild(host);

    container.addEventListener("mouseenter", () => {
      isOverContainer = true;
      updateVisibility();
    });
    container.addEventListener("mouseleave", () => {
      isOverContainer = false;
      setTimeout(updateVisibility, 100);
    });
    host.addEventListener("mouseenter", () => {
      isOverHost = true;
      updateVisibility();
    });
    host.addEventListener("mouseleave", () => {
      isOverHost = false;
      setTimeout(updateVisibility, 100);
    });

    const checkFullscreen = () => {
      updateVisibility();
    };
    document.addEventListener("fullscreenchange", checkFullscreen);
    document.addEventListener("webkitfullscreenchange", checkFullscreen);
    document.addEventListener("mozfullscreenchange", checkFullscreen);
    document.addEventListener("msfullscreenchange", checkFullscreen);

    mount();
    window.addEventListener("scroll", mount, { passive: true });
    window.addEventListener("resize", mount, { passive: true });

    const ro = new ResizeObserver(() => {
      mount();
    });
    ro.observe(mediaEl);

    const cleanup = new MutationObserver(() => {
      if (!mediaEl.isConnected || !host.isConnected) {
        window.removeEventListener("scroll", mount);
        window.removeEventListener("resize", mount);
        document.removeEventListener("fullscreenchange", checkFullscreen);
        document.removeEventListener("webkitfullscreenchange", checkFullscreen);
        document.removeEventListener("mozfullscreenchange", checkFullscreen);
        document.removeEventListener("msfullscreenchange", checkFullscreen);
        ro.disconnect();
        host.remove();
        injectedSet.delete(mediaEl);
        cleanup.disconnect();
      }
    });
    cleanup.observe(document.documentElement, { childList: true, subtree: true });
  }

  // ── Two-tier probe orchestration ─────────────────────────────────────────
  //
  // Returns: { data, url, tier }
  //   tier: "native" | "stream" | "direct" | null (complete failure)
  //
  async function runTieredProbe(mediaEl, overrideUrl = null, overrideTitle = null) {
    // Collect the sniffed stream URLs from the background SW
    const bgRes = await sendToBackground({ type: "GET_TAB_STREAMS" });
    const sniffedUrls = (bgRes?.urls || []).filter(
      (u) => u && !u.startsWith("blob:")
    );
    const elementSrc = overrideUrl || (mediaEl ? (mediaEl.currentSrc || mediaEl.src || "").trim() : "");
    const pageUrl = overrideUrl || window.location.href;
    const mediaTitle = overrideTitle || getMediaTitle(mediaEl);

    // ── Tier 1: Try yt-dlp on the PAGE URL ────────────────────────────────
    setStatus("🔍 Probing with yt-dlp…");
    const t1 = await sendToBackground({ type: "EXTRACT", url: pageUrl, page_title: mediaTitle });
    if (t1?.ok) {
      const method = t1.data.extraction_method;
      // Accept any real formats from the page URL — the page URL is always a
      // safe HTTP URL so we never risk passing a blob: to the backend here.
      // We accept both "yt-dlp" (known extractor) and "stream" (yt-dlp handled
      // an HLS/DASH manifest linked from the page) but not "direct" (which
      // just means yt-dlp gave up and echoed the URL back with no real info).
      const hasRealFormats = t1.data.formats?.some(
        (f) => (f.vcodec && f.vcodec !== "none" && f.vcodec !== "direct") ||
               (f.acodec && f.acodec !== "none" && f.acodec !== "direct")
      );
      if (hasRealFormats && method !== "direct") {
        return { data: t1.data, url: pageUrl, tier: method === "stream" ? "stream" : "native" };
      }
    }

    // ── Tier 2: Try yt-dlp on each sniffed HLS/DASH/MP4 stream URL ────────
    // Prioritise manifest types (m3u8 / mpd) over raw segments
    const STREAM_PRIORITY = /\.(m3u8|mpd)(\?|#|$)/i;
    const orderedStreams = [
      ...sniffedUrls.filter((u) => STREAM_PRIORITY.test(u)),
      ...sniffedUrls.filter((u) => !STREAM_PRIORITY.test(u)),
    ];
    // Also add the element's own src as a candidate — but ONLY if it is a
    // real HTTP/HTTPS URL. YouTube and other MSE-based players set the video
    // element src to a blob: URL which exists only in the browser's memory;
    // passing it to yt-dlp (or any server-side tool) always fails.
    if (
      elementSrc &&
      (elementSrc.startsWith("http://") || elementSrc.startsWith("https://")) &&
      !orderedStreams.includes(elementSrc)
    ) {
      orderedStreams.push(elementSrc);
    }

    for (const streamUrl of orderedStreams) {
      setStatus(`🔗 Probing stream: ${truncate(streamUrl, 50)}…`);
      const t2 = await sendToBackground({ type: "EXTRACT", url: streamUrl, page_title: mediaTitle });
      if (t2?.ok) {
        const hasFormats = t2.data.formats?.length > 0;
        if (hasFormats) {
          const method = t2.data.extraction_method;
          const tier = method === "direct" ? "direct" : (method === "stream" ? "stream" : "native");
          return { data: t2.data, url: streamUrl, tier };
        }
      }
    }

    // ── Complete failure ───────────────────────────────────────────────────
    return null;
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Modal orchestration
  // ─────────────────────────────────────────────────────────────────────────
  let _statusEl = null; // set when modal is open

  function setStatus(msg) {
    if (_statusEl) _statusEl.textContent = msg;
  }

  async function openExtractorModal(mediaEl) {
    const elementSrc = (mediaEl.currentSrc || mediaEl.src || "").trim();

    // Only gate on about:blank (genuine edge case). For all real pages, the
    // tiered probe inside showModal does the full check. Avoid pre-fetching
    // tab streams here since runTieredProbe fetches them immediately after.
    if (!elementSrc && window.location.href === "about:blank") {
      showToast("✗ No media URL detected on this element");
      return;
    }

    if (window !== window.top) {
      sendToBackground({
        type: "SHOW_EXTRACTOR_MODAL_ON_TOP",
        url: window.location.href,
        mediaSrc: elementSrc,
        mediaTitle: getMediaTitle(mediaEl)
      });
      return;
    }

    showModal(mediaEl);
  }

  function showModal(mediaEl, overrideUrl = null, overrideTitle = null) {
    const backdrop = document.createElement("div");
    backdrop.id = "ss-modal-backdrop";
    backdrop.innerHTML = `
      <style>
        #ss-modal-backdrop {
          position: fixed;
          inset: 0;
          z-index: 2147483647;
          background: rgba(0, 0, 0, 0.8);
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
          display: flex;
          align-items: center;
          justify-content: center;
          font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          box-sizing: border-box;
        }
        
        #ss-modal {
          width: 480px;
          max-width: 92vw;
          background: rgba(10, 10, 10, 0.95);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 16px;
          box-shadow: 0 24px 64px rgba(0, 0, 0, 0.9), 0 0 0 1px rgba(255, 255, 255, 0.05) inset;
          color: #f5f5f7;
          padding: 24px;
          display: flex;
          flex-direction: column;
          gap: 16px;
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
          box-sizing: border-box;
          animation: modalScaleUp 0.3s cubic-bezier(0.32, 0.72, 0, 1) forwards;
        }
        
        @keyframes modalScaleUp {
          from { opacity: 0; transform: scale(0.96) translateY(8px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
        
        #ss-modal-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          box-sizing: border-box;
        }
        
        #ss-modal-title {
          font-size: 16px;
          font-weight: 700;
          color: #ffffff;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        
        #ss-close {
          cursor: pointer;
          color: #8e8e9c;
          font-size: 12px;
          display: flex;
          align-items: center;
          justify-content: center;
          width: 28px;
          height: 28px;
          border-radius: 50%;
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.08);
          transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), color 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        
        #ss-close:hover {
          background: rgba(255, 69, 58, 0.15);
          color: #ff453a;
          border-color: rgba(255, 69, 58, 0.3);
          transform: rotate(90deg);
        }
        
        #ss-close:active {
          transform: scale(0.92) !important;
        }
        
        #ss-tier-badge {
          align-self: flex-start;
          padding: 4px 12px;
          border-radius: 20px;
          font-size: 9.5px;
          font-weight: 700;
          letter-spacing: 0.8px;
          text-transform: uppercase;
          border: 1px solid transparent;
        }
        
        #ss-source-url {
          font-size: 10px;
          color: #a3a3a3;
          word-break: break-all;
          background: rgba(255, 255, 255, 0.03);
          padding: 8px 12px;
          border-radius: 8px;
          border: 1px solid rgba(255, 255, 255, 0.06);
          font-family: monospace;
          margin-bottom: 4px;
          box-sizing: border-box;
        }
        
        #ss-status {
          font-size: 12.5px;
          color: #a0a0b0;
          line-height: 1.4;
        }
        
        #ss-formats {
          max-height: 220px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
          gap: 6px;
          padding-right: 4px;
          box-sizing: border-box;
        }
        
        #ss-formats::-webkit-scrollbar {
          width: 4px;
        }
        #ss-formats::-webkit-scrollbar-track {
          background: transparent;
        }
        #ss-formats::-webkit-scrollbar-thumb {
          background: rgba(255, 255, 255, 0.1);
          border-radius: 8px;
        }
        #ss-formats::-webkit-scrollbar-thumb:hover {
          background: rgba(255, 255, 255, 0.2);
        }
        
        .ss-row {
          padding: 12px 16px;
          background: rgba(255, 255, 255, 0.02);
          border: 1px solid rgba(255, 255, 255, 0.06);
          border-radius: 8px;
          cursor: pointer;
          font-size: 13px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), color 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
          color: #e2e2ec;
          font-variant-numeric: tabular-nums;
        }
        
        .ss-row:hover {
          background: rgba(255, 255, 255, 0.05);
          border-color: rgba(255, 255, 255, 0.2);
          color: #ffffff;
        }
        
        .ss-row:active {
          transform: scale(0.98);
        }
        
        .ss-row.selected {
          border-color: #ffffff !important;
          background: rgba(255, 255, 255, 0.08) !important;
        }
        
        #ss-category-wrap {
          display: flex;
          flex-direction: column;
          gap: 6px;
          box-sizing: border-box;
        }
        
        #ss-category-wrap label {
          font-size: 10px;
          color: #8e8e9c;
          text-transform: uppercase;
          letter-spacing: 0.8px;
          font-weight: 600;
        }
        
        #ss-cat, #ss-custom {
          background: rgba(255, 255, 255, 0.04);
          color: #ffffff;
          border: 1px solid rgba(255, 255, 255, 0.08);
          padding: 12px 16px;
          border-radius: 8px;
          font-size: 13px;
          outline: none;
          font-family: inherit;
          transition: border-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
          width: 100%;
        }
        
        #ss-cat:focus, #ss-custom:focus {
          border-color: rgba(255, 255, 255, 0.3);
          background: rgba(255, 255, 255, 0.08);
          box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.12);
        }
        
        #ss-actions {
          display: flex;
          justify-content: flex-end;
          gap: 8px;
          margin-top: 4px;
          box-sizing: border-box;
        }
        
        .ss-btn {
          font-family: inherit;
          font-size: 12.5px;
          font-weight: 600;
          padding: 12px 18px;
          border-radius: 8px;
          cursor: pointer;
          border: none;
          outline: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
        }
        
        .ss-btn:active:not(:disabled) {
          transform: scale(0.96) !important;
        }
        
        .ss-btn-cancel {
          background: transparent;
          color: #a0a0b0;
          border: 1px solid rgba(255, 255, 255, 0.08);
        }
        
        .ss-btn-cancel:hover {
          border-color: rgba(255, 255, 255, 0.2);
          background: rgba(255, 255, 255, 0.04);
          color: #ffffff;
        }
        
        .ss-btn-confirm {
          background: #ffffff;
          color: #000000;
          border: 1px solid #ffffff;
          box-shadow: 0 4px 12px rgba(255, 255, 255, 0.05);
        }
        
        .ss-btn-confirm:hover {
          background: #e5e5e7;
          border-color: #e5e5e7;
          box-shadow: 0 6px 18px rgba(255, 255, 255, 0.15);
        }
      </style>

      <div id="ss-modal">
        <!-- Header -->
        <div id="ss-modal-header">
          <div id="ss-modal-title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
            </svg>
            Acquire Media
          </div>
          <span id="ss-close">✕</span>
        </div>

        <!-- Tier badge + status -->
        <div style="display:flex;flex-direction:column;gap:8px;box-sizing:border-box;">
          <div id="ss-tier-badge" style="display:none;"></div>
          <div id="ss-source-url" style="display:none;"></div>
          <div id="ss-status">Analysing page…</div>
        </div>

        <!-- Format list -->
        <div id="ss-formats" style="display:none;"></div>

        <!-- Category + path -->
        <div id="ss-category-wrap" style="display:none;"></div>

        <!-- Actions -->
        <div id="ss-actions" style="display:none;">
          <button id="ss-cancel" class="ss-btn ss-btn-cancel">Cancel</button>
          <button id="ss-confirm" class="ss-btn ss-btn-confirm">Confirm Download</button>
        </div>
      </div>
    `;

    const parent = document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement || document.body || document.documentElement;
    parent.appendChild(backdrop);

    _statusEl = backdrop.querySelector("#ss-status");

    function closeModal() {
      _statusEl = null;
      backdrop.remove();
    }
    backdrop.querySelector("#ss-close").addEventListener("click", closeModal);
    backdrop.querySelector("#ss-cancel").addEventListener("click", closeModal);

    // Start the tiered probe
    (async () => {
      const result = await runTieredProbe(mediaEl, overrideUrl, overrideTitle);
      // Modal may have been closed by the user while the probe ran
      if (!_statusEl) return;
      if (!result) {
        setStatus("✗ No downloadable content found on this page or its streams.");
        return;
      }
      renderProbeResult(backdrop, result, closeModal);
    })();
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Render probe result into the open modal
  // ─────────────────────────────────────────────────────────────────────────
  function renderProbeResult(backdrop, { data, url, tier }, closeModal) {
    // Tier badge
    const badge = backdrop.querySelector("#ss-tier-badge");
    const TIER_STYLES = {
      native: { bg: "rgba(255, 255, 255, 0.08)", color: "#ffffff", label: "⚡ yt-dlp Native" },
      stream: { bg: "rgba(48, 209, 88, 0.15)", color: "#30d158", label: "📡 Stream (HLS/DASH)" },
      direct: { bg: "rgba(255, 159, 10, 0.15)", color: "#ff9f0a", label: "⬇ Direct Download" },
    };
    const style = TIER_STYLES[tier] || TIER_STYLES.direct;
    badge.style.background = style.bg;
    badge.style.color = style.color;
    badge.style.borderColor = `${style.color}35`;
    badge.textContent = style.label;
    badge.style.display = "inline-block";

    // Source URL
    const srcEl = backdrop.querySelector("#ss-source-url");
    srcEl.textContent = url;
    srcEl.style.display = "block";

    // Status line
    const formatCount = data.formats.length;
    setStatus(
      `${data.title || "—"}` +
      (data.duration ? ` · ${Math.round(data.duration)}s` : "") +
      ` · ${formatCount} format${formatCount !== 1 ? "s" : ""}`
    );

    // Format list
    const list = backdrop.querySelector("#ss-formats");
    list.style.display = "flex";
    list.style.setProperty('--selected-color', style.color);
    list.innerHTML = "";

    // For native/stream tiers: show real format rows
    // For direct tier: single "Direct download" row
    let selectedFormat = null;
    let selectedUrl = url; // the URL that will be submitted to /api/download
    let bestAudio = null;
    let bestVideo = null;

    const setSelected = (row) => {
      [...list.children].forEach((c) => c.classList.remove("selected"));
      row.classList.add("selected");
      showCategoryRow();
    };

    if (tier === "direct") {
      // Single direct-download row — no format choice needed
      const row = document.createElement("div");
      row.className = "ss-row";
      row.innerHTML = `<strong>Direct HTTP Download</strong>
        <span style="color:#8e8e9c;font-size:11px;margin-left:8px;">${data.formats[0]?.ext?.toUpperCase() || "FILE"}</span>`;
      row.addEventListener("click", () => {
        selectedFormat = data.formats[0];
        setSelected(row);
      });
      list.appendChild(row);
    } else {
      const allVideoFormats = data.formats.filter(
        (f) => f.vcodec && f.vcodec !== "none" && f.vcodec !== "direct"
      );

      // Codec priority waterfall: AV1 (av01) -> VP9 (vp09/vp9) -> H.264 (avc/h264)
      let videoFormats = [];
      const isAV1 = (vc) => {
        const s = String(vc).toLowerCase();
        return s.includes("av01") || s.includes("av1");
      };
      const isVP9 = (vc) => {
        const s = String(vc).toLowerCase();
        return s.includes("vp09") || s.includes("vp9");
      };
      const isH264 = (vc) => {
        const s = String(vc).toLowerCase();
        return s.includes("avc") || s.includes("h264");
      };

      if (allVideoFormats.some((f) => isAV1(f.vcodec))) {
        videoFormats = allVideoFormats.filter((f) => isAV1(f.vcodec));
      } else if (allVideoFormats.some((f) => isVP9(f.vcodec))) {
        videoFormats = allVideoFormats.filter((f) => isVP9(f.vcodec));
      } else if (allVideoFormats.some((f) => isH264(f.vcodec))) {
        videoFormats = allVideoFormats.filter((f) => isH264(f.vcodec));
      } else {
        videoFormats = allVideoFormats;
      }

      const byRes = new Map();
      for (const f of videoFormats) {
        const key = f.resolution || `${f.tbr || 0}k`;
        if (!byRes.has(key) || (f.tbr || 0) > (byRes.get(key).tbr || 0)) {
          byRes.set(key, f);
        }
      }
      const sorted = [...byRes.values()].sort(
        (a, b) => (b.height || b.tbr || 0) - (a.height || a.tbr || 0)
      );

      // Find all audio-only formats first to estimate combined size
      const audioFormats = data.formats.filter(
        (f) => (f.vcodec === "none" || !f.vcodec) && f.acodec && f.acodec !== "none"
      );
      bestAudio = null;
      if (audioFormats.length > 0) {
        bestAudio = audioFormats.reduce((best, f) => {
          if (!best) return f;
          const bestAbr = best.abr || 0;
          const fAbr = f.abr || 0;
          if (fAbr !== bestAbr) {
            return fAbr > bestAbr ? f : best;
          }
          const bestSize = best.filesize || 0;
          const fSize = f.filesize || 0;
          return fSize > bestSize ? f : best;
        }, null);
      }

      let bestQualitySize = null;
      bestVideo = sorted[0];
      if (bestVideo && bestVideo.filesize) {
        bestQualitySize = bestVideo.filesize + (bestAudio?.filesize || 0);
      }

      // "Best" auto option (yt-dlp picks codec priority from settings)
      const best = document.createElement("div");
      best.className = "ss-row";
      best.style.color = style.color;
      best.innerHTML = `<strong>★ Best quality</strong>
        <span style="font-size:10.5px;color:#8e8e9c;margin-left:6px;">AV1 → VP9 → H.264${bestQualitySize ? ` · ~${formatBytes(bestQualitySize)}` : ""}</span>`;
      best.addEventListener("click", () => {
        selectedFormat = null; // null = let yt-dlp pick
        setSelected(best);
      });
      list.appendChild(best);

      for (const f of sorted) {
        const row = document.createElement("div");
        row.className = "ss-row";
        
        let totalSize = f.filesize;
        let isEstimated = false;
        if (f.vcodec && f.vcodec !== "none" && (!f.acodec || f.acodec === "none") && bestAudio && bestAudio.filesize) {
          totalSize = (f.filesize || 0) + bestAudio.filesize;
          isEstimated = true;
        }

        const label = [
          f.resolution || "?",
          f.ext,
          f.fps ? `${f.fps}fps` : null,
          f.vcodec !== "none" ? f.vcodec : null,
          totalSize ? (isEstimated ? `~${formatBytes(totalSize)}` : formatBytes(totalSize)) : null,
        ].filter(Boolean).join(" · ");
        row.innerHTML = `<strong>${f.resolution || "unknown"}</strong>
          <span style="color:#8e8e9c;font-size:11px;margin-left:6px;">${label.replace(f.resolution + " · ", "")}</span>`;
        row.addEventListener("click", () => {
          selectedFormat = f;
          setSelected(row);
        });
        list.appendChild(row);
      }

      if (audioFormats.length > 0) {
        const sep = document.createElement("div");
        sep.style.cssText = "font-size:9.5px;color:#8e8e9c;text-transform:uppercase;letter-spacing:1px;padding:12px 0 4px;font-weight:600;";
        sep.textContent = "Audio only";
        list.appendChild(sep);

        const audioByBr = new Map();
        for (const f of audioFormats) {
          const key = `${f.acodec}@${Math.round(f.abr || 0)}`;
          if (!audioByBr.has(key) || (f.abr || 0) > (audioByBr.get(key).abr || 0)) {
            audioByBr.set(key, f);
          }
        }
        [...audioByBr.values()]
          .sort((a, b) => (b.abr || 0) - (a.abr || 0))
          .forEach((f) => {
            const row = document.createElement("div");
            row.className = "ss-row";
            row.innerHTML = `<strong>${f.acodec}</strong>
              <span style="color:#8e8e9c;font-size:11px;margin-left:6px;">${f.abr ? f.abr + "kbps" : ""} · ${f.ext}</span>`;
            row.addEventListener("click", () => {
              selectedFormat = f;
              setSelected(row);
            });
            list.appendChild(row);
          });
      }
    }

    // ── Category selector — shown after format is chosen ──────────────────
    async function showCategoryRow() {
      const wrap = backdrop.querySelector("#ss-category-wrap");
      if (wrap.style.display === "flex") return; // already shown
      wrap.style.display = "flex";
      const settings = await fetchSettings();
      wrap.innerHTML = `
        <label>Save to</label>
        <select id="ss-cat">
          ${Object.keys(settings.categories || {})
          .map((c) => `<option value="${c}">${c}</option>`)
          .join("")}
          <option value="__custom">Custom path…</option>
        </select>
        <input id="ss-custom" type="text" placeholder="/absolute/path/optional" style="display:none;margin-top:6px;" />
      `;
      const sel = wrap.querySelector("#ss-cat");
      const cust = wrap.querySelector("#ss-custom");
      sel.addEventListener("change", () => {
        cust.style.display = sel.value === "__custom" ? "block" : "none";
      });

      const actions = backdrop.querySelector("#ss-actions");
      actions.style.display = "flex";

      // Wire confirm button (replace any previous listener by cloning)
      const oldBtn = actions.querySelector("#ss-confirm");
      const confirmBtn = oldBtn.cloneNode(true);
      oldBtn.replaceWith(confirmBtn);
      confirmBtn.addEventListener("click", async () => {
        const category = sel.value === "__custom" ? null : sel.value;
        const customPath = sel.value === "__custom" ? cust.value.trim() : null;
        const isVideo = selectedFormat ? (selectedFormat.vcodec && selectedFormat.vcodec !== "none") : true;
        
        let estimatedTotalBytes = null;
        if (selectedFormat) {
          if (isVideo && (!selectedFormat.acodec || selectedFormat.acodec === "none") && bestAudio && bestAudio.filesize) {
            estimatedTotalBytes = (selectedFormat.filesize || 0) + bestAudio.filesize;
          } else {
            estimatedTotalBytes = selectedFormat.filesize || null;
          }
        } else {
          // Best quality chosen
          if (bestVideo && bestVideo.filesize) {
            estimatedTotalBytes = bestVideo.filesize + (bestAudio?.filesize || 0);
          }
        }

        const payload = {
          url: selectedUrl,
          format_id: selectedFormat?.format_id || null,
          category,
          custom_path: customPath,
          is_video: isVideo,
          page_title: getMediaTitle(),
          is_stream: (tier === "stream"),
          estimated_total_bytes: estimatedTotalBytes,
        };
        confirmBtn.disabled = true;
        confirmBtn.textContent = "Queuing…";
        const r = await sendToBackground({ type: "DOWNLOAD", payload });
        if (r?.ok) {
          closeModal();
          showToast("✓ Download queued");
        } else {
          confirmBtn.disabled = false;
          confirmBtn.textContent = "Confirm Download";
          showToast("✗ Failed: " + (r?.error || "unknown"));
        }
      });
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Helpers
  // ─────────────────────────────────────────────────────────────────────────
  function showToast(msg) {
    const t = document.createElement("div");
    t.style.cssText = [
      "position:fixed", "bottom:32px", "right:32px", "z-index:2147483647",
      "background:#0d0d0d", "color:#ffffff",
      "border:1px solid rgba(255, 255, 255, 0.15)",
      "padding:14px 24px", "border-radius:12px", "font-size:13.5px",
      "font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif",
      "box-shadow:0 10px 40px rgba(0, 0, 0, 0.8)",
      "backdrop-filter:blur(16px)", "-webkit-backdrop-filter:blur(16px)",
      "display:flex", "align-items:center", "font-weight:500",
      "transition:all 0.3s ease",
    ].join(";");

    // Build SVG icon element safely (no innerHTML to avoid XSS from error strings)
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "14"); svg.setAttribute("height", "14");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2.5");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
    svg.style.marginRight = "8px"; svg.style.flexShrink = "0";
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", "12"); circle.setAttribute("cy", "12"); circle.setAttribute("r", "10");
    const line1 = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line1.setAttribute("x1", "12"); line1.setAttribute("y1", "16"); line1.setAttribute("x2", "12"); line1.setAttribute("y2", "12");
    const line2 = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line2.setAttribute("x1", "12"); line2.setAttribute("y1", "8"); line2.setAttribute("x2", "12.01"); line2.setAttribute("y2", "8");
    svg.append(circle, line1, line2);

    const text = document.createTextNode(msg);
    t.append(svg, text);
    document.documentElement.appendChild(t);
    setTimeout(() => t.remove(), 2800);
  }

  function formatBytes(b) {
    if (!b) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(1)} ${u[i]}`;
  }

  function truncate(str, max) {
    return str.length > max ? str.slice(0, max - 1) + "…" : str;
  }

  // ── openDownloadAddPopup (Premium confirmation modal for standard downloads) ─────
  function openDownloadAddPopup(url, filename) {
    if (document.getElementById("ss-download-backdrop")) return;

    const backdrop = document.createElement("div");
    backdrop.id = "ss-download-backdrop";
    backdrop.innerHTML = `
      <style>
        #ss-download-backdrop {
          position: fixed;
          inset: 0;
          z-index: 2147483647;
          background: rgba(0, 0, 0, 0.85);
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
          display: flex;
          align-items: center;
          justify-content: center;
          font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          box-sizing: border-box;
        }
        
        #ss-download-modal {
          width: 480px;
          max-width: 92vw;
          background: rgba(10, 10, 10, 0.95);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 16px;
          box-shadow: 0 24px 64px rgba(0, 0, 0, 0.9), 0 0 0 1px rgba(255, 255, 255, 0.05) inset;
          color: #f5f5f7;
          padding: 24px;
          display: flex;
          flex-direction: column;
          gap: 16px;
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
          box-sizing: border-box;
          animation: modalScaleUp 0.35s cubic-bezier(0.32, 0.72, 0, 1) forwards;
        }
        
        @keyframes modalScaleUp {
          from { opacity: 0; transform: scale(0.96) translateY(8px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }

        #ss-download-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          box-sizing: border-box;
        }
        
        #ss-download-title {
          font-size: 16px;
          font-weight: 700;
          color: #ffffff;
          display: flex;
          align-items: center;
          gap: 8px;
        }

        #ss-download-close {
          cursor: pointer;
          color: #8e8e9c;
          font-size: 12px;
          display: flex;
          align-items: center;
          justify-content: center;
          width: 28px;
          height: 28px;
          border-radius: 50%;
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.08);
          transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), color 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        
        #ss-download-close:hover {
          background: rgba(255, 69, 58, 0.15);
          color: #ff453a;
          border-color: rgba(255, 69, 58, 0.3);
          transform: rotate(90deg);
        }

        #ss-download-close:active {
          transform: scale(0.92) !important;
        }

        .ss-field-group {
          display: flex;
          flex-direction: column;
          gap: 6px;
          box-sizing: border-box;
        }

        .ss-field-group label {
          font-size: 10px;
          color: #8e8e9c;
          text-transform: uppercase;
          letter-spacing: 0.8px;
          font-weight: 600;
        }

        #ss-download-url {
          font-size: 10.5px;
          color: #a3a3a3;
          word-break: break-all;
          background: rgba(255, 255, 255, 0.03);
          padding: 8px 12px;
          border-radius: 8px;
          border: 1px solid rgba(255, 255, 255, 0.06);
          font-family: monospace;
          max-height: 50px;
          overflow-y: auto;
          box-sizing: border-box;
        }

        #ss-download-filename, #ss-download-cat, #ss-download-custom {
          background: rgba(255, 255, 255, 0.04);
          color: #ffffff;
          border: 1px solid rgba(255, 255, 255, 0.08);
          padding: 10px 14px;
          border-radius: 8px;
          font-size: 13px;
          outline: none;
          font-family: inherit;
          transition: border-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
          width: 100%;
        }

        #ss-download-filename:focus, #ss-download-cat:focus, #ss-download-custom:focus {
          border-color: rgba(255, 255, 255, 0.3);
          background: rgba(255, 255, 255, 0.08);
          box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.12);
        }

        #ss-download-actions {
          display: flex;
          justify-content: flex-end;
          gap: 8px;
          margin-top: 10px;
          box-sizing: border-box;
        }

        .ss-btn {
          font-family: inherit;
          font-size: 12px;
          font-weight: 600;
          padding: 10px 14px;
          border-radius: 8px;
          cursor: pointer;
          border: none;
          outline: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), background-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.2s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
        }

        .ss-btn:active:not(:disabled) {
          transform: scale(0.96) !important;
        }

        .ss-btn-cancel {
          background: transparent;
          color: #a0a0b0;
          border: 1px solid rgba(255, 255, 255, 0.08);
        }

        .ss-btn-cancel:hover {
          border-color: rgba(255, 255, 255, 0.2);
          background: rgba(255, 255, 255, 0.04);
          color: #ffffff;
        }

        .ss-btn-chrome {
          background: rgba(255, 255, 255, 0.08);
          color: #f5f5f7;
          border: 1px solid rgba(255, 255, 255, 0.15);
        }

        .ss-btn-chrome:hover {
          background: rgba(255, 255, 255, 0.15);
          color: #ffffff;
          border-color: rgba(255, 255, 255, 0.25);
        }

        .ss-btn-confirm {
          background: #ffffff;
          color: #000000;
          border: 1px solid #ffffff;
          box-shadow: 0 4px 12px rgba(255, 255, 255, 0.05);
        }

        .ss-btn-confirm:hover {
          background: #e5e5e7;
          border-color: #e5e5e7;
          box-shadow: 0 6px 18px rgba(255, 255, 255, 0.15);
        }
      </style>

      <div id="ss-download-modal">
        <!-- Header -->
        <div id="ss-download-header">
          <div id="ss-download-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
            </svg>
            Route Download
          </div>
          <span id="ss-download-close">✕</span>
        </div>

        <!-- URL field -->
        <div class="ss-field-group">
          <label>Source URL</label>
          <div id="ss-download-url"></div>
        </div>

        <!-- Filename field -->
        <div class="ss-field-group">
          <label>Save Filename</label>
          <input type="text" id="ss-download-filename" />
        </div>

        <!-- Category field -->
        <div class="ss-field-group">
          <label>Category</label>
          <select id="ss-download-cat"></select>
          <input type="text" id="ss-download-custom" placeholder="/absolute/path/optional" style="display:none;margin-top:6px;" />
        </div>

        <!-- Action buttons -->
        <div id="ss-download-actions">
          <button id="ss-download-cancel" class="ss-btn ss-btn-cancel">Cancel</button>
          <button id="ss-download-chrome" class="ss-btn ss-btn-chrome">Download via Chrome</button>
          <button id="ss-download-confirm" class="ss-btn ss-btn-confirm">Download with App</button>
        </div>
      </div>
    `;

    const parent = document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement || document.body || document.documentElement;
    parent.appendChild(backdrop);

    const closeBtn = backdrop.querySelector("#ss-download-close");
    const cancelBtn = backdrop.querySelector("#ss-download-cancel");
    const chromeBtn = backdrop.querySelector("#ss-download-chrome");
    const confirmBtn = backdrop.querySelector("#ss-download-confirm");

    const urlEl = backdrop.querySelector("#ss-download-url");
    const filenameEl = backdrop.querySelector("#ss-download-filename");
    const catSel = backdrop.querySelector("#ss-download-cat");
    const customPathEl = backdrop.querySelector("#ss-download-custom");

    urlEl.textContent = url;
    filenameEl.value = filename || "";

    function closeModal() {
      backdrop.remove();
    }

    closeBtn.addEventListener("click", closeModal);
    cancelBtn.addEventListener("click", closeModal);

    // Load categories
    (async () => {
      const settings = await fetchSettings();
      const categories = settings?.categories || {};
      catSel.innerHTML = "";
      Object.keys(categories).forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        catSel.appendChild(opt);
      });
      const customOpt = document.createElement("option");
      customOpt.value = "__custom";
      customOpt.textContent = "Custom path…";
      catSel.appendChild(customOpt);

      catSel.addEventListener("change", () => {
        customPathEl.style.display = catSel.value === "__custom" ? "block" : "none";
      });
    })();

    // Download via Chrome (Bypass)
    chromeBtn.addEventListener("click", async () => {
      chromeBtn.disabled = true;
      const customFilename = filenameEl.value.trim();
      const r = await sendToBackground({
        type: "BYPASS_DOWNLOAD",
        url: url,
        filename: customFilename || filename
      });
      if (r?.ok) {
        closeModal();
      } else {
        chromeBtn.disabled = false;
        showToast("✗ Failed to route to Chrome");
      }
    });

    // Download via App (DownloadAnything FastAPI)
    confirmBtn.addEventListener("click", async () => {
      confirmBtn.disabled = true;
      confirmBtn.textContent = "Queuing…";
      const category = catSel.value === "__custom" ? null : catSel.value;
      const customPath = catSel.value === "__custom" ? customPathEl.value.trim() : null;
      const customFilename = filenameEl.value.trim();

      const payload = {
        url: url,
        format_id: "direct_stream",
        category,
        custom_path: customPath,
        is_video: false, // flag standard downloads as non-video files
        page_title: getMediaTitle(null),
        filename: customFilename || filename || "download",
        is_stream: false
      };

      const r = await sendToBackground({ type: "DOWNLOAD", payload });
      if (r?.ok) {
        closeModal();
        showToast("✓ Routed to DownloadAnything");
      } else {
        confirmBtn.disabled = false;
        confirmBtn.textContent = "Download with App";
        showToast("✗ Failed: " + (r?.error || "unknown"));
      }
    });
  }

  // ── Scan & observe ────────────────────────────────────────────────────────
  function scan() {
    for (const el of findAllMedia(document)) createOverlay(el);
  }
  scan();

  // Debounce the observer — pages emit many rapid mutations during playback.
  // We only need to re-scan when new elements are actually added.
  let _scanTimer = null;
  const mo = new MutationObserver(() => {
    if (_scanTimer) return;
    _scanTimer = setTimeout(() => { _scanTimer = null; scan(); }, 250);
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(scan, 2000); // catch lazy iframes

  // ── Alt-Key Bypass Listener (IDM-style) ──────────────────────────────────
  document.addEventListener("click", (e) => {
    if (e.altKey) {
      const target = e.target.closest("a");
      if (target && target.href) {
        sendToBackground({ type: "ADD_ALT_BYPASS", url: target.href });
      }
    }
  }, { capture: true, passive: true });

  async function openExtractorModalFromParams(url, mediaSrc, mediaTitle) {
    const mockMediaEl = {
      currentSrc: mediaSrc,
      src: mediaSrc,
      getAttribute: (attr) => attr === "title" ? mediaTitle : null,
      parentElement: null,
      isConnected: true,
      className: "",
      tagName: "VIDEO"
    };
    showModal(mockMediaEl, url, mediaTitle);
  }

  // ── Message listener ──────────────────────────────────────────────────────
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "SHOW_ADD_DOWNLOAD_POPUP") {
      openDownloadAddPopup(msg.url, msg.filename);
      sendResponse({ ok: true });
    } else if (msg.type === "OPEN_EXTRACTOR_MODAL_IN_TOP") {
      openExtractorModalFromParams(msg.url, msg.mediaSrc, msg.mediaTitle);
      sendResponse({ ok: true });
    }
  });
})();