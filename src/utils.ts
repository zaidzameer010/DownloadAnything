export function formatBytes(bytes: number): string {
	if (bytes <= 0) return "\u2014";
	if (bytes >= 1024 * 1024 * 1024)
		return `${(bytes / 1024 / 1024 / 1024).toFixed(3)} GB`;
	if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(3)} MB`;
	if (bytes >= 1024) return `${(bytes / 1024).toFixed(3)} KB`;
	return `${bytes} B`;
}

export function formatSpeed(bytesPerSec: number): string {
	if (bytesPerSec <= 0) return "0.0 KB/s";
	if (bytesPerSec >= 1024 * 1024)
		return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`;
	if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`;
	return `${bytesPerSec.toFixed(0)} B/s`;
}

export function formatDuration(sec?: number): string {
	if (!sec || sec <= 0) return "00:00";
	const h = Math.floor(sec / 3600);
	const m = Math.floor((sec % 3600) / 60);
	const s = Math.round(sec % 60);

	const pad = (n: number) => n.toString().padStart(2, "0");

	if (h > 0) {
		return `${h}:${pad(m)}:${pad(s)}`;
	}
	return `${m}:${pad(s)}`;
}

export function getSessionTabId(): number {
	const key = "downloadanything_tab_id";
	try {
		let id = sessionStorage.getItem(key);
		if (!id) {
			id = Math.floor(100000 + Math.random() * 900000).toString();
			sessionStorage.setItem(key, id);
		}
		return parseInt(id, 10);
	} catch {
		return Math.floor(100000 + Math.random() * 900000);
	}
}

export const CLIENT_VERSION = import.meta.env.APP_VERSION || "0.0.0";
export const DEFAULT_SERVER_URL = "ws://127.0.0.1:8765/ws";
