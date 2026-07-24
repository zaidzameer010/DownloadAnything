import type { ColumnDef } from "@tanstack/react-table";
import {
	flexRender,
	getCoreRowModel,
	useReactTable,
} from "@tanstack/react-table";
import {
	AlertCircle,
	CheckCircle2,
	FileDown,
	Loader2,
	Pause,
	Settings,
	UploadCloud,
} from "lucide-react";
import { useMemo } from "react";
import type { UseDownloaderReturn } from "../hooks/useDownloader";
import type { Job } from "../types";
import { formatBytes, formatSpeed } from "../utils";

interface DashboardProps {
	downloader: UseDownloaderReturn;
}

function DirectDownloader({ downloader }: { downloader: UseDownloaderReturn }) {
	const { inputUrl, setInputUrl, isProbing, handleProbeUrl } = downloader;

	return (
		<section className="magic-input-card">
			<div className="magic-input-title-row">
				<h3>Direct Downloader</h3>
				<span>
					Paste any media link (YouTube, Twitter, Vimeo, etc.) to analyze and
					select formats
				</span>
			</div>

			<div className="magic-input-wrapper">
				<input
					type="text"
					className="magic-input"
					placeholder="https://www.youtube.com/watch?v=..."
					value={inputUrl}
					onChange={(e) => setInputUrl(e.target.value)}
					onKeyDown={(e) => {
						if (e.key === "Enter" && !isProbing && inputUrl.trim())
							handleProbeUrl();
					}}
					disabled={isProbing}
				/>
				<div className="magic-btn-group">
					<button
						className="action-btn"
						onClick={handleProbeUrl}
						disabled={!inputUrl.trim() || isProbing}
						style={{ minWidth: "120px" }}
					>
						{isProbing ? (
							<>
								<Loader2 size={14} className="animate-spin" />
								<span>Analyzing...</span>
							</>
						) : (
							<>
								<FileDown size={14} />
								<span>Download</span>
							</>
						)}
					</button>
				</div>
			</div>
		</section>
	);
}

function DownloadsTable({ downloader }: { downloader: UseDownloaderReturn }) {
	const {
		displayJobs,
		filterTab,
		setFilterTab,
		counts,
		hasCompletedJobs,
		handleClearCompleted,
		setContextMenu,
	} = downloader;

	const columns = useMemo<ColumnDef<Job>[]>(
		() => [
			{
				header: "Filename",
				accessorKey: "title",
				cell: ({ row }) => {
					const job = row.original;
					const isPostProcessing = job.status === "postprocessing";

					let displayName = "Unknown filename";
					if (job.file_path) {
						const parts = job.file_path.split(/[/\\]/);
						const name = parts[parts.length - 1];
						if (name) displayName = name;
					} else if (job.title && !/^https?:\/\//i.test(job.title)) {
						displayName = job.title;
					} else {
						try {
							const urlObj = new URL(job.url);
							const base = urlObj.pathname.split("/").pop();
							if (base && base.trim() !== "") {
								displayName = decodeURIComponent(base);
							} else {
								displayName = job.url;
							}
						} catch {
							const base = job.url.split("/").pop();
							displayName = base || job.url;
						}
					}

					let subInfo: React.ReactNode = null;
					if (isPostProcessing) {
						subInfo = (
							<span
								style={{
									color: "var(--status-postprocessing)",
									display: "inline-flex",
									alignItems: "center",
									gap: "6px",
								}}
							>
								<Settings size={12} className="animate-spin" />
								<span>Post-processing...</span>
							</span>
						);
					} else if (job.status === "queued") {
						subInfo = (
							<span
								style={{
									color: "var(--text-muted)",
									display: "inline-flex",
									alignItems: "center",
									gap: "6px",
								}}
							>
								<Loader2
									size={12}
									className="animate-spin"
									style={{ color: "var(--text-muted)" }}
								/>
								<span>Waiting in queue...</span>
							</span>
						);
					} else if (job.status === "paused") {
						subInfo = (
							<span
								style={{
									color: "var(--status-paused)",
									display: "inline-flex",
									alignItems: "center",
									gap: "6px",
								}}
							>
								<Pause size={12} />
								<span>Paused</span>
							</span>
						);
					} else if (job.status === "failed") {
						subInfo = (
							<span
								style={{
									color: "var(--status-failed)",
									overflow: "hidden",
									textOverflow: "ellipsis",
									whiteSpace: "nowrap",
									maxWidth: "320px",
									display: "inline-flex",
									alignItems: "center",
									gap: "6px",
								}}
								title={job.error}
							>
								<AlertCircle size={12} />
								<span>{job.error || "Download failed"}</span>
							</span>
						);
					} else if (job.status === "seeding") {
						subInfo = (
							<span
								style={{
									color: "var(--status-seeding)",
									display: "inline-flex",
									alignItems: "center",
									gap: "6px",
								}}
							>
								<UploadCloud size={12} />
								<span>Seeding</span>
							</span>
						);
					} else if (job.status === "completed") {
						subInfo = (
							<span
								style={{
									color: "var(--status-completed)",
									display: "inline-flex",
									alignItems: "center",
									gap: "6px",
								}}
							>
								<CheckCircle2 size={12} />
								<span>Completed</span>
							</span>
						);
					}

					return (
						<div className="job-title-cell">
							<div className="job-thumbnail-box">
								{job.thumbnail ? (
									<img src={job.thumbnail} alt="preview" />
								) : (
									<FileDown size={18} style={{ color: "var(--text-muted)" }} />
								)}
							</div>
							<div className="job-title-info">
								<div className="job-title-name" title={displayName}>
									{displayName}
								</div>
								{(job.media_type === "torrent" || subInfo) && (
									<div className="job-subinfo-row">
										{job.media_type === "torrent" && (
											<span
												className="torrent-inline-meta"
												style={{ marginRight: "8px", display: "inline-block" }}
											>
												{job.torrent_peers ?? 0} peers ·{" "}
												{job.torrent_seeds ?? 0} seeds
												{job.torrent_completed_pieces !== undefined
													? ` · ${job.torrent_completed_pieces}/${job.torrent_piece_count ?? "?"} pieces`
													: ""}
											</span>
										)}
										{subInfo}
									</div>
								)}
							</div>
						</div>
					);
				},
			},
			{
				header: "Status",
				accessorKey: "status",
				cell: ({ getValue }) => {
					const val = getValue() as string;
					return <span className={`status-badge ${val}`}>{val}</span>;
				},
			},
			{
				header: "Progress",
				accessorKey: "progress",
				cell: ({ row }) => {
					const job = row.original;
					const isPostProcessing = job.status === "postprocessing";
					const progressPct = job.progress;
					const progressDisplay = progressPct.toFixed(2);

					let barFillClass = "";
					if (job.status === "downloading") barFillClass = "downloading";
					else if (job.status === "postprocessing")
						barFillClass = "postprocessing";
					else if (job.status === "seeding") barFillClass = "seeding";
					else if (job.status === "completed") barFillClass = "completed";
					else if (job.status === "failed") barFillClass = "failed";
					else if (job.status === "paused") barFillClass = "paused";

					return (
						<div className="job-progress-cell">
							<div className="progress-bar-bg">
								<div
									className={`progress-bar-fill ${barFillClass} ${isPostProcessing ? "indeterminate" : ""}`}
									style={{
										width: isPostProcessing ? "100%" : `${progressPct}%`,
									}}
								></div>
							</div>
							<div className="progress-stats-row tabular-nums">
								<span className="progress-speed">
									{isPostProcessing
										? "Post-processing..."
										: job.status === "seeding"
											? "Seeding..."
											: formatSpeed(job.speed)}
								</span>
								<span className="progress-percent">
									{progressDisplay}%
									{job.status === "downloading" && job.fragment_index
										? ` (${job.fragment_index}/${job.fragment_count || "?"})`
										: ""}
								</span>
							</div>
						</div>
					);
				},
			},
			{
				header: "Size",
				id: "size",
				cell: ({ row }) => {
					const job = row.original;
					const combinedDl =
						job.combined_downloaded_bytes ??
						job.downloaded_bytes + job.audio_downloaded_bytes;
					const combinedTotal =
						job.combined_total_bytes ?? job.total_bytes + job.audio_total_bytes;

					const hasTotal = combinedTotal > 0;
					const dlStr = formatBytes(combinedDl);
					const totalStr = hasTotal ? formatBytes(combinedTotal) : "?";

					if (job.status === "completed") {
						return (
							<span
								className="job-size-cell tabular-nums"
								style={{ color: "var(--status-completed)", fontWeight: 700 }}
							>
								{formatBytes(combinedTotal || combinedDl)}
							</span>
						);
					}

					if (combinedDl === 0 && !hasTotal) {
						return <span className="job-size-cell job-size-total">—</span>;
					}

					return (
						<div className="job-size-cell tabular-nums">
							<span>{dlStr}</span>
							<span className="job-size-total"> / {totalStr}</span>
						</div>
					);
				},
			},
		],
		[],
	);

	const table = useReactTable({
		data: displayJobs,
		columns,
		getCoreRowModel: getCoreRowModel(),
		getRowId: (row) => row.job_id,
	});

	return (
		<section className="downloads-panel">
			<div className="queue-filter-toolbar">
				<div className="filter-tabs">
					<button
						className={`filter-tab ${filterTab === "all" ? "active" : ""}`}
						onClick={() => setFilterTab("all")}
					>
						All <span className="filter-count tabular-nums">{counts.all}</span>
					</button>
					<button
						className={`filter-tab ${filterTab === "downloading" ? "active" : ""}`}
						onClick={() => setFilterTab("downloading")}
					>
						Active{" "}
						<span className="filter-count tabular-nums">
							{counts.downloading}
						</span>
					</button>
					<button
						className={`filter-tab ${filterTab === "seeding" ? "active" : ""}`}
						onClick={() => setFilterTab("seeding")}
					>
						Seeding{" "}
						<span className="filter-count tabular-nums">{counts.seeding}</span>
					</button>
					<button
						className={`filter-tab ${filterTab === "completed" ? "active" : ""}`}
						onClick={() => setFilterTab("completed")}
					>
						Completed{" "}
						<span className="filter-count tabular-nums">
							{counts.completed}
						</span>
					</button>
					<button
						className={`filter-tab ${filterTab === "paused" ? "active" : ""}`}
						onClick={() => setFilterTab("paused")}
					>
						Paused{" "}
						<span className="filter-count tabular-nums">{counts.paused}</span>
					</button>
					<button
						className={`filter-tab ${filterTab === "failed" ? "active" : ""}`}
						onClick={() => setFilterTab("failed")}
					>
						Failed{" "}
						<span className="filter-count tabular-nums">{counts.failed}</span>
					</button>
				</div>

				{hasCompletedJobs && (
					<button
						className="action-btn-secondary clear-completed-btn"
						onClick={handleClearCompleted}
					>
						<span>Clear Completed</span>
					</button>
				)}
			</div>

			<div className="job-table-wrapper">
				<table className="job-table">
					<thead>
						{table.getHeaderGroups().map((headerGroup) => (
							<tr key={headerGroup.id}>
								{headerGroup.headers.map((header) => (
									<th
										key={header.id}
										style={{
											width:
												header.id === "title"
													? "40%"
													: header.id === "status"
														? "12%"
														: header.id === "progress"
															? "23%"
															: header.id === "size"
																? "13%"
																: "12%",
										}}
									>
										{header.isPlaceholder
											? null
											: flexRender(
													header.column.columnDef.header,
													header.getContext(),
												)}
									</th>
								))}
							</tr>
						))}
					</thead>
					<tbody>
						{table.getRowModel().rows.map((row) => {
							const job = row.original;
							return (
								<tr
									key={row.id}
									className="animate-fade-in-up"
									style={{ cursor: "context-menu" }}
									onContextMenu={(e) => {
										e.preventDefault();
										setContextMenu({
											x: e.clientX,
											y: e.clientY,
											jobId: job.job_id,
										});
									}}
								>
									{row.getVisibleCells().map((cell) => (
										<td key={cell.id}>
											{flexRender(
												cell.column.columnDef.cell,
												cell.getContext(),
											)}
										</td>
									))}
								</tr>
							);
						})}
					</tbody>
				</table>

				{displayJobs.length === 0 && (
					<div className="empty-queue-placeholder">
						<FileDown size={40} />
						<p>
							No downloads in the pipeline.
							<br />
							Paste a link above or trigger downloads via the browser extension.
						</p>
					</div>
				)}
			</div>
		</section>
	);
}

export function Dashboard({ downloader }: DashboardProps) {
	return (
		<div
			style={{ display: "flex", flexDirection: "column", gap: "24px" }}
			className="animate-fade-in-up"
		>
			<DirectDownloader downloader={downloader} />
			<DownloadsTable downloader={downloader} />
		</div>
	);
}
