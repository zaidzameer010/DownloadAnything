/**
 * api.js — API Client Wrapper (WebSocket edition)
 */
export const API = "http://127.0.0.1:8000";

let ws = null;
const pendingRequests = new Map(); // requestId -> { resolve, reject, timeoutId }
let requestCounter = 0;

const WS_REQUEST_TIMEOUT_MS = 30_000;

export function connectWebSocket(onOpen, onMessage, onClose) {
  const loc = window.location;
  const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
  const host = API ? API.replace(/^https?:\/\//, "") : loc.host;
  const wsUrl = `${wsProto}//${host}/ws/progress`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    if (onOpen) onOpen();
  };

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "tasks") {
        onMessage(msg);
      } else if (msg.type === "response") {
        const { request_id, ok, data, error } = msg;
        const pending = pendingRequests.get(request_id);
        if (pending) {
          clearTimeout(pending.timeoutId);
          pendingRequests.delete(request_id);
          if (ok) {
            pending.resolve(data);
          } else {
            pending.reject(new Error(error || "Request failed"));
          }
        }
      }
    } catch (e) {
      console.error("WS message parse error:", e);
    }
  };

  ws.onclose = () => {
    // Reject all in-flight requests so callers never hang indefinitely
    for (const [, pending] of pendingRequests) {
      clearTimeout(pending.timeoutId);
      pending.reject(new Error("WebSocket disconnected"));
    }
    pendingRequests.clear();
    ws = null;
    onClose();
  };

  ws.onerror = () => {
    // onclose fires immediately after onerror; cleanup happens there
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

    // Per-request timeout so promises never leak on silent server failures
    const timeoutId = setTimeout(() => {
      if (pendingRequests.has(requestId)) {
        pendingRequests.delete(requestId);
        reject(new Error("Request timed out"));
      }
    }, WS_REQUEST_TIMEOUT_MS);

    pendingRequests.set(requestId, { resolve, reject, timeoutId });
    ws.send(JSON.stringify({ action, request_id: requestId, payload }));
  });
}

export async function saveSettings(payload) {
  return sendWSRequest("save_settings", payload);
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


