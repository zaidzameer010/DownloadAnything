import { cancelTask, deleteTask, saveSettings, pauseTask, resumeTask, revealTask } from "./api.js";
import { updateSettings, notify, getState, switchTab } from "./state.js";

const selectedTaskIds = new Set();

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

// ── Expose Global Callbacks for inline table markup ────────────────────────
window.uiCancelTask = async (id) => {
  try {
    await cancelTask(id);
    showToast("Cancellation requested");
  } catch (err) {
    showToast("Failed to cancel task");
  }
};

window.uiDeleteTask = async (id) => {
  try {
    await deleteTask(id);
    showToast("Task removed");
  } catch (err) {
    showToast("Failed to delete task");
  }
};

// ── Render Views ───────────────────────────────────────────────────────────
export function renderDashboard(state) {
  renderOfflineBanner(state);
  renderMeta(state);
  renderNavigation(state);
  renderActiveView(state);

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

function renderTasks(state) {
  const tbody = document.getElementById("task-body");
  const activeCountEl = document.getElementById("meta-active");
  if (!tbody) return;

  // Clean up selected task IDs that no longer exist
  const currentIds = new Set(state.tasks.map(t => t.task_id));
  for (const id of selectedTaskIds) {
    if (!currentIds.has(id)) {
      selectedTaskIds.delete(id);
    }
  }

  if (!state.tasks.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px;font-weight:500;">No downloads in queue</td></tr>`;
    if (activeCountEl) activeCountEl.textContent = "0";
    updateSelectionUI();
    if (window.lucide) window.lucide.createIcons();
    return;
  }

  const active = state.tasks.filter(t => t.status === "downloading").length;
  if (activeCountEl) activeCountEl.textContent = String(active);

  tbody.innerHTML = state.tasks.map(t => {
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
        <td>
          <div class="progress-ring">
            <svg width="36" height="36">
              <circle class="bg" cx="18" cy="18" r="${r}" fill="none" stroke-width="3"/>
              <circle class="fg" cx="18" cy="18" r="${r}" fill="none" stroke-width="3"
                stroke-dasharray="${c}" stroke-dashoffset="${off}"/>
            </svg>
            <span>${Math.round(t.progress)}%</span>
          </div>
        </td>
        <td title="${t.url}" style="font-weight:600;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(t.title || t.url)}</td>
        <td>${fmtBytes(t.total_bytes || t.downloaded_bytes)}</td>
        <td>${fmtSpeed(t.speed)}</td>
        <td>${fmtETA(t.eta)}</td>
        <td class="status-col"><span class="badge ${t.status}">${badgeIcon}${t.status}</span></td>
        <td title="${loc}" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:11px;">${escapeHtml(loc)}</td>
      </tr>`;
  }).join("");

  updateSelectionUI();
}

function renderSettingsForm(state) {
  const conc = document.getElementById("set-concurrency");
  const merge = document.getElementById("set-merge");
  const path = document.getElementById("set-default-path");

  if (conc && document.activeElement !== conc) {
    conc.value = state.settings.max_concurrent_downloads || 3;
  }
  if (merge && document.activeElement !== merge) {
    merge.value = state.settings.merge_output_format || "mp4";
  }
  if (path && document.activeElement !== path) {
    path.value = state.settings.default_download_path || "";
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
  const isFinished = task.status === "completed";
  
  let html = "";
  
  if (isDownloading) {
    html += `<button class="context-menu-item" onclick="window.uiPauseTask('${taskId}')"><i data-lucide="pause"></i> Pause</button>`;
  } else if (isPaused) {
    html += `<button class="context-menu-item" onclick="window.uiResumeTask('${taskId}')"><i data-lucide="play"></i> Resume</button>`;
  } else if (task.status === "cancelled" || task.status === "error") {
    html += `<button class="context-menu-item" onclick="window.uiResumeTask('${taskId}')"><i data-lucide="refresh-cw"></i> Restart / Resume</button>`;
  }

  html += `<button class="context-menu-item" onclick="window.uiRevealTask('${taskId}')"><i data-lucide="folder-open"></i> Reveal in Finder</button>`;
  
  html += `<div class="context-menu-divider"></div>`;
  html += `<button class="context-menu-item" onclick="window.uiRemoveTaskOnly('${taskId}')"><i data-lucide="trash-2"></i> Remove from list</button>`;
  html += `<button class="context-menu-item danger" onclick="window.uiDeleteTaskAndFile('${taskId}')"><i data-lucide="file-x"></i> Delete File</button>`;

  menu.innerHTML = html;
  menu.style.display = "flex";
  
  // Position menu
  const menuWidth = 190;
  const menuHeight = menu.offsetHeight || 200;
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

      const payload = {
        max_concurrent_downloads: concEl ? parseInt(concEl.value, 10) : 3,
        merge_output_format: mergeEl ? mergeEl.value : "mp4",
        default_download_path: pathEl ? pathEl.value : "",
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
}
