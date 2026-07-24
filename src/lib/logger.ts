export type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_RANK: Record<LogLevel, number> = {
	debug: 0,
	info: 1,
	warn: 2,
	error: 3,
};

const DEFAULT_DEV_LEVEL: LogLevel = "debug";
const DEFAULT_PROD_LEVEL: LogLevel = "warn";

function resolveLevel(): LogLevel {
	const envLevel = import.meta.env.VITE_LOG_LEVEL as string | undefined;
	if (envLevel && envLevel in LEVEL_RANK) {
		return envLevel as LogLevel;
	}

	try {
		const stored = localStorage.getItem("logLevel");
		if (stored && stored in LEVEL_RANK) {
			return stored as LogLevel;
		}
	} catch {
		// localStorage may be unavailable in some contexts.
	}

	return import.meta.env.DEV ? DEFAULT_DEV_LEVEL : DEFAULT_PROD_LEVEL;
}

let currentLevel: LogLevel = resolveLevel();

export function setLogLevel(level: LogLevel): void {
	currentLevel = level;
	try {
		localStorage.setItem("logLevel", level);
	} catch {
		// Ignore storage errors.
	}
}

export function getLogLevel(): LogLevel {
	return currentLevel;
}

function isEnabled(level: LogLevel): boolean {
	return LEVEL_RANK[level] >= LEVEL_RANK[currentLevel];
}

function emit(level: LogLevel, ...args: unknown[]): void {
	if (!isEnabled(level)) {
		return;
	}

	const prefix = `[${level.toUpperCase()}]`;
	switch (level) {
		case "error":
			console.error(prefix, ...args);
			break;
		case "warn":
			console.warn(prefix, ...args);
			break;
		case "info":
			console.info(prefix, ...args);
			break;
		default:
			console.log(prefix, ...args);
	}
}

export const logger = {
	debug: (...args: unknown[]) => emit("debug", ...args),
	info: (...args: unknown[]) => emit("info", ...args),
	warn: (...args: unknown[]) => emit("warn", ...args),
	error: (...args: unknown[]) => emit("error", ...args),
	setLevel: setLogLevel,
	get level() {
		return currentLevel;
	},
};
