(function() {
  if (window.DownloadAnythingModalInjected) return;
  window.DownloadAnythingModalInjected = true;

  console.log("DownloadAnything Modal Injected.");

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  const generateJobId = (prefix = "job") => {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return `${prefix}_${crypto.randomUUID()}`;
    }
    return `${prefix}_${Math.random().toString(36).slice(2, 11)}`;
  };

  let modalElement = null;
  let shadowRoot = null;
  let currentJobId = null;
  let mediaUrl = null;
  let lastOutputDir = "";
  const initiatedJobIds = new Set();

  let currentTitle = "";
  let selectedFormatId = "";
  let selectedOutputDir = "";
  let currentSniffedStreams = [];
  let interceptedData = null;

  function getPageVideoTitle() {
    // 1. Try Open Graph title
    const ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle && ogTitle.content) {
      const val = ogTitle.content.trim();
      if (val && val.length > 2) return val;
    }

    // 2. Try Twitter title
    const twTitle = document.querySelector('meta[name="twitter:title"]');
    if (twTitle && twTitle.content) {
      const val = twTitle.content.trim();
      if (val && val.length > 2) return val;
    }

    // 3. Try to find the main H1 heading on the page
    const h1 = document.querySelector('h1');
    if (h1) {
      const val = h1.textContent.trim();
      if (val && val.length > 2 && val.length < 150) return val;
    }

    // 4. Fallback to standard document title
    let docTitle = (document.title || "").trim();
    return docTitle || "video";
  }

  function extractMediaTitle(el) {
    if (!el) return null;

    // 1. Check direct attributes of the media element
    for (const attr of ["title", "aria-label", "alt", "name", "id"]) {
      const val = el.getAttribute(attr);
      if (val && val.trim().length > 2 && val.trim().length < 150) {
        return val.trim();
      }
    }

    // 2. Traversal up to check siblings/headings in the parent player container
    let parent = el.parentElement;
    for (let depth = 0; depth < 5 && parent; depth++) {
      if (parent === document.body || parent === document.documentElement) break;

      const heading = parent.querySelector("h1, h2, h3, h4, h5, h6");
      if (heading) {
        const val = heading.textContent.trim();
        if (val && val.length > 2 && val.length < 150) return val;
      }

      const titleClasses = [
        "[class*='title']", "[class*='name']", "[class*='caption']", "[class*='heading']",
        "[id*='title']", "[id*='name']", "[id*='caption']"
      ];
      for (const selector of titleClasses) {
        const titleEl = parent.querySelector(selector);
        if (titleEl && titleEl !== el) {
          const val = titleEl.textContent.trim();
          if (val && val.length > 2 && val.length < 150 && !val.includes("\n")) {
            return val;
          }
        }
      }
      
      parent = parent.parentElement;
    }

    return null;
  }

  function getActiveMediaTitle() {
    const activeMedia = window.DownloadAnythingActiveMedia;
    return extractMediaTitle(activeMedia);
  }

  function resolveBestTitleForUrl(url) {
    const matchedStream = currentSniffedStreams.find(s => s.url === url);
    if (matchedStream && matchedStream.title) {
      return matchedStream.title;
    }
    
    const activeMedia = window.DownloadAnythingActiveMedia;
    if (activeMedia) {
      const src = activeMedia.src;
      if (src === url || (src && url.includes(src)) || (src && src.includes(url))) {
        const activeTitle = getActiveMediaTitle();
        if (activeTitle) return activeTitle;
      }
    }
    
    return getPageVideoTitle();
  }

  


  // Load last output dir from storage
  chrome.storage.local.get(["lastOutputDir"], (res) => {
    if (res.lastOutputDir) lastOutputDir = res.lastOutputDir;
  });

  const Modal = {
    show(targetUrl) {
      this.create();
      this.setProbingState();
      
      // Get sniffed streams first (just to keep our registry current)
      chrome.runtime.sendMessage({ type: "GET_SNIFFED_STREAMS" }, (res) => {
        currentSniffedStreams = res?.streams || [];
        
        const initialUrl = targetUrl || window.location.href;
        mediaUrl = initialUrl;
        
        // Start probing
        chrome.runtime.sendMessage({
          type: "PROBE_MEDIA",
          url: initialUrl,
          title: resolveBestTitleForUrl(initialUrl)
        });
      });
    },

    showIntercepted(download) {
      this.create();

      interceptedData = download;
      currentJobId = generateJobId("job_intercept");
      currentTitle = download.filename || "downloaded_file";
      mediaUrl = download.url;

      const content = shadowRoot.getElementById("modalContent");
      if (!content) return;

      let sizeStr = "Unknown Size";
      if (download.fileSize && download.fileSize > 0) {
        const sizeBytes = download.fileSize;
        if (sizeBytes >= 1024 * 1024 * 1024) {
          sizeStr = `${(sizeBytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
        } else if (sizeBytes >= 1024 * 1024) {
          sizeStr = `${(sizeBytes / 1024 / 1024).toFixed(2)} MB`;
        } else if (sizeBytes >= 1024) {
          sizeStr = `${(sizeBytes / 1024).toFixed(2)} KB`;
        } else {
          sizeStr = `${sizeBytes} B`;
        }
      }

      const mediaHtml = `
        <div class="media-info">
          <img class="media-thumb" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='88' height='50' viewBox='0 0 88 50'><rect width='88' height='50' fill='%231f2937' rx='4'/><g transform='translate(32, 13)' fill='none' stroke='%23a855f7' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'/><polyline points='14 2 14 8 20 8'/><line x1='16' y1='13' x2='8' y2='13'/><line x1='16' y1='17' x2='8' y2='17'/><polyline points='10 9 9 9 8 9'/></g></svg>" alt="" />
          <div class="media-details">
            <div class="media-title" title="${escapeHtml(currentTitle)}">${escapeHtml(currentTitle)}</div>
            <div class="media-meta">
              <span>Size: ${sizeStr}</span>
              <span>•</span>
              <span>Type: ${escapeHtml(download.mime || 'Unknown')}</span>
              <span>•</span>
              <span class="badge engine browser-engine" style="border-color: #a855f7; color: #a855f7;">Browser Intercept</span>
            </div>
          </div>
        </div>
      `;

      const formatsHtml = `
        <div class="format-list">
          <label class="format-item">
            <input type="radio" name="videoFormat" value="best" checked>
            <div class="format-label-group">
              <span class="format-label">Original File (Direct Download)</span>
              <div class="format-badges">
                <span class="badge muxed" style="border-color: #a855f7; color: #a855f7;">direct</span>
              </div>
            </div>
          </label>
        </div>
      `;

      const outputHtml = `
        <div class="output-row">
          <label>Save Destination</label>
          <div class="output-input-group">
            <select class="category-select" id="selCategory">
              <option value="">Loading categories...</option>
            </select>
          </div>
        </div>
      `;

      content.innerHTML = `
        ${mediaHtml}
        <div style="font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;">Download Info</div>
        ${formatsHtml}
        ${outputHtml}
      `;

      // Enable Download button
      shadowRoot.getElementById("btnFooterDownload").disabled = false;

      // Load categories list and populate
      chrome.runtime.sendMessage({ type: "GET_CATEGORIES" });
    },

    create() {
      if (modalElement) return;

      modalElement = document.createElement("div");
      modalElement.id = "download-anything-modal-root";
      document.body.appendChild(modalElement);

      shadowRoot = modalElement.attachShadow({ mode: "open" });

      // Injected Stylesheet
      const styleLink = document.createElement("link");
      styleLink.rel = "stylesheet";
      styleLink.href = chrome.runtime.getURL("content/styles.css");
      shadowRoot.appendChild(styleLink);

      // Backdrop
      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.addEventListener("click", (e) => {
        // Only dismiss if not downloading
        const isDownloading = shadowRoot?.querySelector(".progress-container");
        if (e.target === backdrop && !isDownloading) {
          this.close();
        }
      });

      // Escape key to close
      document.addEventListener("keydown", this.handleEsc);

      // Modal Box
      const box = document.createElement("div");
      box.className = "modal-box";
      box.innerHTML = `
        <div class="modal-header">
          <h2>Download Settings</h2>
          <button class="close-btn" id="btnClose">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width: 14px; height: 14px; display: block;">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
        <div class="modal-content" id="modalContent">
          <!-- Dynamically Loaded States -->
        </div>
        <div class="modal-footer" id="modalFooter">
          <button class="footer-btn cancel" id="btnFooterCancel">Cancel</button>
          <button class="footer-btn download" id="btnFooterDownload" disabled>Download</button>
        </div>
      `;

      backdrop.appendChild(box);
      shadowRoot.appendChild(backdrop);

      shadowRoot.getElementById("btnClose").addEventListener("click", () => this.close());
      shadowRoot.getElementById("btnFooterCancel").addEventListener("click", () => this.close());
      shadowRoot.getElementById("btnFooterDownload").addEventListener("click", () => this.startDownload());
    },

    close() {
      document.removeEventListener("keydown", this.handleEsc);
      
      if (currentJobId && !initiatedJobIds.has(currentJobId)) {
        chrome.runtime.sendMessage({
          type: "CANCEL_PROBE",
          jobId: currentJobId
        });
      }

      if (modalElement) {
        modalElement.remove();
        modalElement = null;
        shadowRoot = null;
      }
      currentJobId = null;
      interceptedData = null;
    },

    handleEsc(e) {
      if (e.key === "Escape") {
        const isDownloading = shadowRoot?.querySelector(".progress-container");
        if (!isDownloading) {
          Modal.close();
        }
      }
    },

    changeSource(newUrl) {
      if (mediaUrl === newUrl) return;

      if (currentJobId) {
        chrome.runtime.sendMessage({
          type: "CANCEL_PROBE",
          jobId: currentJobId
        });
      }

      mediaUrl = newUrl;
      this.setProbingState();
      chrome.runtime.sendMessage({
        type: "PROBE_MEDIA",
        url: newUrl,
        title: resolveBestTitleForUrl(newUrl)
      });
    },

    setProbingState() {
      const content = shadowRoot.getElementById("modalContent");
      if (!content) return;
      
      content.innerHTML = `
        <div class="progress-container">
          <div class="progress-label-row">
            <span>Analyzing Media Formats...<span class="tui-cursor"></span></span>
          </div>
          <div class="progress-track">
            <div class="progress-fill indeterminate"></div>
          </div>
          <div class="progress-stats">
            <span>Running yt-dlp metadata probe</span>
          </div>
        </div>
      `;
      shadowRoot.getElementById("btnFooterDownload").disabled = true;
    },

    showError(errorText, suggestion = null) {
      const content = shadowRoot.getElementById("modalContent");
      if (!content) return;

      let suggestionHtml = "";
      if (suggestion === "cookies_required") {
        suggestionHtml = `
          <div class="warning-message">
            YouTube requested authentication. Clicking below will automatically share your current browser tab cookies to authenticate the backend server.
          </div>
          <button class="suggestion-btn" id="btnRetryCookies">Retry with Browser Cookies</button>
        `;
      } else if (suggestion === "po_token_required") {
        suggestionHtml = `
          <div class="warning-message">
            YouTube requires a proof-of-work (PO) token. Make sure you have installed and loaded a PO Token Provider plugin on your yt-dlp backend.
          </div>
        `;
      } else if (suggestion === "geo_blocked") {
        suggestionHtml = `
          <div class="warning-message">
            This content is geo-blocked by the publisher. Setup a proxy or a VPN on your server host to bypass.
          </div>
        `;
      }

      content.innerHTML = `
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <div style="font-weight: 700; color: #ffffff; text-transform: uppercase;">Format Probing Failed</div>
          <div class="error-message">${escapeHtml(errorText)}</div>
          ${suggestionHtml}
        </div>
      `;

      if (suggestion === "cookies_required") {
        shadowRoot.getElementById("btnRetryCookies").addEventListener("click", () => {
          this.setProbingState();
          chrome.runtime.sendMessage({
            type: "PROBE_MEDIA",
            url: mediaUrl
          });
        });
      }
    },

    populateFormats(data) {
      currentJobId = data.jobId;
      currentTitle = data.title || "video";
      const content = shadowRoot.getElementById("modalContent");
      if (!content) return;

      // Format duration (seconds to HH:MM:SS)
      let durationStr = "N/A";
      if (data.duration) {
        const d = Math.round(data.duration);
        const hrs = Math.floor(d / 3600);
        const mins = Math.floor((d % 3600) / 60);
        const secs = d % 60;
        durationStr = hrs > 0 
          ? `${hrs}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}` 
          : `${mins}:${secs.toString().padStart(2, '0')}`;
      }

      // Title & Thumbnail
      const mediaHtml = `
        <div class="media-info">
          <img class="media-thumb" src="${data.thumbnail || "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='80' viewBox='0 0 120 80'><rect width='120' height='80' fill='%232a2a2a'/><path d='M50,30 L75,40 L50,50 Z' fill='%23666666'/></svg>"}" alt="" />
          <div class="media-details">
            <div class="media-title" title="${escapeHtml(data.title)}">${escapeHtml(data.title)}</div>
            <div class="media-meta">
              <span>Uploader: ${escapeHtml(data.uploader || 'Unknown')}</span>
              <span>•</span>
              <span>Duration: ${durationStr}</span>
              <span>•</span>
              ${data.mediaType === "stream" 
                ? '<span class="badge engine stream-engine">Stream</span>' 
                : '<span class="badge engine ytdlp-engine">yt-dlp</span>'}
            </div>
          </div>
        </div>
      `;

      // Render format selection list
      let formatsHtml = `<div class="format-list">`;
      let formatsList = data.formats || [];
      if (formatsList.length === 0) {
        formatsList = [{
          formatId: "best",
          label: "Best Available (Original Quality)",
          codecFamily: "unknown",
          ext: "mp4",
          isCombined: true,
          hdr: false,
          streamType: data.mediaType === "stream" ? "stream" : undefined
        }];
      }

      formatsList.forEach((f, idx) => {
        const checkedAttr = idx === 0 ? "checked" : "";
        
        let badgesHtml = "";
        badgesHtml += `<span class="badge codec">${escapeHtml(f.codecFamily)}</span>`;
        if (f.hdr) badgesHtml += `<span class="badge hdr">hdr</span>`;
        if (f.isCombined) badgesHtml += `<span class="badge muxed">combined</span>`;
        if (f.streamType) badgesHtml += `<span class="badge ${f.streamType.toLowerCase()}">${escapeHtml(f.streamType)}</span>`;

        formatsHtml += `
          <label class="format-item">
            <input type="radio" name="videoFormat" value="${f.formatId}" data-ext="${f.ext}" ${checkedAttr}>
            <div class="format-label-group">
              <span class="format-label">${escapeHtml(f.label)}</span>
              <div class="format-badges">${badgesHtml}</div>
            </div>
          </label>
        `;
      });
      formatsHtml += `</div>`;

      // Render category selector
      const outputHtml = `
        <div class="output-row">
          <label>Save Destination</label>
          <div class="output-input-group">
            <select class="category-select" id="selCategory">
              <option value="">Loading categories...</option>
            </select>
          </div>
        </div>
      `;

      content.innerHTML = `
        ${mediaHtml}
        <div style="font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;">Available Resolutions</div>
        ${formatsHtml}
        ${outputHtml}
      `;

      // Enable Download button
      shadowRoot.getElementById("btnFooterDownload").disabled = false;

      // Load categories list and populate
      chrome.runtime.sendMessage({ type: "GET_CATEGORIES" });
    },

    startDownload() {
      if (!currentJobId) return;

      const checkedRadio = shadowRoot.querySelector('input[name="videoFormat"]:checked');
      if (!checkedRadio) return;

      const formatId = checkedRadio.value;
      const ext = checkedRadio.getAttribute("data-ext") || "mp4";
      const select = shadowRoot.getElementById("selCategory");
      const outputDir = select ? select.value : lastOutputDir;

      let cleanTitle = currentTitle.replace(/[\x00-\x1F\x7F]/g, '');
      const mappings = {
        '/': '／',
        '\\': '＼',
        ':': '：',
        '*': '＊',
        '?': '？',
        '"': '＂',
        '<': '＜',
        '>': '＞',
        '|': '｜'
      };
      for (const [char, replacement] of Object.entries(mappings)) {
        cleanTitle = cleanTitle.replaceAll(char, replacement);
      }
      cleanTitle = cleanTitle.replace(/\s+/g, ' ');
      cleanTitle = cleanTitle.trim();
      while (cleanTitle.endsWith('.')) {
        cleanTitle = cleanTitle.slice(0, -1).trim();
      }
      if (!cleanTitle) cleanTitle = 'video';
      const filename = `${cleanTitle}.${ext}`;

      selectedFormatId = formatId;
      selectedOutputDir = outputDir;

      chrome.runtime.sendMessage({
        type: "CHECK_FILE_EXISTS",
        path: outputDir,
        filename: filename,
        jobId: currentJobId
      });
    },

    proceedWithDownload(jobId, formatId, outputDir, conflictResolution) {
      initiatedJobIds.add(jobId);

      const msg = {
        type: "START_DOWNLOAD",
        jobId: jobId,
        formatId: formatId,
        outputDir: outputDir,
        conflictResolution: conflictResolution
      };

      if (interceptedData) {
        msg.url = interceptedData.url;
        msg.title = interceptedData.filename;
        msg.referer = interceptedData.referrer;
        msg.fileSize = interceptedData.fileSize;
        msg.mime = interceptedData.mime;
      }

      chrome.runtime.sendMessage(msg);

      Modal.showToast("Download started...");
      this.close();
    },

    createAlertModal(title, contentHtml, footerButtons) {
      const overlay = document.createElement("div");
      overlay.className = "modal-backdrop";
      overlay.style.zIndex = "2147483645"; // Render on top of main picker

      const box = document.createElement("div");
      box.className = "modal-box";
      box.style.maxWidth = "420px";
      box.innerHTML = `
        <div class="modal-header">
          <h2>${title}</h2>
          <button class="close-btn" id="btnAlertClose">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width: 14px; height: 14px; display: block;">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
        <div class="modal-content" style="padding: 16px 20px; font-size: 11.5px; line-height: 1.4; color: var(--text); display: flex; flex-direction: column; gap: 10px;">
          ${contentHtml}
        </div>
        <div class="modal-footer" id="alertFooter">
          <!-- Action Buttons -->
        </div>
      `;

      overlay.appendChild(box);
      shadowRoot.appendChild(overlay);

      const closeAlert = () => overlay.remove();
      box.querySelector("#btnAlertClose").addEventListener("click", closeAlert);

      const footer = box.querySelector("#alertFooter");
      footerButtons.forEach(btnInfo => {
        const btn = document.createElement("button");
        btn.className = `footer-btn ${btnInfo.class || ''}`;
        btn.innerText = btnInfo.text;
        btn.style.fontSize = "10.5px";
        btn.style.padding = "6px 12px";
        btn.addEventListener("click", () => {
          btnInfo.onClick(closeAlert);
        });
        footer.appendChild(btn);
      });
    },

    showDuplicateJobAlert(title, url, status, jobId) {
      const contentHtml = `
        <div style="color: var(--text-muted);">
          This link has already been submitted and exists in the task registry.
        </div>
        <div style="background: rgba(255,255,255,0.01); border: 1px solid var(--border); padding: 12px; border-radius: var(--radius-md); display: flex; flex-direction: column; gap: 8px; margin-top: 4px;">
          <div style="font-weight: 700; color: #ffffff;">${escapeHtml(title)}</div>
          <div style="font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); word-break: break-all;">${escapeHtml(url)}</div>
          <div style="font-size: 10px; text-transform: uppercase; font-weight: 800; margin-top: 2px;">
            Status: <span class="badge muxed" style="border-color: var(--border-bright); color: #ffffff; padding: 1px 4px;">${escapeHtml(status)}</span>
          </div>
        </div>
      `;

      this.createAlertModal("Link Already In List", contentHtml, [
        {
          text: "Dismiss",
          class: "cancel",
          onClick: (closeAlert) => {
            closeAlert();
            this.close();
          }
        }
      ]);
    },

    showDuplicateFileAlert(filename, path, jobId) {
      const contentHtml = `
        <div style="color: var(--text-muted);">
          A file with the same name already exists in the target save destination.
        </div>
        <div style="background: rgba(255,255,255,0.01); border: 1px solid var(--border); padding: 12px; border-radius: var(--radius-md); display: flex; flex-direction: column; gap: 6px; margin-top: 4px;">
          <div style="font-weight: 700; color: #ffffff; word-break: break-all;">${escapeHtml(filename)}</div>
          <div style="font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); word-break: break-all;">Path: ${escapeHtml(path)}</div>
        </div>
        <div style="color: var(--text-muted); margin-top: 4px;">
          Would you like to overwrite the existing file or download it as a new copy with an incremented name?
        </div>
      `;

      this.createAlertModal("Duplicate File Detected", contentHtml, [
        {
          text: "Cancel",
          class: "cancel",
          onClick: (closeAlert) => {
            closeAlert();
          }
        },
        {
          text: "Add Anyway (Rename)",
          class: "cancel",
          onClick: (closeAlert) => {
            closeAlert();
            this.proceedWithDownload(jobId, selectedFormatId, path, "rename");
          }
        },
        {
          text: "Replace / Overwrite",
          class: "download",
          onClick: (closeAlert) => {
            closeAlert();
            this.proceedWithDownload(jobId, selectedFormatId, path, "replace");
          }
        }
      ]);
    },

    setDownloadingUI() {
      const content = shadowRoot.getElementById("modalContent");
      // Keep only thumbnail block + replace resolution list with progress bar
      const mediaInfo = shadowRoot.querySelector(".media-info")?.outerHTML || "";
      
      content.innerHTML = `
        ${mediaInfo}
        <div class="progress-container" id="activeProgress">
          <div class="progress-label-row">
            <span id="progressStage">Queueing Download...</span>
            <span id="progressPercent">0%</span>
          </div>
          <div class="progress-track">
            <div class="progress-fill" id="progressFill" style="width: 0%;"></div>
          </div>
          <div class="progress-stats">
            <span id="progressSpeed">Waiting...</span>
            <span id="progressETA">ETA: --</span>
          </div>
          <div class="progress-stats" style="margin-top: 4px; border-top: 1px dashed var(--border); padding-top: 4px;">
            <span id="progressFragments">Fragments: --</span>
          </div>
        </div>
      `;

      // Convert Footer "Download" button to "Cancel" or "Stop"
      const footer = shadowRoot.getElementById("modalFooter");
      footer.innerHTML = `
        <button class="footer-btn cancel" id="btnCancelDownload">Stop Download</button>
      `;

      shadowRoot.getElementById("btnCancelDownload").addEventListener("click", () => {
        shadowRoot.getElementById("btnCancelDownload").innerText = "Stopping...";
        shadowRoot.getElementById("btnCancelDownload").disabled = true;
        chrome.runtime.sendMessage({
          type: "CANCEL_DOWNLOAD",
          jobId: currentJobId
        });
      });
    },

    updateProgress(progress) {
      const activeProgress = shadowRoot.getElementById("activeProgress");
      if (!activeProgress) return;

      const fill = shadowRoot.getElementById("progressFill");
      const percent = shadowRoot.getElementById("progressPercent");
      const stage = shadowRoot.getElementById("progressStage");
      const speed = shadowRoot.getElementById("progressSpeed");
      const eta = shadowRoot.getElementById("progressETA");
      const frags = shadowRoot.getElementById("progressFragments");

      // Stage label
      if (progress.status === "downloading") {
        stage.innerText = "Downloading...";
      } else if (progress.status === "postprocessing") {
        stage.innerText = "Post-processing...";
        fill.classList.add("indeterminate");
        speed.innerText = "";
        eta.innerText = "";
        frags.innerText = "";
        return;
      }

      // Percentage and Fill
      let pct = 0;
      let isEstimate = false;
      
      const total = progress.totalBytes;
      const est = progress.totalBytesEstimate;
      const dl = progress.downloadedBytes;
      
      if (total && total > 0) {
        pct = Math.round((dl / total) * 100);
      } else if (est && est > 0) {
        pct = Math.round((dl / est) * 100);
        isEstimate = true;
      }

      if (fill) fill.style.width = `${pct}%`;
      if (percent) percent.innerText = `${pct}%${isEstimate ? ' (est)' : ''}`;

      // Speed formatting
      if (speed && progress.speed) {
        const mbSpeed = progress.speed / 1024 / 1024;
        speed.innerText = `${mbSpeed.toFixed(2)} MB/s`;
      }

      // ETA formatting
      if (eta && progress.eta) {
        const totalSecs = Math.round(progress.eta);
        const m = Math.floor(totalSecs / 60);
        const s = totalSecs % 60;
        eta.innerText = `ETA: ${m}m ${s}s`;
      }

      // Fragments
      if (frags && progress.fragmentIndex) {
        const idx = progress.fragmentIndex;
        const totalFrags = progress.fragmentCount || "?";
        frags.innerText = `Fragment: ${idx} / ${totalFrags}`;
      }
    },

    showToast(message, isError = false) {
      // Clean old toasts
      const oldToast = document.getElementById("download-anything-toast");
      if (oldToast) oldToast.remove();

      const toast = document.createElement("div");
      toast.id = "download-anything-toast";
      toast.className = `dl-toast ${isError ? 'dl-toast-error' : 'dl-toast-success'}`;
      toast.innerText = message;
      document.body.appendChild(toast);

      // Force reflow to register initial state, then add show class to animate
      toast.offsetHeight;
      toast.classList.add("show");

      setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => {
          if (toast.parentNode) toast.remove();
        }, 250);
      }, 4000);
    }
  };

  // Listen to messages from background worker
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "SHOW_MODAL") {
      Modal.show(message.url);
      return;
    }

    if (message.type === "INTERCEPTED_DOWNLOAD") {
      Modal.showIntercepted(message.download);
      return;
    }

    // Show completed/failed toasts even if the modal is closed
    if (message.type === "download_completed" && initiatedJobIds.has(message.jobId)) {
      Modal.showToast(`Saved: ${message.filePath.split(/[/\\]/).pop()}`);
      initiatedJobIds.delete(message.jobId);
      if (message.jobId === currentJobId) {
        Modal.close();
      }
    } else if (message.type === "download_failed" && initiatedJobIds.has(message.jobId)) {
      Modal.showToast(`Download failed: ${message.error}`, true);
      initiatedJobIds.delete(message.jobId);
      if (message.jobId === currentJobId) {
        Modal.close();
      }
    } else if (message.type === "download_canceled" && initiatedJobIds.has(message.jobId)) {
      Modal.showToast("Download cancelled", true);
      initiatedJobIds.delete(message.jobId);
      if (message.jobId === currentJobId) {
        Modal.close();
      }
    }

    if (!shadowRoot) return;

    switch (message.type) {
      case "STREAM_SNIFFED":
        if (!currentSniffedStreams.some(s => s.url === message.stream.url)) {
          message.stream.title = resolveBestTitleForUrl(message.stream.url);
          currentSniffedStreams.push(message.stream);
        }
        break;
      case "categories_list":
        const select = shadowRoot.getElementById("selCategory");
        if (select) {
          if (message.categories && message.categories.length > 0) {
            select.innerHTML = "";
            message.categories.forEach((cat) => {
              const opt = document.createElement("option");
              opt.value = cat.path;
              opt.textContent = `${cat.name} (${cat.path})`;
              select.appendChild(opt);
            });
          } else {
            select.innerHTML = `<option value="${escapeHtml(lastOutputDir || '')}">Default Location (${escapeHtml(lastOutputDir || 'Downloads')})</option>`;
          }
        }
        break;
      case "probe_started":
        currentJobId = message.jobId;
        Modal.setProbingState();
        break;
      case "probe_result":
        if (message.jobId === currentJobId) {
          Modal.populateFormats(message);
        }
        break;
      case "probe_failed":
        {
          const isStreamUrl = currentSniffedStreams.some(s => s.url === mediaUrl);
          const isUnsupported = message.isUnsupportedUrl || false;
          
          if (isUnsupported && !isStreamUrl && currentSniffedStreams.length > 0) {
            // Sort descending by timestamp (most recently sniffed first)
            const sortedStreams = [...currentSniffedStreams].sort((a, b) => b.timestamp - a.timestamp);
            const fallbackUrl = sortedStreams[0].url;
            console.log("[Fallback] Probed URL unsupported. Falling back to sniffed stream:", fallbackUrl);
            Modal.showToast("Media unsupported. Loading detected stream...");
            Modal.changeSource(fallbackUrl);
          } else {
            Modal.showError(message.error, message.suggestion);
          }
        }
        break;
      case "duplicate_job_alert":
        Modal.showDuplicateJobAlert(message.title, message.url, message.status, message.jobId);
        break;
      case "file_exists_result":
        if (message.exists) {
          Modal.showDuplicateFileAlert(message.filename, message.path, message.jobId);
        } else {
          Modal.proceedWithDownload(message.jobId, selectedFormatId, message.path, "replace");
        }
        break;
      case "download_queued":
        console.log("Download queued on server path:", message.outputPath);
        break;
      case "download_progress":
        if (message.jobId === currentJobId) {
          Modal.updateProgress(message);
        }
        break;
      case "SERVER_DISCONNECTED":
        Modal.showToast("Service disconnected. Retrying...", true);
        break;
      case "SERVER_CONNECTED":
        Modal.showToast("Service reconnected!");
        break;
    }
  });

  // Expose to window namespace
  window.DownloadAnythingModal = Modal;
})();
