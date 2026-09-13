import {
	Accordion,
	Alert,
	Button,
	Card,
	Checkbox,
	Drawer,
	Dropdown,
	Label,
	ListBox,
	Modal,
	ProgressBar,
	ScrollShadow,
	SearchField,
	Select,
	Spinner,
	Tabs,
	Typography,
} from "@heroui/react";
import { Squircle } from "@squircle-js/react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
	Archive,
	Check,
	Clipboard,
	Download,
	File,
	FileAudio,
	FileText,
	FileVideo,
	FolderOpen,
	HardDriveDownload,
	Pause,
	Play,
	Search,
	Trash2,
	X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { useBackend } from "../hooks/useBackend";
import type {
	DuplicateAction,
	DuplicateInfo,
	FormatRow,
	Job,
	JobStatus,
	PlaylistEntry,
	ProbeResponse,
	Settings,
	TorrentInfo,
	TorrentProgressFile,
} from "../lib/backend";
import {
	formatBytes,
	formatDuration,
	formatEta,
	formatSpeed,
} from "../lib/format";
import SquircleChip from "./SquircleChip";

type Backend = ReturnType<typeof useBackend>;
type Filter = "all" | "active" | "completed" | "failed";

const FILTERS: { key: Filter; label: string }[] = [
	{ key: "all", label: "All downloads" },
	{ key: "active", label: "Active" },
	{ key: "completed", label: "Completed" },
	{ key: "failed", label: "Failed" },
];

const ACTIVE_STATUSES = new Set<JobStatus>([
	"queued",
	"downloading",
	"paused",
	"postprocessing",
]);

const DIRECT_FILE_EXTENSIONS = new Set([
	"7z",
	"bz2",
	"csv",
	"deb",
	"dmg",
	"doc",
	"docx",
	"exe",
	"gz",
	"iso",
	"json",
	"msi",
	"pdf",
	"pkg",
	"ppt",
	"pptx",
	"rar",
	"rpm",
	"tar",
	"txt",
	"xls",
	"xlsx",
	"xml",
	"xz",
	"zip",
]);

function directFileMediaType(value: string): string | undefined {
	try {
		const extension = new URL(value).pathname.split(".").pop()?.toLowerCase();
		return extension && DIRECT_FILE_EXTENSIONS.has(extension)
			? "file"
			: undefined;
	} catch {
		return undefined;
	}
}

function matchesFilter(job: Job, filter: Filter): boolean {
	if (filter === "active") return ACTIVE_STATUSES.has(job.status);
	if (filter === "completed") return job.status === "completed";
	if (filter === "failed")
		return job.status === "failed" || job.status === "cancelled";
	return true;
}

function statusColor(
	status: JobStatus,
): "default" | "accent" | "success" | "warning" | "danger" {
	if (status === "completed") return "success";
	if (status === "failed" || status === "cancelled") return "danger";
	if (status === "paused") return "warning";
	if (status === "downloading" || status === "postprocessing") return "accent";
	return "default";
}

function StatusChip({ status }: { status: JobStatus }) {
	return (
		<SquircleChip color={statusColor(status)} size="sm" variant="soft">
			{status}
		</SquircleChip>
	);
}

const FALLBACK_FORMAT: FormatRow = {
	id: "best",
	selector: "",
	label: "Best quality",
	resolution: "",
	ext: "",
	tbr: null,
	fps: null,
	vcodec: "",
	acodec: "",
	kind: "video",
	size: null,
	sizeIsEstimate: false,
};

function getFormatHeight(format: FormatRow): number | null {
	if (format.kind === "audio") return null;
	const text = [format.resolution, format.label].filter(Boolean).join(" ");
	const heightMatch = text.match(/(\d{2,4})p(?:\d{2,3})?/i);
	if (heightMatch) return Number.parseInt(heightMatch[1], 10);
	const dimMatch = text.match(/(\d{3,5})x(\d{3,5})/i);
	if (dimMatch) return Number.parseInt(dimMatch[2], 10);
	const wordMatch = text.match(/\b(4k|uhd|8k|fhd|qhd|2k|hd)\b/i);
	if (wordMatch) {
		const known: Record<string, number> = {
			"4k": 2160,
			uhd: 2160,
			"8k": 4320,
			fhd: 1080,
			qhd: 1440,
			"2k": 1440,
			hd: 720,
		};
		return known[wordMatch[1].toLowerCase()] ?? null;
	}
	return null;
}

function isLowResolution(format: FormatRow): boolean {
	if (format.kind === "audio") return false;
	const height = getFormatHeight(format);
	return height !== null && height < 240;
}

function TitleCell({ job }: { job: Job }) {
	const Icon = getFileIcon(job.filename || job.url, job.mediaType);
	const [error, setError] = useState(false);
	// The backend swaps a protected thumbnail URL for a data URI shortly after
	// start; retry the <img> when the source changes instead of staying latched.
	useEffect(() => setError(false), [job.thumbnail]);

	const showThumbnail = job.thumbnail && !error;

	return (
		<div className="flex min-w-0 items-center gap-3">
			<Squircle
				className="flex w-20 aspect-video shrink-0 items-center justify-center overflow-hidden bg-surface-tertiary text-muted"
				cornerRadius={6}
			>
				{showThumbnail ? (
					<img
						alt={job.title || "Download thumbnail"}
						className="h-full w-full object-cover"
						src={job.thumbnail}
						onError={() => setError(true)}
					/>
				) : (
					<Icon aria-hidden className="size-5" />
				)}
			</Squircle>
			<div className="min-w-0">
				<Typography
					className="truncate font-medium"
					title={job.title || job.url}
					type="body-sm"
				>
					{job.title || job.url}
				</Typography>
			</div>
		</div>
	);
}

function ProgressCell({ job }: { job: Job }) {
	const indeterminate = job.status === "downloading" && !job.total;
	const secondary = [formatSpeed(job.speed), formatEta(job.eta)]
		.filter(Boolean)
		.join(" · ");

	return (
		<div className="min-w-47.5 space-y-2">
			<ProgressBar
				aria-label={`${job.title || "Download"} progress`}
				className="w-full"
				color={
					job.status === "failed"
						? "danger"
						: job.status === "completed"
							? "success"
							: "accent"
				}
				isIndeterminate={indeterminate}
				value={Math.min(100, job.percent)}
				size="sm"
			>
				<Squircle asChild cornerRadius={4}>
					<ProgressBar.Track>
						<ProgressBar.Fill />
					</ProgressBar.Track>
				</Squircle>
			</ProgressBar>
			<div className="flex items-center justify-between gap-3 text-xs">
				<span className="tabular-nums font-medium text-foreground">
					{job.percent.toFixed(1)}%
				</span>
				{secondary ? (
					<span className="tabular-nums text-muted">{secondary}</span>
				) : null}
			</div>
			{job.error ? (
				<Typography
					className="truncate text-danger"
					title={job.error}
					type="body-xs"
				>
					{job.error}
				</Typography>
			) : null}
		</div>
	);
}

function SizeCell({ job }: { job: Job }) {
	return (
		<div className="flex flex-col items-center justify-center space-y-1 text-center w-full">
			<Typography
				className="tabular-nums font-medium text-center"
				type="body-sm"
			>
				{formatBytes(job.downloaded)}{" "}
				<span className="text-muted">/ {formatBytes(job.total)}</span>
			</Typography>
			{job.segmentsTotal ? (
				<Typography
					className="tabular-nums text-center"
					color="muted"
					type="body-xs"
				>
					{job.segmentsDone ?? 0}/{job.segmentsTotal} segments
				</Typography>
			) : null}
		</div>
	);
}

type ContextMenuState = { x: number; y: number } | null;

const STATUS_ACTIONS = {
	pause: (status: JobStatus) => status === "queued" || status === "downloading",
	resume: (status: JobStatus) =>
		status === "paused" || status === "failed" || status === "cancelled",
	cancel: (status: JobStatus) =>
		status === "queued" || status === "downloading",
};

function actionTargets(
	jobs: Job[],
	action: "pause" | "resume" | "cancel" | "remove",
) {
	if (action === "remove") return jobs;
	return jobs.filter((job) => STATUS_ACTIONS[action](job.status));
}

function canReveal(job: Job) {
	return (
		(job.status === "completed" || job.isPlaylist) &&
		(Boolean(job.finalFilepath) ||
			Boolean(job.filename && job.directory) ||
			Boolean(job.directory))
	);
}

function JobContextMenu({
	state,
	selectedJobs,
	backend,
	onProperties,
	onClose,
	onError,
}: {
	state: Exclude<ContextMenuState, null>;
	selectedJobs: Job[];
	backend: Backend;
	onProperties: (job: Job) => void;
	onClose: () => void;
	onError: (message: string) => void;
}) {
	const isSingle = selectedJobs.length === 1;

	const pauseTargets = actionTargets(selectedJobs, "pause");
	const resumeTargets = actionTargets(selectedJobs, "resume");
	const cancelTargets = actionTargets(selectedJobs, "cancel");
	const removeTargets = actionTargets(selectedJobs, "remove");

	const canShowReveal = isSingle && canReveal(selectedJobs[0]);
	const canShowProperties = isSingle;

	const left = Math.min(state.x, window.innerWidth - 256);
	const top = Math.min(state.y, window.innerHeight - 288);

	const handleAction = async (key: string) => {
		onClose();
		if (["pause", "resume", "cancel", "remove"].includes(key)) {
			const action = key as "pause" | "resume" | "cancel" | "remove";
			const targets = actionTargets(selectedJobs, action);
			if (targets.length === 0) return;
			const results = await Promise.all(
				targets.map((job) => backend.jobAction(job.id, action)),
			);
			const error = results
				.map((res) => res.error)
				.filter(Boolean)
				.join("; ");
			if (error) onError(`Could not ${action}: ${error}`);
			return;
		}
		if (key === "reveal") {
			const job = selectedJobs[0];
			if (!job) return;
			const result = await backend.reveal(job.id);
			if (!result.ok) {
				onError(result.error ?? "Could not reveal file");
			}
			return;
		}
		if (key === "properties") onProperties(selectedJobs[0]);
	};

	const plural = (label: string, count: number) =>
		count <= 1 ? label : `${label} ${count}`;

	const label = isSingle
		? selectedJobs[0]?.title || selectedJobs[0]?.url || "selected"
		: `${selectedJobs.length} selected`;

	return (
		<Dropdown isOpen onOpenChange={(open) => !open && onClose()}>
			<Squircle asChild cornerRadius={24}>
				<Dropdown.Popover
					aria-label={`Actions for ${label}`}
					className="fixed z-50 min-w-56"
					style={{ left: Math.max(8, left), top: Math.max(8, top) }}
				>
					<Dropdown.Menu onAction={(key) => handleAction(String(key))}>
						{pauseTargets.length > 0 ? (
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Dropdown.Item id="pause" textValue="Pause">
									<Pause aria-hidden className="size-4 text-muted" />
									<Label>{plural("Pause", pauseTargets.length)}</Label>
								</Dropdown.Item>
							</Squircle>
						) : null}
						{resumeTargets.length > 0 ? (
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Dropdown.Item id="resume" textValue="Resume">
									<Play aria-hidden className="size-4 text-muted" />
									<Label>{plural("Resume", resumeTargets.length)}</Label>
								</Dropdown.Item>
							</Squircle>
						) : null}
						{cancelTargets.length > 0 ? (
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Dropdown.Item id="cancel" textValue="Cancel">
									<X aria-hidden className="size-4 text-muted" />
									<Label>{plural("Cancel", cancelTargets.length)}</Label>
								</Dropdown.Item>
							</Squircle>
						) : null}
						{canShowReveal ? (
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Dropdown.Item
									id="reveal"
									textValue="Reveal in Finder or Explorer"
								>
									<FolderOpen aria-hidden className="size-4 text-muted" />
									<Label>
										Reveal in{" "}
										{navigator.platform.includes("Mac") ? "Finder" : "Explorer"}
									</Label>
								</Dropdown.Item>
							</Squircle>
						) : null}
						{canShowProperties ? (
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Dropdown.Item id="properties" textValue="Properties">
									<Clipboard aria-hidden className="size-4 text-muted" />
									<Label>Properties</Label>
								</Dropdown.Item>
							</Squircle>
						) : null}
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Dropdown.Item id="remove" textValue="Remove" variant="danger">
								<Trash2 aria-hidden className="size-4 text-danger" />
								<Label>{plural("Remove", removeTargets.length)}</Label>
							</Dropdown.Item>
						</Squircle>
					</Dropdown.Menu>
				</Dropdown.Popover>
			</Squircle>
		</Dropdown>
	);
}

function JobTableHeader() {
	return (
		<Squircle
			aria-hidden="true"
			className="mb-2 grid min-w-230 w-full grid-cols-[minmax(0,2fr)_minmax(0,0.7fr)_minmax(0,1.4fr)_minmax(0,0.9fr)] border border-separator bg-surface-tertiary text-xs font-semibold text-muted shadow-sm"
			cornerRadius={12}
		>
			<span className="px-4 py-3 text-start">Title</span>
			<span className="px-4 py-3 text-center">Status</span>
			<span className="px-4 py-3 text-start">Progress</span>
			<span className="px-4 py-3 text-center">Size</span>
		</Squircle>
	);
}

type JobGroup = {
	root: Job;
	children: Job[];
};

function groupJobs(jobs: Job[]): JobGroup[] {
	const byParent = new Map<string, Job[]>();
	const roots: Job[] = [];
	for (const job of jobs) {
		if (job.parentId) {
			const list = byParent.get(job.parentId);
			if (list) list.push(job);
			else byParent.set(job.parentId, [job]);
		} else {
			roots.push(job);
		}
	}
	roots.sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0));
	return roots.map((root) => {
		const children = byParent.get(root.id) ?? [];
		if (root.childIds && root.childIds.length > 0) {
			const order = new Map(root.childIds.map((id, index) => [id, index]));
			children.sort(
				(a, b) => (order.get(a.id) ?? 9999) - (order.get(b.id) ?? 9999),
			);
		}
		return { root, children };
	});
}

function JobRow({
	job,
	isChild,
	className,
	isSelected,
	onSelect,
	onContextMenu,
}: {
	job: Job;
	isChild?: boolean;
	className?: string;
	isSelected: boolean;
	onSelect: (id: string, event: React.MouseEvent) => void;
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	const titleIndent = isChild ? "ps-8" : "ps-4";
	const base = isSelected
		? "bg-surface-tertiary ring-1 ring-inset ring-accent/35"
		: "bg-surface-secondary hover:bg-surface-secondary-hover active:bg-surface-secondary-active";
	return (
		<Squircle
			cornerRadius={isChild ? 0 : 16}
			data-job-row
			className={[
				"group grid cursor-pointer select-none grid-cols-[minmax(0,2fr)_minmax(0,0.7fr)_minmax(0,1.4fr)_minmax(0,0.9fr)] items-center",
				base,
				className || "",
			].join(" ")}
			onClick={(event) => onSelect(job.id, event)}
			onContextMenu={(event) => {
				event.preventDefault();
				onContextMenu(job, event.clientX, event.clientY);
			}}
			tabIndex={0}
			onKeyDown={(event) => {
				if (event.key === "Enter" || event.key === " ") {
					event.preventDefault();
					onSelect(job.id, event as unknown as React.MouseEvent);
					return;
				}
				if (
					event.key !== "ContextMenu" &&
					!(event.shiftKey && event.key === "F10")
				)
					return;
				event.preventDefault();
				const rect = event.currentTarget.getBoundingClientRect();
				onContextMenu(
					job,
					rect.left + rect.width / 2,
					rect.top + rect.height / 2,
				);
			}}
		>
			<div className={`min-w-0 py-4 pe-4 ${titleIndent}`}>
				<TitleCell job={job} />
			</div>
			<div className="px-4 py-4 text-center">
				<StatusChip status={job.status} />
			</div>
			<div className="px-4 py-4">
				<ProgressCell job={job} />
			</div>
			<div className="px-4 py-4 text-center">
				<SizeCell job={job} />
			</div>
		</Squircle>
	);
}

function ParentGroup({
	group,
	selectedIds,
	onSelect,
	onContextMenu,
}: {
	group: JobGroup;
	selectedIds: Set<string>;
	onSelect: (id: string, event: React.MouseEvent) => void;
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	const { root, children } = group;
	const isSelected = selectedIds.has(root.id);
	const rootClass = isSelected
		? "bg-surface-tertiary ring-1 ring-inset ring-accent/35"
		: "bg-surface-secondary hover:bg-surface-secondary-hover active:bg-surface-secondary-active";
	return (
		<Accordion className="w-full overflow-hidden" hideSeparator>
			<Squircle asChild cornerRadius={16}>
				<Accordion.Item className="w-full overflow-hidden" id={root.id}>
					{({ isExpanded }) => (
						<>
							<div
								data-job-row
								className={[
									"group grid w-full cursor-pointer select-none grid-cols-[minmax(0,2fr)_minmax(0,0.7fr)_minmax(0,1.4fr)_minmax(0,0.9fr)] items-center",
									rootClass,
								].join(" ")}
								onClick={(event) => onSelect(root.id, event)}
								onContextMenu={(event) => {
									event.preventDefault();
									onContextMenu(root, event.clientX, event.clientY);
								}}
								tabIndex={0}
								onKeyDown={(event) => {
									if (event.key === "Enter" || event.key === " ") {
										event.preventDefault();
										onSelect(root.id, event as unknown as React.MouseEvent);
										return;
									}
									if (
										event.key !== "ContextMenu" &&
										!(event.shiftKey && event.key === "F10")
									)
										return;
									event.preventDefault();
									const rect = event.currentTarget.getBoundingClientRect();
									onContextMenu(
										root,
										rect.left + rect.width / 2,
										rect.top + rect.height / 2,
									);
								}}
							>
								<div className="flex min-w-0 items-center gap-2 py-4 pe-4 ps-3">
									<Squircle asChild className="ring-inset" cornerRadius={8}>
										<Accordion.Trigger
											aria-label={
												isExpanded ? "Collapse playlist" : "Expand playlist"
											}
											className="flex size-7 shrink-0 flex-initial items-center justify-center p-0 text-muted transition-colors hover:bg-surface-hover hover:text-foreground active:bg-surface-active active:scale-95"
											onClick={(event) => event.stopPropagation()}
										>
											<Accordion.Indicator className="m-0 size-4 shrink-0 text-muted" />
										</Accordion.Trigger>
									</Squircle>
									<span className="min-w-0 flex-1">
										<TitleCell job={root} />
									</span>
								</div>
								<div className="px-4 py-4 text-center">
									<StatusChip status={root.status} />
								</div>
								<div className="px-4 py-4">
									<ProgressCell job={root} />
								</div>
								<div className="px-4 py-4 text-center">
									<SizeCell job={root} />
								</div>
							</div>
							<Accordion.Panel>
								<div className="overflow-hidden bg-surface-secondary border-t border-separator">
									{children.map((child, index) => (
										<JobRow
											className={
												index !== children.length - 1
													? "border-b border-separator"
													: ""
											}
											isChild
											isSelected={selectedIds.has(child.id)}
											job={child}
											key={child.id}
											onContextMenu={onContextMenu}
											onSelect={onSelect}
										/>
									))}
								</div>
							</Accordion.Panel>
						</>
					)}
				</Accordion.Item>
			</Squircle>
		</Accordion>
	);
}

function getFileIcon(filename: string, mediaType?: string) {
	const ext =
		filename
			.split(/[?#]/, 1)[0]
			.split("/")
			.pop()
			?.split(".")
			.pop()
			?.toLowerCase() || "";
	if (["mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m4v"].includes(ext)) {
		return FileVideo;
	}
	if (["mp3", "m4a", "flac", "aac", "wav", "ogg", "opus"].includes(ext)) {
		return FileAudio;
	}
	if (["txt", "pdf", "doc", "docx", "srt", "vtt", "nfo"].includes(ext)) {
		return FileText;
	}
	if (["zip", "rar", "7z", "tar", "gz", "bz2", "iso"].includes(ext)) {
		return Archive;
	}
	if (mediaType === "audio") return FileAudio;
	if (mediaType === "video") return FileVideo;
	return File;
}

function TorrentFileRow({
	file,
	jobStatus,
	isLast,
}: {
	file: TorrentProgressFile;
	jobStatus: JobStatus;
	isLast: boolean;
}) {
	const Icon = getFileIcon(file.filename);
	const fileStatus: JobStatus =
		file.percent >= 100
			? "completed"
			: jobStatus === "downloading"
				? file.status
				: jobStatus;
	const isQueued = fileStatus === "queued";

	return (
		<div
			data-job-row
			className={`grid grid-cols-[minmax(0,2fr)_minmax(0,0.7fr)_minmax(0,1.4fr)_minmax(0,0.9fr)] items-center bg-surface-secondary py-3 text-xs ${
				!isLast ? "border-b border-separator" : ""
			}`}
		>
			<div className="flex min-w-0 items-center gap-2.5 px-4 ps-10">
				<Icon className="size-4 shrink-0 text-muted" />
				<span
					className="truncate font-medium text-foreground"
					title={file.filename}
				>
					{file.filename}
				</span>
			</div>
			<div className="px-4 text-center">
				<StatusChip status={fileStatus} />
			</div>
			<div className="px-4">
				<div className="space-y-1">
					<ProgressBar
						aria-label={`${file.filename} progress`}
						className="h-1.5 w-full"
						color="accent"
						value={isQueued ? 0 : Math.max(0, Math.min(100, file.percent))}
					>
						<ProgressBar.Track className="h-1.5 overflow-hidden rounded-full bg-surface-tertiary">
							<ProgressBar.Fill className="h-full transition-all duration-300" />
						</ProgressBar.Track>
					</ProgressBar>
					<div className="flex justify-between text-[10px] text-muted">
						<span>{isQueued ? "Waiting" : `${file.percent.toFixed(1)}%`}</span>
					</div>
				</div>
			</div>
			<div className="px-4 text-center text-muted">
				{isQueued
					? formatBytes(file.size)
					: `${formatBytes(file.downloaded)} / ${formatBytes(file.size)}`}
			</div>
		</div>
	);
}

function TorrentGroup({
	job,
	selectedIds,
	onSelect,
	onContextMenu,
}: {
	job: Job;
	selectedIds: Set<string>;
	onSelect: (id: string, event: React.MouseEvent) => void;
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	const files = job.files || [];
	const isSelected = selectedIds.has(job.id);
	const rootClass = isSelected
		? "bg-surface-tertiary ring-1 ring-inset ring-accent/35"
		: "bg-surface-secondary hover:bg-surface-secondary-hover active:bg-surface-secondary-active";
	return (
		<Accordion className="w-full overflow-hidden" hideSeparator>
			<Squircle asChild cornerRadius={16}>
				<Accordion.Item className="w-full overflow-hidden" id={job.id}>
					{({ isExpanded }) => (
						<>
							<div
								data-job-row
								className={[
									"group grid w-full cursor-pointer select-none grid-cols-[minmax(0,2fr)_minmax(0,0.7fr)_minmax(0,1.4fr)_minmax(0,0.9fr)] items-center",
									rootClass,
								].join(" ")}
								onClick={(event) => onSelect(job.id, event)}
								onContextMenu={(event) => {
									event.preventDefault();
									onContextMenu(job, event.clientX, event.clientY);
								}}
								tabIndex={0}
								onKeyDown={(event) => {
									if (event.key === "Enter" || event.key === " ") {
										event.preventDefault();
										onSelect(job.id, event as unknown as React.MouseEvent);
										return;
									}
									if (
										event.key !== "ContextMenu" &&
										!(event.shiftKey && event.key === "F10")
									)
										return;
									event.preventDefault();
									const rect = event.currentTarget.getBoundingClientRect();
									onContextMenu(
										job,
										rect.left + rect.width / 2,
										rect.top + rect.height / 2,
									);
								}}
							>
								<div className="flex min-w-0 items-center gap-2 py-4 pe-4 ps-3">
									<Squircle asChild className="ring-inset" cornerRadius={8}>
										<Accordion.Trigger
											aria-label={
												isExpanded ? "Collapse torrent" : "Expand torrent"
											}
											className="flex size-7 shrink-0 flex-initial items-center justify-center p-0 text-muted transition-colors hover:bg-surface-hover hover:text-foreground active:bg-surface-active active:scale-95"
											onClick={(event) => event.stopPropagation()}
										>
											<Accordion.Indicator className="m-0 size-4 shrink-0 text-muted" />
										</Accordion.Trigger>
									</Squircle>
									<span className="min-w-0 flex-1">
										<TitleCell job={job} />
									</span>
								</div>
								<div className="px-4 py-4 text-center">
									<StatusChip status={job.status} />
								</div>
								<div className="px-4 py-4">
									<ProgressCell job={job} />
								</div>
								<div className="px-4 py-4 text-center">
									<SizeCell job={job} />
								</div>
							</div>
							<Accordion.Panel>
								<div className="overflow-hidden bg-surface-secondary border-t border-separator">
									{files.map((file, index) => (
										<TorrentFileRow
											file={file}
											isLast={index === files.length - 1}
											jobStatus={job.status}
											key={file.path || index}
										/>
									))}
								</div>
							</Accordion.Panel>
						</>
					)}
				</Accordion.Item>
			</Squircle>
		</Accordion>
	);
}

function JobGroupList({
	groups,
	selectedIds,
	onSelect,
	onContextMenu,
}: {
	groups: JobGroup[];
	selectedIds: Set<string>;
	onSelect: (id: string, event: React.MouseEvent) => void;
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	return (
		<div className="w-full overflow-x-auto">
			<div className="min-w-230 space-y-2">
				<JobTableHeader />
				<ScrollShadow className="max-h-130 space-y-2 pr-1" visibility="auto">
					{groups.map((group) =>
						group.root.isPlaylist || group.children.length > 0 ? (
							<ParentGroup
								group={group}
								key={group.root.id}
								selectedIds={selectedIds}
								onContextMenu={onContextMenu}
								onSelect={onSelect}
							/>
						) : (group.root.engine === "torrent" ||
								group.root.mediaType === "torrent") &&
							group.root.files &&
							group.root.files.length > 0 ? (
							<TorrentGroup
								job={group.root}
								key={group.root.id}
								selectedIds={selectedIds}
								onContextMenu={onContextMenu}
								onSelect={onSelect}
							/>
						) : (
							<JobRow
								isSelected={selectedIds.has(group.root.id)}
								job={group.root}
								key={group.root.id}
								onContextMenu={onContextMenu}
								onSelect={onSelect}
							/>
						),
					)}
				</ScrollShadow>
			</div>
		</div>
	);
}

function LocationSelect({
	locations,
	value,
	onChange,
}: {
	locations: { name: string; path: string }[];
	value: string;
	onChange: (value: string) => void;
}) {
	return (
		<Select
			className="w-full"
			value={value}
			onChange={(next) => onChange(String(next))}
		>
			<Label>Download location</Label>
			<Squircle asChild className="ring-inset" cornerRadius={12}>
				<Select.Trigger>
					<Select.Value />
					<Select.Indicator />
				</Select.Trigger>
			</Squircle>
			<Squircle asChild cornerRadius={24}>
				<Select.Popover>
					<ListBox>
						{locations.map((location) => (
							<Squircle
								asChild
								className="ring-inset"
								cornerRadius={16}
								key={location.path}
							>
								<ListBox.Item id={location.path} textValue={location.name}>
									{location.name}
									<ListBox.ItemIndicator />
								</ListBox.Item>
							</Squircle>
						))}
					</ListBox>
				</Select.Popover>
			</Squircle>
		</Select>
	);
}

function PlaylistSummary({ probe }: { probe: ProbeResponse }) {
	const result = probe.result;
	if (!result) return null;
	const title = result.playlistTitle || result.title;
	const count = result.playlistCount ?? (result.entries?.length || 0);

	return (
		<Squircle asChild cornerRadius={24}>
			<Card
				className="gap-4 border border-border/70 bg-surface/75 p-4 shadow-sm"
				variant="transparent"
			>
				<div className="flex items-center gap-4">
					<Squircle
						className="flex w-28 sm:w-32 aspect-video shrink-0 items-center justify-center overflow-hidden border border-border/50 bg-surface-secondary text-muted"
						cornerRadius={12}
					>
						{result.thumbnail ? (
							<img
								alt={title || "Playlist thumbnail"}
								className="size-full object-cover"
								src={result.thumbnail}
							/>
						) : (
							<FileVideo aria-hidden className="size-6" />
						)}
					</Squircle>
					<div className="min-w-0">
						<Typography
							className="line-clamp-2 font-semibold text-foreground"
							type="h6"
						>
							{title || result.webpageUrl}
						</Typography>
						<div className="mt-2 flex flex-wrap gap-2">
							<SquircleChip color="accent" size="sm" variant="soft">
								{probe.engine}
							</SquircleChip>
							{result.extractor ? (
								<SquircleChip size="sm" variant="soft">
									{result.extractor}
								</SquircleChip>
							) : null}
							{result.uploader ? (
								<SquircleChip size="sm" variant="soft">
									{result.uploader}
								</SquircleChip>
							) : null}
							{count ? (
								<SquircleChip size="sm" variant="soft">
									{count} videos
								</SquircleChip>
							) : null}
						</div>
					</div>
				</div>
				<Typography
					className="truncate font-mono text-[11px] text-muted"
					type="body-xs"
				>
					{result.webpageUrl}
				</Typography>
			</Card>
		</Squircle>
	);
}

function PlaylistPanel({
	entries,
	currentEntryId,
	selectedUrls,
	onToggle,
}: {
	entries: PlaylistEntry[];
	currentEntryId?: string | null;
	selectedUrls: Set<string>;
	onToggle: (url: string, checked: boolean) => void;
}) {
	return (
		<div className="space-y-2 max-h-80 overflow-y-auto pr-1">
			{entries.map((entry) => {
				const selected = selectedUrls.has(entry.url);
				const isCurrent = currentEntryId && entry.id === currentEntryId;
				return (
					<Squircle
						className={`flex items-start gap-3 border p-3 transition-colors ${selected ? "border-accent/60 bg-accent-soft/20" : "border-border/70 bg-surface/75 hover:border-border-visible hover:bg-surface-secondary/70 active:bg-surface-secondary"}`}
						cornerRadius={12}
						key={entry.url}
					>
						<Checkbox
							aria-label={`Select ${entry.title}`}
							isSelected={selected}
							onChange={(checked) => onToggle(entry.url, checked)}
						>
							<Checkbox.Content>
								<Squircle asChild className="ring-inset" cornerRadius={6}>
									<Checkbox.Control>
										<Checkbox.Indicator />
									</Checkbox.Control>
								</Squircle>
							</Checkbox.Content>
						</Checkbox>
						<Squircle
							className="flex w-20 aspect-video shrink-0 items-center justify-center overflow-hidden border border-border/50 bg-surface-secondary text-muted"
							cornerRadius={8}
						>
							{entry.thumbnail ? (
								<img
									alt={entry.title || "Thumbnail"}
									className="size-full object-cover"
									src={entry.thumbnail}
								/>
							) : (
								<FileVideo aria-hidden className="size-5" />
							)}
						</Squircle>
						<div className="min-w-0 flex-1">
							<div className="flex flex-wrap items-center gap-2">
								<Typography
									className={`line-clamp-2 ${selected ? "font-semibold text-foreground" : "font-medium text-foreground"}`}
									type="body-sm"
								>
									{entry.title}
								</Typography>
								{isCurrent ? (
									<SquircleChip color="accent" size="sm" variant="soft">
										Current
									</SquircleChip>
								) : null}
							</div>
							<div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
								{entry.uploader ? <span>{entry.uploader}</span> : null}
								{entry.uploader && entry.duration ? <span>·</span> : null}
								{entry.duration ? (
									<span className="tabular-nums">
										{formatDuration(entry.duration)}
									</span>
								) : null}
							</div>
						</div>
					</Squircle>
				);
			})}
		</div>
	);
}

function stemFromName(name: string): string {
	const lastDot = name.lastIndexOf(".");
	return lastDot > 0 ? name.slice(0, lastDot) : name;
}

function MediaSummary({
	probe,
	selectedFormat,
}: {
	probe: ProbeResponse;
	selectedFormat?: FormatRow;
}) {
	const result = probe.result;
	if (!result) return null;

	const isAudio = selectedFormat?.kind === "audio";
	const filename = result.filename || result.webpageUrl;
	const displayFilename =
		isAudio && filename ? `${stemFromName(filename)}.mp3` : filename;

	return (
		<Squircle asChild cornerRadius={24}>
			<Card
				className="gap-4 border border-border/70 bg-surface/75 p-4 shadow-sm"
				variant="transparent"
			>
				<div className="flex items-center gap-4">
					<Squircle
						className="flex w-28 sm:w-32 aspect-video shrink-0 items-center justify-center overflow-hidden border border-border/50 bg-surface-secondary text-muted"
						cornerRadius={12}
					>
						{result.thumbnail ? (
							<img
								alt={result.title || "Media thumbnail"}
								className="size-full object-cover"
								src={result.thumbnail}
							/>
						) : isAudio ? (
							<FileAudio aria-hidden className="size-6" />
						) : (
							<FileVideo aria-hidden className="size-6" />
						)}
					</Squircle>
					<div className="min-w-0">
						<Typography
							className="line-clamp-2 font-semibold text-foreground"
							type="h6"
						>
							{result.title || result.webpageUrl}
						</Typography>
						<div className="mt-2 flex flex-wrap gap-2">
							<SquircleChip color="accent" size="sm" variant="soft">
								{probe.engine}
							</SquircleChip>
							{result.extractor ? (
								<SquircleChip size="sm" variant="soft">
									{result.extractor}
								</SquircleChip>
							) : null}
							{result.uploader ? (
								<SquircleChip size="sm" variant="soft">
									{result.uploader}
								</SquircleChip>
							) : null}
							{result.isPlaylist && result.playlistCount ? (
								<SquircleChip size="sm" variant="soft">
									{result.playlistCount} videos
								</SquircleChip>
							) : null}
						</div>
					</div>
				</div>
				<Typography
					className="truncate font-mono text-[11px] text-muted"
					type="body-xs"
				>
					{displayFilename}
				</Typography>
			</Card>
		</Squircle>
	);
}

function FormatPanel({
	title = "Quality",
	formats,
	selectedId,
	onSelect,
}: {
	title?: string;
	formats: FormatRow[];
	selectedId: string;
	onSelect: (id: string) => void;
}) {
	return (
		<div className="space-y-3">
			<div className="flex items-center justify-between">
				<Typography className="font-semibold" type="h6">
					{title}
				</Typography>
				<Typography color="muted" type="body-xs">
					{formats.length} {formats.length === 1 ? "format" : "formats"}{" "}
					available
				</Typography>
			</div>
			<div className="space-y-2">
				{formats.map((format) => {
					const isSelected = format.id === selectedId;
					const isAudio = format.kind === "audio";
					const sublabel = isAudio
						? [
								format.ext ? format.ext.toUpperCase() : null,
								format.acodec || null,
							]
								.filter(Boolean)
								.join(" · ") || "Audio"
						: format.resolution || format.kind;

					return (
						<Squircle
							asChild
							className="ring-inset"
							cornerRadius={12}
							key={format.id}
						>
							<Button
								aria-pressed={isSelected}
								className={`h-auto min-h-14 justify-between px-4 py-3 text-start transition-colors ${isSelected ? "border border-accent/60 bg-accent-soft/25 ring-1 ring-inset ring-accent/25 shadow-sm" : "border border-border/70 bg-surface/75 hover:border-border-visible hover:bg-surface-secondary/70 active:bg-surface-secondary"}`}
								fullWidth
								onPress={() => onSelect(format.id)}
								variant="outline"
							>
								<span className="flex min-w-0 items-center gap-3">
									<span
										className={`flex size-5 items-center justify-center rounded-full border transition-colors ${isSelected ? "border-accent bg-accent text-accent-foreground shadow-xs" : "border-border-visible bg-surface-secondary/80 text-transparent"}`}
									>
										{isSelected ? (
											<Check aria-hidden className="size-3" />
										) : null}
									</span>
									<span className="min-w-0">
										<Typography
											className={`truncate ${isSelected ? "font-semibold text-foreground" : "font-medium text-foreground"}`}
											type="body-sm"
										>
											{format.label}
										</Typography>
										<Typography color="muted" type="body-xs">
											{sublabel}
										</Typography>
									</span>
								</span>
								<span className="flex shrink-0 items-center gap-3">
									<SquircleChip
										color={isSelected ? "accent" : "default"}
										size="sm"
										variant="soft"
									>
										{format.ext ? format.ext.toUpperCase() : format.kind}
									</SquircleChip>
									<Typography
										className="tabular-nums"
										color="muted"
										type="body-xs"
									>
										{format.size == null
											? "Size unknown"
											: `${format.sizeIsEstimate ? "~ " : ""}${formatBytes(format.size)}`}
									</Typography>
								</span>
							</Button>
						</Squircle>
					);
				})}
			</div>
		</div>
	);
}

function VideoPanel(props: {
	formats: FormatRow[];
	selectedId: string;
	onSelect: (id: string) => void;
}) {
	return <FormatPanel title="Quality" {...props} />;
}

function AudioPanel(props: {
	formats: FormatRow[];
	selectedId: string;
	onSelect: (id: string) => void;
}) {
	return <FormatPanel title="Audio quality" {...props} />;
}

function ProbeModal({
	probe,
	settings,
	onClose,
	onDownloadVideo,
	onDownloadPlaylist,
}: {
	probe: ProbeResponse;
	settings: Settings | null;
	onClose: () => void;
	onDownloadVideo: (format: FormatRow, directory: string) => void;
	onDownloadPlaylist: (selectedUrls: string[], directory: string) => void;
}) {
	const result = probe.result;

	const allFormats = result?.formats ?? [];
	const visibleFormats = allFormats.filter(
		(format) => format.kind === "audio" || !isLowResolution(format),
	);
	const videoFormatsRaw = visibleFormats.filter(
		(format) => format.kind !== "audio",
	);
	const audioFormats = visibleFormats.filter(
		(format) => format.kind === "audio",
	);
	const videoFormats =
		videoFormatsRaw.length > 0
			? videoFormatsRaw
			: audioFormats.length === 0
				? [FALLBACK_FORMAT]
				: [];

	const hasVideo = videoFormats.length > 0;
	const hasPlaylist = Boolean(
		result?.isPlaylist && (result.entries?.length ?? 0) > 0,
	);
	const hasAudio = audioFormats.length > 0 && !hasPlaylist;

	const [selectedKey, setSelectedKey] = useState<
		"video" | "audio" | "playlist"
	>(hasVideo ? "video" : hasAudio ? "audio" : "playlist");

	const [selectedVideoFormatId, setSelectedVideoFormatId] = useState(
		videoFormats[0]?.id ?? "best",
	);
	const [selectedAudioFormatId, setSelectedAudioFormatId] = useState(
		audioFormats[0]?.id ?? "",
	);

	const selectedVideoFormat =
		videoFormats.find((format) => format.id === selectedVideoFormatId) ??
		videoFormats[0];
	const selectedAudioFormat =
		audioFormats.find((format) => format.id === selectedAudioFormatId) ??
		audioFormats[0];

	const selectedFormat =
		selectedKey === "audio" ? selectedAudioFormat : selectedVideoFormat;

	const allEntryUrls = useMemo(
		() => result?.entries?.map((entry) => entry.url) ?? [],
		[result],
	);
	const [selectedUrls, setSelectedUrls] = useState<Set<string>>(
		() => new Set(allEntryUrls),
	);

	const locations = useMemo(() => {
		const paths = new Set<string>();
		const values: { name: string; path: string }[] = [];
		const add = (name: string, path: string) => {
			if (!path || paths.has(path)) return;
			paths.add(path);
			values.push({ name, path });
		};
		if (settings) {
			add(`Default · ${settings.downloadDir}`, settings.downloadDir);
			for (const preset of settings.presetPaths) add(preset.name, preset.path);
		}
		return values;
	}, [settings]);
	const [directory, setDirectory] = useState(locations[0]?.path ?? "");

	if (!result) return null;

	const selectedCount = selectedUrls.size;

	const toggleUrl = (url: string, checked: boolean) => {
		setSelectedUrls((current) => {
			const next = new Set(current);
			if (checked) next.add(url);
			else next.delete(url);
			return next;
		});
	};

	const allSelected =
		result.entries?.length === selectedCount && selectedCount > 0;

	return (
		<Modal.Backdrop isOpen onOpenChange={(open) => !open && onClose()}>
			<Modal.Container scroll="inside" size="lg">
				<Squircle asChild cornerRadius={24}>
					<Modal.Dialog className="modal-no-press-scale w-full sm:max-w-2xl">
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Modal.CloseTrigger />
						</Squircle>
						<Modal.Header>
							<Modal.Heading>
								{(hasVideo || hasAudio) && hasPlaylist
									? "Download options"
									: hasAudio && !hasVideo
										? "Select audio format"
										: "Select a format"}
							</Modal.Heading>
						</Modal.Header>
						<Modal.Body className="gap-5">
							{selectedKey === "playlist" ? (
								<PlaylistSummary probe={probe} />
							) : (
								<MediaSummary probe={probe} selectedFormat={selectedFormat} />
							)}

							<Tabs
								selectedKey={selectedKey}
								onSelectionChange={(key) =>
									setSelectedKey(String(key) as "video" | "audio" | "playlist")
								}
							>
								<Tabs.ListContainer className="rounded-full border border-border/60 bg-surface/80 p-1">
									<Tabs.List
										aria-label="Download options"
										className="gap-1 p-0"
									>
										{hasVideo ? (
											<Tabs.Tab
												className="gap-2 rounded-full px-4 py-1.5 text-xs sm:text-sm font-medium transition-colors text-muted data-[selected=true]:text-foreground"
												id="video"
											>
												Video
												<Tabs.Indicator className="rounded-full bg-segment border border-border-visible/60 shadow-sm" />
											</Tabs.Tab>
										) : null}
										{hasAudio ? (
											<Tabs.Tab
												className="gap-2 rounded-full px-4 py-1.5 text-xs sm:text-sm font-medium transition-colors text-muted data-[selected=true]:text-foreground"
												id="audio"
											>
												Audio
												<Tabs.Indicator className="rounded-full bg-segment border border-border-visible/60 shadow-sm" />
											</Tabs.Tab>
										) : null}
										{hasPlaylist ? (
											<Tabs.Tab
												className="gap-2 rounded-full px-4 py-1.5 text-xs sm:text-sm font-medium transition-colors text-muted data-[selected=true]:text-foreground"
												id="playlist"
											>
												Playlist
												<Tabs.Indicator className="rounded-full bg-segment border border-border-visible/60 shadow-sm" />
											</Tabs.Tab>
										) : null}
									</Tabs.List>
								</Tabs.ListContainer>
								{hasVideo ? (
									<Tabs.Panel id="video" className="p-0 pt-2">
										<VideoPanel
											formats={videoFormats}
											selectedId={selectedVideoFormatId}
											onSelect={setSelectedVideoFormatId}
										/>
									</Tabs.Panel>
								) : null}
								{hasAudio ? (
									<Tabs.Panel id="audio" className="p-0 pt-2">
										<AudioPanel
											formats={audioFormats}
											selectedId={selectedAudioFormatId}
											onSelect={setSelectedAudioFormatId}
										/>
									</Tabs.Panel>
								) : null}
								{hasPlaylist ? (
									<Tabs.Panel id="playlist" className="p-0 pt-2">
										<div className="space-y-4">
											<div className="flex items-center justify-between">
												<Typography className="font-semibold" type="h6">
													Videos to download
												</Typography>
												<div className="flex items-center gap-2 text-xs text-muted">
													<Checkbox
														isSelected={allSelected}
														onChange={(checked) =>
															setSelectedUrls(
																checked ? new Set(allEntryUrls) : new Set(),
															)
														}
													>
														<Checkbox.Content className="text-foreground">
															<Squircle
																asChild
																className="ring-inset"
																cornerRadius={6}
															>
																<Checkbox.Control>
																	<Checkbox.Indicator />
																</Checkbox.Control>
															</Squircle>
															Select all
														</Checkbox.Content>
													</Checkbox>
												</div>
											</div>
											{result.entries ? (
												<PlaylistPanel
													currentEntryId={result.currentEntryId}
													entries={result.entries}
													selectedUrls={selectedUrls}
													onToggle={toggleUrl}
												/>
											) : null}
										</div>
									</Tabs.Panel>
								) : null}
							</Tabs>
							<LocationSelect
								locations={locations}
								onChange={setDirectory}
								value={directory}
							/>
						</Modal.Body>
						<Modal.Footer>
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Button slot="close" variant="tertiary">
									Cancel
								</Button>
							</Squircle>
							{selectedKey === "playlist" ? (
								<Squircle asChild className="ring-inset" cornerRadius={12}>
									<Button
										isDisabled={selectedCount === 0}
										onPress={() =>
											onDownloadPlaylist([...selectedUrls], directory)
										}
									>
										<Download aria-hidden className="size-4" />
										Download {selectedCount} video
										{selectedCount === 1 ? "" : "s"}
									</Button>
								</Squircle>
							) : (
								<Squircle asChild className="ring-inset" cornerRadius={12}>
									<Button
										isDisabled={!selectedFormat}
										onPress={() =>
											selectedFormat &&
											onDownloadVideo(selectedFormat, directory)
										}
									>
										<Download aria-hidden className="size-4" />
										Download
									</Button>
								</Squircle>
							)}
						</Modal.Footer>
					</Modal.Dialog>
				</Squircle>
			</Modal.Container>
		</Modal.Backdrop>
	);
}

function TorrentDrawer({
	torrent,
	settings,
	onClose,
	onDownload,
}: {
	torrent: TorrentInfo;
	settings: Settings | null;
	onClose: () => void;
	onDownload: (directory: string, selectedFiles: string[]) => void;
}) {
	const locations = useMemo(() => {
		const paths = new Set<string>();
		const values: { name: string; path: string }[] = [];
		const add = (name: string, path: string) => {
			if (!path || paths.has(path)) return;
			paths.add(path);
			values.push({ name, path });
		};
		if (settings) {
			add(`Default · ${settings.downloadDir}`, settings.downloadDir);
			for (const preset of settings.presetPaths) add(preset.name, preset.path);
		}
		return values;
	}, [settings]);
	const [directory, setDirectory] = useState(locations[0]?.path ?? "");
	const [selectedFiles, setSelectedFiles] = useState(
		() => new Set(torrent.files.map((file) => file.path)),
	);

	return (
		<Drawer.Backdrop isOpen onOpenChange={(open) => !open && onClose()}>
			<Drawer.Content placement="right">
				<Drawer.Dialog className="w-full sm:max-w-2xl">
					<Squircle asChild className="ring-inset" cornerRadius={12}>
						<Drawer.CloseTrigger />
					</Squircle>
					<Drawer.Header>
						<Drawer.Heading>Download torrent</Drawer.Heading>
					</Drawer.Header>
					<Drawer.Body className="gap-6">
						<Squircle asChild cornerRadius={24}>
							<Card
								className="gap-4 border border-border/70 bg-surface/75 p-4 shadow-sm"
								variant="transparent"
							>
								<div className="flex items-center gap-4">
									<Squircle
										className="flex size-16 shrink-0 items-center justify-center bg-accent-soft text-accent"
										cornerRadius={12}
									>
										<Archive aria-hidden className="size-7" />
									</Squircle>
									<div className="min-w-0">
										<Typography
											className="line-clamp-2 font-semibold text-foreground"
											type="h6"
										>
											{torrent.name}
										</Typography>
										<div className="mt-2 flex flex-wrap gap-2">
											<SquircleChip color="accent" size="sm" variant="soft">
												libtorrent
											</SquircleChip>
											<SquircleChip size="sm" variant="soft">
												{formatBytes(torrent.totalSize)}
											</SquircleChip>
											<SquircleChip size="sm" variant="soft">
												{torrent.fileCount} files
											</SquircleChip>
										</div>
									</div>
								</div>
							</Card>
						</Squircle>
						<div className="space-y-3">
							<div className="flex items-center justify-between">
								<Typography className="font-semibold" type="h6">
									Files to download
								</Typography>
								<Typography color="muted" type="body-xs">
									{selectedFiles.size} selected
								</Typography>
							</div>
							<Squircle
								className="divide-y divide-border/60 border border-border/70 bg-surface/60"
								cornerRadius={12}
							>
								{torrent.files.map((file) => (
									<Checkbox
										className="w-full px-4 py-3"
										isSelected={selectedFiles.has(file.path)}
										key={file.path}
										onChange={(checked) =>
											setSelectedFiles((current) => {
												const next = new Set(current);
												if (checked) next.add(file.path);
												else next.delete(file.path);
												return next;
											})
										}
									>
										<Checkbox.Content className="min-w-0 flex-1">
											<Squircle asChild className="ring-inset" cornerRadius={6}>
												<Checkbox.Control>
													<Checkbox.Indicator />
												</Checkbox.Control>
											</Squircle>
											<span className="flex min-w-0 items-center justify-between gap-3">
												<span className="truncate text-sm">{file.path}</span>
												<span className="shrink-0 font-mono text-xs text-muted">
													{formatBytes(file.size)}
												</span>
											</span>
										</Checkbox.Content>
									</Checkbox>
								))}
							</Squircle>
						</div>
						<LocationSelect
							locations={locations}
							onChange={setDirectory}
							value={directory}
						/>
					</Drawer.Body>
					<Drawer.Footer>
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Button slot="close" variant="tertiary">
								Cancel
							</Button>
						</Squircle>
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Button
								isDisabled={selectedFiles.size === 0}
								onPress={() => onDownload(directory, [...selectedFiles])}
							>
								<Download aria-hidden className="size-4" />
								Download torrent
							</Button>
						</Squircle>
					</Drawer.Footer>
				</Drawer.Dialog>
			</Drawer.Content>
		</Drawer.Backdrop>
	);
}

function JobPropertiesModal({
	job,
	onClose,
}: {
	job: Job;
	onClose: () => void;
}) {
	const created = useMemo(
		() => new Date(job.createdAt * 1000).toLocaleString(),
		[job.createdAt],
	);
	const fields = [
		["ID", job.id],
		["Title", job.title],
		["URL", job.url],
		["Page URL", job.referer],
		["Engine", job.engine],
		["Status", job.status],
		["Progress", `${job.percent.toFixed(1)}%`],
		[
			"Downloaded",
			`${formatBytes(job.downloaded)} / ${formatBytes(job.total)}`,
		],
		["Speed", formatSpeed(job.speed)],
		["ETA", formatEta(job.eta)],
		[
			"Segments",
			job.segmentsTotal
				? `${job.segmentsDone ?? 0} / ${job.segmentsTotal}`
				: "",
		],
		["Destination", job.directory],
		["Temp directory", job.tempDirectory],
		["Final file", job.finalFilepath],
		["Error", job.error],
		["Created", created],
	].filter(
		([, value]) => value !== undefined && value !== null && value !== "",
	);

	return (
		<Modal.Backdrop isOpen onOpenChange={(open) => !open && onClose()}>
			<Modal.Container size="lg">
				<Squircle asChild cornerRadius={24}>
					<Modal.Dialog>
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Modal.CloseTrigger />
						</Squircle>
						<Modal.Header>
							<Modal.Heading>Task properties</Modal.Heading>
						</Modal.Header>
						<Modal.Body className="gap-5">
							{job.thumbnail ? (
								<Squircle asChild cornerRadius={16}>
									<img
										alt=""
										className="aspect-video w-full object-cover"
										src={job.thumbnail}
									/>
								</Squircle>
							) : null}
							<Squircle
								asChild
								className="grid gap-px overflow-hidden border border-border/70 bg-border/60 sm:grid-cols-2"
								cornerRadius={16}
							>
								<dl>
									{fields.map(([label, value]) => (
										<div className="bg-surface/85 p-3" key={label}>
											<dt className="text-[11px] font-semibold tracking-wide text-muted uppercase">
												{label}
											</dt>
											<dd className="mt-1 wrap-break-word font-mono text-xs text-foreground">
												{value}
											</dd>
										</div>
									))}
								</dl>
							</Squircle>
						</Modal.Body>
						<Modal.Footer>
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Button slot="close" variant="tertiary">
									Close
								</Button>
							</Squircle>
						</Modal.Footer>
					</Modal.Dialog>
				</Squircle>
			</Modal.Container>
		</Modal.Backdrop>
	);
}

type DuplicatePromptState = {
	info: DuplicateInfo;
	retry: (action: DuplicateAction) => Promise<void>;
};

function DuplicatePrompt({
	state,
	onClose,
	onChoose,
}: {
	state: DuplicatePromptState;
	onClose: () => void;
	onChoose: (action: DuplicateAction) => void;
}) {
	const existing =
		typeof state.info.existing === "string"
			? state.info.existing
			: state.info.existing.filename || state.info.existing.title;
	return (
		<Modal.Backdrop isOpen onOpenChange={(open) => !open && onClose()}>
			<Modal.Container size="md">
				<Squircle asChild cornerRadius={24}>
					<Modal.Dialog>
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Modal.CloseTrigger />
						</Squircle>
						<Modal.Header>
							<Modal.Heading>Duplicate found</Modal.Heading>
						</Modal.Header>
						<Modal.Body className="gap-3">
							<Typography type="body-sm">
								{state.info.type === "job"
									? "A matching download is already active."
									: state.info.type === "torrent"
										? "Torrent data already exists in the selected destination."
										: "A file with this name already exists in the destination."}
							</Typography>
							{existing ? (
								<Squircle
									className="border border-border/60 bg-surface/75 p-3"
									cornerRadius={12}
								>
									<Typography
										className="wrap-break-word font-mono text-foreground/90"
										type="body-xs"
									>
										<span className="text-muted">Existing:</span> {existing}
									</Typography>
								</Squircle>
							) : null}
							{state.info.suggestedName ? (
								<Squircle
									className="border border-border/60 bg-surface/75 p-3"
									cornerRadius={12}
								>
									<Typography
										className="wrap-break-word font-mono text-foreground/90"
										type="body-xs"
									>
										<span className="text-muted">Suggested:</span>{" "}
										{state.info.suggestedName}
									</Typography>
								</Squircle>
							) : null}
						</Modal.Body>
						<Modal.Footer>
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Button variant="tertiary" onPress={() => onChoose("skip")}>
									Skip
								</Button>
							</Squircle>
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Button
									variant="danger-soft"
									onPress={() => onChoose("override")}
								>
									Override
								</Button>
							</Squircle>
							{state.info.type === "torrent" ? (
								<Squircle asChild className="ring-inset" cornerRadius={12}>
									<Button onPress={() => onChoose("resume")}>Resume</Button>
								</Squircle>
							) : (
								<Squircle asChild className="ring-inset" cornerRadius={12}>
									<Button onPress={() => onChoose("rename")}>Rename</Button>
								</Squircle>
							)}
						</Modal.Footer>
					</Modal.Dialog>
				</Squircle>
			</Modal.Container>
		</Modal.Backdrop>
	);
}

function EmptyQueue() {
	return (
		<Squircle asChild cornerRadius={24}>
			<Card
				className="items-center gap-4 border border-dashed border-separator bg-transparent px-6 py-16 text-center shadow-none"
				variant="transparent"
			>
				<Squircle
					className="flex size-12 items-center justify-center bg-surface-tertiary text-muted"
					cornerRadius={16}
				>
					<HardDriveDownload aria-hidden className="size-6" />
				</Squircle>
				<div className="max-w-md space-y-2">
					<Typography className="font-semibold" type="h6">
						Your queue is clear
					</Typography>
					<Typography color="muted" type="body-sm">
						Probe a video, page, or magnet link above to add the first transfer.
					</Typography>
				</div>
			</Card>
		</Squircle>
	);
}

export default function DownloadsPage({ backend }: { backend: Backend }) {
	const { connected, settings, jobs } = backend;
	const [url, setUrl] = useState("");
	const [probing, setProbing] = useState(false);
	const [probe, setProbe] = useState<ProbeResponse | null>(null);
	const [probeError, setProbeError] = useState("");
	const [filter, setFilter] = useState<Filter>("all");
	const [contextMenu, setContextMenu] = useState<ContextMenuState>(null);
	const [propertiesJob, setPropertiesJob] = useState<Job | null>(null);
	const [downloadError, setDownloadError] = useState("");
	const [probeSourceUrl, setProbeSourceUrl] = useState("");
	const [duplicatePrompt, setDuplicatePrompt] =
		useState<DuplicatePromptState | null>(null);
	const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
	const probeReqIdRef = useRef<string | null>(null);
	const probeGeneration = useRef(0);
	const jobGroups = useMemo(() => groupJobs(jobs), [jobs]);
	const filtered = useMemo(
		() => jobGroups.filter((group) => matchesFilter(group.root, filter)),
		[jobGroups, filter],
	);
	const counts = useMemo(() => {
		const roots = jobGroups.map((group) => group.root);
		return {
			all: roots.length,
			active: roots.filter((job) => matchesFilter(job, "active")).length,
			completed: roots.filter((job) => matchesFilter(job, "completed")).length,
			failed: roots.filter((job) => matchesFilter(job, "failed")).length,
		};
	}, [jobGroups]);

	const selectableJobs = useMemo(() => {
		const all: Job[] = [];
		for (const group of filtered) {
			all.push(group.root);
			all.push(...group.children);
		}
		return all;
	}, [filtered]);

	const selectedJobs = useMemo(
		() => jobs.filter((job) => selectedIds.has(job.id)),
		[jobs, selectedIds],
	);

	const lastSelectedIdRef = useRef<string | null>(null);

	const handleSelect = useCallback(
		(id: string, event: React.MouseEvent) => {
			const isMeta = event.metaKey || event.ctrlKey;
			const isShift = event.shiftKey;

			if (isShift && lastSelectedIdRef.current) {
				const anchorId = lastSelectedIdRef.current;
				const anchorIdx = selectableJobs.findIndex((j) => j.id === anchorId);
				const currentIdx = selectableJobs.findIndex((j) => j.id === id);

				if (anchorIdx !== -1 && currentIdx !== -1) {
					const start = Math.min(anchorIdx, currentIdx);
					const end = Math.max(anchorIdx, currentIdx);
					const rangeIds = selectableJobs
						.slice(start, end + 1)
						.map((j) => j.id);

					setSelectedIds((prev) => {
						if (isMeta) {
							const next = new Set(prev);
							for (const rangeId of rangeIds) next.add(rangeId);
							return next;
						}
						return new Set(rangeIds);
					});
					return;
				}
			}

			lastSelectedIdRef.current = id;

			if (isMeta) {
				setSelectedIds((prev) => {
					const next = new Set(prev);
					if (next.has(id)) next.delete(id);
					else next.add(id);
					return next;
				});
			} else {
				setSelectedIds(new Set([id]));
			}
		},
		[selectableJobs],
	);

	useEffect(() => {
		const handleKeyDown = (event: KeyboardEvent) => {
			const target = event.target as HTMLElement | null;
			const isInput =
				target &&
				(target.tagName === "INPUT" ||
					target.tagName === "TEXTAREA" ||
					target.isContentEditable);
			if (isInput) return;

			if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
				if (selectableJobs.length === 0) return;
				event.preventDefault();
				setSelectedIds(new Set(selectableJobs.map((j) => j.id)));
				return;
			}

			if (event.key === "Escape" && selectedIds.size > 0) {
				setSelectedIds(new Set());
			}
		};

		window.addEventListener("keydown", handleKeyDown);
		return () => window.removeEventListener("keydown", handleKeyDown);
	}, [selectableJobs, selectedIds.size]);

	useEffect(() => {
		if (selectedIds.size === 0) return;

		const handleClickOutside = (event: MouseEvent) => {
			if (event.button !== 0) return;
			const target = event.target as HTMLElement | null;
			if (!target) return;

			if (
				target.closest("[data-job-row]") ||
				target.closest("[data-slot='dropdown-popover']") ||
				target.closest("[role='menu']") ||
				target.closest("[role='dialog']") ||
				target.closest("[data-slot='modal']") ||
				target.closest("[data-slot='drawer']")
			) {
				return;
			}

			setSelectedIds(new Set());
		};

		window.addEventListener("click", handleClickOutside);
		return () => window.removeEventListener("click", handleClickOutside);
	}, [selectedIds.size]);

	const handleJobContextMenu = useCallback((job: Job, x: number, y: number) => {
		setSelectedIds((prev) => {
			if (prev.has(job.id)) return prev;
			return new Set([job.id]);
		});
		lastSelectedIdRef.current = job.id;
		setContextMenu({ x, y });
	}, []);

	// biome-ignore lint/correctness/useExhaustiveDependencies: filter is intentionally the effect trigger
	useEffect(() => {
		setSelectedIds(new Set());
	}, [filter]);

	const runProbe = async (overrideUrl?: string) => {
		const targetUrl = (overrideUrl ?? url).trim();
		if (!targetUrl || !connected) return;
		const generation = ++probeGeneration.current;
		setProbing(true);
		setProbeError("");
		setDownloadError("");
		const response = await backend.probe(
			targetUrl,
			undefined,
			(id) => {
				if (generation === probeGeneration.current) probeReqIdRef.current = id;
			},
			directFileMediaType(targetUrl),
		);
		if (generation !== probeGeneration.current) return;
		setProbing(false);
		probeReqIdRef.current = null;
		if (response.ok) {
			setProbeSourceUrl(targetUrl);
			setProbe(response);
		} else {
			setProbeSourceUrl("");
			setProbeError(
				response.error || "Probe failed. Check the URL and try again.",
			);
		}
	};

	const cancelProbe = async () => {
		const reqId = probeReqIdRef.current;
		if (!reqId) return;
		++probeGeneration.current;
		probeReqIdRef.current = null;
		setProbing(false);
		await backend.cancelProbe(reqId);
	};

	const runProbeRef = useRef(runProbe);
	runProbeRef.current = runProbe;

	useEffect(() => {
		let active = true;
		let unlisten: (() => void) | undefined;
		async function initTorrentFileListeners() {
			try {
				const stop = await listen<string>("open-torrent-file", (event) => {
					if (event.payload) {
						setUrl(event.payload);
						void runProbeRef.current(event.payload);
					}
				});
				if (active) unlisten = stop;
				else stop();
			} catch {
				// Not running in Tauri desktop environment
			}
			try {
				const torrentPath = await invoke<string | null>("get_cli_torrent_file");
				if (active && torrentPath) {
					setUrl(torrentPath);
					void runProbeRef.current(torrentPath);
				}
			} catch {
				// Not running in Tauri desktop environment
			}
		}
		void initTorrentFileListeners();
		return () => {
			active = false;
			unlisten?.();
		};
	}, []);

	const startDownload = async (
		format: FormatRow,
		directory: string,
		duplicateAction?: DuplicateAction,
	) => {
		if (!probe?.result || !probe.url) return;
		const result = probe.result;
		const isAudio = format.kind === "audio";
		const useFormatSelector = probe.engine === "ytdlp" || !format.url;
		const response = await backend.download({
			url: useFormatSelector ? probe.url : format.url || probe.url,
			formatId: useFormatSelector ? format.selector || undefined : undefined,
			directory: directory || undefined,
			filename: result.filename || undefined,
			title: result.title,
			thumbnail: result.thumbnail,
			engine: probe.engine,
			mediaType: isAudio
				? "audio"
				: format.kind === "file"
					? "file"
					: undefined,
			referer: probeSourceUrl || undefined,
			duplicateAction,
		});
		if (
			!response.ok &&
			response.error === "duplicate" &&
			response.duplicate &&
			!duplicateAction
		) {
			setDuplicatePrompt({
				info: response.duplicate,
				retry: (action) => startDownload(format, directory, action),
			});
			return;
		}
		if (!response.ok) {
			setDownloadError(
				response.error || "Could not start the download. Try again.",
			);
			return;
		}
		setDownloadError("");
		setDuplicatePrompt(null);
		setProbe(null);
		setProbeSourceUrl("");
		setUrl("");
	};

	const startPlaylistDownload = async (
		selectedUrls: string[],
		directory: string,
		duplicateAction?: DuplicateAction,
	) => {
		if (!probe?.result || !probe.url) return;
		const result = probe.result;
		const response = await backend.download({
			url: probe.url,
			directory: directory || undefined,
			downloadPlaylist: true,
			selectedEntryUrls: selectedUrls,
			title: result.playlistTitle || result.title,
			thumbnail: result.thumbnail,
			engine: probe.engine,
			referer: probeSourceUrl || undefined,
			duplicateAction,
		});
		if (
			!response.ok &&
			response.error === "duplicate" &&
			response.duplicate &&
			!duplicateAction
		) {
			setDuplicatePrompt({
				info: response.duplicate,
				retry: (action) =>
					startPlaylistDownload(selectedUrls, directory, action),
			});
			return;
		}
		if (!response.ok) {
			setDownloadError(
				response.error || "Could not start the playlist download. Try again.",
			);
			return;
		}
		setDownloadError("");
		setDuplicatePrompt(null);
		setProbe(null);
		setProbeSourceUrl("");
		setUrl("");
	};

	const startTorrentDownload = async (
		directory: string,
		selectedFiles: string[],
		duplicateAction?: DuplicateAction,
	) => {
		if (!probe?.torrent) return;
		const response = await backend.download({
			url: probe.torrent.magnet,
			directory: directory || undefined,
			selectedFiles,
			filename: probe.torrent.name,
			title: probe.torrent.name,
			engine: "torrent",
			mediaType: "torrent",
			referer: probeSourceUrl || undefined,
			duplicateAction,
		});
		if (
			!response.ok &&
			response.error === "duplicate" &&
			response.duplicate &&
			!duplicateAction
		) {
			setDuplicatePrompt({
				info: response.duplicate,
				retry: (action) =>
					startTorrentDownload(directory, selectedFiles, action),
			});
			return;
		}
		if (!response.ok) {
			setDownloadError(
				response.error || "Could not start the torrent download. Try again.",
			);
			return;
		}
		setDownloadError("");
		setDuplicatePrompt(null);
		setProbe(null);
		setProbeSourceUrl("");
		setUrl("");
	};

	return (
		<div className="space-y-8">
			<Squircle asChild cornerRadius={24}>
				<Card
					className="gap-5 border border-separator bg-surface p-5 shadow-none sm:p-6"
					variant="default"
				>
					<div className="flex flex-col gap-3 sm:flex-row sm:items-end">
						<SearchField
							aria-label="Source URL"
							className="min-w-0 flex-1"
							fullWidth
							onChange={setUrl}
							onSubmit={() => void runProbe()}
							value={url}
						>
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<SearchField.Group className="min-h-12 border border-field-border bg-field-background px-3">
									<SearchField.SearchIcon />
									<SearchField.Input
										className="font-mono text-sm"
										placeholder="https://example.com/video or magnet link"
									/>
									<Squircle asChild className="ring-inset" cornerRadius={8}>
										<SearchField.ClearButton />
									</Squircle>
								</SearchField.Group>
							</Squircle>
						</SearchField>
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Button
								className="min-h-12 sm:min-w-28"
								isDisabled={!connected || (!probing && !url.trim())}
								variant={probing ? "danger-soft" : "primary"}
								onPress={() => void (probing ? cancelProbe() : runProbe())}
							>
								{probing ? (
									<>
										<Spinner color="current" size="sm" />
										Cancel probe
									</>
								) : (
									<>
										<Search aria-hidden className="size-4" />
										Probe source
									</>
								)}
							</Button>
						</Squircle>
					</div>
					{probeError ? (
						<Squircle asChild cornerRadius={24}>
							<Alert status="danger">
								<Alert.Indicator />
								<Alert.Content>
									<Alert.Title>Probe failed</Alert.Title>
									<Alert.Description>{probeError}</Alert.Description>
								</Alert.Content>
							</Alert>
						</Squircle>
					) : null}
					{downloadError ? (
						<Squircle asChild cornerRadius={24}>
							<Alert status="danger">
								<Alert.Indicator />
								<Alert.Content>
									<Alert.Title>Could not start download</Alert.Title>
									<Alert.Description>{downloadError}</Alert.Description>
								</Alert.Content>
							</Alert>
						</Squircle>
					) : null}
				</Card>
			</Squircle>

			<Squircle asChild cornerRadius={24}>
				<Card
					className="gap-5 border border-separator bg-surface p-5 shadow-none sm:p-6"
					variant="default"
				>
					<div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
						<div>
							<Typography className="font-semibold" type="h5">
								Queue
							</Typography>
							<Typography className="mt-1" color="muted" type="body-sm">
								Live transfer status from the connected backend.
							</Typography>
						</div>
						<Squircle asChild className="ring-inset" cornerRadius={12}>
							<Button
								size="sm"
								variant="tertiary"
								onPress={async () => {
									const result = await backend.clearFinished();
									if (!result.ok) {
										setDownloadError(
											"Could not clear finished downloads. Check the backend connection.",
										);
									}
								}}
							>
								<Trash2 aria-hidden className="size-4" />
								Clear finished
							</Button>
						</Squircle>
					</div>
					<Tabs
						selectedKey={filter}
						onSelectionChange={(key) => setFilter(String(key) as Filter)}
					>
						<Tabs.ListContainer className="rounded-full border border-separator/60 bg-surface-secondary p-1">
							<Tabs.List aria-label="Filter downloads" className="gap-1 p-0">
								{FILTERS.map(({ key, label }) => (
									<Tabs.Tab
										className="gap-2 rounded-full px-3.5 py-1.5 text-xs sm:text-sm font-medium transition-colors data-[selected=true]:text-foreground"
										id={key}
										key={key}
									>
										{label}
										<SquircleChip
											className="min-w-5 justify-center rounded-full tabular-nums"
											color="success"
											size="sm"
											variant={filter === key ? "primary" : "soft"}
										>
											{counts[key]}
										</SquircleChip>
										<Tabs.Indicator className="rounded-full bg-surface-tertiary shadow-sm" />
									</Tabs.Tab>
								))}
							</Tabs.List>
						</Tabs.ListContainer>
					</Tabs>
					{filtered.length ? (
						<JobGroupList
							groups={filtered}
							selectedIds={selectedIds}
							onContextMenu={handleJobContextMenu}
							onSelect={handleSelect}
						/>
					) : (
						<EmptyQueue />
					)}
				</Card>
			</Squircle>

			{contextMenu ? (
				<JobContextMenu
					backend={backend}
					selectedJobs={selectedJobs}
					onClose={() => setContextMenu(null)}
					onError={(message) => setDownloadError(message)}
					onProperties={setPropertiesJob}
					state={contextMenu}
				/>
			) : null}

			{probe?.ok && probe.engine === "torrent" && probe.torrent ? (
				<TorrentDrawer
					onClose={() => {
						setProbe(null);
						setProbeSourceUrl("");
					}}
					onDownload={(directory, selectedFiles) =>
						void startTorrentDownload(directory, selectedFiles)
					}
					settings={settings}
					torrent={probe.torrent}
				/>
			) : null}
			{probe?.ok && probe.result ? (
				<ProbeModal
					onClose={() => {
						setProbe(null);
						setProbeSourceUrl("");
					}}
					onDownloadPlaylist={(selectedUrls, directory) =>
						void startPlaylistDownload(selectedUrls, directory)
					}
					onDownloadVideo={(format, directory) =>
						void startDownload(format, directory)
					}
					probe={probe}
					settings={settings}
				/>
			) : null}
			{duplicatePrompt ? (
				<DuplicatePrompt
					onChoose={(action) => {
						const retry = duplicatePrompt.retry;
						setDuplicatePrompt(null);
						void retry(action);
					}}
					onClose={() => setDuplicatePrompt(null)}
					state={duplicatePrompt}
				/>
			) : null}
			{propertiesJob ? (
				<JobPropertiesModal
					job={propertiesJob}
					onClose={() => setPropertiesJob(null)}
				/>
			) : null}
		</div>
	);
}
