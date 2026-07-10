export class WSClient {
  constructor(url, tabId, onMessage, onClose, onOpen) {
    this.url = url;
    this.tabId = tabId;
    this.onMessage = onMessage;
    this.onClose = onClose;
    this.onOpen = onOpen;
    
    this.ws = null;
    this.reconnectAttempts = 0;
    this.maxDelay = 4000; // Cap backoff at 4 seconds
    this.pingInterval = null;
    this.isConnecting = false;
    this.sendQueue = [];
    this._retryTimer = null;
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    this.isConnecting = true;
    console.log(`Connecting WebSocket for tab ${this.tabId} to ${this.url}`);
    
    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      console.error("WS instantiation failed:", err);
      this.handleClose();
      return;
    }

    this.ws.onopen = () => {
      console.log("WebSocket connection established");
      this.reconnectAttempts = 0;
      this.isConnecting = false;
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
      
      // Perform handshake
      this.ws.send(JSON.stringify({
        type: "hello",
        clientVersion: "1.0.0",
        tabId: this.tabId
      }));
      
      // Start KeepAlive pings (20 seconds interval)
      this.startHeartbeat();

      // Flush send queue
      while (this.sendQueue.length > 0) {
        const payload = this.sendQueue.shift();
        console.log("Flushing queued payload to WebSocket:", payload);
        this.ws.send(JSON.stringify(payload));
      }

      if (this.onOpen) this.onOpen();
    };

    this.ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "pong") {
          // Heartbeat ack, swallow
          return;
        }
        if (!validateServerMessage(payload)) {
          console.warn("Invalid server payload ignored:", payload);
          return;
        }
        if (this.onMessage) this.onMessage(payload);
      } catch (err) {
        console.error("Failed to parse WS payload:", err, event.data);
      }
    };

    this.ws.onclose = () => {
      console.log("WebSocket connection closed");
      this.handleClose();
    };

    this.ws.onerror = (err) => {
      console.error("WebSocket connection error:", err);
    };
  }

  startHeartbeat() {
    this.stopHeartbeat();
    this.pingInterval = setInterval(() => {
      this.send({
        type: "ping",
        ts: Date.now()
      });
    }, 20 * 1000);
  }

  stopHeartbeat() {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  handleClose() {
    this.stopHeartbeat();
    this.isConnecting = false;
    if (this.onClose) this.onClose();

    const delay = Math.min(this.maxDelay, 250 * 2 ** this.reconnectAttempts++);
    clearTimeout(this._retryTimer);
    this._retryTimer = setTimeout(() => this.connect(), delay);
  }

  send(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    } else {
      console.log("WebSocket is not yet open. Queueing payload:", data);
      this.sendQueue.push(data);
    }
  }

  disconnect() {
    this.stopHeartbeat();
    clearTimeout(this._retryTimer);
    this._retryTimer = null;
    if (this.ws) {
      // Clear event handlers to prevent reconnect loop on intentional close
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.onmessage = null;
      this.ws.close();
      this.ws = null;
    }
    this.sendQueue = [];
  }
}

const VALID_SERVER_MESSAGE_TYPES = new Set([
  "hello", "probe_started", "probe_result", "probe_failed", "download_queued",
  "download_progress", "download_completed", "download_failed", "download_canceled",
  "pong", "jobs_list", "categories_list", "directory_contents", "settings_data",
  "browse_failed", "directory_selected", "duplicate_job_alert", "file_exists_result"
]);

function validateServerMessage(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  if (typeof payload.type !== "string" || !VALID_SERVER_MESSAGE_TYPES.has(payload.type)) {
    return false;
  }
  
  const type = payload.type;
  if (["probe_started", "probe_result", "probe_failed", "download_queued", "download_progress", 
       "download_completed", "download_failed", "download_canceled", "duplicate_job_alert", 
       "file_exists_result"].includes(type)) {
    if (typeof payload.jobId !== "string") return false;
  }
  
  return true;
}
