import { getClientVersion } from "./constants.js";
import { createLogger } from "./logger.js";

const logger = createLogger("ws-client");

const MAX_QUEUED_MESSAGES = 100;

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
		if (
			this.ws &&
			(this.ws.readyState === WebSocket.OPEN ||
				this.ws.readyState === WebSocket.CONNECTING)
		) {
			return;
		}

		this.isConnecting = true;

		const wsUrl = this.url;
		let socket;
		try {
			socket = new WebSocket(wsUrl);
			this.ws = socket;
		} catch (error) {
			logger.error("WS instantiation failed:", error);
			this.handleClose();
			return;
		}

		socket.onopen = () => {
			if (this.ws !== socket) return;
			this.reconnectAttempts = 0;
			this.isConnecting = false;
			clearTimeout(this._retryTimer);
			this._retryTimer = null;

			try {
				socket.send(
					JSON.stringify({
						type: "hello",
						clientVersion: getClientVersion(),
						tabId: this.tabId,
					}),
				);
			} catch (error) {
				logger.warn("Failed to send WebSocket handshake:", error);
				socket.close();
				return;
			}

			this.startHeartbeat();

			while (
				this.sendQueue.length > 0 &&
				this.ws === socket &&
				socket.readyState === WebSocket.OPEN
			) {
				const payload = this.sendQueue.shift();
				try {
					socket.send(JSON.stringify(payload));
				} catch (error) {
					logger.warn("Failed to flush queued WebSocket payload:", error);
					this.sendQueue.unshift(payload);
					socket.close();
					break;
				}
			}

			if (this.onOpen) this.onOpen();
		};

		socket.onmessage = (event) => {
			if (this.ws !== socket) return;
			try {
				const payload = JSON.parse(event.data);
				if (payload.type === "pong") return;
				if (!validateServerMessage(payload)) {
					logger.warn("Invalid server payload ignored:", payload);
					return;
				}
				if (this.onMessage) this.onMessage(payload);
			} catch (error) {
				logger.error("Failed to parse WS payload:", error, event.data);
			}
		};

		socket.onclose = () => {
			if (this.ws !== socket) return;
			this.handleClose();
		};

		socket.onerror = (error) => {
			if (this.ws === socket) {
				logger.error("WebSocket connection error:", error);
			}
		};
	}

	startHeartbeat() {
		this.stopHeartbeat();
		this.pingInterval = setInterval(() => {
			this.send({
				type: "ping",
				ts: Date.now(),
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
			if (this.sendQueue.length >= MAX_QUEUED_MESSAGES) {
				this.sendQueue.shift();
			}
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
	"hello",
	"probe_started",
	"probe_result",
	"probe_failed",
	"download_queued",
	"download_progress",
	"download_completed",
	"download_failed",
	"download_canceled",
	"pong",
	"jobs_list",
	"categories_list",
	"settings_data",
	"browse_failed",
	"directory_selected",
	"duplicate_job_alert",
	"file_exists_result",
	"needs_refresh",
]);

function validateServerMessage(payload) {
	if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
		return false;
	}
	if (
		typeof payload.type !== "string" ||
		!VALID_SERVER_MESSAGE_TYPES.has(payload.type)
	) {
		return false;
	}

	const type = payload.type;
	const jobMessageTypes = new Set([
		"probe_started",
		"probe_result",
		"probe_failed",
		"download_queued",
		"download_progress",
		"download_completed",
		"download_failed",
		"download_canceled",
		"duplicate_job_alert",
		"file_exists_result",
		"needs_refresh",
	]);
	if (jobMessageTypes.has(type)) {
		if (typeof payload.jobId !== "string") return false;
	}

	return true;
}
