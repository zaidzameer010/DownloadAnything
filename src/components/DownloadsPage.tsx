import {
	Accordion,
	Alert,
	Button,
	Card,
	Checkbox,
	Chip,
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
import {
	Archive,
	Check,
	Clipboard,
	Download,
	FileVideo,
	FolderOpen,
	HardDriveDownload,
	Pause,
	Play,
	Search,
	Trash2,
	X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { useBackend } from "../hooks/useBackend";
import type {
	FormatRow,
	Job,
	JobStatus,
	PlaylistEntry,
	ProbeResponse,
	Settings,
	TorrentInfo,
} from "../lib/backend";
import {
	formatBytes,
	formatDuration,
	formatEta,
	formatSpeed,
} from "../lib/format";

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
		<Chip color={statusColor(status)} size="sm" variant="soft">
			{status}
		</Chip>
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

function TitleCell({ job }: { job: Job }) {
	return (
		<div className="flex min-w-0 items-center gap-3">
			<div className="flex h-11 w-16 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-surface-tertiary text-muted">
				{job.thumbnail ? (
					<img
						alt={job.title || "Download thumbnail"}
						className="size-full object-contain p-0.5"
						src={job.thumbnail}
					/>
				) : (
					<FileVideo aria-hidden className="size-5" />
				)}
			</div>
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
				<ProgressBar.Track>
					<ProgressBar.Fill />
				</ProgressBar.Track>
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

type ContextMenuState = { job: Job; x: number; y: number } | null;

function JobContextMenu({
	state,
	backend,
	onProperties,
	onClose,
	onError,
}: {
	state: Exclude<ContextMenuState, null>;
	backend: Backend;
	onProperties: (job: Job) => void;
	onClose: () => void;
	onError: (message: string) => void;
}) {
	const canPause =
		state.job.status === "queued" || state.job.status === "downloading";
	const canResume =
		state.job.status === "paused" ||
		state.job.status === "failed" ||
		state.job.status === "cancelled";
	const canCancel =
		state.job.status === "queued" || state.job.status === "downloading";
	const canReveal =
		(state.job.status === "completed" || state.job.isPlaylist) &&
		(Boolean(state.job.finalFilepath) ||
			Boolean(state.job.filename && state.job.directory) ||
			Boolean(state.job.directory));

	const left = Math.min(state.x, window.innerWidth - 256);
	const top = Math.min(state.y, window.innerHeight - 288);

	const handleAction = async (key: string) => {
		onClose();
		if (["pause", "resume", "cancel", "remove"].includes(key)) {
			const action = key as "pause" | "resume" | "cancel" | "remove";
			const response = await backend.jobAction(state.job.id, action);
			if (!response.ok) {
				onError(response.error ?? `Could not ${action} the download`);
			}
			return;
		}
		if (key === "reveal") {
			const result = await backend.reveal(state.job.id);
			if (!result.ok) {
				onError(result.error ?? "Could not reveal file");
			}
			return;
		}
		if (key === "properties") onProperties(state.job);
	};

	return (
		<Dropdown isOpen onOpenChange={(open) => !open && onClose()}>
			<Dropdown.Popover
				aria-label={`Actions for ${state.job.title || state.job.url}`}
				className="fixed z-50 min-w-56"
				style={{ left: Math.max(8, left), top: Math.max(8, top) }}
			>
				<Dropdown.Menu onAction={(key) => handleAction(String(key))}>
					{canPause ? (
						<Dropdown.Item id="pause" textValue="Pause">
							<Pause aria-hidden className="size-4 text-muted" />
							<Label>Pause</Label>
						</Dropdown.Item>
					) : null}
					{canResume ? (
						<Dropdown.Item id="resume" textValue="Resume">
							<Play aria-hidden className="size-4 text-muted" />
							<Label>Resume</Label>
						</Dropdown.Item>
					) : null}
					{canCancel ? (
						<Dropdown.Item id="cancel" textValue="Cancel">
							<X aria-hidden className="size-4 text-muted" />
							<Label>Cancel</Label>
						</Dropdown.Item>
					) : null}
					{canReveal ? (
						<Dropdown.Item id="reveal" textValue="Reveal in Finder or Explorer">
							<FolderOpen aria-hidden className="size-4 text-muted" />
							<Label>
								Reveal in{" "}
								{navigator.platform.includes("Mac") ? "Finder" : "Explorer"}
							</Label>
						</Dropdown.Item>
					) : null}
					<Dropdown.Item id="properties" textValue="Properties">
						<Clipboard aria-hidden className="size-4 text-muted" />
						<Label>Properties</Label>
					</Dropdown.Item>
					<Dropdown.Item id="remove" textValue="Remove" variant="danger">
						<Trash2 aria-hidden className="size-4 text-danger" />
						<Label>Remove</Label>
					</Dropdown.Item>
				</Dropdown.Menu>
			</Dropdown.Popover>
		</Dropdown>
	);
}

function JobTableHeader() {
	return (
		<div
			aria-hidden="true"
			className="mb-2 grid min-w-230 w-full grid-cols-[40%_14%_28%_18%] rounded-xl border border-separator bg-surface-tertiary text-xs font-semibold text-muted shadow-sm"
		>
			<span className="px-4 py-3 text-start">Title</span>
			<span className="px-4 py-3 text-center">Status</span>
			<span className="px-4 py-3 text-start">Progress</span>
			<span className="px-4 py-3 text-center">Size</span>
		</div>
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
		const children = (byParent.get(root.id) ?? []).filter((child) =>
			(root.childIds ?? []).includes(child.id),
		);
		if (root.childIds) {
			const order = new Map(root.childIds.map((id, index) => [id, index]));
			children.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
		}
		return { root, children };
	});
}

function JobRow({
	job,
	isChild,
	className,
	onContextMenu,
}: {
	job: Job;
	isChild?: boolean;
	className?: string;
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	const titleIndent = isChild ? "ps-8" : "ps-4";
	return (
		<div
			className={[
				"group grid cursor-context-menu grid-cols-[40%_14%_28%_18%] items-center bg-surface-secondary hover:bg-surface-hover",
				className || "",
			].join(" ")}
			onContextMenu={(event) => {
				event.preventDefault();
				onContextMenu(job, event.clientX, event.clientY);
			}}
			onKeyDown={(event) => {
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
			<div className={`min-w-0 px-4 py-4 ${titleIndent}`}>
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
		</div>
	);
}

function ParentGroup({
	group,
	onContextMenu,
}: {
	group: JobGroup;
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	const { root, children } = group;
	return (
		<Accordion className="w-full" hideSeparator variant="surface">
			<Accordion.Item id={root.id}>
				<Accordion.Heading>
					<Accordion.Trigger className="w-full p-0">
						<div
							className="group grid w-full cursor-context-menu grid-cols-[40%_14%_28%_18%] items-center rounded-t-2xl bg-surface-secondary hover:bg-surface-hover"
							onContextMenu={(event) => {
								event.preventDefault();
								onContextMenu(root, event.clientX, event.clientY);
							}}
							onKeyDown={(event) => {
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
							<div className="flex min-w-0 items-center gap-2 px-4 py-4 ps-4">
								<span className="min-w-0 flex-1">
									<TitleCell job={root} />
								</span>
								<Accordion.Indicator className="shrink-0 text-muted" />
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
					</Accordion.Trigger>
				</Accordion.Heading>
				<Accordion.Panel>
					<div className="overflow-hidden rounded-b-2xl bg-surface-secondary">
						{children.map((child, index) => (
							<JobRow
								className={
									index !== children.length - 1
										? "border-b border-separator"
										: ""
								}
								isChild
								job={child}
								key={child.id}
								onContextMenu={onContextMenu}
							/>
						))}
					</div>
				</Accordion.Panel>
			</Accordion.Item>
		</Accordion>
	);
}

function JobGroupList({
	groups,
	onContextMenu,
}: {
	groups: JobGroup[];
	onContextMenu: (job: Job, x: number, y: number) => void;
}) {
	return (
		<div className="w-full overflow-x-auto">
			<div className="min-w-230 space-y-2">
				<JobTableHeader />
				<ScrollShadow className="max-h-[520px] space-y-2 pr-1" visibility="auto">
					{groups.map((group) =>
						group.children.length ? (
							<ParentGroup
								group={group}
								key={group.root.id}
								onContextMenu={onContextMenu}
							/>
						) : (
							<JobRow
								className="rounded-2xl"
								job={group.root}
								key={group.root.id}
								onContextMenu={onContextMenu}
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
			<Select.Trigger>
				<Select.Value />
				<Select.Indicator />
			</Select.Trigger>
			<Select.Popover>
				<ListBox>
					{locations.map((location) => (
						<ListBox.Item
							id={location.path}
							key={location.path}
							textValue={location.name}
						>
							{location.name}
							<ListBox.ItemIndicator />
						</ListBox.Item>
					))}
				</ListBox>
			</Select.Popover>
		</Select>
	);
}

function PlaylistSummary({ probe }: { probe: ProbeResponse }) {
	const result = probe.result;
	if (!result) return null;
	const title = result.playlistTitle || result.title;
	const count = result.playlistCount ?? (result.entries?.length || 0);

	return (
		<Card
			className="gap-4 border border-separator bg-surface-secondary p-4 shadow-none"
			variant="transparent"
		>
			<div className="flex items-center gap-4">
				<div className="flex size-16 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-surface-tertiary text-muted">
					{result.thumbnail ? (
						<img
							alt={title || "Playlist thumbnail"}
							className="size-full object-cover"
							src={result.thumbnail}
						/>
					) : (
						<FileVideo aria-hidden className="size-6" />
					)}
				</div>
				<div className="min-w-0">
					<Typography className="line-clamp-2 font-semibold" type="h6">
						{title || result.webpageUrl}
					</Typography>
					<div className="mt-2 flex flex-wrap gap-2">
						<Chip color="accent" size="sm" variant="soft">
							{probe.engine}
						</Chip>
						{result.extractor ? (
							<Chip size="sm" variant="soft">
								{result.extractor}
							</Chip>
						) : null}
						{result.uploader ? (
							<Chip size="sm" variant="soft">
								{result.uploader}
							</Chip>
						) : null}
						{count ? (
							<Chip size="sm" variant="soft">
								{count} videos
							</Chip>
						) : null}
					</div>
				</div>
			</div>
			<Typography
				className="truncate font-mono text-[11px]"
				color="muted"
				type="body-xs"
			>
				{result.webpageUrl}
			</Typography>
		</Card>
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
					<div
						className="flex items-start gap-3 rounded-xl border border-separator bg-surface-secondary p-3"
						key={entry.url}
					>
						<Checkbox
							aria-label={`Select ${entry.title}`}
							isSelected={selected}
							onChange={(checked) => onToggle(entry.url, checked)}
						>
							<Checkbox.Content>
								<Checkbox.Control>
									<Checkbox.Indicator />
								</Checkbox.Control>
							</Checkbox.Content>
						</Checkbox>
						<div className="flex h-12 w-16 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-surface-tertiary text-muted">
							{entry.thumbnail ? (
								<img
									alt={entry.title || "Thumbnail"}
									className="size-full object-cover"
									src={entry.thumbnail}
								/>
							) : (
								<FileVideo aria-hidden className="size-5" />
							)}
						</div>
						<div className="min-w-0 flex-1">
							<div className="flex flex-wrap items-center gap-2">
								<Typography className="line-clamp-2 font-medium" type="body-sm">
									{entry.title}
								</Typography>
								{isCurrent ? (
									<Chip color="accent" size="sm" variant="soft">
										Current
									</Chip>
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
					</div>
				);
			})}
		</div>
	);
}

function MediaSummary({ probe }: { probe: ProbeResponse }) {
	const result = probe.result;
	if (!result) return null;

	return (
		<Card
			className="gap-4 border border-separator bg-surface-secondary p-4 shadow-none"
			variant="transparent"
		>
			<div className="flex items-center gap-4">
				<div className="flex size-16 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-surface-tertiary text-muted">
					{result.thumbnail ? (
						<img
							alt={result.title || "Media thumbnail"}
							className="size-full object-cover"
							src={result.thumbnail}
						/>
					) : (
						<FileVideo aria-hidden className="size-6" />
					)}
				</div>
				<div className="min-w-0">
					<Typography className="line-clamp-2 font-semibold" type="h6">
						{result.title || result.webpageUrl}
					</Typography>
					<div className="mt-2 flex flex-wrap gap-2">
						<Chip color="accent" size="sm" variant="soft">
							{probe.engine}
						</Chip>
						{result.extractor ? (
							<Chip size="sm" variant="soft">
								{result.extractor}
							</Chip>
						) : null}
						{result.uploader ? (
							<Chip size="sm" variant="soft">
								{result.uploader}
							</Chip>
						) : null}
						{result.isPlaylist && result.playlistCount ? (
							<Chip size="sm" variant="soft">
								{result.playlistCount} videos
							</Chip>
						) : null}
					</div>
				</div>
			</div>
			<Typography
				className="truncate font-mono text-[11px]"
				color="muted"
				type="body-xs"
			>
				{result.filename || result.webpageUrl}
			</Typography>
		</Card>
	);
}

function VideoPanel({
	formats,
	selectedId,
	onSelect,
}: {
	formats: FormatRow[];
	selectedId: string;
	onSelect: (id: string) => void;
}) {
	return (
		<div className="space-y-3">
			<div className="flex items-center justify-between">
				<Typography className="font-semibold" type="h6">
					Quality
				</Typography>
				<Typography color="muted" type="body-xs">
					{formats.length} formats available
				</Typography>
			</div>
			<div className="space-y-2">
				{formats.map((format) => {
					const isSelected = format.id === selectedId;
					return (
						<Button
							aria-pressed={isSelected}
							className="h-auto min-h-14 justify-between rounded-xl px-4 py-3 text-start"
							fullWidth
							onPress={() => onSelect(format.id)}
							variant={isSelected ? "secondary" : "tertiary"}
							key={format.id}
						>
							<span className="flex min-w-0 items-center gap-3">
								<span
									className={`flex size-5 items-center justify-center rounded-full border ${isSelected ? "border-accent bg-accent text-accent-foreground" : "border-separator"}`}
								>
									{isSelected ? <Check aria-hidden className="size-3" /> : null}
								</span>
								<span className="min-w-0">
									<Typography className="truncate font-medium" type="body-sm">
										{format.label}
									</Typography>
									<Typography color="muted" type="body-xs">
										{format.resolution || format.kind}
									</Typography>
								</span>
							</span>
							<span className="flex shrink-0 items-center gap-3">
								<Chip
									color={format.kind === "video" ? "accent" : "default"}
									size="sm"
									variant="soft"
								>
									{format.kind}
								</Chip>
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
					);
				})}
			</div>
		</div>
	);
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

	const formats = result?.formats.length ? result.formats : [FALLBACK_FORMAT];
	const hasVideo = (result?.formats.length ?? 0) > 0;
	const hasPlaylist = result?.isPlaylist && (result.entries?.length ?? 0) > 0;

	const [selectedKey, setSelectedKey] = useState<"video" | "playlist">(
		hasVideo ? "video" : "playlist",
	);

	const [selectedFormatId, setSelectedFormatId] = useState(
		formats[0]?.id ?? "best",
	);
	const selectedFormat =
		formats.find((format) => format.id === selectedFormatId) ?? formats[0];

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
				<Modal.Dialog className="w-full sm:max-w-2xl">
					<Modal.CloseTrigger />
					<Modal.Header>
						<Modal.Heading>
							{hasVideo && hasPlaylist ? "Download options" : "Select a format"}
						</Modal.Heading>
					</Modal.Header>
					<Modal.Body className="gap-6">
						<Tabs
							selectedKey={selectedKey}
							onSelectionChange={(key) =>
								setSelectedKey(String(key) as "video" | "playlist")
							}
						>
							<Tabs.ListContainer className="bg-transparent p-0">
								<Tabs.List
									aria-label="Download options"
									className="w-full gap-1 overflow-x-auto rounded-xl bg-surface-secondary p-1"
								>
									{hasVideo ? (
										<Tabs.Tab
											className="gap-2 rounded-lg px-3 py-2 text-xs sm:text-sm"
											id="video"
										>
											Video
											<Tabs.Indicator />
										</Tabs.Tab>
									) : null}
									{hasPlaylist ? (
										<Tabs.Tab
											className="gap-2 rounded-lg px-3 py-2 text-xs sm:text-sm"
											id="playlist"
										>
											Playlist
											<Tabs.Indicator />
										</Tabs.Tab>
									) : null}
								</Tabs.List>
							</Tabs.ListContainer>
							{hasVideo ? (
								<Tabs.Panel id="video">
									<div className="space-y-6 pt-2">
										<MediaSummary probe={probe} />
										<VideoPanel
											formats={formats}
											selectedId={selectedFormatId}
											onSelect={setSelectedFormatId}
										/>
									</div>
								</Tabs.Panel>
							) : null}
							{hasPlaylist ? (
								<Tabs.Panel id="playlist">
									<div className="space-y-4 pt-2">
										<PlaylistSummary probe={probe} />
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
														<Checkbox.Control>
															<Checkbox.Indicator />
														</Checkbox.Control>
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
						<Button slot="close" variant="tertiary">
							Cancel
						</Button>
						{selectedKey === "video" ? (
							<Button
								onPress={() => onDownloadVideo(selectedFormat, directory)}
							>
								<Download aria-hidden className="size-4" />
								Download
							</Button>
						) : (
							<Button
								isDisabled={selectedCount === 0}
								onPress={() => onDownloadPlaylist([...selectedUrls], directory)}
							>
								<Download aria-hidden className="size-4" />
								Download {selectedCount} video
								{selectedCount === 1 ? "" : "s"}
							</Button>
						)}
					</Modal.Footer>
				</Modal.Dialog>
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
					<Drawer.CloseTrigger />
					<Drawer.Header>
						<Drawer.Heading>Download torrent</Drawer.Heading>
					</Drawer.Header>
					<Drawer.Body className="gap-6">
						<Card
							className="gap-4 border border-separator bg-surface-secondary p-4 shadow-none"
							variant="transparent"
						>
							<div className="flex items-center gap-4">
								<div className="flex size-16 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
									<Archive aria-hidden className="size-7" />
								</div>
								<div className="min-w-0">
									<Typography className="line-clamp-2 font-semibold" type="h6">
										{torrent.name}
									</Typography>
									<div className="mt-2 flex flex-wrap gap-2">
										<Chip color="accent" size="sm" variant="soft">
											libtorrent
										</Chip>
										<Chip size="sm" variant="soft">
											{formatBytes(torrent.totalSize)}
										</Chip>
										<Chip size="sm" variant="soft">
											{torrent.fileCount} files
										</Chip>
									</div>
								</div>
							</div>
						</Card>
						<div className="space-y-3">
							<div className="flex items-center justify-between">
								<Typography className="font-semibold" type="h6">
									Files to download
								</Typography>
								<Typography color="muted" type="body-xs">
									{selectedFiles.size} selected
								</Typography>
							</div>
							<div className="divide-y divide-separator rounded-xl border border-separator">
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
											<Checkbox.Control>
												<Checkbox.Indicator />
											</Checkbox.Control>
											<span className="flex min-w-0 items-center justify-between gap-3">
												<span className="truncate text-sm">{file.path}</span>
												<span className="shrink-0 font-mono text-xs text-muted">
													{formatBytes(file.size)}
												</span>
											</span>
										</Checkbox.Content>
									</Checkbox>
								))}
							</div>
						</div>
						<LocationSelect
							locations={locations}
							onChange={setDirectory}
							value={directory}
						/>
					</Drawer.Body>
					<Drawer.Footer>
						<Button slot="close" variant="tertiary">
							Cancel
						</Button>
						<Button
							isDisabled={selectedFiles.size === 0}
							onPress={() => onDownload(directory, [...selectedFiles])}
						>
							<Download aria-hidden className="size-4" />
							Download torrent
						</Button>
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
				<Modal.Dialog>
					<Modal.CloseTrigger />
					<Modal.Header>
						<Modal.Heading>Task properties</Modal.Heading>
					</Modal.Header>
					<Modal.Body className="gap-5">
						{job.thumbnail ? (
							<img
								alt=""
								className="aspect-video w-full rounded-2xl object-cover"
								src={job.thumbnail}
							/>
						) : null}
						<dl className="grid gap-px overflow-hidden rounded-2xl border border-separator bg-separator sm:grid-cols-2">
							{fields.map(([label, value]) => (
								<div className="bg-surface-secondary p-3" key={label}>
									<dt className="text-[11px] font-semibold tracking-wide text-muted uppercase">
										{label}
									</dt>
									<dd className="mt-1 wrap-break-word font-mono text-xs text-foreground">
										{value}
									</dd>
								</div>
							))}
						</dl>
					</Modal.Body>
					<Modal.Footer>
						<Button slot="close" variant="tertiary">
							Close
						</Button>
					</Modal.Footer>
				</Modal.Dialog>
			</Modal.Container>
		</Modal.Backdrop>
	);
}

function EmptyQueue() {
	return (
		<Card
			className="items-center gap-4 border border-dashed border-separator bg-transparent px-6 py-16 text-center shadow-none"
			variant="transparent"
		>
			<div className="flex size-12 items-center justify-center rounded-2xl bg-surface-tertiary text-muted">
				<HardDriveDownload aria-hidden className="size-6" />
			</div>
			<div className="max-w-md space-y-2">
				<Typography className="font-semibold" type="h6">
					Your queue is clear
				</Typography>
				<Typography color="muted" type="body-sm">
					Probe a video, page, or magnet link above to add the first transfer.
				</Typography>
			</div>
		</Card>
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

	const runProbe = async (overrideUrl?: string) => {
		const targetUrl = (overrideUrl ?? url).trim();
		if (!targetUrl || !connected) return;
		setProbing(true);
		setProbeError("");
		setDownloadError("");
		const response = await backend.probe(targetUrl);
		setProbing(false);
		if (response.ok) setProbe(response);
		else
			setProbeError(
				response.error || "Probe failed. Check the URL and try again.",
			);
	};

	useEffect(() => {
		let unlisten: (() => void) | undefined;
		async function initTorrentFileListeners() {
			try {
				const { listen } = await import("@tauri-apps/api/event");
				unlisten = await listen<string>("open-torrent-file", (event) => {
					if (event.payload) {
						setUrl(event.payload);
						void runProbe(event.payload);
					}
				});
			} catch {
				// Not running in Tauri desktop environment
			}
			try {
				const { invoke } = await import("@tauri-apps/api/core");
				const torrentPath = await invoke<string | null>("get_cli_torrent_file");
				if (torrentPath) {
					setUrl(torrentPath);
					void runProbe(torrentPath);
				}
			} catch {
				// Not running in Tauri desktop environment
			}
		}
		if (connected) {
			void initTorrentFileListeners();
		}
		return () => {
			if (unlisten) unlisten();
		};
	}, [connected]);

	const startDownload = async (format: FormatRow, directory: string) => {
		if (!probe?.result || !probe.url) return;
		const result = probe.result;
		const response = await backend.download({
			url: format.url || probe.url,
			formatId: format.url ? undefined : format.selector || undefined,
			directory: directory || undefined,
			filename: result.filename || undefined,
			title: result.title,
			thumbnail: result.thumbnail,
			engine: probe.engine,
		});
		if (!response.ok) {
			setDownloadError(
				response.error || "Could not start the download. Try again.",
			);
			return;
		}
		setDownloadError("");
		setProbe(null);
		setUrl("");
	};

	const startPlaylistDownload = async (
		selectedUrls: string[],
		directory: string,
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
		});
		if (!response.ok) {
			setDownloadError(
				response.error || "Could not start the playlist download. Try again.",
			);
			return;
		}
		setDownloadError("");
		setProbe(null);
		setUrl("");
	};

	const startTorrentDownload = async (
		directory: string,
		selectedFiles: string[],
	) => {
		if (!probe?.torrent) return;
		const response = await backend.download({
			url: probe.torrent.magnet,
			directory: directory || undefined,
			selectedFiles,
			filename: probe.torrent.name,
			title: probe.torrent.name,
			engine: "torrent",
		});
		if (!response.ok) {
			setDownloadError(
				response.error || "Could not start the torrent download. Try again.",
			);
			return;
		}
		setDownloadError("");
		setProbe(null);
		setUrl("");
	};

	return (
		<div className="space-y-8">
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
						<SearchField.Group className="min-h-12 rounded-xl border border-field-border bg-field-background px-3">
							<SearchField.SearchIcon />
							<SearchField.Input
								className="font-mono text-sm"
								placeholder="https://example.com/video or magnet link"
							/>
							<SearchField.ClearButton />
						</SearchField.Group>
					</SearchField>
					<Button
						className="min-h-12 sm:min-w-28"
						isDisabled={!connected || probing || !url.trim()}
						isPending={probing}
						onPress={() => void runProbe()}
					>
						{({ isPending }) => (
							<>
								{isPending ? (
									<Spinner color="current" size="sm" />
								) : (
									<Search aria-hidden className="size-4" />
								)}
								{isPending ? "Probing" : "Probe source"}
							</>
						)}
					</Button>
				</div>
				{probeError ? (
					<Alert status="danger">
						<Alert.Indicator />
						<Alert.Content>
							<Alert.Title>Probe failed</Alert.Title>
							<Alert.Description>{probeError}</Alert.Description>
						</Alert.Content>
					</Alert>
				) : null}
				{downloadError ? (
					<Alert status="danger">
						<Alert.Indicator />
						<Alert.Content>
							<Alert.Title>Could not start download</Alert.Title>
							<Alert.Description>{downloadError}</Alert.Description>
						</Alert.Content>
					</Alert>
				) : null}
			</Card>

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
				</div>
				<Tabs
					selectedKey={filter}
					onSelectionChange={(key) => setFilter(String(key) as Filter)}
				>
					<Tabs.ListContainer className="bg-transparent p-0">
						<Tabs.List
							aria-label="Filter downloads"
							className="w-full gap-1 overflow-x-auto rounded-xl bg-surface-secondary p-1"
						>
							{FILTERS.map(({ key, label }) => (
								<Tabs.Tab
									className="gap-2 rounded-lg px-3 py-2 text-xs sm:text-sm"
									id={key}
									key={key}
								>
									{label}
									<Chip
										className="min-w-5 justify-center tabular-nums"
										color="success"
										size="sm"
										variant={filter === key ? "primary" : "soft"}
									>
										{counts[key]}
									</Chip>
									<Tabs.Indicator />
								</Tabs.Tab>
							))}
						</Tabs.List>
					</Tabs.ListContainer>
				</Tabs>
				{filtered.length ? (
					<JobGroupList
						groups={filtered}
						onContextMenu={(job, x, y) => setContextMenu({ job, x, y })}
					/>
				) : (
					<EmptyQueue />
				)}
			</Card>

			{contextMenu ? (
				<JobContextMenu
					backend={backend}
					onClose={() => setContextMenu(null)}
					onError={(message) => setDownloadError(message)}
					onProperties={setPropertiesJob}
					state={contextMenu}
				/>
			) : null}

			{probe?.ok && probe.engine === "torrent" && probe.torrent ? (
				<TorrentDrawer
					onClose={() => setProbe(null)}
					onDownload={(directory, selectedFiles) =>
						void startTorrentDownload(directory, selectedFiles)
					}
					settings={settings}
					torrent={probe.torrent}
				/>
			) : null}
			{probe?.ok && probe.result ? (
				<ProbeModal
					onClose={() => setProbe(null)}
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
			{propertiesJob ? (
				<JobPropertiesModal
					job={propertiesJob}
					onClose={() => setPropertiesJob(null)}
				/>
			) : null}
		</div>
	);
}
