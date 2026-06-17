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
  function createOverlay(mediaEl) {
    if (injectedSet.has(mediaEl)) return;
    injectedSet.add(mediaEl);

    const container = findPlayerContainer(mediaEl);

    const host = document.createElement("div");
    host.id = "ss-overlay-host-" + Math.random().toString(36).slice(2, 8);
    host.style.cssText = [
      "position:absolute", "top:10px", "left:10px",
      "width:max-content", "height:max-content", "display:block",
      "z-index:2147483647", "pointer-events:auto",
      "font-family:Inter,Segoe UI,-apple-system,sans-serif",
    ].join(";");

    const shadow = host.attachShadow({ mode: "closed" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; display: block; width: max-content; height: max-content; }
        .btn {
          display:flex; align-items:center; gap:6px;
          padding:8px 14px; cursor:pointer;
          background:rgba(0, 0, 0, 0.8);
          color:#ffffff; font-size:12px; font-weight:600;
          border:1px solid rgba(255, 255, 255, 0.15);
          border-radius:8px; backdrop-filter:blur(12px);
          -webkit-backdrop-filter:blur(12px);
          box-shadow:0 4px 16px rgba(0,0,0,0.6);
          transition:all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .btn:hover { 
          transform:translateY(-1px); 
          box-shadow:0 6px 20px rgba(0,0,0,0.8); 
          border-color:rgba(255, 255, 255, 0.35);
        }
        .btn svg { transition: transform 0.25s ease; }
        .btn:hover svg { transform: translateY(0.5px); }
      </style>
      <div class="btn">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
        </svg>
        Download
      </div>
    `;
    shadow.querySelector(".btn").addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openExtractorModal(mediaEl);
    });

    const mount = () => {
      const rect = mediaEl.getBoundingClientRect();
      const cr = container.getBoundingClientRect();
      host.style.left = rect.left - cr.left + 10 + "px";
      host.style.top = rect.top - cr.top + 10 + "px";
    };
    
    container.appendChild(host);
    mount();
    window.addEventListener("scroll", mount, { passive: true });
    window.addEventListener("resize", mount, { passive: true });
  }

  // ── Two-tier probe orchestration ─────────────────────────────────────────
  //
  // Returns: { data, url, tier }
  //   tier: "native" | "stream" | "direct" | null (complete failure)
  //
  async function runTieredProbe(mediaEl) {
    // Collect the sniffed stream URLs from the background SW
    const bgRes = await sendToBackground({ type: "GET_TAB_STREAMS" });
    const sniffedUrls = (bgRes?.urls || []).filter(
      (u) => u && !u.startsWith("blob:")
    );
    const elementSrc = (mediaEl.currentSrc || mediaEl.src || "").trim();
    const pageUrl = window.location.href;

    // ── Tier 1: Try yt-dlp on the PAGE URL ────────────────────────────────
    setStatus("🔍 Probing with yt-dlp…");
    const t1 = await sendToBackground({ type: "EXTRACT", url: pageUrl, page_title: document.title });
    if (t1?.ok) {
      const method = t1.data.extraction_method;
      // Accept any real formats from the page URL — the page URL is always a
      // safe HTTP URL so we never risk passing a blob: to the backend here.
      // We accept both "yt-dlp" (known extractor) and "stream" (yt-dlp handled
      // an HLS/DASH manifest linked from the page) but not "direct" (which
      // just means yt-dlp gave up and echoed the URL back with no real info).
      const hasRealFormats = t1.data.formats?.some(
        (f) => f.vcodec !== "none" && f.vcodec !== "direct"
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
      const t2 = await sendToBackground({ type: "EXTRACT", url: streamUrl, page_title: document.title });
      if (t2?.ok) {
        const hasFormats = t2.data.formats?.length > 0;
        if (hasFormats) {
          // Distinguish HLS/DASH native from a direct HTTP fallback
          const tier =
            t2.data.extraction_method === "direct" ? "direct" : "stream";
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
    // Verify we have at least one real candidate before opening the modal
    const bgRes = await sendToBackground({ type: "GET_TAB_STREAMS" });
    const sniffed = (bgRes?.urls || []).filter(
      (u) => u && !u.startsWith("blob:")
    );
    const elementSrc = (mediaEl.currentSrc || mediaEl.src || "").trim();

    if (!elementSrc && sniffed.length === 0 && window.location.href === "about:blank") {
      showToast("✗ No media URL detected on this element");
      return;
    }

    showModal(mediaEl);
  }

  function showModal(mediaEl) {
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
          box-shadow: 0 24px 64px rgba(0, 0, 0, 0.9);
          color: #f5f5f7;
          padding: 24px;
          display: flex;
          flex-direction: column;
          gap: 16px;
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
          box-sizing: border-box;
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
          transition: all 0.2s;
        }
        
        #ss-close:hover {
          background: rgba(255, 69, 58, 0.15);
          color: #ff453a;
          border-color: rgba(255, 69, 58, 0.3);
          transform: rotate(90deg);
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
          transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
          color: #e2e2ec;
        }
        
        .ss-row:hover {
          background: rgba(255, 255, 255, 0.05);
          border-color: rgba(255, 255, 255, 0.2);
          color: #ffffff;
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
          transition: all 0.2s;
          box-sizing: border-box;
          width: 100%;
        }
        
        #ss-cat:focus, #ss-custom:focus {
          border-color: rgba(255, 255, 255, 0.3);
          background: rgba(255, 255, 255, 0.08);
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
          transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
          box-sizing: border-box;
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
          transform: translateY(-0.5px);
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

    document.documentElement.appendChild(backdrop);

    _statusEl = backdrop.querySelector("#ss-status");

    function closeModal() {
      _statusEl = null;
      backdrop.remove();
    }
    backdrop.querySelector("#ss-close").addEventListener("click", closeModal);
    backdrop.querySelector("#ss-cancel").addEventListener("click", closeModal);

    // Start the tiered probe
    runTieredProbe(mediaEl).then((result) => {
      if (!result) {
        setStatus("✗ No downloadable content found on this page or its streams.");
        return;
      }
      renderProbeResult(backdrop, result, closeModal);
    });
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Render probe result into the open modal
  // ─────────────────────────────────────────────────────────────────────────
  function renderProbeResult(backdrop, { data, url, tier }, closeModal) {
    // Tier badge
    const badge = backdrop.querySelector("#ss-tier-badge");
    const TIER_STYLES = {
      native: { bg: "rgba(255, 255, 255, 0.08)", color: "#ffffff",  label: "⚡ yt-dlp Native" },
      stream: { bg: "rgba(48, 209, 88, 0.15)", color: "#30d158",  label: "📡 Stream (HLS/DASH)" },
      direct: { bg: "rgba(255, 159, 10, 0.15)",  color: "#ff9f0a",  label: "⬇ Direct Download" },
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

      // "Best" auto option (yt-dlp picks codec priority from settings)
      const best = document.createElement("div");
      best.className = "ss-row";
      best.style.color = style.color;
      best.innerHTML = `<strong>★ Best quality</strong>
        <span style="font-size:10.5px;color:#8e8e9c;margin-left:6px;">AV1 → VP9 → H.264</span>`;
      best.addEventListener("click", () => {
        selectedFormat = null; // null = let yt-dlp pick
        setSelected(best);
      });
      list.appendChild(best);

      for (const f of sorted) {
        const row = document.createElement("div");
        row.className = "ss-row";
        const label = [
          f.resolution || "?",
          f.ext,
          f.fps ? `${f.fps}fps` : null,
          f.vcodec !== "none" ? f.vcodec : null,
          f.filesize ? formatBytes(f.filesize) : null,
        ].filter(Boolean).join(" · ");
        row.innerHTML = `<strong>${f.resolution || "unknown"}</strong>
          <span style="color:#8e8e9c;font-size:11px;margin-left:6px;">${label.replace(f.resolution + " · ", "")}</span>`;
        row.addEventListener("click", () => {
          selectedFormat = f;
          setSelected(row);
        });
        list.appendChild(row);
      }

      // Audio-only section (if any audio-only formats exist)
      const audioFormats = data.formats.filter(
        (f) => (f.vcodec === "none" || !f.vcodec) && f.acodec && f.acodec !== "none"
      );
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
        const payload = {
          url: selectedUrl,
          format_id: selectedFormat?.format_id || null,
          category,
          custom_path: customPath,
          is_video: isVideo,
          page_title: document.title,
          is_stream: (tier === "stream"),
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
    t.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-right:8px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>${msg}`;
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

  // ── Scan & observe ────────────────────────────────────────────────────────
  function scan() {
    for (const el of findAllMedia(document)) createOverlay(el);
  }
  scan();

  const mo = new MutationObserver(() => scan());
  mo.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(scan, 2000); // catch lazy iframes
})();