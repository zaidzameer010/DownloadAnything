import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
	useAria2: boolean;
	aria2MaxConnections: number;
	aria2ConcurrentDownloads: number;
	aria2Split: number;
	aria2MinSplitSize: string;
	aria2Preallocate: boolean;
	aria2CheckCertificate: boolean;
	aria2AlwaysResume: boolean;
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
	torrentOutputDir: string;
	torrentSeedRatio: number;
	torrentSeedTimeMinutes: number;
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
	const [useAria2, setUseAria2] = useState(true);
	const [aria2MaxConnections, setAria2MaxConnections] = useState(16);
	const [aria2ConcurrentDownloads, setAria2ConcurrentDownloads] = useState(5);
	const [aria2Split, setAria2Split] = useState(16);
	const [aria2MinSplitSize, setAria2MinSplitSize] = useState("1M");
	const [aria2Preallocate, setAria2Preallocate] = useState(true);
	const [aria2CheckCertificate, setAria2CheckCertificate] = useState(true);
	const [aria2AlwaysResume, setAria2AlwaysResume] = useState(true);

	const [concurrentFragmentDownloads, setConcurrentFragmentDownloads] =
		useState(4);
	const [downloadRetries, setDownloadRetries] = useState(10);
	const [fragmentRetries, setFragmentRetries] = useState(10);
	const [rateLimit, setRateLimit] = useState("");
	const [subtitlesLangs, setSubtitlesLangs] = useState("all");
	const [ffmpegLocation, setFfmpegLocation] = useState("");
	const [torrentEnabled, setTorrentEnabled] = useState(true);
	const [torrentMaxActive, setTorrentMaxActive] = useState(32);
	const [torrentDownloadLimit, setTorrentDownloadLimit] = useState(0);
	const [torrentUploadLimit, setTorrentUploadLimit] = useState(0);
	const [torrentOutputDir, setTorrentOutputDir] = useState("");
	const [torrentSeedRatio, setTorrentSeedRatio] = useState(100);
	const [torrentSeedTimeMinutes, setTorrentSeedTimeMinutes] = useState(100000);
	const [torrentPeerLimit, setTorrentPeerLimit] = useState(2000);
	const [torrentUploadPeerLimit, setTorrentUploadPeerLimit] = useState(500);

	// Categories
	const [categories, setCategories] = useState<Category[]>([]);
	const [newCatName, setNewCatName] = useState("");
	const [newCatPath, setNewCatPath] = useState("");

	// Server status
	const [isConnected, setIsConnected] = useState(false);
	const [serverInfo, setServerInfo] = useState<ServerInfo>({
		ytDlpVersion: "Unknown",
		ffmpegAvailable: false,
		poTokenPluginLoaded: false,
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
	const [selectedCategoryPath, _setSelectedCategoryPath] = useState("");
	const selectedCategoryPathRef = useRef("");
	const setSelectedCategoryPath = useCallback((val: string) => {
		selectedCategoryPathRef.current = val;
		_setSelectedCategoryPath(val);
	}, []);
	const [drawerCustomPath, setDrawerCustomPath] = useState("");

	// Alerts
	const [duplicateJobAlert, setDuplicateJobAlert] =
		useState<DuplicateJobAlert | null>(null);
	const [duplicateFileAlert, setDuplicateFileAlert] =
		useState<DuplicateFileAlert | null>(null);
	const [deleteFileConfirm, setDeleteFileConfirm] = useState<string | null>(
		null,
	);
	const [genericAlert, setGenericAlert] = useState<GenericAlert | null>(null);

	const selectedFormatIdRef = useRef("");
	const selectedOutputDirRef = useRef("");

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
		useAria2,
		aria2MaxConnections,
		aria2ConcurrentDownloads,
		aria2Split,
		aria2MinSplitSize,
		aria2Preallocate,
		aria2CheckCertificate,
		aria2AlwaysResume,
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
		torrentOutputDir,
		torrentSeedRatio,
		torrentSeedTimeMinutes,
		torrentPeerLimit,
		torrentUploadPeerLimit,
	});

	useEffect(() => {
		currentSettingsRef.current = {
			mergeFormat,
			embedThumbnail,
			embedSubs,
			cookiesFromBrowser,
			useAria2,
			aria2MaxConnections,
			aria2ConcurrentDownloads,
			aria2Split,
			aria2MinSplitSize,
			aria2Preallocate,
			aria2CheckCertificate,
			aria2AlwaysResume,
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
			torrentOutputDir,
			torrentSeedRatio,
			torrentSeedTimeMinutes,
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
						useAria2: updated.useAria2,
						aria2MaxConnections: updated.aria2MaxConnections,
						aria2ConcurrentDownloads: updated.aria2ConcurrentDownloads,
						aria2Split: updated.aria2Split,
						aria2MinSplitSize: updated.aria2MinSplitSize,
						aria2Preallocate: updated.aria2Preallocate,
						aria2CheckCertificate: updated.aria2CheckCertificate,
						aria2AlwaysResume: updated.aria2AlwaysResume,
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
						torrentOutputDir:
							updated.torrentOutputDir === "" ? null : updated.torrentOutputDir,
						torrentSeedRatio: updated.torrentSeedRatio,
						torrentSeedTimeMinutes: updated.torrentSeedTimeMinutes,
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
		) => {
			if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
				wsRef.current.send(
					JSON.stringify({
						type: "choose",
						jobId,
						formatId,
						outputDir,
						conflictResolution,
					}),
				);
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

		console.log(`Connecting WebSocket to ${serverUrlRef.current}...`);
		try {
			const ws = new WebSocket(serverUrlRef.current);
			wsRef.current = ws;

			ws.onopen = () => {
				console.log("WebSocket connected");
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
								poTokenPluginLoaded: msg.poTokenPluginLoaded || false,
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
								combinedTotal > 0 ? (combinedDl / combinedTotal) * 100 : 0;
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
							updateLocalJob(msg.jobId, { status: "failed", error: msg.error });
							break;

						case "download_canceled":
							updateLocalJob(msg.jobId, { status: "canceled" });
							break;

						case "jobs_list": {
							const jobsMap: Record<string, Job> = {};
							msg.jobs.forEach((job: Job) => {
								jobsMap[job.job_id] = job;
							});
							setJobs(jobsMap);
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
								);
							}
							break;

						case "settings_data":
							setMergeFormat(msg.settings.mergeFormat);
							setEmbedThumbnail(msg.settings.embedThumbnail);
							setEmbedSubs(msg.settings.embedSubs);
							setCookiesFromBrowser(msg.settings.cookiesFromBrowser || "none");
							setUseAria2(msg.settings.useAria2 ?? true);
							setAria2MaxConnections(msg.settings.aria2MaxConnections ?? 16);
							setAria2ConcurrentDownloads(
								msg.settings.aria2ConcurrentDownloads ?? 5,
							);
							setAria2Split(msg.settings.aria2Split ?? 16);
							setAria2MinSplitSize(msg.settings.aria2MinSplitSize || "1M");
							setAria2Preallocate(msg.settings.aria2Preallocate ?? true);
							setAria2CheckCertificate(
								msg.settings.aria2CheckCertificate ?? true,
							);
							setAria2AlwaysResume(msg.settings.aria2AlwaysResume ?? true);
							setConcurrentFragmentDownloads(
								msg.settings.concurrentFragmentDownloads ?? 4,
							);
							setDownloadRetries(msg.settings.downloadRetries ?? 10);
							setFragmentRetries(msg.settings.fragmentRetries ?? 10);
							setRateLimit(msg.settings.rateLimit || "");
							setSubtitlesLangs(msg.settings.subtitlesLangs || "all");
							setFfmpegLocation(msg.settings.ffmpegLocation || "");
							setTorrentEnabled(msg.settings.torrentEnabled ?? true);
							setTorrentMaxActive(msg.settings.torrentMaxActive ?? 32);
							setTorrentDownloadLimit(msg.settings.torrentDownloadLimit ?? 0);
							setTorrentUploadLimit(msg.settings.torrentUploadLimit ?? 0);
							setTorrentOutputDir(msg.settings.torrentOutputDir || "");
							setTorrentSeedRatio(msg.settings.torrentSeedRatio ?? 100);
							setTorrentSeedTimeMinutes(
								msg.settings.torrentSeedTimeMinutes ?? 100000,
							);
							setTorrentPeerLimit(msg.settings.torrentPeerLimit ?? 2000);
							setTorrentUploadPeerLimit(
								msg.settings.torrentUploadPeerLimit ?? 500,
							);
							break;

						case "browse_failed":
							console.error("Directory browse failed:", msg.error);
							break;
					}
				} catch (err) {
					console.error("Failed to parse WS message:", err);
				}
			};

			ws.onclose = () => {
				console.log("WebSocket closed");
				setIsConnected(false);
				if (heartbeatIntervalRef.current)
					clearInterval(heartbeatIntervalRef.current);
				if (reconnectTimeoutRef.current)
					clearTimeout(reconnectTimeoutRef.current);
				reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000);
			};

			ws.onerror = (err) => {
				console.error("WebSocket error:", err);
			};
		} catch (e) {
			console.error("Failed to connect WebSocket:", e);
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

	const handlePasteUrl = useCallback(async () => {
		try {
			const text = await navigator.clipboard.readText();
			if (text) setInputUrl(text);
		} catch (err) {
			console.error("Clipboard paste blocked or unsupported:", err);
		}
	}, []);

	const handleChooseFormat = useCallback(() => {
		if (
			!probedInfo ||
			(probedInfo.mediaType !== "torrent" && !selectedFormatId)
		)
			return;
		const finalDest = drawerCustomPath || selectedCategoryPath;
		if (probedInfo.mediaType === "torrent" && probedInfo.torrent) {
			if (wsRef.current?.readyState === WebSocket.OPEN) {
				wsRef.current.send(
					JSON.stringify({
						type: "choose",
						jobId: probedInfo.jobId,
						formatId: "torrent",
						outputDir: finalDest,
						conflictResolution: "replace",
					}),
				);
			}
			setShowFormatDrawer(false);
			setProbedInfo(null);
			setInputUrl("");
			return;
		}
		const chosenFormatObj = probedInfo.formats.find(
			(f) => f.formatId === selectedFormatId,
		);
		const estimatedExt = chosenFormatObj?.ext || "mp4";
		let cleanTitle = probedInfo.title || "video";
		const mappings: Record<string, string> = {
			"/": "／",
			"\\": "＼",
			":": "：",
			"*": "＊",
			"?": "？",
			'"': "＂",
			"<": "＜",
			">": "＞",
			"|": "｜",
		};
		for (const [char, replacement] of Object.entries(mappings)) {
			cleanTitle = cleanTitle.replaceAll(char, replacement);
		}
		cleanTitle = cleanTitle.replace(/\s+/g, " ").trim();
		while (cleanTitle.endsWith(".")) {
			cleanTitle = cleanTitle.slice(0, -1).trim();
		}
		if (!cleanTitle) cleanTitle = "video";
		const filename = `${cleanTitle}.${estimatedExt}`;

		selectedFormatIdRef.current = selectedFormatId;
		selectedOutputDirRef.current = finalDest;

		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(
				JSON.stringify({
					type: "check_file_exists",
					path: finalDest,
					filename,
					jobId: probedInfo.jobId,
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
	}, []);

	const handleClearCompleted = useCallback(() => {
		Object.values(jobs).forEach((j) => {
			if (["completed", "failed", "canceled"].includes(j.status)) {
				handleRemoveJob(j.job_id);
			}
		});
	}, [jobs, handleRemoveJob]);

	const handlePauseJob = useCallback((jobId: string) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "pause", jobId }));
		}
	}, []);

	const handleResumeJob = useCallback((jobId: string) => {
		if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
			wsRef.current.send(JSON.stringify({ type: "resume", jobId }));
		}
	}, []);

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
			console.error("Failed to detect browsers:", err);
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
			alert(
				`DownloadAnything extension loaded into ${browser.name} successfully!`,
			);
		} catch (err) {
			alert(`Installation failed: ${err}`);
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
		useAria2,
		setUseAria2,
		aria2MaxConnections,
		setAria2MaxConnections,
		aria2ConcurrentDownloads,
		setAria2ConcurrentDownloads,
		aria2Split,
		setAria2Split,
		aria2MinSplitSize,
		setAria2MinSplitSize,
		aria2Preallocate,
		setAria2Preallocate,
		aria2CheckCertificate,
		setAria2CheckCertificate,
		aria2AlwaysResume,
		setAria2AlwaysResume,
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
		torrentOutputDir,
		setTorrentOutputDir,
		torrentSeedRatio,
		setTorrentSeedRatio,
		torrentSeedTimeMinutes,
		setTorrentSeedTimeMinutes,
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
		displayJobs,
		counts,
		hasCompletedJobs,
		pushSettings,
		fetchDirectory,
		handleRevealFile,
		handleProbeUrl,
		handlePasteUrl,
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
