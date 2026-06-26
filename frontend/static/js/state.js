/**
 * state.js — Reactive Global State Container
 */
import { connectWebSocket } from "./api.js";

const state = {
  tasks: [],
  settings: {},
  health: { active_workers: "—", yt_dlp_version: "—" },
  online: true,
  activeTab: "downloads",

  // Backend startup state
  backend: {
    ready: typeof window !== "undefined" && !window.__TAURI__,
    logs: []
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
    state.backend.ready = true;
    notify();
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




export function switchTab(tabName) {
  if (state.activeTab === tabName) return;
  state.activeTab = tabName;
  notify();
}

// ── Tauri Backend Log Listener ──────────────────────────────────────────────
if (typeof window !== "undefined" && window.__TAURI__ && window.__TAURI__.event) {
  window.__TAURI__.event.listen('backend-log', (event) => {
    state.backend.logs.push(event.payload);
    // Keep only last 100 console log lines to avoid ballooning memory
    if (state.backend.logs.length > 100) {
      state.backend.logs.shift();
    }
    notify();
  });
}
