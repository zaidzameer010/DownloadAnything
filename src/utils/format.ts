export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || isNaN(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${value.toFixed(1)} ${units[index]}`;
}

export function fmtSpeed(speedBytesPerSec: number | null | undefined): string {
  if (speedBytesPerSec === null || speedBytesPerSec === undefined || isNaN(speedBytesPerSec)) return "—";
  return `${fmtBytes(speedBytesPerSec)}/s`;
}

export function fmtETA(etaSeconds: number | null | undefined): string {
  if (etaSeconds === null || etaSeconds === undefined || isNaN(etaSeconds)) return "—";
  if (etaSeconds < 60) return `${Math.round(etaSeconds)}s`;
  if (etaSeconds < 3600) return `${Math.round(etaSeconds / 60)}m`;
  return `${Math.round(etaSeconds / 3600)}h`;
}

export function escapeHtml(str: string | null | undefined): string {
  if (!str) return "";
  return String(str).replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#39;";
      default:
        return char;
    }
  });
}
