/**
 * api.js — API Client Wrapper (WebSocket edition)
 */
export const API = "http://127.0.0.1:8000";

let ws = null;
const pendingRequests = new Map();
let requestCounter = 0;

export function connectWebSocket(onMessage, onClose) {
  const loc = window.location;
  const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
  const host = API ? API.replace(/^https?:\/\//, "") : loc.host;
  const wsUrl = `${wsProto}//${host}/ws/progress`;

  ws = new WebSocket(wsUrl);

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "tasks") {
        onMessage(msg);
      } else if (msg.type === "response") {
        const { request_id, ok, data, error } = msg;
        if (pendingRequests.has(request_id)) {
          const { resolve, reject } = pendingRequests.get(request_id);
          pendingRequests.delete(request_id);
          if (ok) {
            resolve(data);
          } else {
            reject(new Error(error || "Request failed"));
          }
        }
      }
    } catch (e) {
      console.error("WS message parse error:", e);
    }
  };

  ws.onclose = () => {
    ws = null;
    onClose();
  };

  ws.onerror = () => {
    if (ws) ws.close();
  };

  return ws;
}

function sendWSRequest(action, payload = {}) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reject(new Error("WebSocket is not connected"));
      return;
    }
    const requestId = `web-${Date.now()}-${requestCounter++}`;
    pendingRequests.set(requestId, { resolve, reject });
    ws.send(JSON.stringify({ action, request_id: requestId, payload }));
  });
}

export async function fetchSettings() {
  return sendWSRequest("get_settings");
}

export async function saveSettings(payload) {
  return sendWSRequest("save_settings", payload);
}

export async function fetchHealth() {
  return sendWSRequest("get_health");
}

export async function cancelTask(id) {
  return sendWSRequest("cancel", { task_id: id });
}

export async function deleteTask(id, deleteFile = false) {
  return sendWSRequest("delete", { task_id: id, delete_file: deleteFile });
}

export async function pauseTask(id) {
  return sendWSRequest("pause", { task_id: id });
}

export async function resumeTask(id) {
  return sendWSRequest("resume", { task_id: id });
}

export async function revealTask(id) {
  return sendWSRequest("reveal", { task_id: id });
}
