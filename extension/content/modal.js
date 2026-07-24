(() => {
	const logger = window.__DMA_LOGGER__;

	if (window.DownloadAnythingModalInjected) return;
	window.DownloadAnythingModalInjected = true;

	const escapeHtml = (s) =>
		String(s == null ? "" : s).replace(
			/[&<>"']/g,
			(c) =>
				({
					"&": "&amp;",
					"<": "&lt;",
					">": "&gt;",
					'"': "&quot;",
					"'": "&#39;",
				})[c],
		);

	const DEFAULT_THUMBNAIL =
		"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='80' viewBox='0 0 120 80'><rect width='120' height='80' fill='%232a2a2a'/><path d='M50,30 L75,40 L50,50 Z' fill='%23666666'/></svg>";

	const sanitizeUrl = (url, allowedSchemes = ["http:", "https:", "data:"]) => {
		if (!url) return null;
		try {
			const u = new URL(url, window.location.href);
			if (!allowedSchemes.includes(u.protocol)) return null;
			if (u.protocol === "data:") {
				if (!u.pathname.toLowerCase().startsWith("image/")) return null;
				const hrefLower = u.href.toLowerCase();
				if (
					hrefLower.includes("<script") ||
					hrefLower.includes("onerror") ||
					hrefLower.includes("onload") ||
					hrefLower.includes("javascript:")
				) {
					return null;
				}
			}
			return u.href;
		} catch {
			return null;
		}
	};

	const formatBytes = (bytes) => {
		if (!bytes) return "0 B";
		const units = ["B", "KB", "MB", "GB", "TB"];
		const index = Math.min(
			Math.floor(Math.log(bytes) / Math.log(1024)),
			units.length - 1,
		);
		return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
	};

	const getEngineBadge = (mediaType, fileType) => {
		// Prefer high-level file type labels when present (video/audio/installer/…).
		if (fileType) {
			const labels = {
				video: "Video",
				audio: "Audio",
				image: "Image",
				document: "Document",
				archive: "Archive",
				installer: "Installer",
				font: "Font",
				text: "Text",
				stream: "Stream",
				torrent: "Torrent",
				other: "File",
			};
			const cls =
				fileType === "stream"
					? "stream"
					: fileType === "torrent"
						? "torrent"
						: "file";
			return { label: labels[fileType] || "File", cls };
		}
		switch (mediaType) {
			case "stream":
				return { label: "Stream", cls: "stream" };
			case "file":
			case "direct":
			case "video":
			case "audio":
			case "image":
			case "document":
			case "archive":
			case "installer":
			case "font":
			case "text":
			case "other":
				return { label: "Direct", cls: "file" };
			case "torrent":
				return { label: "Torrent", cls: "torrent" };
			case "ytdlp":
				return { label: "yt-dlp", cls: "ytdlp" };
			default:
				return { label: "yt-dlp", cls: "ytdlp" };
		}
	};

	const generateJobId = (prefix = "job") => {
		if (
			typeof crypto !== "undefined" &&
			typeof crypto.randomUUID === "function"
		) {
			return `${prefix}_${crypto.randomUUID()}`;
		}
		if (
			typeof crypto !== "undefined" &&
			typeof crypto.getRandomValues === "function"
		) {
			const bytes = new Uint8Array(16);
			crypto.getRandomValues(bytes);
			return `${prefix}_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
		}
		return `${prefix}_${Date.now().toString(36)}`;
	};

	const MSG = window.DownloadAnythingMessaging;

	function isExtensionContextValid() {
		return MSG.isExtensionContextValid();
	}

	function safeSendMessage(message, callback) {
		MSG.safeSendMessage("Modal", message, callback);
	}

	function safeGetURL(path) {
		if (!isExtensionContextValid()) return "";
		try {
			return chrome.runtime.getURL(path);
		} catch {
			return "";
		}
	}

	function safeStorageLocalGet(keys, callback) {
		if (
			!isExtensionContextValid() ||
			!chrome.storage ||
			!chrome.storage.local
		) {
			if (typeof callback === "function") callback({});
			return;
		}
		try {
			chrome.storage.local.get(keys, (res) => {
				if (chrome.runtime.lastError) {
					logger.warn(
						"[Modal] storage error:",
						chrome.runtime.lastError.message,
					);
					if (typeof callback === "function") callback({});
					return;
				}
				if (typeof callback === "function") callback(res || {});
			});
		} catch (err) {
			logger.warn("[Modal] storage get threw:", err);
			if (typeof callback === "function") callback({});
		}
	}

	function _applySettings(res) {
		if (res?.settings?.mergeFormat) {
			userMergeFormat = res.settings.mergeFormat;
		}
	}

	safeStorageLocalGet(["settings"], _applySettings);

	let modalElement = null;
	let shadowRoot = null;
	let currentJobId = null;
	let currentMediaType = null;
	let mediaUrl = null;
	let lastOutputDir = "";
	let customOutputDir = "";
	const initiatedJobIds = new Set();

	let currentTitle = "";
	// Resolved basename from probe_result (title + ext). Display uses currentTitle.
	let currentFilename = "";
	let selectedFormatId = "";
	let currentSniffedStreams = [];
	let interceptedData = null;
	let currentFormats = [];
	let currentTorrentFiles = [];
	// UI-selected output container; populated from storage/settings_data.
	let userMergeFormat = "";
	let selectedTorrentFiles = new Set();
	let fallbackStage = "native"; // "native" | "stream" | "direct" | "done"
	let fallbackQueue = [];
	const fallbackUrlsTried = new Set();

	// Load last output dir from storage
	safeStorageLocalGet(["lastOutputDir"], (res) => {
		if (res.lastOutputDir) lastOutputDir = res.lastOutputDir;
	});

	const Modal = {
		show(targetUrl) {
			this.create();
			this.setProbingState();

			// Initialize fallback state
			fallbackStage = "native";
			fallbackQueue = [];
			fallbackUrlsTried.clear();

			// Get sniffed streams first (just to keep our registry current)
			safeSendMessage({ type: "GET_SNIFFED_STREAMS" }, (res) => {
				if (!modalElement) return;
				currentSniffedStreams = Array.isArray(res?.streams) ? res.streams : [];

				const initialUrl = targetUrl || window.location.href;
				mediaUrl = initialUrl;
				fallbackUrlsTried.add(initialUrl);

				currentJobId = generateJobId("job_probe");

				// Start probing; pass the page title so the backend can use it as a filename hint.
				safeSendMessage({
					type: "PROBE_MEDIA",
					jobId: currentJobId,
					url: initialUrl,
					title: document.title || "",
					referer: window.location.href,
				});
			});
		},

		showIntercepted(download) {
			this.create();

			interceptedData = download;
			currentJobId = generateJobId("job_intercept");

			// Display title only — never the full path Chrome may put in item.filename.
			// Final basename is resolved on the backend from raw hints.
			const rawName = (download.filename || "").split(/[/\\]/).pop() || "";
			const stem = rawName.includes(".")
				? rawName.slice(0, rawName.lastIndexOf("."))
				: rawName;
			const extGuess = rawName.includes(".")
				? rawName.slice(rawName.lastIndexOf(".") + 1)
				: "";
			currentTitle = stem || document.title || "downloaded_file";
			currentFilename = "";
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
            <div class="meta-badges-row">
              <span class="meta-badge-chip">${sizeStr}</span>
              <span class="meta-badge-chip">${escapeHtml(download.mime || "Unknown")}</span>
              <span class="meta-badge-chip engine browser">Browser Intercept</span>
            </div>
          </div>
        </div>
      `;

			const formatsHtml = `
        <div class="format-list">
          <div class="format-list-header">
            <div class="format-list-radio-col"></div>
            <div class="format-list-label-col">Format</div>
            <div class="format-list-ext-col">Ext</div>
            <div class="format-list-size-col">Size</div>
          </div>
          <label class="format-list-row selected">
            <div class="format-list-radio-col">
              <input type="radio" name="videoFormat" value="best" data-ext="${escapeHtml(extGuess)}" checked>
            </div>
            <div class="format-list-label-col">
              <span class="format-list-label">Original File (Direct Download)</span>
            </div>
            <div class="format-list-ext-col">${escapeHtml(extGuess || "—")}</div>
            <div class="format-list-size-col">${sizeStr}</div>
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
        <div class="section-label">Download Info</div>
        ${formatsHtml}
        ${outputHtml}
      `;

			// Enable Download button
			shadowRoot.getElementById("btnFooterDownload").disabled = false;

			// Load categories list and populate
			safeSendMessage({ type: "GET_CATEGORIES" });
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
			const styleHref = safeGetURL("content/styles.css");
			if (!styleHref) return;
			styleLink.href = styleHref;
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

			shadowRoot
				.getElementById("btnClose")
				.addEventListener("click", () => this.close());
			shadowRoot
				.getElementById("btnFooterCancel")
				.addEventListener("click", () => this.close());
			shadowRoot
				.getElementById("btnFooterDownload")
				.addEventListener("click", () => this.startDownload());
		},

		close() {
			document.removeEventListener("keydown", this.handleEsc);

			if (currentJobId && !initiatedJobIds.has(currentJobId)) {
				safeSendMessage({
					type: "CANCEL_PROBE",
					jobId: currentJobId,
				});
			}

			if (modalElement) {
				modalElement.remove();
				modalElement = null;
				shadowRoot = null;
			}
			currentJobId = null;
			interceptedData = null;
			currentTorrentFiles = [];
			selectedTorrentFiles = new Set();
		},

		handleEsc(e) {
			if (e.key === "Escape") {
				const isDownloading = shadowRoot?.querySelector(".progress-container");
				if (!isDownloading) {
					Modal.close();
				}
			}
		},

		changeSource(newUrl, refererUrl = null) {
			if (mediaUrl === newUrl) return;

			if (currentJobId) {
				safeSendMessage({
					type: "CANCEL_PROBE",
					jobId: currentJobId,
				});
			}

			currentJobId = generateJobId("job_probe");
			mediaUrl = newUrl;
			this.setProbingState();
			safeSendMessage({
				type: "PROBE_MEDIA",
				jobId: currentJobId,
				url: newUrl,
				title: document.title || "",
				referer: refererUrl || window.location.href,
			});
		},

		setProbingState() {
			const content = shadowRoot.getElementById("modalContent");
			if (!content) return;

			content.innerHTML = `
        <div class="progress-container">
          <div class="progress-label-row">
            <span>Analyzing Media Formats...</span>
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
				shadowRoot
					.getElementById("btnRetryCookies")
					.addEventListener("click", () => {
						this.setProbingState();
						currentJobId = generateJobId("job_probe");
						safeSendMessage({
							type: "PROBE_MEDIA",
							jobId: currentJobId,
							url: mediaUrl,
							title: document.title || "",
							referer: window.location.href,
						});
					});
			}
		},

		renderFormatList(formatsList, activeTab) {
			const formatListContainer = shadowRoot.getElementById(
				"formatListContainer",
			);
			if (!formatListContainer) return;

			let formatsHtml = `
				<div class="format-list-header">
					<div class="format-list-radio-col"></div>
					<div class="format-list-label-col">Stream Name</div>
					<div class="format-list-ext-col">Ext</div>
					<div class="format-list-size-col">Est. Size</div>
				</div>
			`;

			const filtered = formatsList.filter((f) => {
				const hasDimension =
					(f.height && f.height > 0) || (f.width && f.width > 0);
				const isVideo =
					hasDimension ||
					f.formatId === "best" ||
					(f.codecFamily === "video" && !f.isStream);
				return activeTab === "video" ? isVideo : !isVideo;
			});

			if (filtered.length === 0) {
				formatsHtml += `
					<div style="padding: 24px; text-align: center; color: var(--text-muted); font-size: 13px;">
						No formats discovered for this tab.
					</div>
				`;
			} else {
				filtered.forEach((f, idx) => {
					const checkedAttr = idx === 0 ? "checked" : "";
					const sizeStr = f.estSizeBytes ? formatBytes(f.estSizeBytes) : "—";
					const isSelectedClass = idx === 0 ? "selected" : "";

					formatsHtml += `
						<div class="format-list-row ${isSelectedClass}" data-format-id="${escapeHtml(f.formatId)}">
							<div class="format-list-radio-col">
								<input type="radio" name="videoFormat" value="${escapeHtml(f.formatId || "")}" data-ext="${escapeHtml(f.ext || "")}" ${checkedAttr}>
							</div>
							<div class="format-list-label-col">
								<span class="format-list-label" title="${escapeHtml(f.label || "")}">${escapeHtml(f.label || "")}</span>
							</div>
							<div class="format-list-ext-col">
								<span class="format-list-ext">${escapeHtml((f.ext || "—").toUpperCase())}</span>
							</div>
							<div class="format-list-size-col">
								<span class="format-list-size">${sizeStr}</span>
							</div>
						</div>
					`;
				});
			}

			formatListContainer.innerHTML = formatsHtml;
		},

		populateFormats(data) {
			currentJobId = data.jobId;
			currentTitle = data.title || "video";
			currentFilename = data.filename || "";
			currentMediaType = data.mediaType;
			mediaUrl = data.url || mediaUrl;
			const content = shadowRoot.getElementById("modalContent");
			if (!content) return;

			// Format duration (seconds to HH:MM:SS)
			let durationStr = "N/A";
			if (data.duration) {
				const d = Math.round(data.duration);
				const hrs = Math.floor(d / 3600);
				const mins = Math.floor((d % 3600) / 60);
				const secs = d % 60;
				durationStr =
					hrs > 0
						? `${hrs}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`
						: `${mins}:${secs.toString().padStart(2, "0")}`;
			}

			// Title & Thumbnail
			const mediaHtml = `
        <div class="media-info">
          <img class="media-thumb" src="${escapeHtml(sanitizeUrl(data.thumbnail) || DEFAULT_THUMBNAIL)}" alt="" />
          <div class="media-details">
            <div class="media-title" title="${escapeHtml(data.title || "")}">${escapeHtml(data.title || "")}</div>
            <div class="meta-badges-row">
              ${data.uploader ? `<span class="meta-badge-chip">Uploader: ${escapeHtml(data.uploader)}</span>` : ""}
              ${data.duration ? `<span class="meta-badge-chip">Duration: ${durationStr}</span>` : ""}
              ${
								data.mediaType
									? (
											() => {
												const badge = getEngineBadge(
													data.mediaType,
													data.fileType,
												);
												return `<span class="meta-badge-chip engine ${badge.cls}">${escapeHtml(badge.label)}</span>`;
											}
										)()
									: ""
							}
            </div>
          </div>
        </div>
      `;

			// Output select box with appended Browse folder button layout
			const outputHtml = `
				<div class="output-row" style="border: 1px dashed var(--border); border-radius: var(--radius-md); padding: 12px; display: flex; flex-direction: column; gap: 8px;">
					<label style="font-size: 9px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em;">Save to Preset Category</label>
					<div style="display: flex; gap: 8px; width: 100%;">
						<select class="category-select" id="selCategory" style="flex: 1; outline: none;">
							<option value="">Loading categories...</option>
						</select>
						<button class="footer-btn" id="btnBrowseDir" title="Browse Custom Output Location..." style="padding: 8px 12px; display: flex; align-items: center; justify-content: center; height: 32px; box-sizing: border-box; background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: var(--radius-md); color: var(--text-secondary); cursor: pointer; transition: all 0.2s ease;">
							<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>
						</button>
					</div>
					<div id="customLocationContainer" style="display: none; justify-content: space-between; align-items: center; font-size: 11px; color: var(--text-secondary); marginTop: 4px; background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px 10px; box-sizing: border-box;">
						<span style="word-break: break-all;">
							Custom Location: <strong style="color: #ffffff;" id="customLocationPath"></strong>
						</span>
						<button id="btnClearCustomDir" style="background: none; border: none; color: var(--status-failed); cursor: pointer; font-size: 11px; font-weight: 600; padding: 0 0 0 8px;">
							Clear
						</button>
					</div>
				</div>
			`;

			if (data.mediaType === "torrent" && data.torrent) {
				currentTorrentFiles = data.torrent.files || [];
				selectedTorrentFiles = new Set(currentTorrentFiles.map((f) => f.index));

				const totalSize = data.torrent.totalSize || 0;
				const fileListHtml = currentTorrentFiles
					.map(
						(file) => `
							<label class="torrent-file-row" for="torrent-file-${file.index}">
								<input type="checkbox" id="torrent-file-${file.index}" class="torrent-file-checkbox" data-index="${file.index}" checked>
								<span title="${escapeHtml(file.path)}">${escapeHtml(file.path)}</span>
								<small>${formatBytes(file.size)}</small>
							</label>
						`,
					)
					.join("");

				content.innerHTML = `
					${mediaHtml}
					<div class="torrent-summary">${formatBytes(data.torrent.totalSize)} · ${data.torrent.pieceCount} pieces · ${escapeHtml(data.torrent.infoHash)}</div>
					<div style="font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin: 16px 0 6px;">Select Files to Download</div>
					<div class="torrent-file-list" id="torrentFileList">
						<div class="torrent-file-row" style="font-weight: 600; border-bottom: 1px solid var(--border); padding-bottom: 6px; margin-bottom: 4px;">
							<input type="checkbox" id="torrentSelectAll" checked>
							<span>File name</span>
							<small id="torrentSelectedSize">${formatBytes(totalSize)} / ${formatBytes(totalSize)}</small>
						</div>
						${fileListHtml}
					</div>
					${outputHtml}
				`;
				shadowRoot.getElementById("btnFooterDownload").disabled =
					selectedTorrentFiles.size === 0;

				const updateSelectedSize = () => {
					const selectedSize = currentTorrentFiles
						.filter((f) => selectedTorrentFiles.has(f.index))
						.reduce((sum, f) => sum + (f.size || 0), 0);
					const el = shadowRoot.getElementById("torrentSelectedSize");
					if (el) {
						el.textContent = `${formatBytes(selectedSize)} / ${formatBytes(totalSize)}`;
					}
				};

				const updateSelectAllState = () => {
					const selectAllEl = shadowRoot.getElementById("torrentSelectAll");
					if (!selectAllEl) return;
					const allSelected =
						currentTorrentFiles.length > 0 &&
						selectedTorrentFiles.size === currentTorrentFiles.length;
					const someSelected = selectedTorrentFiles.size > 0 && !allSelected;
					selectAllEl.checked = allSelected;
					selectAllEl.indeterminate = someSelected;
				};

				const updateDownloadButton = () => {
					const btn = shadowRoot.getElementById("btnFooterDownload");
					if (btn) btn.disabled = selectedTorrentFiles.size === 0;
				};

				const selectAllEl = shadowRoot.getElementById("torrentSelectAll");
				if (selectAllEl) {
					selectAllEl.addEventListener("change", () => {
						if (selectAllEl.checked) {
							selectedTorrentFiles = new Set(
								currentTorrentFiles.map((f) => f.index),
							);
							shadowRoot
								.querySelectorAll(".torrent-file-checkbox")
								.forEach((cb) => {
									cb.checked = true;
								});
						} else {
							selectedTorrentFiles = new Set();
							shadowRoot
								.querySelectorAll(".torrent-file-checkbox")
								.forEach((cb) => {
									cb.checked = false;
								});
						}
						updateSelectedSize();
						updateDownloadButton();
					});
				}

				const fileListEl = shadowRoot.getElementById("torrentFileList");
				if (fileListEl) {
					fileListEl.addEventListener("change", (e) => {
						if (
							!e.target ||
							!e.target.classList.contains("torrent-file-checkbox")
						)
							return;
						const idx = parseInt(e.target.getAttribute("data-index"), 10);
						if (e.target.checked) selectedTorrentFiles.add(idx);
						else selectedTorrentFiles.delete(idx);
						updateSelectAllState();
						updateSelectedSize();
						updateDownloadButton();
					});
				}

				// Bind Torrent output events
				const btnBrowse = shadowRoot.getElementById("btnBrowseDir");
				if (btnBrowse) {
					btnBrowse.addEventListener("click", () => {
						const select = shadowRoot.getElementById("selCategory");
						const initialDir = customOutputDir || (select ? select.value : "");
						safeSendMessage({
							type: "REQUEST_BROWSE",
							initialDir: initialDir,
						});
					});
				}
				const btnClearCustom = shadowRoot.getElementById("btnClearCustomDir");
				if (btnClearCustom) {
					btnClearCustom.addEventListener("click", () => {
						customOutputDir = "";
						const container = shadowRoot.getElementById(
							"customLocationContainer",
						);
						if (container) container.style.display = "none";
					});
				}
				const selCategory = shadowRoot.getElementById("selCategory");
				if (selCategory) {
					selCategory.addEventListener("change", () => {
						customOutputDir = "";
						const container = shadowRoot.getElementById(
							"customLocationContainer",
						);
						if (container) container.style.display = "none";
					});
				}

				safeSendMessage({ type: "GET_CATEGORIES" });
				return;
			}

			currentFormats = data.formats || [];
			if (currentFormats.length === 0) {
				currentFormats = [
					{
						formatId: "best",
						label: "Best Available (Original Quality)",
						ext: userMergeFormat,
						estSizeBytes: 0,
					},
				];
			}

			const tabsHtml = `
				<div class="modal-tabs">
					<button type="button" class="modal-tab-btn active" id="tabBtnVideo">
						<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>
						Video
					</button>
					<button type="button" class="modal-tab-btn" id="tabBtnAudio">
						<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-music"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
						Audio
					</button>
				</div>
			`;

			const formatsHtml = `<div class="format-list" id="formatListContainer"></div>`;

			content.innerHTML = `
				${mediaHtml}
				<div style="font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;">Available Resolutions</div>
				${tabsHtml}
				${formatsHtml}
				${outputHtml}
			`;

			// Render the initial format list
			let activeTab = "video";
			Modal.renderFormatList(currentFormats, activeTab);

			// Bind row selections (Click Row delegation)
			const formatListEl = content.querySelector("#formatListContainer");
			if (formatListEl) {
				formatListEl.addEventListener("click", (e) => {
					const row = e.target.closest(".format-list-row");
					if (!row) return;

					// Unselect previous
					const currentSelected = formatListEl.querySelector(
						".format-list-row.selected",
					);
					if (currentSelected) currentSelected.classList.remove("selected");

					// Select new
					row.classList.add("selected");
					const radio = row.querySelector('input[type="radio"]');
					if (radio) radio.checked = true;
				});
			}

			// Bind Tab clicks
			const tabBtnVideo = shadowRoot.getElementById("tabBtnVideo");
			const tabBtnAudio = shadowRoot.getElementById("tabBtnAudio");
			if (tabBtnVideo && tabBtnAudio) {
				tabBtnVideo.addEventListener("click", () => {
					if (activeTab === "video") return;
					activeTab = "video";
					tabBtnVideo.classList.add("active");
					tabBtnAudio.classList.remove("active");
					Modal.renderFormatList(currentFormats, activeTab);
				});
				tabBtnAudio.addEventListener("click", () => {
					if (activeTab === "audio") return;
					activeTab = "audio";
					tabBtnAudio.classList.add("active");
					tabBtnVideo.classList.remove("active");
					Modal.renderFormatList(currentFormats, activeTab);
				});
			}

			// Bind Browse events
			const btnBrowse = shadowRoot.getElementById("btnBrowseDir");
			if (btnBrowse) {
				btnBrowse.addEventListener("click", () => {
					const select = shadowRoot.getElementById("selCategory");
					const initialDir = customOutputDir || (select ? select.value : "");
					safeSendMessage({
						type: "REQUEST_BROWSE",
						initialDir: initialDir,
					});
				});
			}
			const btnClearCustom = shadowRoot.getElementById("btnClearCustomDir");
			if (btnClearCustom) {
				btnClearCustom.addEventListener("click", () => {
					customOutputDir = "";
					const container = shadowRoot.getElementById(
						"customLocationContainer",
					);
					if (container) container.style.display = "none";
				});
			}
			const selCategory = shadowRoot.getElementById("selCategory");
			if (selCategory) {
				selCategory.addEventListener("change", () => {
					customOutputDir = "";
					const container = shadowRoot.getElementById(
						"customLocationContainer",
					);
					if (container) container.style.display = "none";
				});
			}

			// Enable Download button
			shadowRoot.getElementById("btnFooterDownload").disabled = false;

			// Load categories list and populate
			safeSendMessage({ type: "GET_CATEGORIES" });
		},

		startDownload() {
			const downloadBtn = shadowRoot?.getElementById("btnFooterDownload");
			if (downloadBtn && downloadBtn.disabled) return;
			if (!currentJobId) return;

			if (currentMediaType === "torrent") {
				if (selectedTorrentFiles.size === 0) {
					Modal.showToast("Select at least one file to download", true);
					return;
				}
				const select = shadowRoot.getElementById("selCategory");
				const outputDir =
					customOutputDir || (select ? select.value : lastOutputDir);
				initiatedJobIds.add(currentJobId);
				safeSendMessage({
					type: "START_DOWNLOAD",
					jobId: currentJobId,
					formatId: "torrent",
					outputDir,
					conflictResolution: "replace",
					torrentSelectedFileIndices: Array.from(selectedTorrentFiles),
					url: mediaUrl,
					title: currentTitle,
					pageUrl: window.location.href,
				});
				Modal.showToast("Torrent download started...");
				this.close();
				return;
			}

			const checkedRadio = shadowRoot.querySelector(
				'input[name="videoFormat"]:checked',
			);
			if (!checkedRadio) return;

			const formatId = checkedRadio.value;
			const ext = checkedRadio.getAttribute("data-ext") || null;
			const select = shadowRoot.getElementById("selCategory");
			const outputDir =
				customOutputDir || (select ? select.value : lastOutputDir);

			selectedFormatId = formatId;
			lastOutputDir = outputDir;
			chrome.storage.local.set({ lastOutputDir: outputDir }).catch(() => {});

			// Let the backend resolve the final filename; send only raw hints.
			// title  = display/page title (never a full path)
			// filename = browser's original download name (intercept path only)
			// ext     = preferred format extension from the selected radio
			const checkMsg = {
				type: "CHECK_FILE_EXISTS",
				path: outputDir,
				jobId: currentJobId,
				title: currentTitle || document.title || "",
				ext: ext,
				url: interceptedData ? interceptedData.url : mediaUrl,
				mime: interceptedData ? interceptedData.mime : null,
			};
			if (interceptedData && interceptedData.filename) {
				// Raw Chrome download item.filename (may include directories).
				checkMsg.filename = interceptedData.filename;
			} else if (currentFilename) {
				// Already-resolved basename from probe_result.
				checkMsg.filename = currentFilename;
			}

			safeSendMessage(checkMsg);
		},

		proceedWithDownload(
			jobId,
			formatId,
			outputDir,
			conflictResolution,
			resolvedFilename,
		) {
			initiatedJobIds.add(jobId);

			const selectedFormat = currentFormats.find(
				(f) => f.formatId === formatId,
			);
			const selectedUrl = selectedFormat?.url || mediaUrl;

			const msg = {
				type: "START_DOWNLOAD",
				jobId: jobId,
				formatId: formatId,
				outputDir: outputDir,
				conflictResolution: conflictResolution,
			};

			if (interceptedData) {
				msg.url = interceptedData.url;
				// Raw hints only — backend resolve_filename is the single source of truth.
				// Prefer the already-resolved basename from file_exists_result when present.
				msg.title = currentTitle || document.title || "";
				msg.filename = resolvedFilename || interceptedData.filename;
				msg.referer = interceptedData.referrer;
				msg.fileSize = interceptedData.fileSize;
				msg.mime = interceptedData.mime;
			} else {
				// Probed path: title is the display stem from probe_result.
				msg.url = selectedUrl;
				msg.title = currentTitle;
				if (resolvedFilename) {
					msg.filename = resolvedFilename;
				}
			}

			msg.pageUrl = window.location.href;
			safeSendMessage(msg);

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
          <h2>${escapeHtml(title)}</h2>
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
			footerButtons.forEach((btnInfo) => {
				const btn = document.createElement("button");
				btn.className = `footer-btn ${btnInfo.class || ""}`;
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
					},
				},
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
					},
				},
				{
					text: "Add Anyway (Rename)",
					class: "cancel",
					onClick: (closeAlert) => {
						closeAlert();
						this.proceedWithDownload(
							jobId,
							selectedFormatId,
							path,
							"rename",
							filename,
						);
					},
				},
				{
					text: "Replace / Overwrite",
					class: "download",
					onClick: (closeAlert) => {
						closeAlert();
						this.proceedWithDownload(
							jobId,
							selectedFormatId,
							path,
							"replace",
							filename,
						);
					},
				},
			]);
		},

		setDownloadingUI() {
			const content = shadowRoot.getElementById("modalContent");
			// Keep only thumbnail block + replace resolution list with progress bar
			const mediaInfo =
				shadowRoot.querySelector(".media-info")?.outerHTML || "";

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

			shadowRoot
				.getElementById("btnCancelDownload")
				.addEventListener("click", () => {
					shadowRoot.getElementById("btnCancelDownload").innerText =
						"Stopping...";
					shadowRoot.getElementById("btnCancelDownload").disabled = true;
					safeSendMessage({
						type: "CANCEL_DOWNLOAD",
						jobId: currentJobId,
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
				fill.classList.remove("indeterminate");
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

			const total = progress.combinedTotalBytes ?? progress.totalBytes;
			const est = progress.totalBytesEstimate;
			const dl =
				progress.combinedDownloadedBytes ?? progress.downloadedBytes ?? 0;

			if (total && total > 0) {
				pct = Math.round((dl / total) * 100);
			} else if (est && est > 0) {
				pct = Math.round((dl / est) * 100);
				isEstimate = true;
			}

			if (fill) fill.style.width = `${pct}%`;
			if (percent) percent.innerText = `${pct}%${isEstimate ? " (est)" : ""}`;

			// Speed formatting
			if (speed && progress.speed) {
				const mbSpeed = progress.speed / 1024 / 1024;
				speed.innerText = `${mbSpeed.toFixed(2)} MB/s`;
			}

			// ETA formatting
			if (eta && progress.eta) {
				const totalSecs = Math.round(progress.eta);
				const d = Math.floor(totalSecs / 86400);
				const h = Math.floor((totalSecs % 86400) / 3600);
				const m = Math.floor((totalSecs % 3600) / 60);
				const s = totalSecs % 60;
				let etaText = "";
				if (d > 0) {
					etaText = `${d}d ${h}h ${m}m`;
				} else if (h > 0) {
					etaText = `${h}h ${m}m ${s}s`;
				} else if (m > 0) {
					etaText = `${m}m ${s}s`;
				} else {
					etaText = `${s}s`;
				}
				eta.innerText = `ETA: ${etaText}`;
			}

			// Fragments
			if (frags && progress.fragmentIndex != null) {
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
			toast.className = `dl-toast ${isError ? "dl-toast-error" : "dl-toast-success"}`;
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
		},
	};

	// Listen to messages from background worker
	chrome.runtime.onMessage?.addListener?.((message) => {
		if (message.type === "SHOW_MODAL") {
			Modal.show(message.url);
			return;
		}

		if (message.type === "INTERCEPTED_DOWNLOAD") {
			Modal.showIntercepted(message.download);
			return;
		}

		if (message.type === "SHOW_TOAST") {
			Modal.showToast(message.message, message.isError);
			return;
		}

		if (message.type === "BACKEND_STATUS") {
			if (!message.available) {
				Modal.showToast("Backend service went offline.", true);
			} else {
				Modal.showToast("Backend service is back online!");
			}
			return;
		}

		// Show completed/failed toasts even if the modal is closed
		if (
			message.type === "download_completed" &&
			initiatedJobIds.has(message.jobId)
		) {
			Modal.showToast(`Saved: ${message.filePath.split(/[/\\]/).pop()}`);
			initiatedJobIds.delete(message.jobId);
			if (message.jobId === currentJobId) {
				Modal.close();
			}
		} else if (
			message.type === "download_failed" &&
			initiatedJobIds.has(message.jobId)
		) {
			Modal.showToast(`Download failed: ${message.error}`, true);
			initiatedJobIds.delete(message.jobId);
			if (message.jobId === currentJobId) {
				Modal.close();
			}
		} else if (
			message.type === "download_canceled" &&
			initiatedJobIds.has(message.jobId)
		) {
			Modal.showToast("Download cancelled", true);
			initiatedJobIds.delete(message.jobId);
			if (message.jobId === currentJobId) {
				Modal.close();
			}
		}

		if (!shadowRoot) return;

		switch (message.type) {
			case "directory_selected": {
				customOutputDir = message.path;
				const container = shadowRoot.getElementById("customLocationContainer");
				const pathLabel = shadowRoot.getElementById("customLocationPath");
				if (container && pathLabel) {
					pathLabel.textContent = message.path;
					container.style.display = "flex";
				}
				break;
			}
			case "STREAM_SNIFFED": {
				const identity = message.stream.key || message.stream.url;
				const existingIndex = currentSniffedStreams.findIndex(
					(stream) => (stream.key || stream.url) === identity,
				);
				message.stream.title = "Stream";
				if (existingIndex >= 0) {
					currentSniffedStreams[existingIndex] = {
						...currentSniffedStreams[existingIndex],
						...message.stream,
					};
				} else {
					currentSniffedStreams.push(message.stream);
				}
				break;
			}
			case "categories_list": {
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
						select.innerHTML = `<option value="${escapeHtml(lastOutputDir || "")}">Default Location (${escapeHtml(lastOutputDir || "Downloads")})</option>`;
					}
				}
				break;
			}
			case "settings_data":
				_applySettings(message);
				break;
			case "probe_started":
				if (message.jobId === currentJobId) {
					Modal.setProbingState();
				}
				break;
			case "probe_result":
				if (message.jobId === currentJobId) {
					Modal.populateFormats(message);
				}
				break;
			case "probe_failed":
				if (message.jobId !== currentJobId) break;
				{
					// When the server tells us this is a dedicated yt-dlp site, do not
					// fall back to sniffed stream/direct URLs — those are for generic pages.
					if (message.skipFallback) {
						fallbackStage = "done";
						Modal.showError(message.error, message.suggestion);
						break;
					}

					if (fallbackStage === "native") {
						// Transition to stream stage and populate HLS/DASH candidates
						fallbackStage = "stream";
						const streamCandidates =
							window.StreamExtractorFallback.filterCandidates(
								currentSniffedStreams,
								fallbackUrlsTried,
							);
						fallbackQueue =
							window.StreamExtractorFallback.sortCandidates(streamCandidates);
					}

					if (fallbackStage === "stream") {
						// Try the next stream candidate from fallbackQueue
						let nextCandidate = null;
						while (fallbackQueue.length > 0) {
							const candidate = fallbackQueue.shift();
							if (!fallbackUrlsTried.has(candidate.key || candidate.url)) {
								nextCandidate = candidate;
								break;
							}
						}

						if (nextCandidate) {
							Modal.showToast("Probing failed. Trying stream extraction...");
							fallbackUrlsTried.add(nextCandidate.key || nextCandidate.url);
							Modal.changeSource(
								nextCandidate.url,
								nextCandidate.documentUrl || nextCandidate.initiator,
							);
							break;
						} else {
							// No more stream candidates left, transition to direct stage
							fallbackStage = "direct";
							const mediaCandidates =
								window.DirectMediaFallback.filterCandidates(
									currentSniffedStreams,
									fallbackUrlsTried,
								);
							fallbackQueue =
								window.DirectMediaFallback.sortCandidates(mediaCandidates);
						}
					}

					if (fallbackStage === "direct") {
						// Try the next direct media candidate from fallbackQueue
						let nextCandidate = null;
						while (fallbackQueue.length > 0) {
							const candidate = fallbackQueue.shift();
							if (!fallbackUrlsTried.has(candidate.key || candidate.url)) {
								nextCandidate = candidate;
								break;
							}
						}

						if (nextCandidate) {
							Modal.showToast(
								"Probing failed. Trying direct media extraction...",
							);
							fallbackUrlsTried.add(nextCandidate.key || nextCandidate.url);
							Modal.changeSource(
								nextCandidate.url,
								nextCandidate.documentUrl || nextCandidate.initiator,
							);
							break;
						} else {
							fallbackStage = "done";
						}
					}

					Modal.showError(message.error, message.suggestion);
				}
				break;
			case "duplicate_job_alert":
				Modal.showDuplicateJobAlert(
					message.title,
					message.url,
					message.status,
					message.jobId,
				);
				break;
			case "file_exists_result":
				if (message.jobId !== currentJobId) break;
				if (message.exists) {
					Modal.showDuplicateFileAlert(
						message.filename,
						message.path,
						message.jobId,
					);
				} else {
					Modal.proceedWithDownload(
						message.jobId,
						selectedFormatId,
						message.path,
						"replace",
						message.filename,
					);
				}
				break;
			case "download_queued":
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
