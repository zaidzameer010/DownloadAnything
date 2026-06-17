/**
 * state.js — Reactive Global State Container
 */
import { connectWebSocket, fetchSettings, fetchHealth } from "./api.js";

const state = {
  tasks: [],
  settings: {},
  health: { active_workers: "—", yt_dlp_version: "—" },
  online: true,
  activeTab: "downloads",
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

  wsConnection = connectWebSocket(
    (msg) => {
      state.tasks = msg.data || [];
      if (msg.health) {
        state.health = msg.health;
      }
      if (msg.settings) {
        state.settings = msg.settings;
      }
      state.online = true;
      notify();
    },
    () => {
      wsConnection = null;
      state.online = false;
      state.health = { active_workers: "offline", yt_dlp_version: "—" };
      notify();
      setTimeout(connectStateWebSocket, 2500); // retry connect
    }
  );
}

// ── Health & Initialisation loaders (Deprecated — handled via WebSocket) ───────────
export async function pollHealthState() {
  // No-op: state is pushed automatically via WebSocket
}

export async function loadInitialSettings() {
  // No-op: state is pushed automatically via WebSocket
}

export function switchTab(tabName) {
  if (state.activeTab === tabName) return;
  state.activeTab = tabName;
  notify();
}
