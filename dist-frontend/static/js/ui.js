import { cancelTask, deleteTask, saveSettings, pauseTask, resumeTask, revealTask } from "./api.js";
import { updateSettings, notify, getState, switchTab, triggerBinaryInstall } from "./state.js";

const selectedTaskIds = new Set();
let currentCategoryFilter = "all";
let currentSearchQuery = "";

// ── Formatting Utilities ───────────────────────────────────────────────────
export function fmtBytes(b) {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${u[i]}`;
}

export function fmtSpeed(s) {
  return s ? `${fmtBytes(s)}/s` : "—";
}

export function fmtETA(s) {
  if (!s) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c]));
}

// ── Toast System ───────────────────────────────────────────────────────────
export function showToast(msg) {
  const t = document.getElementById("toast");
  const text = document.getElementById("toast-text");
  if (!t || !text) return;
  text.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}

// ── Window-Exposed Callbacks used from context menu (data-attr delegation) ─
// uiCancelTask / uiDeleteTask were formerly used from inline onclick;
// they are now dead — context menu uses uiRemoveTaskOnly / uiDeleteTaskAndFile.

// ── Render Views ───────────────────────────────────────────────────────────
export function renderOnboarding(state) {
  const overlay = document.getElementById("onboarding-overlay");
  if (!overlay) return;

  if (!state.onboarding || !state.onboarding.visible) {
    overlay.style.display = "none";
    return;
  }

  overlay.style.display = "flex";

  const renderStatus = (binaryKey, valElId, barElId, fillElId) => {
    const bin = state.onboarding[binaryKey];
    const valEl = document.getElementById(valElId);
    const barEl = document.getElementById(barElId)?.querySelector(".status-progress-bar");
    const fillEl = document.getElementById(fillElId);

    if (!valEl) return;

    valEl.className = "status-val";

    if (bin.status === "checking") {
      valEl.textContent = "Checking...";
    } else if (bin.status === "installed") {
      valEl.textContent = "Installed";
      valEl.classList.add("installed");
      if (barEl) barEl.style.display = "none";
    } else if (bin.status === "missing") {
      valEl.textContent = "Not Installed";
      valEl.classList.add("missing");
      if (barEl) barEl.style.display = "none";
    } else if (bin.status === "downloading") {
      valEl.textContent = `Downloading (${bin.progress}%)`;
      valEl.classList.add("downloading");
      if (barEl) barEl.style.display = "block";
      if (fillEl) fillEl.style.width = `${bin.progress}%`;
    } else if (bin.status === "extracting") {
      valEl.textContent = "Extracting...";
      valEl.classList.add("downloading");
      if (barEl) barEl.style.display = "block";
      if (fillEl) fillEl.style.width = "100%";
    }
  };

  renderStatus("ffmpeg", "val-ffmpeg", "status-ffmpeg", "fill-ffmpeg");
  renderStatus("ytdlp", "val-ytdlp", "status-ytdlp", "fill-ytdlp");

  const errorEl = document.getElementById("onboarding-error");
  if (errorEl) {
    if (state.onboarding.error) {
      errorEl.textContent = `Error: ${state.onboarding.error}`;
      errorEl.style.display = "block";
    } else {
      errorEl.style.display = "none";
    }
  }

  const installBtn = document.getElementById("btn-onboarding-install");
  if (installBtn) {
    const eitherMissing = state.onboarding.ffmpeg.status === "missing" || state.onboarding.ytdlp.status === "missing";
    
    if (state.onboarding.installing) {
      installBtn.style.display = "inline-flex";
      installBtn.disabled = true;
      installBtn.innerHTML = '<i data-lucide="loader" class="spin" style="animation: spin 1s linear infinite; margin-right: 8px;"></i> Installing...';
    } else if (eitherMissing) {
      installBtn.style.display = "inline-flex";
      installBtn.disabled = false;
      installBtn.innerHTML = '<i data-lucide="download-cloud"></i> Install Required Tools';
    } else {
      installBtn.style.display = "none";
    }
    
    if (window.lucide) window.lucide.createIcons();
  }
}

export function renderDashboard(state) {
  renderOfflineBanner(state);
  renderMeta(state);
  renderNavigation(state);
  renderActiveView(state);
  renderOnboarding(state);

  if (state.activeTab === "downloads") {
    renderTasks(state);
  } else if (state.activeTab === "settings") {
    renderSettingsForm(state);
    renderCategories(state);
  }
}

function renderNavigation(state) {
  const navDownloads = document.getElementById("nav-downloads");
  const navSettings = document.getElementById("nav-settings");
  const badge = document.getElementById("nav-active-count");

  if (state.activeTab === "downloads") {
    navDownloads?.classList.add("active");
    navSettings?.classList.remove("active");
  } else {
    navDownloads?.classList.remove("active");
    navSettings?.classList.add("active");
  }

  const activeCount = state.tasks.filter(t => t.status === "downloading").length;
  if (badge) {
    badge.textContent = String(activeCount);
    badge.style.display = activeCount > 0 ? "inline-flex" : "none";
  }
}

function renderActiveView(state) {
  const downloadsPanel = document.getElementById("view-downloads");
  const settingsPanel = document.getElementById("view-settings");

  if (state.activeTab === "downloads") {
    downloadsPanel?.classList.add("active");
    settingsPanel?.classList.remove("active");
  } else {
    downloadsPanel?.classList.remove("active");
    settingsPanel?.classList.add("active");
  }
}

function renderOfflineBanner(state) {
  const banner = document.getElementById("offline-banner");
  if (!banner) return;
  if (!state.online) {
    banner.classList.add("visible");
  } else {
    banner.classList.remove("visible");
  }
}

function renderMeta(state) {
  const workers = document.getElementById("meta-workers");
  const ydlp = document.getElementById("meta-ydlp");
  if (workers) workers.textContent = state.health.active_workers;
  if (ydlp) ydlp.textContent = state.health.yt_dlp_version;
}

function renderCategoryFilters(state) {
  const container = document.getElementById("download-category-filters");
  if (!container) return;

  const categories = Object.keys(state.settings.categories || {});
  const hash = categories.join(",");
  if (container.dataset.categoriesHash === hash) {
    // Just update active class
    container.querySelectorAll(".filter-btn").forEach(btn => {
      if (btn.dataset.filter === currentCategoryFilter) {
        btn.classList.add("active");
      } else {
        btn.classList.remove("active");
      }
    });
    return;
  }

  container.dataset.categoriesHash = hash;
  let html = `<button class="filter-btn ${currentCategoryFilter === "all" ? "active" : ""}" data-filter="all">All</button>`;
  categories.forEach(cat => {
    html += `<button class="filter-btn ${currentCategoryFilter === cat ? "active" : ""}" data-filter="${escapeHtml(cat)}">${escapeHtml(cat)}</button>`;
  });
  container.innerHTML = html;

  container.querySelectorAll(".filter-btn").forEach(btn => {
    btn.onclick = () => {
      currentCategoryFilter = btn.dataset.filter;
      container.querySelectorAll(".filter-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.filter === currentCategoryFilter);
      });
      renderTasks(getState());
    };
  });
}

function renderTasks(state) {
  const tbody = document.getElementById("task-body");
  const activeCountEl = document.getElementById("meta-active");
  if (!tbody) return;

  // Render category filters dynamically
  renderCategoryFilters(state);

  // Clean up selected task IDs that no longer exist
  const currentIds = new Set(state.tasks.map(t => t.task_id));
  for (const id of selectedTaskIds) {
    if (!currentIds.has(id)) {
      selectedTaskIds.delete(id);
    }
  }

  // Filter tasks by category
  let filteredTasks = state.tasks;
  if (currentCategoryFilter !== "all") {
    filteredTasks = filteredTasks.filter(t => t.category === currentCategoryFilter);
  }

  // Filter tasks by search query
  if (currentSearchQuery.trim()) {
    const query = currentSearchQuery.toLowerCase().trim();
    filteredTasks = filteredTasks.filter(t => {
      const title = (t.title || "").toLowerCase();
      const url = (t.url || "").toLowerCase();
      return title.includes(query) || url.includes(query);
    });
  }

  if (!state.tasks.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px;font-weight:500;">No downloads in queue</td></tr>`;
    if (activeCountEl) activeCountEl.textContent = "0";
    updateSelectionUI();
    if (window.lucide) window.lucide.createIcons();
    return;
  }

  if (!filteredTasks.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px;font-weight:500;">No matching downloads found</td></tr>`;
    if (activeCountEl) {
      const active = state.tasks.filter(t => t.status === "downloading").length;
      activeCountEl.textContent = String(active);
    }
    updateSelectionUI();
    if (window.lucide) window.lucide.createIcons();
    return;
  }

  const active = state.tasks.filter(t => t.status === "downloading").length;
  if (activeCountEl) activeCountEl.textContent = String(active);

  tbody.innerHTML = filteredTasks.map(t => {
    const r = 16;
    const c = 2 * Math.PI * r;
    const off = c - (t.progress / 100) * c;
    let loc = t.final_path || t.custom_path || (state.settings.categories?.[t.category]) || "—";
    if (loc && loc !== "—" && loc === t.final_path) {
      const sep = loc.includes("\\") ? "\\" : "/";
      const parts = loc.split(sep);
      parts.pop();
      loc = parts.join(sep) + sep;
    }
    const isSelected = selectedTaskIds.has(t.task_id);
    const rowClass = isSelected ? "selected" : "";

    let badgeIcon = "";
    if (t.status === "downloading") {
      badgeIcon = `<i data-lucide="arrow-down-to-line" style="width:11px;height:11px;margin-right:3px;"></i>`;
    } else if (t.status === "completed") {
      badgeIcon = `<i data-lucide="check" style="width:11px;height:11px;margin-right:3px;"></i>`;
    } else if (t.status === "error") {
      badgeIcon = `<i data-lucide="alert-triangle" style="width:11px;height:11px;margin-right:3px;"></i>`;
    } else if (t.status === "queued") {
      badgeIcon = `<i data-lucide="clock" style="width:11px;height:11px;margin-right:3px;"></i>`;
    } else if (t.status === "cancelled") {
      badgeIcon = `<i data-lucide="ban" style="width:11px;height:11px;margin-right:3px;"></i>`;
    } else if (t.status === "paused") {
      badgeIcon = `<i data-lucide="pause" style="width:11px;height:11px;margin-right:3px;"></i>`;
    }

    return `
      <tr class="${rowClass}" data-task-id="${t.task_id}">
        <td class="checkbox-col">
          <input type="checkbox" class="task-checkbox" data-id="${t.task_id}" ${isSelected ? "checked" : ""} style="cursor:pointer;" />
        </td>
        <td class="progress-col">
          ${t.status === "completed" ? "—" : `
          <div class="progress-ring">
            <svg width="36" height="36">
              <circle class="bg" cx="18" cy="18" r="${r}" fill="none" stroke-width="3"/>
              <circle class="fg" cx="18" cy="18" r="${r}" fill="none" stroke-width="3"
                stroke-dasharray="${c}" stroke-dashoffset="${off}"/>
            </svg>
            <span>${Math.round(t.progress)}%</span>
          </div>`}
        </td>
        <td class="title-col" title="${escapeHtml(t.title || t.url)}">${escapeHtml(t.title || t.url)}</td>
        <td class="size-col">${fmtBytes(t.total_bytes || t.downloaded_bytes)}</td>
        <td class="speed-col">${fmtSpeed(t.speed)}</td>
        <td class="eta-col">${fmtETA(t.eta)}</td>
        <td class="status-col"><span class="badge ${t.status}">${badgeIcon}${t.status}</span></td>
        <td class="location-col" title="${escapeHtml(loc)}">${escapeHtml(loc)}</td>
      </tr>`;
  }).join("");

  updateSelectionUI();
  if (window.lucide) window.lucide.createIcons();
}

function renderSettingsForm(state) {
  const conc = document.getElementById("set-concurrency");
  const merge = document.getElementById("set-merge");
  const path = document.getElementById("set-default-path");
  const concurrentFragments = document.getElementById("set-concurrent-fragments");
  const speedLimit = document.getElementById("set-speed-limit");
  const proxy = document.getElementById("set-proxy");
  const cookiesBrowser = document.getElementById("set-cookies-browser");
  const embedThumbnail = document.getElementById("set-embed-thumbnail");
  const embedSubtitles = document.getElementById("set-embed-subtitles");
  const subtitleLang = document.getElementById("set-subtitle-lang");
  const subtitleLangRow = document.getElementById("subtitle-lang-row");

  if (conc && document.activeElement !== conc) {
    conc.value = state.settings.max_concurrent_downloads || 3;
  }
  if (merge && document.activeElement !== merge) {
    merge.value = state.settings.merge_output_format || "mp4";
  }
  if (path && document.activeElement !== path) {
    path.value = state.settings.default_download_path || "";
  }
  if (concurrentFragments && document.activeElement !== concurrentFragments) {
    concurrentFragments.value = state.settings.concurrent_fragments || 16;
  }
  if (speedLimit && document.activeElement !== speedLimit) {
    const bytes = state.settings.rate_limit_bytes_per_sec || 0;
    speedLimit.value = bytes ? Math.round(bytes / 1024) : "";
  }
  if (proxy && document.activeElement !== proxy) {
    proxy.value = state.settings.proxy || "";
  }
  if (cookiesBrowser && document.activeElement !== cookiesBrowser) {
    cookiesBrowser.value = state.settings.cookies_from_browser || "none";
  }
  if (embedThumbnail && document.activeElement !== embedThumbnail) {
    embedThumbnail.checked = !!state.settings.embed_thumbnail;
  }
  if (embedSubtitles && document.activeElement !== embedSubtitles) {
    embedSubtitles.checked = !!state.settings.embed_subtitles;
    if (subtitleLangRow) {
      subtitleLangRow.style.display = embedSubtitles.checked ? "flex" : "none";
    }
  }
  if (subtitleLang && document.activeElement !== subtitleLang) {
    subtitleLang.value = state.settings.subtitle_language || "en";
  }
}

function renderCategories(state) {
  const wrap = document.getElementById("cat-list");
  if (!wrap) return;

  // Don't reconstruct the inputs if the user is currently typing to prevent cursor loss
  if (wrap.querySelector("input:focus")) return;

  wrap.innerHTML = "";
  for (const [name, path] of Object.entries(state.settings.categories || {})) {
    const row = document.createElement("div");
    row.className = "cat-row";
    row.innerHTML = `
      <input type="text" value="${escapeHtml(name)}" data-key="${escapeHtml(name)}" class="cat-name" />
      <input type="text" value="${escapeHtml(path)}" class="cat-path" />
      <button class="ghost" data-del="${escapeHtml(name)}" title="Delete Category"><i data-lucide="trash-2" style="width:14px;height:14px;"></i></button>
    `;

    row.querySelector("[data-del]").onclick = () => {
      delete state.settings.categories[name];
      notify();
    };

    row.querySelector(".cat-name").onchange = (e) => {
      const newKey = e.target.value.trim();
      if (!newKey) return;
      delete state.settings.categories[name];
      state.settings.categories[newKey] = row.querySelector(".cat-path").value;
      notify();
    };

    row.querySelector(".cat-path").onchange = (e) => {
      state.settings.categories[row.querySelector(".cat-name").value] = e.target.value;
      notify();
    };

    wrap.appendChild(row);
  }

  if (window.lucide) window.lucide.createIcons();
}

// ── Selection & Context Menu Helpers ────────────────────────────────────────
function updateSelectionUI() {
  const count = selectedTaskIds.size;
  const bar = document.getElementById("bulk-actions-bar");
  const countEl = document.getElementById("bulk-count");
  const selectAllCheckbox = document.getElementById("select-all-tasks");
  const state = getState();

  if (countEl) countEl.textContent = String(count);

  const bulkPauseBtn = document.getElementById("bulk-pause");
  const bulkResumeBtn = document.getElementById("bulk-resume");

  if (count > 0) {
    bar?.classList.add("show");
    const selectedTasks = state.tasks.filter(t => selectedTaskIds.has(t.task_id));
    const hasActive = selectedTasks.some(t => t.status === "downloading" || t.status === "queued");
    const hasPaused = selectedTasks.some(t => t.status === "paused" || t.status === "cancelled" || t.status === "error");

    if (bulkPauseBtn) {
      bulkPauseBtn.style.display = hasActive ? "inline-flex" : "none";
    }
    if (bulkResumeBtn) {
      bulkResumeBtn.style.display = (!hasActive && hasPaused) ? "inline-flex" : "none";
    }
  } else {
    bar?.classList.remove("show");
  }

  // Update header checkbox state
  if (selectAllCheckbox) {
    const selectableCount = state.tasks.length;
    selectAllCheckbox.checked = selectableCount > 0 && count === selectableCount;
    selectAllCheckbox.indeterminate = count > 0 && count < selectableCount;
  }

  // Highlight rows in table and set input states
  const rows = document.querySelectorAll("#task-body tr");
  rows.forEach(row => {
    const id = row.dataset.taskId;
    if (selectedTaskIds.has(id)) {
      row.classList.add("selected");
      const cb = row.querySelector(".task-checkbox");
      if (cb) cb.checked = true;
    } else {
      row.classList.remove("selected");
      const cb = row.querySelector(".task-checkbox");
      if (cb) cb.checked = false;
    }
  });
}

export function showContextMenu(taskId, x, y) {
  const menu = document.getElementById("context-menu");
  if (!menu) return;

  const state = getState();
  const task = state.tasks.find(t => t.task_id === taskId);
  if (!task) return;

  const isDownloading = task.status === "downloading" || task.status === "queued";
  const isPaused = task.status === "paused";

  // Build menu items using DOM (not innerHTML) so taskId is never embedded in
  // attribute strings — avoids any potential XSS from unexpected ID formats.
  menu.innerHTML = "";

  const addItem = (icon, label, cls, handler) => {
    const btn = document.createElement("button");
    btn.className = cls ? `context-menu-item ${cls}` : "context-menu-item";
    btn.dataset.taskId = taskId;
    const ico = document.createElement("i");
    ico.setAttribute("data-lucide", icon);
    btn.appendChild(ico);
    btn.append(` ${label}`);
    btn.addEventListener("click", handler);
    menu.appendChild(btn);
  };

  const addDivider = () => {
    const d = document.createElement("div");
    d.className = "context-menu-divider";
    menu.appendChild(d);
  };

  if (isDownloading) {
    addItem("pause", "Pause", "", () => window.uiPauseTask(taskId));
  } else if (isPaused) {
    addItem("play", "Resume", "", () => window.uiResumeTask(taskId));
  } else if (task.status === "cancelled" || task.status === "error") {
    addItem("refresh-cw", "Restart / Resume", "", () => window.uiResumeTask(taskId));
  }

  addItem("folder-open", "Reveal in Finder", "", () => window.uiRevealTask(taskId));
  addDivider();
  addItem("trash-2", "Remove from list", "", () => window.uiRemoveTaskOnly(taskId));
  addItem("file-x", "Delete File", "danger", () => window.uiDeleteTaskAndFile(taskId));

  menu.style.display = "flex";

  // Measure after display so offsetHeight is accurate
  const menuWidth = menu.offsetWidth || 190;
  const menuHeight = menu.offsetHeight || 160;
  const windowWidth = window.innerWidth;
  const windowHeight = window.innerHeight;

  let left = x;
  let top = y;
  if (x + menuWidth > windowWidth) left = windowWidth - menuWidth - 10;
  if (y + menuHeight > windowHeight) top = windowHeight - menuHeight - 10;

  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;

  if (window.lucide) window.lucide.createIcons();

  const hideMenu = () => {
    menu.style.display = "none";
    document.removeEventListener("click", hideMenu);
  };
  setTimeout(() => document.addEventListener("click", hideMenu), 50);
}

// ── Window-Exposed Callbacks ──────────────────────────────────────────────
window.uiPauseTask = async (id) => {
  try {
    await pauseTask(id);
    showToast("Download paused");
  } catch (err) {
    showToast("Failed to pause task");
  }
};

window.uiResumeTask = async (id) => {
  try {
    await resumeTask(id);
    showToast("Download resumed");
  } catch (err) {
    showToast("Failed to resume task");
  }
};

window.uiRevealTask = async (id) => {
  try {
    await revealTask(id);
  } catch (err) {
    showToast("Failed to reveal folder");
  }
};

window.uiRemoveTaskOnly = async (id) => {
  try {
    await deleteTask(id, false);
    selectedTaskIds.delete(id);
    updateSelectionUI();
    showToast("Task removed from list");
  } catch (err) {
    showToast("Failed to remove task");
  }
};

window.uiDeleteTaskAndFile = async (id) => {
  if (confirm("Are you sure you want to permanently delete the downloaded file from disk?")) {
    try {
      await deleteTask(id, true);
      selectedTaskIds.delete(id);
      updateSelectionUI();
      showToast("Task and file deleted");
    } catch (err) {
      showToast("Failed to delete task and file");
    }
  }
};

// ── Bind Event Listeners ───────────────────────────────────────────────────
export function setupUIEventListeners() {
  const navDownloads = document.getElementById("nav-downloads");
  const navSettings = document.getElementById("nav-settings");
  const addCatBtn = document.getElementById("add-cat");
  const saveBtn = document.getElementById("save-settings");

  const tbody = document.getElementById("task-body");
  const selectAllCheckbox = document.getElementById("select-all-tasks");
  const bulkPauseBtn = document.getElementById("bulk-pause");
  const bulkResumeBtn = document.getElementById("bulk-resume");
  const bulkRemoveBtn = document.getElementById("bulk-remove");
  const bulkDeleteBtn = document.getElementById("bulk-delete");
  const bulkClearBtn = document.getElementById("bulk-clear");

  if (tbody) {
    tbody.onclick = (e) => {
      // Checkbox click
      const cb = e.target.closest(".task-checkbox");
      if (cb) {
        const id = cb.dataset.id;
        if (cb.checked) {
          selectedTaskIds.add(id);
        } else {
          selectedTaskIds.delete(id);
        }
        updateSelectionUI();
        return;
      }

      // Action button clicked - ignore row toggling
      if (e.target.closest("button") || e.target.closest("a") || e.target.closest(".ghost")) {
        return;
      }

      // Clicking on td (excluding checkbox-col)
      const td = e.target.closest("td");
      if (td && !td.classList.contains("checkbox-col")) {
        const tr = td.closest("tr");
        if (tr) {
          const rowCb = tr.querySelector(".task-checkbox");
          if (rowCb) {
            rowCb.checked = !rowCb.checked;
            const id = rowCb.dataset.id;
            if (rowCb.checked) {
              selectedTaskIds.add(id);
            } else {
              selectedTaskIds.delete(id);
            }
            updateSelectionUI();
          }
        }
      }
    };

    tbody.oncontextmenu = (e) => {
      const tr = e.target.closest("tr");
      if (!tr) return;
      const id = tr.dataset.taskId;
      if (!id) return;
      e.preventDefault();
      showContextMenu(id, e.clientX, e.clientY);
    };
  }

  if (selectAllCheckbox) {
    selectAllCheckbox.onchange = (e) => {
      const stateVal = getState();
      if (e.target.checked) {
        stateVal.tasks.forEach(t => selectedTaskIds.add(t.task_id));
      } else {
        selectedTaskIds.clear();
      }
      updateSelectionUI();
    };
  }

  if (bulkClearBtn) {
    bulkClearBtn.onclick = () => {
      selectedTaskIds.clear();
      updateSelectionUI();
    };
  }

  if (bulkPauseBtn) {
    bulkPauseBtn.onclick = async () => {
      const ids = Array.from(selectedTaskIds);
      if (!ids.length) return;
      let successCount = 0;
      await Promise.all(ids.map(async (id) => {
        try {
          await pauseTask(id);
          successCount++;
        } catch (err) {
          console.error(`Failed to pause task ${id}`, err);
        }
      }));
      showToast(`Paused ${successCount} downloads`);
      selectedTaskIds.clear();
      updateSelectionUI();
    };
  }

  if (bulkResumeBtn) {
    bulkResumeBtn.onclick = async () => {
      const ids = Array.from(selectedTaskIds);
      if (!ids.length) return;
      let successCount = 0;
      await Promise.all(ids.map(async (id) => {
        try {
          await resumeTask(id);
          successCount++;
        } catch (err) {
          console.error(`Failed to resume task ${id}`, err);
        }
      }));
      showToast(`Resumed ${successCount} downloads`);
      selectedTaskIds.clear();
      updateSelectionUI();
    };
  }

  if (bulkRemoveBtn) {
    bulkRemoveBtn.onclick = async () => {
      const ids = Array.from(selectedTaskIds);
      if (!ids.length) return;
      let successCount = 0;
      await Promise.all(ids.map(async (id) => {
        try {
          await deleteTask(id, false);
          successCount++;
        } catch (err) {
          console.error(`Failed to remove task ${id}`, err);
        }
      }));
      showToast(`Removed ${successCount} tasks`);
      selectedTaskIds.clear();
      updateSelectionUI();
    };
  }

  if (bulkDeleteBtn) {
    bulkDeleteBtn.onclick = async () => {
      const ids = Array.from(selectedTaskIds);
      if (!ids.length) return;
      if (confirm(`Are you sure you want to permanently delete files for ${ids.length} selected tasks?`)) {
        let successCount = 0;
        await Promise.all(ids.map(async (id) => {
          try {
            await deleteTask(id, true);
            successCount++;
          } catch (err) {
            console.error(`Failed to delete task/file ${id}`, err);
          }
        }));
        showToast(`Deleted ${successCount} files and tasks`);
        selectedTaskIds.clear();
        updateSelectionUI();
      }
    };
  }

  if (navDownloads) {
    navDownloads.onclick = () => switchTab("downloads");
  }
  if (navSettings) {
    navSettings.onclick = () => switchTab("settings");
  }

  // Settings view sub-tabs toggling
  const settingsTabButtons = document.querySelectorAll(".settings-tab-btn");
  settingsTabButtons.forEach(btn => {
    btn.onclick = () => {
      const tabName = btn.dataset.settingsTab;
      
      // Update active button
      settingsTabButtons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      
      // Update active content
      const tabContents = document.querySelectorAll(".settings-tab-content");
      tabContents.forEach(c => c.classList.remove("active"));
      
      const targetContent = document.getElementById(`stab-${tabName}`);
      if (targetContent) {
        targetContent.classList.add("active");
      }
    };
  });

  // Toggle subtitle language input display dynamically
  const embedSubtitlesCb = document.getElementById("set-embed-subtitles");
  const subtitleLangRow = document.getElementById("subtitle-lang-row");
  if (embedSubtitlesCb && subtitleLangRow) {
    embedSubtitlesCb.onchange = (e) => {
      subtitleLangRow.style.display = e.target.checked ? "flex" : "none";
    };
  }

  if (addCatBtn) {
    addCatBtn.onclick = () => {
      const stateVal = getState();
      stateVal.settings.categories = stateVal.settings.categories || {};
      const count = Object.keys(stateVal.settings.categories).length + 1;
      stateVal.settings.categories[`Category${count}`] = stateVal.settings.default_download_path || "";
      notify();
    };
  }

  if (saveBtn) {
    saveBtn.onclick = async () => {
      const stateVal = getState();
      const concEl = document.getElementById("set-concurrency");
      const mergeEl = document.getElementById("set-merge");
      const pathEl = document.getElementById("set-default-path");
      const concurrentFragmentsEl = document.getElementById("set-concurrent-fragments");
      const speedLimitEl = document.getElementById("set-speed-limit");
      const proxyEl = document.getElementById("set-proxy");
      const cookiesBrowserEl = document.getElementById("set-cookies-browser");
      const embedThumbnailEl = document.getElementById("set-embed-thumbnail");
      const embedSubtitlesEl = document.getElementById("set-embed-subtitles");
      const subtitleLangEl = document.getElementById("set-subtitle-lang");

      const speedLimitKb = speedLimitEl ? parseInt(speedLimitEl.value, 10) : 0;
      const rateLimitBytes = speedLimitKb > 0 ? speedLimitKb * 1024 : 0;

      const payload = {
        max_concurrent_downloads: concEl ? parseInt(concEl.value, 10) : 3,
        merge_output_format: mergeEl ? mergeEl.value : "mp4",
        default_download_path: pathEl ? pathEl.value : "",
        concurrent_fragments: concurrentFragmentsEl ? parseInt(concurrentFragmentsEl.value, 10) : 16,
        rate_limit_bytes_per_sec: rateLimitBytes,
        proxy: proxyEl ? proxyEl.value.trim() : "",
        cookies_from_browser: cookiesBrowserEl ? cookiesBrowserEl.value : "none",
        embed_thumbnail: embedThumbnailEl ? embedThumbnailEl.checked : false,
        embed_subtitles: embedSubtitlesEl ? embedSubtitlesEl.checked : false,
        subtitle_language: subtitleLangEl ? subtitleLangEl.value.trim() : "en",
        categories: stateVal.settings.categories || {}
      };

      try {
        const newSettings = await saveSettings(payload);
        updateSettings(newSettings);
        showToast("Settings saved");
      } catch (err) {
        showToast("Failed to save settings");
      }
    };
  }

  const taskSearch = document.getElementById("task-search");
  const clearCompletedBtn = document.getElementById("clear-completed-btn");

  if (taskSearch) {
    taskSearch.oninput = (e) => {
      currentSearchQuery = e.target.value;
      renderTasks(getState());
    };
  }

  if (clearCompletedBtn) {
    clearCompletedBtn.onclick = async () => {
      const stateVal = getState();
      const completedTasks = stateVal.tasks.filter(t => 
        t.status === "completed" || t.status === "error" || t.status === "cancelled"
      );
      
      if (completedTasks.length === 0) {
        showToast("No completed or stopped tasks to clear");
        return;
      }

      let successCount = 0;
      clearCompletedBtn.disabled = true;
      try {
        await Promise.all(completedTasks.map(async (t) => {
          try {
            await deleteTask(t.task_id, false);
            successCount++;
          } catch (err) {
            console.error(`Failed to clear task ${t.task_id}`, err);
          }
        }));
        showToast(`Cleared ${successCount} completed/stopped tasks`);
      } catch (err) {
        showToast("Failed to clear tasks");
      } finally {
        clearCompletedBtn.disabled = false;
      }
    };
  }

  const onboardingInstallBtn = document.getElementById("btn-onboarding-install");
  if (onboardingInstallBtn) {
    onboardingInstallBtn.onclick = () => {
      triggerBinaryInstall();
    };
  }
}
