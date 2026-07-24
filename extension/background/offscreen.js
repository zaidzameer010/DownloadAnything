import { WS_URL } from "../lib/constants.js";
import { WSClient } from "../lib/ws-client.js";

const OFFSCREEN_TAB_ID = -1;
let client = null;

function notifyServiceWorker(message) {
	chrome.runtime
		.sendMessage({ source: "offscreen", ...message })
		.catch(() => {});
}

function connectWS() {
	if (client) {
		client.disconnect();
	}

	client = new WSClient(
		WS_URL,
		OFFSCREEN_TAB_ID,
		(wsMsg) => {
			notifyServiceWorker({
				type: "WS_MESSAGE",
				payload: wsMsg,
			});
		},
		() => {
			notifyServiceWorker({ type: "WS_CLOSE" });
		},
		() => {
			notifyServiceWorker({ type: "WS_OPEN" });
		},
	);

	client.connect();
}

// Initialize connection
connectWS();

chrome.runtime.onMessage.addListener((message) => {
	if (
		!message ||
		typeof message !== "object" ||
		message.target !== "offscreen"
	) {
		return false;
	}
	if (message.target === "offscreen") {
		if (message.type === "SEND_WS") {
			if (client) {
				client.send(message.payload);
			}
		} else if (message.type === "RECONNECT") {
			connectWS();
		}
	}
	return false; // No async response needed
});
