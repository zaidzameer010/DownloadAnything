import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { logger } from "../lib/logger";
import type {
	ActiveTab,
	BrowserInfo,
	Category,
	DuplicateFileAlert,
	DuplicateJobAlert,
	FilterTab,
	GenericAlert,
	Job,
	ProbedInfo,
	ServerInfo,
	SettingsSection,
} from "../types";
import { CLIENT_VERSION, DEFAULT_SERVER_URL, getSessionTabId } from "../utils";

interface TauriCore {
	invoke<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T>;
}

interface TauriWindow extends Window {
	__TAURI__?: { core: TauriCore };
}

const TAB_ID = getSessionTabId();

export interface DownloaderSettings {
	mergeFormat: string;
	embedThumbnail: boolean;
	embedSubs: boolean;
	cookiesFromBrowser: string;
	useAria2Next: boolean;
	aria2NextMaxConnections: number;
	aria2NextConcurrentDownloads: number;
	aria2NextSplit: number;
	aria2NextMinSplitSize: string;
	aria2NextPreallocate: boolean;
	aria2NextCheckCertificate: boolean;
	aria2NextAlwaysResume: boolean;
	concurrentFragmentDownloads: number;
	downloadRetries: number;
	fragmentRetries: number;
	rateLimit: string;
	subtitlesLangs: string;
	ffmpegLocation: string;
	torrentEnabled: boolean;
	torrentMaxActive: number;
	torrentDownloadLimit: number;
	torrentUploadLimit: number;
	torrentSeedRatio: number;
	torrentPeerLimit: number;
	torrentUploadPeerLimit: number;
}

export function useDownloader() {
	// Navigation
	const [activeTab, setActiveTab] = useState<ActiveTab>("downloads");
	const [activeSettingsSection, setActiveSettingsSection] =
		useState<SettingsSection>("general");
	const [filterTab, setFilterTab] = useState<FilterTab>("all");

	// Connection
	const [serverUrl, setServerUrl] = useState(DEFAULT_SERVER_URL);

	// Settings
	const [mergeFormat, setMergeFormat] = useState("mkv");
	const [embedThumbnail, setEmbedThumbnail] = useState(true);
	const [embedSubs, setEmbedSubs] = useState(false);
	const [cookiesFromBrowser, setCookiesFromBrowser] = useState("none");
	const [useAria2Next, setUseAria2Next] = useState(true);
	const [aria2NextMaxConnections, setAria2NextMaxConnections] = useState(16);
	const [aria2NextConcurrentDownloads, setAria2NextConcurrentDownloads] =
		useState(5);
	const [aria2NextSplit, setAria2NextSplit] = useState(16);
	const [aria2NextMinSplitSize, setAria2NextMinSplitSize] = useState("1M");
	const [aria2NextPreallocate, setAria2NextPreallocate] = useState(true);
	const [aria2NextCheckCertificate, setAria2NextCheckCertificate] =
		useState(true);
	const [aria2NextAlwaysResume, setAria2NextAlwaysResume] = useState(true);

	const [concurrentFragmentDownloads, setConcurrentFragmentDownloads] =
		useState(4);
	const [downloadRetries, setDownloadRetries] = useState(10);
	const [fragmentRetries, setFragmentRetries] = useState(10);
	const [rateLimit, setRateLimit] = useState("");
	const [subtitlesLangs, setSubtitlesLangs] = useState("all");
	const [ffmpegLocation, setFfmpegLocation] = useState("");
	const [torrentEnabled, setTorrentEnabled] = useState(true);
	const [torrentMaxActive, setTorrentMaxActive] = useState(4);
	const [torrentDownloadLimit, setTorrentDownloadLimit] = useState(0);
	const [torrentUploadLimit, setTorrentUploadLimit] = useState(0);
	const [torrentSeedRatio, setTorrentSeedRatio] = useState(2);
	const [torrentPeerLimit, setTorrentPeerLimit] = useState(500);
	const [torrentUploadPeerLimit, setTorrentUploadPeerLimit] = useState(20);

	// Categories
	const [categories, setCategories] = useState<Category[]>([]);
	const [newCatName, setNewCatName] = useState("");
	const [newCatPath, setNewCatPath] = useState("");

	// Server status
	const [isConnected, setIsConnected] = useState(false);
	const [serverInfo, setServerInfo] = useState<ServerInfo>({
		ytDlpVersion: "Unknown",
		ffmpegAvailable: false,
	});

	// Extension installer
	const [isExtModalOpen, setIsExtModalOpen] = useState(false);
	const [browsersList, setBrowsersList] = useState<BrowserInfo[]>([]);
	const [isLoadingBrowsers, setIsLoadingBrowsers] = useState(false);

	// Jobs
	const [jobs, setJobs] = useState<Record<string, Job>>({});
	const [contextMenu, setContextMenu] = useState<{
		x: number;
		y: number;
		jobId: string;
	} | null>(null);
	const [propertiesJobId, setPropertiesJobId] = useState<string | null>(null);

	// Direct downloader
	const [inputUrl, setInputUrl] = useState("");
	const myProbingJobIdRef = useRef("");
	const amProbingUrlRef = useRef("");

	const setMyProbingJobId = useCallback((val: string) => {
		myProbingJobIdRef.current = val;
	}, []);

	const setAmProbingUrl = useCallback((val: string) => {
		amProbingUrlRef.current = val;
	}, []);

	const [isProbing, setIsProbing] = useState(false);
	const [showFormatDrawer, setShowFormatDrawer] = useState(false);
	const [probedInfo, setProbedInfo] = useState<ProbedInfo | null>(null);
	const [selectedFormatId, setSelectedFormatId] = useState("");
	const [selectedTorrentFiles, _setSelectedTorrentFiles] = useState<
		Set<number>
	>(new Set());
	const selectedTorrentFilesRef = useRef<Set<number>>(new Set());
	const [selectedCategoryPath, _setSelectedCategoryPath] = useState("");
	const selectedCategoryPathRef = useRef("");
	const setSelectedCategoryPath = useCallback((val: string) => {
		selectedCategoryPathRef.current = val;
		_setSelectedCategoryPath(val);
	}, []);
	const [drawerCustomPath, setDrawerCustomPath] = useState("");

	const setSelectedTorrentFiles = useCallback((next: Set<number>) => {
		selectedTorrentFilesRef.current = next;
		_setSelectedTorrentFiles(next);
	}, []);

	useEffect(() => {
		if (probedInfo?.torrent?.files) {
			const all = new Set(probedInfo.torrent.files.map((f) => f.index));
			selectedTorrentFilesRef.current = all;
			_setSelectedTorrentFiles(all);
		} else {
			selectedTorrentFilesRef.current = new Set();
			_setSelectedTorrentFiles(new Set());
		}
	}, [probedInfo]);

	const toggleTorrentFile = useCallback((index: number) => {
		_setSelectedTorrentFiles((prev) => {
			const next = new Set(prev);
			if (next.has(index)) next.delete(index);
			else next.add(index);
			selectedTorrentFilesRef.current = next;
			return next;
		});
	}, []);

	const selectAllTorrentFiles = useCallback(() => {
		const all = new Set(probedInfo?.torrent?.files.map((f) => f.index) ?? []);
		selectedTorrentFilesRef.current = all;
		_setSelectedTorrentFiles(all);
	}, [probedInfo]);

	const deselectAllTorrentFiles = useCallback(() => {
		selectedTorrentFilesRef.current = new Set();
		_setSelectedTorrentFiles(new Set());
	}, []);

	// Alerts
	const [duplicateJobAlert, setDuplicateJobAlert] =
		useState<DuplicateJobAlert | null>(null);
	const [duplicateFileAlert, setDuplicateFileAlert] =
		useState<DuplicateFileAlert | null>(null);
	const [deleteFileConfirm, setDeleteFileConfirm] = useState<string | null>(
		null,
	);
	const [genericAlert, setGenericAlert] = useState<GenericAlert | null>(null);
	const [urlRefreshJobId, _setUrlRefreshJobId] = useState<string | null>(null);
	const urlRefreshJobIdRef = useRef<string | null>(null);
	const setUrlRefreshJobId = useCallback((val: string | null) => {
		urlRefreshJobIdRef.current = val;
		_setUrlRefreshJobId(val);
	}, []);

	const selectedFormatIdRef = useRef("");

	// WebSocket refs
	const wsRef = useRef<WebSocket | null>(null);
	const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
		null,
	);
	const heartbeatIntervalRef = useRef<ReturnType<typeof setInterval> | null>(
		null,
	);

	// Close context menu on global click
	useEffect(() => {
		const handleGlobalClick = () => setContextMenu(null);
		window.addEventListener("click", handleGlobalClick);
		return () => window.removeEventListener("click", handleGlobalClick);
	}, []);

	// LocalStorage sync for serverUrl
	useEffect(() => {
		const saved = localStorage.getItem("serverUrl");
		if (saved) setServerUrl(saved);
	}, []);

	useEffect(() => {
		localStorage.setItem("serverUrl", serverUrl);
	}, [serverUrl]);

	// Current settings ref for pushSettings
	const currentSettingsRef = useRef<DownloaderSettings>({
		mergeFormat,
		embedThumbnail,
		embedSubs,
		cookiesFromBrowser,
		useAria2Next,
		aria2NextMaxConnections,
		aria2NextConcurrentDownloads,
		aria2NextSplit,
		aria2NextMinSplitSize,
		aria2NextPreallocate,
		aria2NextCheckCertificate,
		aria2NextAlwaysResume,
		concurrentFragmentDownloads,
		downloadRetries,
		fragmentRetries,
		rateLimit,
		subtitlesLangs,
		ffmpegLocation,
		torrentEnabled,
		torrentMaxActive,
		torrentDownloadLimit,
		torrentUploadLimit,
		torrentSeedRatio,
		torrentPeerLimit,
		torrentUploadPeerLimit,
	});

	useEffect(() => {
		currentSettingsRef.current = {
			mergeFormat,
			embedThumbnail,
			embedSubs,
			cookiesFromBrowser,
			useAria2Next,
			aria2NextMaxConnections,
			aria2NextConcurrentDownloads,
			aria2NextSplit,
			aria2NextMinSplitSize,
			aria2NextPreallocate,
			aria2NextCheckCertificate,
			aria2NextAlwaysResume,
			concurrentFragmentDownloads,
			downloadRetries,
			fragmentRetries,
			rateLimit,
			subtitlesLangs,
			ffmpegLocation,
			torrentEnabled,
			torrentMaxActive,
			torrentDownloadLimit,
			torrentUploadLimit,
			torrentSeedRatio,
			torrentPeerLimit,
			torrentUploadPeerLimit,
		};
	});

	// WebSocket helpers
	const updateLocalJob = useCallback((jobId: string, updates: Partial<Job>) => {
		setJobs((prev) => {
			const existing = prev[jobId] || {
				job_id: jobId,
				url: "",
				status: "queued",
				progress: 0,
				downloaded_bytes: 0,
				total_bytes: 0,
				audio_downloaded_bytes: 0,
				audio_total_bytes: 0,
				combined_downloaded_bytes: 0,
				combined_total_bytes: 0,
				stream_phase: "single",
				speed: 0,
				eta: 0,
			};
			return { ...prev, [jobId]: { ...existing, ...updates } };
		});
	}, []);

	const fetchJobsList = useCallback(() => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "get_jobs" }));
		}
	}, []);

	const fetchCategories = useCallback(() => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "get_categories" }));
		}
	}, []);

	const saveCategoriesList = useCallback((list: Category[]) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(
				JSON.stringify({ type: "save_categories", categories: list }),
			);
		}
	}, []);

	const fetchDirectory = useCallback((path: string = "", forField?: string) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(
				JSON.stringify({
					type: "browse_directory",
					path: path || null,
					forField: forField || null,
				}),
			);
		}
	}, []);

	const handleRevealFile = useCallback((jobId: string) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "reveal_file", jobId }));
		}
	}, []);

	const fetchSettings = useCallback(() => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "get_settings" }));
		}
	}, []);

	const saveSettingsOnBackend = useCallback((updated: DownloaderSettings) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(
				JSON.stringify({
					type: "save_settings",
					settings: {
						mergeFormat: updated.mergeFormat,
						embedThumbnail: updated.embedThumbnail,
						embedSubs: updated.embedSubs,
						cookiesFromBrowser:
							updated.cookiesFromBrowser === "none"
								? null
								: updated.cookiesFromBrowser,
						useAria2Next: updated.useAria2Next,
						aria2NextMaxConnections: updated.aria2NextMaxConnections,
						aria2NextConcurrentDownloads: updated.aria2NextConcurrentDownloads,
						aria2NextSplit: updated.aria2NextSplit,
						aria2NextMinSplitSize: updated.aria2NextMinSplitSize,
						aria2NextPreallocate: updated.aria2NextPreallocate,
						aria2NextCheckCertificate: updated.aria2NextCheckCertificate,
						aria2NextAlwaysResume: updated.aria2NextAlwaysResume,
						concurrentFragmentDownloads: updated.concurrentFragmentDownloads,
						downloadRetries: updated.downloadRetries,
						fragmentRetries: updated.fragmentRetries,
						rateLimit: updated.rateLimit === "" ? null : updated.rateLimit,
						subtitlesLangs: updated.subtitlesLangs,
						ffmpegLocation:
							updated.ffmpegLocation === "" ? null : updated.ffmpegLocation,
						torrentEnabled: updated.torrentEnabled,
						torrentMaxActive: updated.torrentMaxActive,
						torrentDownloadLimit: updated.torrentDownloadLimit,
						torrentUploadLimit: updated.torrentUploadLimit,
						torrentSeedRatio: updated.torrentSeedRatio,
						torrentPeerLimit: updated.torrentPeerLimit,
						torrentUploadPeerLimit: updated.torrentUploadPeerLimit,
					},
				}),
			);
		}
	}, []);

	const pushSettings = useCallback(
		(overrides: Partial<DownloaderSettings> = {}) => {
			currentSettingsRef.current = {
				...currentSettingsRef.current,
				...overrides,
			};
			saveSettingsOnBackend(currentSettingsRef.current);
		},
		[saveSettingsOnBackend],
	);

	// WebSocket connection
	const serverUrlRef = useRef(serverUrl);
	useEffect(() => {
		serverUrlRef.current = serverUrl;
	}, [serverUrl]);

	const proceedWithDownload = useCallback(
		(
			jobId: string,
			formatId: string,
			outputDir: string,
			conflictResolution: "replace" | "rename",
			filename?: string,
		) => {
			if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
				const payload: Record<string, unknown> = {
					type: "choose",
					jobId,
					formatId,
					outputDir,
					conflictResolution,
				};
				if (filename) payload.filename = filename;
				wsRef.current.send(JSON.stringify(payload));
				setShowFormatDrawer(false);
				setProbedInfo(null);
				setDuplicateFileAlert(null);
				setInputUrl("");
			}
		},
		[],
	);

	const connectWebSocket = useCallback(() => {
		if (wsRef.current) {
			wsRef.current.onclose = null;
			wsRef.current.onerror = null;
			wsRef.current.close();
		}

		const wsUrl = serverUrlRef.current;
		logger.debug(`Connecting WebSocket to ${wsUrl}...`);

		try {
			const ws = new WebSocket(wsUrl);
			wsRef.current = ws;

			ws.onopen = () => {
				logger.debug("WebSocket connected");
				setIsConnected(true);
				ws.send(
					JSON.stringify({
						type: "hello",
						clientVersion: CLIENT_VERSION,
						tabId: TAB_ID,
					}),
				);

				if (heartbeatIntervalRef.current)
					clearInterval(heartbeatIntervalRef.current);
				heartbeatIntervalRef.current = setInterval(() => {
					if (ws.readyState === WebSocket.OPEN) {
						ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
					}
				}, 20 * 1000);

				fetchJobsList();
				fetchCategories();
				fetchSettings();
			};

			ws.onmessage = (event) => {
				try {
					const msg = JSON.parse(event.data);

					switch (msg.type) {
						case "hello":
							setServerInfo({
								ytDlpVersion: msg.ytDlpVersion,
								ffmpegAvailable: msg.ffmpegAvailable,
							});
							break;

						case "probe_started":
							if (msg.url === amProbingUrlRef.current) {
								setMyProbingJobId(msg.jobId);
								setIsProbing(true);
							}
							break;

						case "probe_result":
							if (msg.jobId === myProbingJobIdRef.current) {
								setIsProbing(false);
								setProbedInfo({
									jobId: msg.jobId,
									title: msg.title,
									duration: msg.duration,
									thumbnail: msg.thumbnail,
									uploader: msg.uploader,
									formats: msg.formats || [],
									mediaType: msg.mediaType,
									fileType: msg.fileType,
									mime: msg.mime,
									torrent: msg.torrent,
								});
								if (msg.formats && msg.formats.length > 0) {
									setSelectedFormatId(msg.formats[0].formatId);
								}
								setShowFormatDrawer(true);
								setMyProbingJobId("");
								setAmProbingUrl("");
							}
							break;

						case "probe_failed":
							if (msg.jobId === myProbingJobIdRef.current) {
								setIsProbing(false);
								setGenericAlert({
									title: "Analysis Failed",
									message: msg.error,
									suggestion:
										msg.suggestion || "Verify the link and try again.",
								});
								setMyProbingJobId("");
								setAmProbingUrl("");
							}
							break;

						case "download_queued":
							updateLocalJob(msg.jobId, {
								job_id: msg.jobId,
								url: msg.url || "",
								status: "downloading",
								speed: 0,
								eta: 0,
								output_dir: msg.outputPath,
								title: msg.title || undefined,
								duration: msg.duration || undefined,
								thumbnail: msg.thumbnail || undefined,
								uploader: msg.uploader || undefined,
								media_type: msg.mediaType || undefined,
							});
							break;

						case "download_progress": {
							const combinedDl =
								msg.combinedDownloadedBytes ??
								(msg.downloadedBytes || 0) + (msg.audioDownloadedBytes || 0);
							const combinedTotal =
								msg.combinedTotalBytes ??
								(msg.totalBytes || msg.totalBytesEstimate || 0) +
									(msg.audioTotalBytes || 0);
							const pct =
								msg.progress ??
								(combinedTotal > 0 ? (combinedDl / combinedTotal) * 100 : 0);
							updateLocalJob(msg.jobId, {
								status: msg.status,
								progress: pct,
								downloaded_bytes: msg.downloadedBytes || 0,
								total_bytes: msg.totalBytes || msg.totalBytesEstimate || 0,
								audio_downloaded_bytes: msg.audioDownloadedBytes || 0,
								audio_total_bytes: msg.audioTotalBytes || 0,
								combined_downloaded_bytes: combinedDl,
								combined_total_bytes: combinedTotal,
								stream_phase: msg.streamPhase || "single",
								speed: msg.speed || 0,
								eta: msg.eta || 0,
								fragment_index: msg.fragmentIndex,
								fragment_count: msg.fragmentCount,
								file_path: msg.filePath || undefined,
								torrent_peers: msg.torrent?.peers,
								torrent_seeds: msg.torrent?.seeds,
								torrent_availability: msg.torrent?.availability,
								torrent_completed_pieces: msg.torrent?.completedPieces,
								torrent_piece_count: msg.torrent?.pieceCount,
							});
							break;
						}

						case "download_completed":
							updateLocalJob(msg.jobId, {
								status: "completed",
								file_path: msg.filePath,
								progress: 100,
							});
							break;

						case "download_failed":
							updateLocalJob(msg.jobId, {
								status: "failed",
								error: msg.error,
								error_category: msg.errorCategory,
							});
							if (
								msg.errorCategory === "expired_url" &&
								msg.needsUrl &&
								msg.pageUrl
							) {
								setUrlRefreshJobId(msg.jobId);
							}
							break;

						case "download_canceled":
							updateLocalJob(msg.jobId, { status: "canceled" });
							break;

						case "download_url": {
							if (msg.jobId === urlRefreshJobIdRef.current) {
								handleRefreshUrl(
									msg.jobId,
									msg.url as string,
									(msg.referer as string) || undefined,
								);
							}
							break;
						}

						case "jobs_list": {
							// Merge server baseline into local state; never overwrite fresher
							// progress fields with stale baseline values.
							setJobs((prev) => {
								const merged = { ...prev };
								for (const job of msg.jobs as Job[]) {
									const existing = merged[job.job_id];
									if (existing) {
										// Prefer local progress if it is ahead of the baseline.
										const keepProgress =
											(existing.combined_downloaded_bytes ?? 0) >
											(job.combined_downloaded_bytes ?? 0);
										merged[job.job_id] = {
											...job,
											downloaded_bytes: keepProgress
												? existing.downloaded_bytes
												: job.downloaded_bytes,
											combined_downloaded_bytes: keepProgress
												? existing.combined_downloaded_bytes
												: job.combined_downloaded_bytes,
											progress: keepProgress ? existing.progress : job.progress,
										};
									} else {
										merged[job.job_id] = job;
									}
								}
								return merged;
							});
							break;
						}

						case "categories_list": {
							setCategories(msg.categories);
							if (
								msg.categories &&
								msg.categories.length > 0 &&
								!selectedCategoryPathRef.current
							) {
								const def =
									msg.categories.find((c: Category) => c.name === "Default") ||
									msg.categories[0];
								setSelectedCategoryPath(def.path);
							}
							break;
						}

						case "directory_selected": {
							if (msg.forField === "new_category") {
								setNewCatPath(msg.path);
							} else if (msg.forField === "drawer") {
								setDrawerCustomPath(msg.path);
							}
							break;
						}

						case "duplicate_job_alert":
							setIsProbing(false);
							setMyProbingJobId("");
							setAmProbingUrl("");
							setDuplicateJobAlert({
								jobId: msg.jobId,
								url: msg.url,
								title: msg.title,
								status: msg.status,
							});
							break;

						case "file_exists_result":
							if (msg.exists) {
								setDuplicateFileAlert({
									filename: msg.filename,
									path: msg.path,
									jobId: msg.jobId,
								});
							} else {
								proceedWithDownload(
									msg.jobId,
									selectedFormatIdRef.current,
									msg.path,
									"replace",
									msg.filename,
								);
							}
							break;

						case "settings_data":
							setMergeFormat(msg.settings.mergeFormat);
							setEmbedThumbnail(msg.settings.embedThumbnail);
							setEmbedSubs(msg.settings.embedSubs);
							setCookiesFromBrowser(msg.settings.cookiesFromBrowser || "none");
							setUseAria2Next(msg.settings.useAria2Next ?? true);
							setAria2NextMaxConnections(
								msg.settings.aria2NextMaxConnections ?? 16,
							);
							setAria2NextConcurrentDownloads(
								msg.settings.aria2NextConcurrentDownloads ?? 5,
							);
							setAria2NextSplit(msg.settings.aria2NextSplit ?? 16);
							setAria2NextMinSplitSize(
								msg.settings.aria2NextMinSplitSize || "1M",
							);
							setAria2NextPreallocate(
								msg.settings.aria2NextPreallocate ?? true,
							);
							setAria2NextCheckCertificate(
								msg.settings.aria2NextCheckCertificate ?? true,
							);
							setAria2NextAlwaysResume(
								msg.settings.aria2NextAlwaysResume ?? true,
							);
							setConcurrentFragmentDownloads(
								msg.settings.concurrentFragmentDownloads ?? 4,
							);
							setDownloadRetries(msg.settings.downloadRetries ?? 10);
							setFragmentRetries(msg.settings.fragmentRetries ?? 10);
							setRateLimit(msg.settings.rateLimit || "");
							setSubtitlesLangs(msg.settings.subtitlesLangs || "all");
							setFfmpegLocation(msg.settings.ffmpegLocation || "");
							setTorrentEnabled(msg.settings.torrentEnabled ?? true);
							setTorrentMaxActive(msg.settings.torrentMaxActive ?? 4);
							setTorrentDownloadLimit(msg.settings.torrentDownloadLimit ?? 0);
							setTorrentUploadLimit(msg.settings.torrentUploadLimit ?? 0);
							setTorrentSeedRatio(msg.settings.torrentSeedRatio ?? 2);
							setTorrentPeerLimit(msg.settings.torrentPeerLimit ?? 500);
							setTorrentUploadPeerLimit(
								msg.settings.torrentUploadPeerLimit ?? 20,
							);
							break;

						case "browse_failed":
							logger.error("Directory browse failed:", msg.error);
							break;
					}
				} catch (err) {
					logger.error("Failed to parse WS message:", err);
				}
			};

			ws.onclose = () => {
				logger.debug("WebSocket closed");
				setIsConnected(false);
				if (heartbeatIntervalRef.current)
					clearInterval(heartbeatIntervalRef.current);
				if (reconnectTimeoutRef.current)
					clearTimeout(reconnectTimeoutRef.current);
				reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000);
			};

			ws.onerror = (err) => {
				logger.error("WebSocket error:", err);
			};
		} catch (e) {
			logger.error("Failed to connect WebSocket:", e);
			setIsConnected(false);
			if (reconnectTimeoutRef.current)
				clearTimeout(reconnectTimeoutRef.current);
			reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000);
		}
	}, [
		fetchJobsList,
		fetchCategories,
		fetchSettings,
		updateLocalJob,
		setSelectedCategoryPath,
		setMyProbingJobId,
		setAmProbingUrl,
		proceedWithDownload,
	]);

	useEffect(() => {
		connectWebSocket();
		return () => {
			if (wsRef.current) wsRef.current.close();
			if (reconnectTimeoutRef.current)
				clearTimeout(reconnectTimeoutRef.current);
			if (heartbeatIntervalRef.current)
				clearInterval(heartbeatIntervalRef.current);
		};
	}, [connectWebSocket]);

	// Actions
	const handleProbeUrl = useCallback(() => {
		const trimmed = inputUrl.trim();
		if (!trimmed) return;

		const existingJob = Object.values(jobs).find((j) => j.url === trimmed);
		if (existingJob) {
			setDuplicateJobAlert({
				jobId: existingJob.job_id,
				url: existingJob.url,
				title: existingJob.title || "Unknown Title",
				status: existingJob.status,
			});
			return;
		}

		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "probe", url: trimmed }));
			setAmProbingUrl(trimmed);
			setIsProbing(true);
		}
	}, [inputUrl, jobs, setAmProbingUrl]);

	const handleChooseFormat = useCallback(() => {
		if (
			!probedInfo ||
			(probedInfo.mediaType !== "torrent" && !selectedFormatId)
		)
			return;
		const finalDest = drawerCustomPath || selectedCategoryPath;
		if (probedInfo.mediaType === "torrent" && probedInfo.torrent) {
			if (selectedTorrentFilesRef.current.size === 0) return;
			if (wsRef.current?.readyState === WebSocket.OPEN) {
				wsRef.current.send(
					JSON.stringify({
						type: "choose",
						jobId: probedInfo.jobId,
						formatId: "torrent",
						outputDir: finalDest,
						conflictResolution: "replace",
						torrentSelectedFileIndices: Array.from(
							selectedTorrentFilesRef.current,
						),
					}),
				);
			}
			setShowFormatDrawer(false);
			setProbedInfo(null);
			setInputUrl("");
			selectedTorrentFilesRef.current = new Set();
			_setSelectedTorrentFiles(new Set());
			return;
		}
		const chosenFormatObj = probedInfo.formats.find(
			(f) => f.formatId === selectedFormatId,
		);
		const estimatedExt = chosenFormatObj?.ext || "mp4";

		selectedFormatIdRef.current = selectedFormatId;

		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(
				JSON.stringify({
					// Raw hints only — backend resolve_filename is the source of truth.
					type: "check_file_exists",
					path: finalDest,
					jobId: probedInfo.jobId,
					title: probedInfo.title || "video",
					ext: estimatedExt,
					filename: probedInfo.filename || undefined,
					mime: probedInfo.mime || undefined,
				}),
			);
		}
	}, [probedInfo, selectedFormatId, drawerCustomPath, selectedCategoryPath]);

	const handleAddCategory = useCallback(() => {
		if (!newCatName.trim() || !newCatPath.trim()) return;

		if (
			categories.find(
				(c) => c.name.toLowerCase() === newCatName.trim().toLowerCase(),
			)
		) {
			setGenericAlert({
				title: "Category Validation",
				message: "A category with this name already exists.",
			});
			return;
		}

		const updated = [
			...categories,
			{ name: newCatName.trim(), path: newCatPath.trim() },
		];
		saveCategoriesList(updated);
		setNewCatName("");
	}, [categories, newCatName, newCatPath, saveCategoriesList]);

	const handleDeleteCategory = useCallback(
		(name: string) => {
			if (name === "Default") return;
			const updated = categories.filter((c) => c.name !== name);
			saveCategoriesList(updated);
		},
		[categories, saveCategoriesList],
	);

	const counts = useMemo(() => {
		const list = Object.values(jobs).filter((j) => j.status !== "probing");
		return {
			all: list.length,
			downloading: list.filter((j) =>
				["downloading", "queued", "postprocessing"].includes(j.status),
			).length,
			seeding: list.filter((j) => j.status === "seeding").length,
			completed: list.filter((j) => j.status === "completed").length,
			paused: list.filter((j) => j.status === "paused").length,
			failed: list.filter((j) => ["failed", "canceled"].includes(j.status))
				.length,
		};
	}, [jobs]);

	const displayJobs = useMemo(() => {
		const list = Object.values(jobs).filter((j) => j.status !== "probing");
		if (filterTab === "all") return list;
		if (filterTab === "downloading") {
			return list.filter((j) =>
				["downloading", "queued", "postprocessing"].includes(j.status),
			);
		}
		if (filterTab === "seeding")
			return list.filter((j) => j.status === "seeding");
		if (filterTab === "completed")
			return list.filter((j) => j.status === "completed");
		if (filterTab === "paused")
			return list.filter((j) => j.status === "paused");
		if (filterTab === "failed")
			return list.filter((j) => ["failed", "canceled"].includes(j.status));
		return list;
	}, [jobs, filterTab]);

	const hasCompletedJobs = useMemo(
		() =>
			Object.values(jobs).some((j) =>
				["completed", "failed", "canceled"].includes(j.status),
			),
		[jobs],
	);

	const handleRemoveJob = useCallback((jobId: string) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "remove_job", jobId }));
		}
		setJobs((prev) => {
			const { [jobId]: _, ...rest } = prev;
			return rest;
		});
	}, []);

	const handleClearCompleted = useCallback(() => {
		Object.values(jobs).forEach((j) => {
			if (["completed", "failed", "canceled"].includes(j.status)) {
				handleRemoveJob(j.job_id);
			}
		});
	}, [jobs, handleRemoveJob]);

	const handlePauseJob = useCallback(
		(jobId: string) => {
			if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
				wsRef.current.send(JSON.stringify({ type: "pause", jobId }));
			}
			updateLocalJob(jobId, { status: "paused" });
		},
		[updateLocalJob],
	);

	const handleResumeJob = useCallback(
		(jobId: string) => {
			if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
				wsRef.current.send(JSON.stringify({ type: "resume", jobId }));
			}
			updateLocalJob(jobId, { status: "queued" });
		},
		[updateLocalJob],
	);

	const handleRefreshUrl = useCallback(
		(jobId: string, url: string, referer?: string) => {
			if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
				wsRef.current.send(
					JSON.stringify({
						type: "refresh_url",
						jobId,
						url,
						referer,
					}),
				);
			}
			setUrlRefreshJobId(null);
			updateLocalJob(jobId, {
				status: "queued",
				url,
				referer,
				error: undefined,
				error_category: undefined,
			});
		},
		[updateLocalJob],
	);

	const handleDeleteFile = useCallback((jobId: string) => {
		setDeleteFileConfirm(jobId);
	}, []);

	const handleConfirmDeleteFile = useCallback(() => {
		if (deleteFileConfirm) {
			if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
				wsRef.current.send(
					JSON.stringify({ type: "delete_file", jobId: deleteFileConfirm }),
				);
			}
			setDeleteFileConfirm(null);
		}
	}, [deleteFileConfirm]);

	const handleInstallExtensionClick = useCallback(async () => {
		setIsLoadingBrowsers(true);
		setIsExtModalOpen(true);

		try {
			const tauri =
				typeof window !== "undefined"
					? (window as TauriWindow).__TAURI__
					: undefined;
			if (tauri) {
				const list = await tauri.core.invoke<BrowserInfo[]>(
					"detect_installed_browsers",
				);
				setBrowsersList(list);
			} else {
				const uaData = (
					navigator as Navigator & {
						userAgentData?: { brands?: { brand: string }[] };
					}
				).userAgentData;
				const brands = uaData?.brands?.map((b) => b.brand.toLowerCase()) ?? [];
				const ua =
					typeof navigator !== "undefined"
						? navigator.userAgent.toLowerCase()
						: "";

				let isBrave = brands.includes("brave");
				if (!isBrave) {
					const brave = (
						navigator as Navigator & {
							brave?: { isBrave?: () => Promise<boolean> };
						}
					).brave;
					if (brave && typeof brave.isBrave === "function") {
						try {
							isBrave = await brave.isBrave();
						} catch {
							isBrave = false;
						}
					}
				}

				const isChrome =
					brands.includes("google chrome") ||
					(ua.includes("chrome") && !isBrave && !ua.includes("edg"));
				const isFirefox = brands.includes("firefox") || ua.includes("firefox");
				const isArk =
					brands.includes("ark") ||
					brands.includes("arc") ||
					ua.includes("ark") ||
					ua.includes("arc");

				setBrowsersList([
					{
						name: "Google Chrome",
						key: "chrome",
						installed: isChrome,
						extensions_url: "chrome://extensions",
					},
					{
						name: "Brave Browser",
						key: "brave",
						installed: isBrave,
						extensions_url: "brave://extensions",
					},
					{
						name: "Mozilla Firefox",
						key: "firefox",
						installed: isFirefox,
						extensions_url: "about:debugging",
					},
					{
						name: "Ark",
						key: "ark",
						installed: isArk,
						extensions_url: "chrome://extensions",
					},
				]);
			}
		} catch (err) {
			logger.error("Failed to detect browsers:", err);
		} finally {
			setIsLoadingBrowsers(false);
		}
	}, []);

	const handleInstallForBrowser = useCallback(async (browser: BrowserInfo) => {
		try {
			const tauri =
				typeof window !== "undefined"
					? (window as TauriWindow).__TAURI__
					: undefined;
			if (tauri) {
				await tauri.core.invoke("install_extension_for_browser", {
					browserKey: browser.key,
				});
			}
			setIsExtModalOpen(false);
			setGenericAlert({
				title: "Extension Installed",
				message: `DownloadAnything extension loaded into ${browser.name} successfully!`,
			});
		} catch (err) {
			setGenericAlert({
				title: "Extension Installation Failed",
				message: `Installation failed: ${err}`,
			});
		}
	}, []);

	return {
		activeTab,
		setActiveTab,
		activeSettingsSection,
		setActiveSettingsSection,
		filterTab,
		setFilterTab,
		serverUrl,
		setServerUrl,
		mergeFormat,
		setMergeFormat,
		embedThumbnail,
		setEmbedThumbnail,
		embedSubs,
		setEmbedSubs,
		cookiesFromBrowser,
		setCookiesFromBrowser,
		useAria2Next,
		setUseAria2Next,
		aria2NextMaxConnections,
		setAria2NextMaxConnections,
		aria2NextConcurrentDownloads,
		setAria2NextConcurrentDownloads,
		aria2NextSplit,
		setAria2NextSplit,
		aria2NextMinSplitSize,
		setAria2NextMinSplitSize,
		aria2NextPreallocate,
		setAria2NextPreallocate,
		aria2NextCheckCertificate,
		setAria2NextCheckCertificate,
		aria2NextAlwaysResume,
		setAria2NextAlwaysResume,
		concurrentFragmentDownloads,
		setConcurrentFragmentDownloads,
		downloadRetries,
		setDownloadRetries,
		fragmentRetries,
		setFragmentRetries,
		rateLimit,
		setRateLimit,
		subtitlesLangs,
		setSubtitlesLangs,
		ffmpegLocation,
		setFfmpegLocation,
		torrentEnabled,
		setTorrentEnabled,
		torrentMaxActive,
		setTorrentMaxActive,
		torrentDownloadLimit,
		setTorrentDownloadLimit,
		torrentUploadLimit,
		setTorrentUploadLimit,
		torrentSeedRatio,
		setTorrentSeedRatio,
		torrentPeerLimit,
		setTorrentPeerLimit,
		torrentUploadPeerLimit,
		setTorrentUploadPeerLimit,
		categories,
		setCategories,
		newCatName,
		setNewCatName,
		newCatPath,
		setNewCatPath,
		isConnected,
		serverInfo,
		isExtModalOpen,
		setIsExtModalOpen,
		browsersList,
		isLoadingBrowsers,
		jobs,
		setJobs,
		contextMenu,
		setContextMenu,
		propertiesJobId,
		setPropertiesJobId,
		inputUrl,
		setInputUrl,
		isProbing,
		showFormatDrawer,
		setShowFormatDrawer,
		probedInfo,
		setProbedInfo,
		selectedFormatId,
		setSelectedFormatId,
		selectedTorrentFiles,
		toggleTorrentFile,
		selectAllTorrentFiles,
		deselectAllTorrentFiles,
		setSelectedTorrentFiles,
		selectedCategoryPath,
		setSelectedCategoryPath,
		drawerCustomPath,
		setDrawerCustomPath,
		duplicateJobAlert,
		setDuplicateJobAlert,
		duplicateFileAlert,
		setDuplicateFileAlert,
		deleteFileConfirm,
		setDeleteFileConfirm,
		genericAlert,
		setGenericAlert,
		urlRefreshJobId,
		setUrlRefreshJobId,
		displayJobs,
		counts,
		hasCompletedJobs,
		pushSettings,
		fetchDirectory,
		handleRevealFile,
		handleProbeUrl,
		handleChooseFormat,
		proceedWithDownload,
		handleAddCategory,
		handleDeleteCategory,
		handleClearCompleted,
		handlePauseJob,
		handleResumeJob,
		handleRemoveJob,
		handleDeleteFile,
		handleConfirmDeleteFile,
		handleInstallExtensionClick,
		handleInstallForBrowser,
		fetchSettings,
		fetchCategories,
	};
}

export type UseDownloaderReturn = ReturnType<typeof useDownloader>;
