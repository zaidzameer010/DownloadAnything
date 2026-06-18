/**
 * state.js — Reactive Global State Container
 */
import { connectWebSocket, fetchSettings, fetchHealth, checkBinaries, installBinaries } from "./api.js";

const state = {
  tasks: [],
  settings: {},
  health: { active_workers: "—", yt_dlp_version: "—" },
  online: true,
  activeTab: "downloads",
  // Onboarding state
  onboarding: {
    visible: false,
    ffmpeg: { status: "checking", progress: 0 },
    ytdlp: { status: "checking", progress: 0 },
    installing: false,
    error: null
  }
};

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  fn(state); // immediate invoke with current state
  return () => listeners.delete(fn);
}

export function notify() {
  for (const fn of listeners) {
    try { fn(state); } catch (e) { console.error("State listener error:", e); }
  }
}

export function getState() {
  return state;
}

export function updateSettings(newSettings) {
  state.settings = newSettings;
  notify();
}

// ── WebSocket Connection ───────────────────────────────────────────────────
let wsConnection = null;

export function connectStateWebSocket() {
  if (wsConnection) {
    wsConnection.close();
  }

  const onOpen = async () => {
    state.online = true;
    notify();
    try {
      const res = await checkBinaries();
      state.onboarding.ffmpeg.status = res.ffmpeg ? "installed" : "missing";
      state.onboarding.ytdlp.status = res.ytdlp ? "installed" : "missing";
      state.onboarding.visible = !res.ffmpeg || !res.ytdlp;
      notify();
    } catch (err) {
      console.error("Binary check failed:", err);
    }
  };

  const onMessage = (msg) => {
    if (msg.type === "tasks") {
      state.tasks = msg.data || [];
      if (msg.health) {
        state.health = msg.health;
      }
      if (msg.settings) {
        state.settings = msg.settings;
      }
      state.online = true;
      notify();
    } else if (msg.type === "onboarding") {
      const { binary, status: bStatus, progress } = msg;
      if (state.onboarding[binary]) {
        state.onboarding[binary].status = bStatus;
        state.onboarding[binary].progress = progress;
        notify();
      }
    }
  };

  const onClose = () => {
    wsConnection = null;
    state.online = false;
    state.health = { active_workers: "offline", yt_dlp_version: "—" };
    notify();
    setTimeout(connectStateWebSocket, 2500); // retry connect
  };

  wsConnection = connectWebSocket(onOpen, onMessage, onClose);
}

// ── Onboarding Installer Actions ───────────────────────────────────────────
export async function triggerBinaryInstall() {
  if (state.onboarding.installing) return;
  state.onboarding.installing = true;
  state.onboarding.error = null;

  if (state.onboarding.ffmpeg.status === "missing") {
    state.onboarding.ffmpeg.status = "downloading";
    state.onboarding.ffmpeg.progress = 0;
  }
  if (state.onboarding.ytdlp.status === "missing") {
    state.onboarding.ytdlp.status = "downloading";
    state.onboarding.ytdlp.progress = 0;
  }
  notify();

  try {
    await installBinaries();
    state.onboarding.ffmpeg.status = "installed";
    state.onboarding.ffmpeg.progress = 100;
    state.onboarding.ytdlp.status = "installed";
    state.onboarding.ytdlp.progress = 100;
    state.onboarding.installing = false;
    notify();

    setTimeout(() => {
      state.onboarding.visible = false;
      notify();
    }, 1200);
  } catch (err) {
    state.onboarding.installing = false;
    state.onboarding.error = err.message || "Installation failed";
    
    // Reset failed components to missing so user can retry
    if (state.onboarding.ffmpeg.status !== "installed") {
      state.onboarding.ffmpeg.status = "missing";
    }
    if (state.onboarding.ytdlp.status !== "installed") {
      state.onboarding.ytdlp.status = "missing";
    }
    notify();
  }
}

// ── Health & Initialisation loaders (Dead — handled via WebSocket push) ───
// Kept as empty exports so any stale import references don't crash.
export async function pollHealthState() {}
export async function loadInitialSettings() {}

export function switchTab(tabName) {
  if (state.activeTab === tabName) return;
  state.activeTab = tabName;
  notify();
}
