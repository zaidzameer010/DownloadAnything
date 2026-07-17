export function formatBytes(bytes: number): string {
	if (bytes <= 0) return "\u2014";
	if (bytes >= 1024 * 1024 * 1024)
		return `${(bytes / 1024 / 1024 / 1024).toFixed(3)} GB`;
	if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(3)} MB`;
	if (bytes >= 1024) return `${(bytes / 1024).toFixed(3)} KB`;
	return `${bytes} B`;
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

export function formatETA(sec: number): string {
	if (sec <= 0) return "";
	const d = Math.floor(sec / 86400);
	const h = Math.floor((sec % 86400) / 3600);
	const m = Math.floor((sec % 3600) / 60);
	const s = Math.round(sec % 60);

	if (d > 0) return `${d}d ${h}h ${m}m`;
	if (h > 0) return `${h}h ${m}m ${s}s`;
	if (m > 0) return `${m}m ${s}s`;
	return `${s}s`;
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
