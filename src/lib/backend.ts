/**
 * Websocket client for the DownloadAnything yt-dlp backend.
 *
 * Singleton shared by the whole dashboard: maintains the connection with
 * reconnect backoff, tracks the jobs map and settings snapshot, and fans
 * events out to subscribers (see `useBackend`).
 */

export const BACKEND_URL = "ws://127.0.0.1:8765";

export interface PresetPath {
	name: string;
	path: string;
}

export interface Settings {
	downloadDir: string;
	presetPaths: PresetPath[];
	rateLimit: number;
	concurrentFragments: number;
	retries: number;
	proxy: string;
	cookiesFromBrowser: string;
	addMetadata: boolean;
	writeThumbnail: boolean;
	writeSubs: boolean;
	mergeOutputFormat: string;
	maxConcurrentDownloads: number;
	aria2NextConnections: number;
	aria2NextMaxConcurrent: number;
	aria2NextMinSplitSize: string;
	aria2NextFileAllocation: string;
	aria2NextExtraArgs: string;
	torrentListenPort: number;
	torrentEnableDht: boolean;
	torrentEnableLsd: boolean;
	torrentEnableUpnp: boolean;
	torrentEnableNatpmp: boolean;
	torrentEnablePex: boolean;
	torrentMaxConnections: number;
	torrentUploadLimit: number;
	torrentMetadataTimeout: number;
}

export type JobStatus =
	| "queued"
	| "downloading"
	| "paused"
	| "postprocessing"
	| "completed"
	| "failed"
	| "cancelled";

export interface Job {
	id: string;
	url: string;
	title: string;
	filename: string;
	thumbnail: string;
	directory: string;
	tempDirectory?: string;
	finalFilepath?: string;
	engine: string;
	status: JobStatus;
	percent: number;
	downloaded: number;
	total: number | null;
	speed: number | null;
	eta: number | null;
	segmentsDone: number | null;
	segmentsTotal: number | null;
	error: string | null;
	createdAt: number;
	revision?: number;
	parentId?: string;
	isPlaylist?: boolean;
	childIds?: string[];
	playlistCount?: number | null;
}

export interface FormatRow {
	id: string;
	selector: string;
	label: string;
	resolution: string;
	ext: string;
	tbr: number | null;
	fps: number | null;
	vcodec: string;
	acodec: string;
	kind: "video" | "audio";
	size: number | null;
	sizeIsEstimate: boolean;
	url?: string;
}

export interface PlaylistEntry {
	id: string;
	title: string;
	uploader: string;
	duration: number | null;
	thumbnail: string;
	webpageUrl: string;
	url: string;
	index: number;
}

export interface ProbeResult {
	id: string | null;
	title: string;
	uploader: string;
	duration: number | null;
	thumbnail: string;
	webpageUrl: string;
	extractor: string;
	isPlaylist: boolean;
	playlistCount: number | null;
	filename: string;
	formats: FormatRow[];
	entries?: PlaylistEntry[];
	playlistTitle?: string;
	isVideoInPlaylist?: boolean;
	currentEntryId?: string | null;
}

export interface TorrentFile {
	path: string;
	size: number;
}

export interface TorrentInfo {
	name: string;
	infoHash: string;
	totalSize: number;
	fileCount: number;
	files: TorrentFile[];
	trackers: string[];
	magnet: string;
}

export interface ProbeResponse {
	ok: boolean;
	engine: "ytdlp" | "extractor" | "torrent" | "none";
	url?: string;
	result?: ProbeResult;
	torrent?: TorrentInfo;
	error?: string;
}

export interface DownloadResponse {
	ok: boolean;
	jobId?: string;
	error?: string;
}

type Listener = () => void;

const REQUEST_TIMEOUT_MS = 120000;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isJob(value: unknown): value is Job {
	if (!isRecord(value) || typeof value.id !== "string") return false;
	if (value.parentId !== undefined && typeof value.parentId !== "string")
		return false;
	if (value.isPlaylist !== undefined && typeof value.isPlaylist !== "boolean")
		return false;
	if (value.childIds !== undefined && !Array.isArray(value.childIds))
		return false;
	if (
		value.playlistCount !== undefined &&
		value.playlistCount !== null &&
		typeof value.playlistCount !== "number"
	)
		return false;
	return true;
}

function isPresetPath(value: unknown): value is PresetPath {
	return (
		isRecord(value) &&
		typeof value.name === "string" &&
		typeof value.path === "string"
	);
}

function isSettings(value: unknown): value is Settings {
	if (
		!isRecord(value) ||
		!Array.isArray(value.presetPaths) ||
		!value.presetPaths.every(isPresetPath)
	) {
		return false;
	}
	const stringKeys = [
		"downloadDir",
		"proxy",
		"cookiesFromBrowser",
		"mergeOutputFormat",
		"aria2NextMinSplitSize",
		"aria2NextFileAllocation",
		"aria2NextExtraArgs",
	];
	const numberKeys = [
		"rateLimit",
		"concurrentFragments",
		"retries",
		"maxConcurrentDownloads",
		"aria2NextConnections",
		"aria2NextMaxConcurrent",
		"torrentListenPort",
		"torrentMaxConnections",
		"torrentUploadLimit",
		"torrentMetadataTimeout",
	];
	const booleanKeys = [
		"addMetadata",
		"writeThumbnail",
		"writeSubs",
		"torrentEnableDht",
		"torrentEnableLsd",
		"torrentEnableUpnp",
		"torrentEnableNatpmp",
		"torrentEnablePex",
	];
	return (
		stringKeys.every((key) => typeof value[key] === "string") &&
		numberKeys.every((key) => typeof value[key] === "number") &&
		booleanKeys.every((key) => typeof value[key] === "boolean")
	);
}

class BackendClient {
	connected = false;
	settings: Settings | null = null;
	jobs: Job[] = [];

	private ws: WebSocket | null = null;
	private reqCounter = 0;
	private reconnectDelay = 1000;
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private pending = new Map<
		string,
		{
			resolve: (value: Record<string, unknown>) => void;
			timer: ReturnType<typeof setTimeout>;
		}
	>();
	private listeners = new Set<Listener>();

	subscribe(listener: Listener): () => void {
		this.listeners.add(listener);
		return () => this.listeners.delete(listener);
	}

	private notify() {
		for (const listener of this.listeners) listener();
	}

	connect() {
		if (this.ws && this.ws.readyState !== WebSocket.CLOSED) return;
		let ws: WebSocket;
		try {
			ws = new WebSocket(BACKEND_URL);
		} catch {
			this.scheduleReconnect();
			return;
		}
		this.ws = ws;

		ws.onopen = () => {
			if (this.ws !== ws) return;
			this.connected = true;
			this.reconnectDelay = 1000;
			ws.send(JSON.stringify({ type: "hello", client: "dashboard" }));
			this.notify();
		};
		ws.onmessage = (event) => {
			if (this.ws === ws) this.onMessage(event);
		};
		ws.onclose = () => {
			if (this.ws !== ws) return;
			this.ws = null;
			if (this.connected) {
				this.connected = false;
				this.notify();
			}
			for (const [, p] of this.pending) {
				clearTimeout(p.timer);
				p.resolve({ ok: false, error: "Backend connection lost" });
			}
			this.pending.clear();
			this.scheduleReconnect();
		};
		ws.onerror = () => {
			// onclose handles reconnect
		};
	}

	private scheduleReconnect() {
		if (this.reconnectTimer) return;
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
			this.connect();
		}, this.reconnectDelay);
	}

	private onMessage(event: MessageEvent) {
		let message: Record<string, unknown>;
		try {
			const parsed: unknown = JSON.parse(String(event.data));
			if (!isRecord(parsed)) return;
			message = parsed;
		} catch {
			return;
		}
		const reqId = typeof message.reqId === "string" ? message.reqId : undefined;
		const pending = reqId ? this.pending.get(reqId) : undefined;
		if (reqId && pending) {
			this.pending.delete(reqId);
			clearTimeout(pending.timer);
			pending.resolve(message);
			return;
		}

		switch (message.type) {
			case "hello":
				this.settings = isSettings(message.settings) ? message.settings : null;
				this.jobs = Array.isArray(message.jobs)
					? message.jobs.filter(isJob)
					: [];
				this.notify();
				break;
			case "settings":
				this.settings = isSettings(message.settings) ? message.settings : null;
				this.notify();
				break;
			case "job_update": {
				if (!isJob(message.job)) break;
				const job = message.job;
				const index = this.jobs.findIndex((j) => j.id === job.id);
				if (index >= 0) {
					const current = this.jobs[index];
					if (
						current.revision !== undefined &&
						job.revision !== undefined &&
						job.revision < current.revision
					)
						return;
					this.jobs[index] = job;
				} else this.jobs.unshift(job);
				this.jobs = [...this.jobs];
				this.notify();
				break;
			}
			case "job_removed":
				this.jobs = this.jobs.filter((j) => j.id !== message.jobId);
				this.notify();
				break;
		}
	}

	private request<T>(payload: Record<string, unknown>): Promise<T> {
		const ws = this.ws;
		if (!ws || ws.readyState !== WebSocket.OPEN) {
			return Promise.resolve({
				ok: false,
				error: "Backend is not connected",
			} as T);
		}
		const reqId = `dash-${Date.now()}-${++this.reqCounter}`;
		return new Promise<T>((resolve) => {
			const timer = setTimeout(() => {
				this.pending.delete(reqId);
				resolve({ ok: false, error: "Request timed out" } as T);
			}, REQUEST_TIMEOUT_MS);
			this.pending.set(reqId, {
				resolve: resolve as (v: Record<string, unknown>) => void,
				timer,
			});
			try {
				ws.send(JSON.stringify({ ...payload, reqId }));
			} catch (error) {
				clearTimeout(timer);
				this.pending.delete(reqId);
				resolve({
					ok: false,
					error:
						error instanceof Error ? error.message : "Could not send request",
				} as T);
			}
		});
	}

	probe(url: string, fallbackUrl?: string) {
		return this.request<ProbeResponse & { reqId: string }>({
			type: "probe",
			url,
			fallbackUrl,
		});
	}

	download(payload: {
		url: string;
		formatId?: string;
		directory?: string;
		selectedFiles?: string[];
		filename?: string;
		title?: string;
		thumbnail?: string;
		engine?: string;
		downloadPlaylist?: boolean;
		selectedEntryUrls?: string[];
	}) {
		return this.request<DownloadResponse>({ type: "download", ...payload });
	}

	setSettings(partial: Partial<Settings>) {
		return this.request<{ ok?: boolean; error?: string }>({
			type: "settings_set",
			settings: partial,
		});
	}

	jobAction(jobId: string, action: "cancel" | "remove" | "pause" | "resume") {
		return this.request<{ ok?: boolean; error?: string }>({
			type: "job_action",
			jobId,
			action,
		});
	}

	clearFinished() {
		return this.request<{ ok?: boolean }>({ type: "clear_finished" });
	}

	reveal(jobId: string) {
		return this.request<{ ok?: boolean; error?: string }>({
			type: "reveal",
			jobId,
		});
	}
}

export const backend = new BackendClient();
