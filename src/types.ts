export interface TorrentFile {
	index: number;
	path: string;
	size: number;
	priority: number;
}

export interface TorrentMetadata {
	name: string;
	files: TorrentFile[];
	totalSize: number;
	pieceLength: number;
	pieceCount: number;
	infoHash: string;
}

export interface TorrentStatus {
	peers: number;
	seeds: number;
	availability: number;
	completedPieces: number;
	pieceCount: number;
}

export interface Job {
	job_id: string;
	url: string;
	status: string;
	progress: number;
	downloaded_bytes: number;
	total_bytes: number;
	audio_downloaded_bytes: number;
	audio_total_bytes: number;
	combined_downloaded_bytes: number;
	combined_total_bytes: number;
	stream_phase: string;
	speed: number;
	eta: number;
	format_id?: string;
	output_dir?: string;
	error?: string;
	title?: string;
	duration?: number;
	thumbnail?: string;
	uploader?: string;
	file_path?: string;
	fragment_index?: number;
	fragment_count?: number;
	media_type?: string;
	torrent_files?: TorrentFile[];
	torrent_info_hash?: string;
	torrent_piece_length?: number;
	torrent_piece_count?: number;
	torrent_peers?: number;
	torrent_seeds?: number;
	torrent_availability?: number;
	torrent_completed_pieces?: number;
}

export interface Category {
	name: string;
	path: string;
}

export interface FormatOption {
	label: string;
	height: number;
	fps: number;
	codecFamily: string;
	ext: string;
	tbr?: number;
	estSizeBytes?: number;
	formatId: string;
	isCombined: boolean;
	hdr: boolean;
	isStream: boolean;
	streamType?: string;
}

export interface ProbedInfo {
	jobId: string;
	title: string;
	duration?: number;
	thumbnail?: string;
	uploader?: string;
	formats: FormatOption[];
	mediaType?: string;
	torrent?: TorrentMetadata;
}

export interface ServerInfo {
	ytDlpVersion: string;
	ffmpegAvailable: boolean;
	poTokenPluginLoaded: boolean;
}

export interface DuplicateJobAlert {
	jobId: string;
	url: string;
	title: string;
	status: string;
}

export interface DuplicateFileAlert {
	filename: string;
	path: string;
	jobId: string;
}

export interface GenericAlert {
	title: string;
	message: string;
	suggestion?: string;
}

export type FilterTab =
	| "all"
	| "downloading"
	| "completed"
	| "paused"
	| "failed";
export type ActiveTab = "downloads" | "settings";
export type SettingsSection =
	| "general"
	| "categories"
	| "engines"
	| "torrent"
	| "integrations";

export interface BrowserInfo {
	name: string;
	key: string;
	installed: boolean;
	extensions_url?: string;
}
