const LEVELS = {
	debug: 0,
	info: 1,
	warn: 2,
	error: 3,
};

const DEFAULT_LEVEL = "warn";

function resolveLevel() {
	if (typeof globalThis !== "undefined" && globalThis.__DMA_LOG_LEVEL__) {
		const envLevel = globalThis.__DMA_LOG_LEVEL__;
		if (envLevel in LEVELS) return envLevel;
	}

	try {
		const stored = localStorage.getItem("logLevel");
		if (stored && stored in LEVELS) return stored;
	} catch {
		// localStorage may be unavailable (e.g. service worker).
	}

	return DEFAULT_LEVEL;
}

let currentLevel = resolveLevel();

export function setLogLevel(level) {
	currentLevel = level;
	try {
		localStorage.setItem("logLevel", level);
	} catch {
		// Ignore storage errors.
	}
}

export function getLogLevel() {
	return currentLevel;
}

function isEnabled(level) {
	return LEVELS[level] >= LEVELS[currentLevel];
}

function emit(namespace, level, ...args) {
	if (!isEnabled(level)) return;

	const prefix = `[${namespace}][${level.toUpperCase()}]`;
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

export function createLogger(namespace = "ext") {
	return {
		debug: (...args) => emit(namespace, "debug", ...args),
		info: (...args) => emit(namespace, "info", ...args),
		warn: (...args) => emit(namespace, "warn", ...args),
		error: (...args) => emit(namespace, "error", ...args),
		setLevel: setLogLevel,
		get level() {
			return currentLevel;
		},
	};
}

export const logger = createLogger("downloadanything");
