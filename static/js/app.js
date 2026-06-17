/**
 * app.js — Dashboard Bootstrapper
 */
import { connectStateWebSocket, subscribe } from "./state.js";
import { renderDashboard, setupUIEventListeners } from "./ui.js";

(() => {
  // Bind settings saving and categories addition logic
  setupUIEventListeners();

  // Wire view renderer to notify and re-render on state mutation
  subscribe((state) => {
    renderDashboard(state);
  });

  // Establish WebSocket connection and begin state synchronization
  connectStateWebSocket();
})();
