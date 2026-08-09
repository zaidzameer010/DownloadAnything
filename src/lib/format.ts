export function formatBytes(bytes: number | null | undefined): string {
	if (!bytes || bytes <= 0) return "—";
	const units = ["B", "KB", "MB", "GB", "TB"];
	let value = bytes;
	let unit = 0;
	while (value >= 1000 && unit < units.length - 1) {
		value /= 1000;
		unit += 1;
	}
	return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
	if (!seconds || seconds <= 0) return "";
	const total = Math.round(seconds);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = total % 60;
	const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
	return `${h > 0 ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

export function formatSpeed(bytesPerSec: number | null | undefined): string {
	return bytesPerSec ? `${formatBytes(bytesPerSec)}/s` : "";
}

export function formatEta(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined || !Number.isFinite(seconds))
		return "";
	let total = Math.max(0, Math.round(seconds));
	if (total === 0) return "0s";

	const days = Math.floor(total / 86400);
	total %= 86400;
	const hours = Math.floor(total / 3600);
	total %= 3600;
	const minutes = Math.floor(total / 60);
	const remainingSeconds = total % 60;

	const parts: string[] = [];
	if (days > 0) parts.push(`${days}d`);
	if (hours > 0) parts.push(`${hours}h`);
	if (minutes > 0) parts.push(`${minutes}m`);
	if (remainingSeconds > 0) parts.push(`${remainingSeconds}s`);
	if (parts.length === 0) return "0s";
	return parts.slice(0, 2).join(" ");
}
