import {
	FileX,
	FolderOpen,
	Info,
	Link,
	Pause,
	Play,
	Trash2,
} from "lucide-react";
import type { UseDownloaderReturn } from "../hooks/useDownloader";

interface ContextMenuProps {
	downloader: UseDownloaderReturn;
}

export function ContextMenu({ downloader }: ContextMenuProps) {
	const {
		contextMenu,
		setContextMenu,
		jobs,
		handlePauseJob,
		handleResumeJob,
		handleRemoveJob,
		handleRevealFile,
		handleDeleteFile,
		setPropertiesJobId,
		setUrlRefreshJobId,
	} = downloader;

	if (!contextMenu) return null;

	const job = jobs[contextMenu.jobId];
	if (!job) return null;

	const isRunning = ["downloading", "queued", "postprocessing"].includes(
		job.status,
	);
	const isOngoing = isRunning || job.status === "seeding";
	const canResume = job.status === "paused" || job.status === "failed";
	const isMac = navigator.userAgent.toLowerCase().includes("mac");
	const revealLabel = isMac ? "Reveal in Finder" : "Reveal in Explorer";

	return (
		<div
			className="context-menu"
			style={{ top: `${contextMenu.y}px`, left: `${contextMenu.x}px` }}
			onClick={(e) => e.stopPropagation()}
		>
			{isOngoing && (
				<div
					className="context-menu-item"
					onClick={() => {
						handlePauseJob(job.job_id);
						setContextMenu(null);
					}}
				>
					<Pause size={14} />
					<span>Pause Task</span>
				</div>
			)}
			{canResume && (
				<div
					className="context-menu-item"
					onClick={() => {
						handleResumeJob(job.job_id);
						setContextMenu(null);
					}}
				>
					<Play size={14} />
					<span>Resume Task</span>
				</div>
			)}
			{job.status === "failed" && job.error_category === "expired_url" && (
				<div
					className="context-menu-item"
					onClick={() => {
						setUrlRefreshJobId(job.job_id);
						setContextMenu(null);
					}}
				>
					<Link size={14} />
					<span>Refresh expired link</span>
				</div>
			)}
			{!isRunning && (
				<div
					className="context-menu-item"
					onClick={() => {
						handleRemoveJob(job.job_id);
						setContextMenu(null);
					}}
				>
					<Trash2 size={14} />
					<span>Remove from List</span>
				</div>
			)}
			{job.file_path && (
				<div
					className="context-menu-item"
					onClick={() => {
						handleRevealFile(job.job_id);
						setContextMenu(null);
					}}
				>
					<FolderOpen size={14} />
					<span>{revealLabel}</span>
				</div>
			)}
			{job.status === "completed" && (
				<div
					className="context-menu-item"
					onClick={() => {
						handleDeleteFile(job.job_id);
						setContextMenu(null);
					}}
				>
					<FileX size={14} style={{ color: "var(--status-failed)" }} />
					<span style={{ color: "var(--status-failed)" }}>
						{job.media_type === "torrent" ? "Delete Folder" : "Delete File"}
					</span>
				</div>
			)}
			<div
				className="context-menu-item"
				onClick={() => {
					setPropertiesJobId(job.job_id);
					setContextMenu(null);
				}}
			>
				<Info size={14} />
				<span>Properties</span>
			</div>
		</div>
	);
}
