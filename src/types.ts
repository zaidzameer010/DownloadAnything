export type TaskStatus =
  | "queued"
  | "downloading"
  | "paused"
  | "completed"
  | "error"
  | "cancelled"
  | "stitching"
  | "embedding"
  | "finalizing";

export interface Task {
  readonly task_id: string;
  readonly url: string;
  readonly title: string | null;
  readonly status: TaskStatus;
  readonly progress: number;
  readonly total_bytes: number | null;
  readonly downloaded_bytes: number;
  readonly speed: number | null;
  readonly eta: number | null;
  readonly category: string;
  readonly final_path: string | null;
  readonly custom_path: string | null;
  readonly format_id?: string | null;
  readonly fragment_index?: number | null;
  readonly fragment_count?: number | null;
  readonly is_video?: boolean;
  readonly page_title?: string | null;
  readonly is_stream?: boolean;
  readonly error?: string | null;
  readonly started_at?: number;
  readonly finished_at?: number;
  readonly has_custom_title?: boolean;
  readonly using_aria2c?: boolean;
}

export type MergeFormat = "mp4" | "mkv" | "webm";

export interface Settings {
  max_concurrent_downloads: number;
  merge_output_format: MergeFormat;
  default_download_path: string;
  concurrent_fragments: number;
  rate_limit_bytes_per_sec: number;
  proxy: string;
  embed_thumbnail: boolean;
  embed_subtitles: boolean;
  subtitle_language: string;
  categories: Record<string, string>;
  enable_download_interception: boolean;
  intercept_media_only: boolean;
}

export interface Health {
  readonly active_workers: string;
  readonly yt_dlp_version: string;
}

export interface BackendState {
  readonly ready: boolean;
  readonly logs: readonly string[];
}

export interface DownloadEngineState {
  readonly tasks: readonly Task[];
  readonly settings: Settings;
  readonly health: Health;
  readonly online: boolean;
  readonly backend: BackendState;
  readonly activeTab: "downloads" | "settings";
}

export type WSAction = "save_settings" | "cancel" | "delete" | "pause" | "resume" | "reveal";

export interface WSRequest {
  readonly action: WSAction;
  readonly request_id: string;
  readonly payload: unknown;
}

export interface WSResponse {
  readonly type: "response";
  readonly request_id: string;
  readonly ok: boolean;
  readonly data?: any;
  readonly error?: string;
}

export interface WSTasksPush {
  readonly type: "tasks";
  readonly data?: Task[];
  readonly health?: Health;
  readonly settings?: Settings;
}

export type WSIncomingMessage = WSResponse | WSTasksPush;
