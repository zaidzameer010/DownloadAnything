(function() {
  if (window.DownloadAnythingOverlayInjected) return;
  window.DownloadAnythingOverlayInjected = true;

  console.log("DownloadAnything Media Detector Injected.");

  const observedMedia = new WeakSet();

  let activeMedia = null;
  let overlayContainer = null;
  let observer = null;
  let hideTimeout = null;

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
    while (parent && parent !== document.body && parent !== document.documentElement && depth < 5) {
      const parentRect = parent.getBoundingClientRect();
      if (parentRect.width > mediaRect.width * 2.2 || parentRect.height > mediaRect.height * 2.2) {
        break;
      }
      lastValidParent = parent;
      parent = getParentElement(parent);
      depth++;
    }
    return lastValidParent || el.parentElement || el;
  }

  function showButton() {
    clearTimeout(hideTimeout);
    if (!overlayContainer) return;
    overlayContainer.style.pointerEvents = "auto";
    const wrapper = overlayContainer.shadowRoot?.querySelector(".dl-overlay-container");
    if (wrapper) {
      wrapper.classList.add("visible");
    }
  }

  function hideButton() {
    clearTimeout(hideTimeout);
    hideTimeout = setTimeout(() => {
      if (!overlayContainer) return;
      overlayContainer.style.pointerEvents = "none";
      const wrapper = overlayContainer.shadowRoot?.querySelector(".dl-overlay-container");
      if (wrapper) {
        wrapper.classList.remove("visible");
      }
    }, 400); // 400ms buffer to allow moving mouse to the button smoothly
  }

  function createOverlay() {
    if (!activeMedia) return;
    const parent = findPlayerContainer(activeMedia);
    if (!parent) return;

    if (!overlayContainer) {
      overlayContainer = document.createElement("div");
      overlayContainer.id = "download-anything-overlay-root";
      overlayContainer.style.position = "absolute";
      overlayContainer.style.zIndex = "2147483647";
      overlayContainer.style.pointerEvents = "none";

      const shadow = overlayContainer.attachShadow({ mode: "open" });
      
      // Inject Stylesheet link
      const styleLink = document.createElement("link");
      styleLink.rel = "stylesheet";
      styleLink.href = chrome.runtime.getURL("content/styles.css");
      shadow.appendChild(styleLink);

      // Create Button Wrapper
      const wrapper = document.createElement("div");
      wrapper.className = "dl-overlay-container";
      wrapper.style.visibility = "hidden"; // Hide initially to prevent FOUC

      styleLink.onload = () => {
        wrapper.style.visibility = ""; // Reveal once stylesheet styles are parsed
      };
      
      // Bind mouse events to the wrapper to keep overlay visible when hovered
      wrapper.addEventListener("mouseenter", showButton);
      wrapper.addEventListener("mouseleave", hideButton);
      
      const btn = document.createElement("button");
      btn.className = "dl-button";
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
          if (!mediaUrl || mediaUrl.startsWith("blob:") || !mediaUrl.startsWith("http")) {
            mediaUrl = window.location.href;
          }
          if (window === window.top) {
            window.DownloadAnythingModal.show(mediaUrl);
          } else {
            chrome.runtime.sendMessage({
              type: "SHOW_MODAL_IN_TOP_FRAME",
              url: mediaUrl
            });
          }
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
    
    if (mediaRect.width === 0 || mediaRect.height === 0) {
      const wrapper = overlayContainer.shadowRoot?.querySelector(".dl-overlay-container");
      if (wrapper) wrapper.classList.remove("visible");
      return;
    }
    
    // Position button at top-right of the video element, relative to the parent element container
    const top = mediaRect.top - parentRect.top + 12;
    // Align right side, accounting for actual button width dynamically
    const wrapper = overlayContainer.shadowRoot?.querySelector(".dl-overlay-container");
    const buttonWidth = (wrapper && wrapper.getBoundingClientRect().width) || 116;
    const left = mediaRect.right - parentRect.left - buttonWidth; 

    overlayContainer.style.top = `${top}px`;
    overlayContainer.style.left = `${left}px`;
  }

  function findAllMedia(root = document) {
    let elements = [];
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
    while (parent && parent !== document.body && parent !== document.documentElement && depth < 5) {
      const parentRect = parent.getBoundingClientRect();
      // Stop walking up if the container becomes too large compared to the media itself
      if (parentRect.width > mediaRect.width * 2.2 || parentRect.height > mediaRect.height * 2.2) {
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
    const mediaElements = findAllMedia();
    mediaElements.forEach(el => setupMediaListeners(el));
  }

  function initObserver() {
    observer = new MutationObserver(() => {
      scanForMedia();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "EXTENSION_ACTIVATED") {
      console.log("Direct activation triggered via toolbar button.");
      let targetUrl = window.location.href;
      if (activeMedia) {
        const src = activeMedia.src;
        if (src && !src.startsWith("blob:") && src.startsWith("http")) {
          targetUrl = src;
        }
      }
      if (window === window.top) {
        window.DownloadAnythingModal.show(targetUrl);
      } else {
        chrome.runtime.sendMessage({
          type: "SHOW_MODAL_IN_TOP_FRAME",
          url: targetUrl
        });
      }
    }
  });

  window.addEventListener("scroll", reposition, { passive: true });
  window.addEventListener("resize", reposition, { passive: true });
  scanForMedia();
  initObserver();
  setInterval(scanForMedia, 2000);
})();
