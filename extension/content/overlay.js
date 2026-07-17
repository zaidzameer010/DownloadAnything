(() => {
	if (window.DownloadAnythingOverlayInjected) return;
	window.DownloadAnythingOverlayInjected = true;

	console.log("DownloadAnything Media Detector Injected.");

	const observedMedia = new WeakSet();

	let activeMedia = null;
	let overlayContainer = null;
	let hideTimeout = null;
	let scanScheduled = false;
	let backendAvailable = true;

	function triggerDownloadFlow(targetUrl) {
		chrome.runtime.sendMessage({ type: "PING_BACKEND" }, (response) => {
			if (response && response.available) {
				if (window === window.top) {
					if (window.DownloadAnythingModal) {
						window.DownloadAnythingModal.show(targetUrl);
					}
				} else {
					chrome.runtime.sendMessage({
						type: "SHOW_MODAL_IN_TOP_FRAME",
						url: targetUrl,
					});
				}
			} else {
				console.warn(
					"[Overlay] Cannot show download modal, backend is offline.",
				);
				if (window === window.top && window.DownloadAnythingModal) {
					window.DownloadAnythingModal.showToast(
						"Backend service is offline. Please start the downloader app.",
						true,
					);
				} else {
					chrome.runtime.sendMessage({
						type: "SHOW_TOAST_IN_TOP_FRAME",
						message:
							"Backend service is offline. Please start the downloader app.",
						isError: true,
					});
				}
			}
		});
	}

	function getParentElement(element) {
		if (!element) return null;
		if (element.parentElement) {
			return element.parentElement;
		}
		if (typeof ShadowRoot !== "undefined") {
			const root = element.getRootNode();
			if (root instanceof ShadowRoot) {
				return root.host;
			}
		}
		return null;
	}

	function findPlayerContainer(el) {
		if (!el) return null;
		const mediaRect = el.getBoundingClientRect();
		let parent = getParentElement(el);
		let lastValidParent = parent;
		let depth = 0;
		while (
			parent &&
			parent !== document.body &&
			parent !== document.documentElement &&
			depth < 5
		) {
			const parentRect = parent.getBoundingClientRect();
			if (
				parentRect.width > mediaRect.width * 2.2 ||
				parentRect.height > mediaRect.height * 2.2
			) {
				break;
			}
			lastValidParent = parent;
			parent = getParentElement(parent);
			depth++;
		}
		return lastValidParent || el.parentElement || el;
	}

	function showButton() {
		if (!backendAvailable) return;
		clearTimeout(hideTimeout);
		if (!overlayContainer) return;
		overlayContainer.style.pointerEvents = "auto";
		const wrapper = overlayContainer.shadowRoot?.querySelector(
			".dl-overlay-container",
		);
		if (wrapper) {
			wrapper.classList.add("visible");
		}
	}

	function hideButton() {
		clearTimeout(hideTimeout);
		hideTimeout = setTimeout(() => {
			if (!overlayContainer) return;
			overlayContainer.style.pointerEvents = "none";
			const wrapper = overlayContainer.shadowRoot?.querySelector(
				".dl-overlay-container",
			);
			if (wrapper) {
				wrapper.classList.remove("visible");
			}
		}, 400); // 400ms buffer to allow moving mouse to the button smoothly
	}

	function createOverlay() {
		if (!backendAvailable) return;
		if (!activeMedia) return;
		const parent = findPlayerContainer(activeMedia);
		if (!parent) return;

		if (!overlayContainer) {
			overlayContainer = document.createElement("div");
			overlayContainer.id = "download-anything-overlay-root";
			overlayContainer.style.position = "absolute";
			overlayContainer.style.zIndex = "2147483647";
			overlayContainer.style.pointerEvents = "none";
			overlayContainer.style.left = "auto";
			overlayContainer.style.right = "auto";
			overlayContainer.style.width = "auto";

			const shadow = overlayContainer.attachShadow({ mode: "open" });

			// Inject Stylesheet link
			const styleLink = document.createElement("link");
			styleLink.rel = "stylesheet";
			try {
				styleLink.href = chrome.runtime.getURL("content/styles.css");
			} catch {
				return;
			}
			shadow.appendChild(styleLink);

			// Create Button Wrapper
			const wrapper = document.createElement("div");
			wrapper.className = "dl-overlay-container";
			wrapper.style.visibility = "hidden"; // Hide initially to prevent FOUC

			styleLink.onload = () => {
				requestAnimationFrame(() => {
					wrapper.style.visibility = "visible";
					reposition();
				});
			};

			// Bind mouse events to the wrapper to keep overlay visible when hovered
			wrapper.addEventListener("mouseenter", showButton);
			wrapper.addEventListener("mouseleave", hideButton);

			const btn = document.createElement("button");
			btn.className = "dl-button";
			btn.style.backgroundColor = "#000000";
			btn.style.color = "#ffffff";
			btn.style.border = "1px solid rgba(255, 255, 255, 0.15)";
			btn.style.appearance = "none";
			btn.style.webkitAppearance = "none";
			btn.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
          <polyline points="7 10 12 15 17 10"></polyline>
          <line x1="12" y1="15" x2="12" y2="3"></line>
        </svg>
        <span>Download</span>
      `;

			btn.addEventListener("click", (e) => {
				e.stopPropagation();
				if (activeMedia) {
					let mediaUrl = activeMedia.src;
					if (
						!mediaUrl ||
						mediaUrl.startsWith("blob:") ||
						!mediaUrl.startsWith("http")
					) {
						mediaUrl = window.location.href;
					}
					triggerDownloadFlow(mediaUrl);
				}
			});

			wrapper.appendChild(btn);
			shadow.appendChild(wrapper);
		}

		// Ensure the parent element has positioning context
		const computedStyle = window.getComputedStyle(parent);
		if (computedStyle.position === "static") {
			parent.style.position = "relative";
		}

		// Move overlayContainer to the current activeMedia's parent element if needed
		if (overlayContainer.parentElement !== parent) {
			parent.appendChild(overlayContainer);
		}
	}

	function reposition() {
		if (!activeMedia || !overlayContainer) return;

		const mediaRect = activeMedia.getBoundingClientRect();
		const parent = overlayContainer.parentElement;
		if (!parent) return;
		const parentRect = parent.getBoundingClientRect();
		const parentStyle = window.getComputedStyle(parent);

		if (mediaRect.width === 0 || mediaRect.height === 0) {
			const wrapper = overlayContainer.shadowRoot?.querySelector(
				".dl-overlay-container",
			);
			if (wrapper) wrapper.classList.remove("visible");
			return;
		}

		// Account for the parent element's border so the overlay is positioned
		// relative to the content/padding edge rather than the outer border edge.
		const borderTop = parseFloat(parentStyle.borderTopWidth) || 0;
		const borderRight = parseFloat(parentStyle.borderRightWidth) || 0;

		// Position at the top-right of the video element, inside the parent container.
		// Using the `right` inset keeps the button aligned to the right edge without
		// needing to know the button's rendered width, which avoids misalignment
		// when the button is hidden or styles are still loading.
		const top = mediaRect.top - parentRect.top - borderTop + 12;
		const right = parentRect.right - mediaRect.right - borderRight + 12;

		overlayContainer.style.top = `${top}px`;
		overlayContainer.style.right = `${right}px`;
		overlayContainer.style.left = "auto";
		overlayContainer.style.width = "auto";
	}

	function findAllMedia(root = document) {
		const elements = [];
		try {
			if (root.querySelectorAll) {
				elements.push(...root.querySelectorAll("video, audio"));
			}
			const all = root.querySelectorAll ? root.querySelectorAll("*") : [];
			for (const el of all) {
				if (el.shadowRoot) {
					elements.push(...findAllMedia(el.shadowRoot));
				}
			}
		} catch (e) {
			// Ignore errors traversing restricted structures
		}
		return elements;
	}

	function setupMediaListeners(el) {
		if (observedMedia.has(el)) return;
		observedMedia.add(el);

		const onEnter = (e) => {
			if (!backendAvailable) return;
			const rect = el.getBoundingClientRect();
			if (rect.width <= 30 || rect.height <= 30) {
				return; // Ignore tiny or hidden/tracking media elements
			}
			activeMedia = el;
			window.DownloadAnythingActiveMedia = el;
			createOverlay();
			reposition();
			showButton();
		};

		const onLeave = () => {
			hideButton();
		};

		// Bind to the media element itself
		el.addEventListener("mouseenter", onEnter);
		el.addEventListener("mouseleave", onLeave);

		// Bind to ancestors up to 5 levels to catch custom players and overlays (crossing shadow boundaries)
		const mediaRect = el.getBoundingClientRect();
		let parent = getParentElement(el);
		let depth = 0;
		while (
			parent &&
			parent !== document.body &&
			parent !== document.documentElement &&
			depth < 5
		) {
			const parentRect = parent.getBoundingClientRect();
			// Stop walking up if the container becomes too large compared to the media itself
			if (
				parentRect.width > mediaRect.width * 2.2 ||
				parentRect.height > mediaRect.height * 2.2
			) {
				break;
			}
			parent.addEventListener("mouseenter", onEnter);
			parent.addEventListener("mouseleave", onLeave);
			parent = getParentElement(parent);
			depth++;
		}

		// Set as active when played
		el.addEventListener("play", () => {
			const rect = el.getBoundingClientRect();
			if (rect.width > 30 && rect.height > 30) {
				activeMedia = el;
				window.DownloadAnythingActiveMedia = el;
				console.log("[Overlay] Active media updated on play event:", el);
			}
		});
	}

	function scanForMedia() {
		if (activeMedia && !activeMedia.isConnected) {
			activeMedia = null;
			window.DownloadAnythingActiveMedia = null;
			hideButton();
		}
		const mediaElements = findAllMedia();
		for (const el of mediaElements) {
			setupMediaListeners(el);
		}
	}

	function scheduleScan() {
		if (scanScheduled) return;
		scanScheduled = true;
		requestAnimationFrame(() => {
			scanScheduled = false;
			scanForMedia();
		});
	}

	function initObserver() {
		const observer = new MutationObserver(scheduleScan);
		observer.observe(document.body, { childList: true, subtree: true });
		document.addEventListener(
			"click",
			(event) => {
				const anchor =
					event.target instanceof Element
						? event.target.closest("a[href]")
						: null;
				const href = anchor?.href || anchor?.getAttribute("href") || "";
				if (!href.toLowerCase().startsWith("magnet:")) return;
				event.preventDefault();
				event.stopPropagation();
				if (window === window.top) {
					window.DownloadAnythingModal.show(href);
				} else {
					chrome.runtime.sendMessage({
						type: "SHOW_MODAL_IN_TOP_FRAME",
						url: href,
					});
				}
			},
			true,
		);
	}

	chrome.runtime.onMessage.addListener((message) => {
		if (message.type === "BACKEND_STATUS") {
			backendAvailable = message.available;
			console.log("[Overlay] Backend status updated:", backendAvailable);
			if (!backendAvailable) {
				if (overlayContainer) {
					const wrapper = overlayContainer.shadowRoot?.querySelector(
						".dl-overlay-container",
					);
					if (wrapper) wrapper.classList.remove("visible");
				}
			} else {
				scanForMedia();
			}
		} else if (message.type === "EXTENSION_ACTIVATED") {
			console.log("Direct activation triggered via toolbar button.");
			let targetUrl = window.location.href;
			if (activeMedia) {
				let src = activeMedia.currentSrc || activeMedia.src;
				if (!src && activeMedia.getElementsByTagName) {
					const sources = activeMedia.getElementsByTagName("source");
					if (sources.length > 0) {
						src = sources[0].src;
					}
				}
				if (src && !src.startsWith("blob:") && src.startsWith("http")) {
					targetUrl = src;
				}
			}
			triggerDownloadFlow(targetUrl);
		}
	});

	window.addEventListener("scroll", reposition, { passive: true });
	window.addEventListener("resize", reposition, { passive: true });
	scanForMedia();
	initObserver();
	setInterval(scheduleScan, 2000);
})();
