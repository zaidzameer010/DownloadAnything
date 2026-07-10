const SERVER_URL = "ws://127.0.0.1:8765/ws";

const sniffedStreams = new Map();
const downloadInitiators = new Map();

// Setup 1-min alarm keepalive to wake service worker
chrome.alarms.create("swKeepAliveAlarm", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "swKeepAliveAlarm") {
    console.debug("Service Worker keep-alive heartbeat alarm fired.");
  }
});

// Broadcast action clicked to content script
chrome.action.onClicked.addListener((tab) => {
  if (typeof tab.id === "number") {
    chrome.tabs.sendMessage(tab.id, { type: "EXTENSION_ACTIVATED" }).catch((err) => {
      console.warn("Failed to activate extension on click:", err);
    });
  }
});

// Offscreen document lifecycle management
let creatingOffscreen;
async function ensureOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT']
  });
  if (contexts.length > 0) {
    return;
  }
  
  if (creatingOffscreen) {
    await creatingOffscreen;
    return;
  }
  
  creatingOffscreen = chrome.offscreen.createDocument({
    url: 'background/offscreen.html',
    reasons: ['DOM_PARSER'],
    justification: 'Keep WebSocket connection alive for downloading media'
  });
  await creatingOffscreen;
  creatingOffscreen = null;
}

async function sendToWS(data) {
  await ensureOffscreenDocument();
  chrome.runtime.sendMessage({
    target: "offscreen",
    type: "SEND_WS",
    payload: data
  });
}

// Storage helpers for Job/Tab mappings
async function registerJobTab(jobId, tabId) {
  const data = await chrome.storage.local.get("jobTabMap");
  const map = data.jobTabMap || {};
  map[jobId] = tabId;
  await chrome.storage.local.set({ jobTabMap: map });
}

async function getTabForJob(jobId) {
  const data = await chrome.storage.local.get("jobTabMap");
  return data.jobTabMap?.[jobId] || null;
}

// Storage helpers for URL/Tab mappings (used for probe mapping)
async function registerUrlTab(url, tabId) {
  const data = await chrome.storage.local.get("urlTabMap");
  const map = data.urlTabMap || {};
  map[url] = tabId;
  await chrome.storage.local.set({ urlTabMap: map });
}

async function getTabForUrl(url) {
  const data = await chrome.storage.local.get("urlTabMap");
  return data.urlTabMap?.[url] || null;
}

async function removeUrlTab(url) {
  const data = await chrome.storage.local.get("urlTabMap");
  if (data.urlTabMap) {
    delete data.urlTabMap[url];
    await chrome.storage.local.set({ urlTabMap: data.urlTabMap });
  }
}

function broadcastToAllTabs(message) {
  chrome.tabs.query({}, (tabs) => {
    if (tabs) {
      tabs.forEach((tab) => {
        if (typeof tab.id === "number") {
          chrome.tabs.sendMessage(tab.id, message).catch(() => {});
        }
      });
    }
  });
}

function trackRequest(url, tabId) {
  if (typeof tabId === "number" && tabId >= 0 && url && url.startsWith("http")) {
    downloadInitiators.set(url, tabId);
    if (downloadInitiators.size > 200) {
      const firstKey = downloadInitiators.keys().next().value;
      downloadInitiators.delete(firstKey);
    }
  }
}

// Listen to requests from content scripts (overlay / modal) and offscreen
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Handle messages from offscreen
  if (message.source === "offscreen") {
    handleOffscreenMessage(message);
    return false; // No async response
  }

  const tabId = sender.tab?.id;
  if (typeof tabId !== "number") {
    return false; // No async response for non-tab origin messages
  }

  if (message.type === "SHOW_MODAL_IN_TOP_FRAME") {
    chrome.tabs.sendMessage(tabId, {
      type: "SHOW_MODAL",
      url: message.url
    }, { frameId: 0 }).catch((err) => {
      console.warn("Failed to send SHOW_MODAL to top frame:", err);
    });
    sendResponse({ status: "forwarded" });
    return true;
  }

  if (message.type === "PROBE_MEDIA") {
    chrome.tabs.get(tabId, async (tab) => {
      if (chrome.runtime.lastError) {
        console.warn("Failed to get tab info during probe:", chrome.runtime.lastError.message);
        return;
      }
      if (!tab) return;

      let pageTitle = tab.title;
      if (pageTitle) {
        pageTitle = pageTitle.trim();
      }
      
      let finalTitle = message.title;
      const lowerTitle = (finalTitle || "").toLowerCase();
      const isGeneric = !finalTitle || 
                        ["index", "master", "playlist", "manifest", "video", "chunk", "stream"].some(g => lowerTitle.includes(g)) ||
                        /^[0-9\-_\s\.]+$/.test(lowerTitle) ||
                        /720p|1080p|480p|360p|4k/.test(lowerTitle);
                        
      if (isGeneric && pageTitle) {
        finalTitle = pageTitle;
      }
      
      await registerUrlTab(message.url, tabId);
      sendToWS({
        type: "probe",
        url: message.url,
        title: finalTitle || null
      });
    });
    sendResponse({ status: "probing" });
    return true;
  }

  if (message.type === "CANCEL_PROBE") {
    sendToWS({
      type: "cancel_probe",
      jobId: message.jobId
    });
    sendResponse({ status: "probe_cancelled" });
    return true;
  }

  if (message.type === "CHECK_FILE_EXISTS") {
    registerJobTab(message.jobId, tabId).then(() => {
      sendToWS({
        type: "check_file_exists",
        path: message.path,
        filename: message.filename,
        jobId: message.jobId
      });
    });
    sendResponse({ status: "checking" });
    return true;
  }

  if (message.type === "START_DOWNLOAD") {
    registerJobTab(message.jobId, tabId).then(() => {
      sendToWS({
        type: "choose",
        jobId: message.jobId,
        formatId: message.formatId,
        outputDir: message.outputDir,
        conflictResolution: message.conflictResolution || "replace",
        url: message.url,
        title: message.title,
        referer: message.referer,
        fileSize: message.fileSize,
        mime: message.mime
      });
    });
    sendResponse({ status: "download_started" });
    return true;
  }

  if (message.type === "CANCEL_DOWNLOAD") {
    sendToWS({
      type: "cancel",
      jobId: message.jobId
    });
    sendResponse({ status: "cancel_sent" });
    return true;
  }

  if (message.type === "GET_CATEGORIES") {
    chrome.storage.local.set({ lastCategoriesTabId: tabId }).then(() => {
      sendToWS({
        type: "get_categories"
      });
    });
    return false;
  }

  if (message.type === "REQUEST_BROWSE") {
    chrome.storage.local.set({ lastBrowseTabId: tabId }).then(() => {
      sendToWS({
        type: "browse_directory",
        path: message.path || message.initialDir || null
      });
    });
    return false;
  }

  if (message.type === "GET_SNIFFED_STREAMS") {
    const streamsMap = sniffedStreams.get(tabId);
    const list = streamsMap ? Array.from(streamsMap.values()) : [];
    sendResponse({ streams: list });
    return true;
  }
});

async function handleOffscreenMessage(message) {
  if (message.type === "WS_OPEN") {
    broadcastToAllTabs({ type: "SERVER_CONNECTED" });
  } else if (message.type === "WS_CLOSE") {
    broadcastToAllTabs({ type: "SERVER_DISCONNECTED" });
  } else if (message.type === "WS_MESSAGE") {
    const wsMsg = message.payload;
    
    // Broadcast updates
    if (wsMsg.type === "jobs_list" || wsMsg.type === "categories_list") {
      if (wsMsg.type === "categories_list") {
        const data = await chrome.storage.local.get("lastCategoriesTabId");
        const targetId = data.lastCategoriesTabId;
        if (typeof targetId === "number") {
          chrome.tabs.sendMessage(targetId, wsMsg).catch(() => {});
        }
      }
      broadcastToAllTabs(wsMsg);
      return;
    }

    if (wsMsg.type === "directory_selected" || wsMsg.type === "browse_failed") {
      const data = await chrome.storage.local.get("lastBrowseTabId");
      const targetId = data.lastBrowseTabId;
      if (typeof targetId === "number") {
        chrome.tabs.sendMessage(targetId, wsMsg).catch(() => {});
      }
      return;
    }

    if (wsMsg.type === "probe_started") {
      const tabId = await getTabForUrl(wsMsg.url);
      if (typeof tabId === "number") {
        await registerJobTab(wsMsg.jobId, tabId);
        await removeUrlTab(wsMsg.url);
        chrome.tabs.sendMessage(tabId, wsMsg).catch(() => {});
      }
      return;
    }

    if (wsMsg.jobId) {
      const tabId = await getTabForJob(wsMsg.jobId);
      if (typeof tabId === "number") {
        chrome.tabs.sendMessage(tabId, wsMsg).catch(() => {});
        
        // Remove mappings for completed/cancelled/failed jobs
        if (["download_completed", "download_failed", "download_canceled"].includes(wsMsg.type)) {
          const data = await chrome.storage.local.get("jobTabMap");
          if (data.jobTabMap) {
            delete data.jobTabMap[wsMsg.jobId];
            await chrome.storage.local.set({ jobTabMap: data.jobTabMap });
          }
        }
      }
    }
  }
}

// Clean up connections when a tab is closed
chrome.tabs.onRemoved.addListener((tabId) => {
  if (sniffedStreams.has(tabId)) {
    sniffedStreams.delete(tabId);
    console.log(`Cleaned up sniffed streams for tab ${tabId}`);
  }
  // Clear any storage job-to-tab mappings for this tab
  chrome.storage.local.get("jobTabMap").then((data) => {
    if (data.jobTabMap) {
      let changed = false;
      for (const [jobId, tId] of Object.entries(data.jobTabMap)) {
        if (tId === tabId) {
          delete data.jobTabMap[jobId];
          changed = true;
        }
      }
      if (changed) {
        chrome.storage.local.set({ jobTabMap: data.jobTabMap });
      }
    }
  });
});

// Clear sniffed streams on navigation commit (main frame, non-same-document)
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) {
    const tabId = details.tabId;
    if (sniffedStreams.has(tabId)) {
      sniffedStreams.delete(tabId);
      console.log(`Cleaned up sniffed streams for tab ${tabId} due to main-frame navigation`);
    }
  }
});

// Sniff HLS / DASH stream requests
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    const tabId = details.tabId;
    if (tabId < 0) return; // Ignore internal or background requests
    
    const url = details.url;
    trackRequest(url, tabId);

    const lowerUrl = url.toLowerCase();
    const isHls = lowerUrl.includes(".m3u8") || lowerUrl.includes("/m3u8") || lowerUrl.includes("/hls/");
    const isDash = lowerUrl.includes(".mpd") || lowerUrl.includes("/manifest.mpd") || lowerUrl.includes("/dash/");
    
    if (isHls || isDash) {
      const type = isHls ? "HLS" : "DASH";
      
      if (!sniffedStreams.has(tabId)) {
        sniffedStreams.set(tabId, new Map());
      }
      
      const streamsMap = sniffedStreams.get(tabId);
      if (!streamsMap.has(url)) {
        const streamInfo = {
          url: url,
          type: type,
          timestamp: Date.now()
        };
        streamsMap.set(url, streamInfo);
        console.log(`[Sniffer] Tab ${tabId} detected ${type} stream: ${url}`);
        
        // Notify content script in top frame in real time
        chrome.tabs.sendMessage(tabId, {
          type: "STREAM_SNIFFED",
          stream: streamInfo
        }).catch(() => {});
        
        // Also broadcast to all active subframes (content scripts inside iframes)
        if (chrome.webNavigation) {
          chrome.webNavigation.getAllFrames({ tabId: tabId }, (frames) => {
            if (frames) {
              frames.forEach((frame) => {
                if (frame.frameId !== 0) {
                  chrome.tabs.sendMessage(tabId, {
                    type: "STREAM_SNIFFED",
                    stream: streamInfo
                  }, { frameId: frame.frameId }).catch(() => {});
                }
              });
            }
          });
        }
      }
    }
  },
  { urls: ["http://*/*", "https://*/*"] }
);

// Intercept browser downloads
if (chrome.downloads && chrome.downloads.onDeterminingFilename) {
  chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
    console.log("[Interception] Download event captured. URL:", item.url, "Filename:", item.filename);

    // Skip downloads triggered by this extension to prevent loop
    if (item.byExtensionId === chrome.runtime.id) {
      console.log("[Interception] Download was triggered by this extension. Skipping.");
      suggest();
      return;
    }

    const url = item.finalUrl || item.url;
    if (
      !url ||
      url.startsWith("http://localhost") ||
      url.startsWith("http://127.0.0.1") ||
      url.startsWith("ws://") ||
      url.startsWith("wss://") ||
      url.startsWith("chrome-extension://") ||
      url.startsWith("blob:") ||
      url.startsWith("data:")
    ) {
      console.log("[Interception] Local or system URL. Skipping.");
      suggest();
      return;
    }

    console.log("[Interception] Intercepting download! Cancelling Chrome native download...");
    
    // Cancel Chrome's native download and catch any potential errors
    chrome.downloads.cancel(item.id).catch((err) => {
      console.log("[Interception] Suppressed cancellation error:", err.message);
    });
    
    // Resolve suggest callback to release the download thread cleanly
    suggest();

    // Extract metadata
    const downloadData = {
      url: url,
      referrer: item.referrer || "",
      filename: item.filename ? item.filename.split(/[/\\]/).pop() : "downloaded_file",
      fileSize: item.fileSize,
      mime: item.mime
    };

    console.log("[Interception] Extracted metadata:", downloadData);

    // Track initiating tabId using downloadInitiators mapping
    let tabId = downloadInitiators.get(url) ?? downloadInitiators.get(item.referrer);

    if (typeof tabId === "number") {
      if (!downloadData.referrer) {
        chrome.tabs.get(tabId, (t) => {
          if (!chrome.runtime.lastError && t && t.url) {
            downloadData.referrer = t.url;
          }
          sendIntercepted(tabId, downloadData);
        });
      } else {
        sendIntercepted(tabId, downloadData);
      }
    } else {
      // Fallback: search for active tab only when no better signal exists
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs && tabs[0];
        const activeTabId = tab?.id;
        if (typeof activeTabId === "number" && tab.url && tab.url.startsWith("http")) {
          if (!downloadData.referrer) {
            downloadData.referrer = tab.url;
          }
          sendIntercepted(activeTabId, downloadData);
        } else {
          console.log("[Interception] No active webpage tab found. Routing directly to backend...");
          routeDirectlyToBackend(9999, downloadData);
        }
      });
    }
  });
}

function sendIntercepted(tabId, downloadData) {
  console.log(`[Interception] Sending INTERCEPTED_DOWNLOAD to tab ${tabId}...`);
  chrome.tabs.sendMessage(tabId, {
    type: "INTERCEPTED_DOWNLOAD",
    download: downloadData
  }).catch((err) => {
    console.warn("[Interception] Content script not responding in tab, routing directly to backend:", err);
    routeDirectlyToBackend(tabId, downloadData);
  });
}

function routeDirectlyToBackend(tabId, downloadData) {
  const targetTabId = (typeof tabId === "number" && tabId !== chrome.tabs.TAB_ID_NONE) ? tabId : 9999;
  const jobId = `job_intercept_${Math.random().toString(36).slice(2, 11)}`;
  
  console.log(`[Interception] Routing direct download to backend: ${downloadData.filename}`);
  registerJobTab(jobId, targetTabId).then(() => {
    sendToWS({
      type: "choose",
      jobId: jobId,
      formatId: "best",
      outputDir: "", // default dir
      conflictResolution: "rename",
      url: downloadData.url,
      title: downloadData.filename,
      referer: downloadData.referrer,
      fileSize: downloadData.fileSize,
      mime: downloadData.mime
    });
  });
}