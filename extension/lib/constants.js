// Shared extension constants. Single source of truth for backend address and
// version so offscreen, service-worker, and content scripts do not drift.

export const SERVER_HOST = "127.0.0.1:8765";
export const WS_URL = `ws://${SERVER_HOST}/ws`;
export const PING_URL = `http://${SERVER_HOST}/ping`;

export function getClientVersion() {
	try {
		return chrome.runtime.getManifest().version || "0.0.0";
	} catch {
		return "0.0.0";
	}
}
