import { X } from "lucide-react";
import type React from "react";
import { useEffect } from "react";
import type { UseDownloaderReturn } from "../hooks/useDownloader";
import { formatBytes } from "../utils";

interface ModalProps {
	isOpen: boolean;
	title: string;
	onClose: () => void;
	children: React.ReactNode;
	footer?: React.ReactNode;
	size?: "sm" | "md" | "lg" | "xl";
}

export function Modal({
	isOpen,
	title,
	onClose,
	children,
	footer,
	size = "md",
}: ModalProps) {
	// Prevent body scrolling when modal is open
	useEffect(() => {
		if (isOpen) {
			document.body.style.overflow = "hidden";
		} else {
			document.body.style.overflow = "";
		}
		return () => {
			document.body.style.overflow = "";
		};
	}, [isOpen]);

	if (!isOpen) return null;

	return (
		<div className="reusable-modal-overlay" onClick={onClose}>
			<div
				className={`reusable-modal-box size-${size}`}
				onClick={(e) => e.stopPropagation()}
			>
				<div className="reusable-modal-header">
					<h3>{title}</h3>
					<button className="reusable-modal-close-btn" onClick={onClose}>
						<X size={16} />
					</button>
				</div>
				<div className="reusable-modal-body">{children}</div>
				{footer && <div className="reusable-modal-footer">{footer}</div>}
			</div>
		</div>
	);
}

interface ModalsProps {
	downloader: UseDownloaderReturn;
}

export function Modals({ downloader }: ModalsProps) {
	const {
		jobs,
		propertiesJobId,
		setPropertiesJobId,
		duplicateJobAlert,
		setDuplicateJobAlert,
		duplicateFileAlert,
		setDuplicateFileAlert,
		proceedWithDownload,
		deleteFileConfirm,
		setDeleteFileConfirm,
		handleConfirmDeleteFile,
		genericAlert,
		setGenericAlert,
		isExtModalOpen,
		setIsExtModalOpen,
		browsersList,
		isLoadingBrowsers,
		handleInstallForBrowser,
		selectedFormatId,
		urlRefreshJobId,
		setUrlRefreshJobId,
	} = downloader;

	const confirmJob = deleteFileConfirm ? jobs[deleteFileConfirm] : null;
	const isTorrent = confirmJob?.media_type === "torrent";

	const urlRefreshJob = urlRefreshJobId ? jobs[urlRefreshJobId] : null;

	return (
		<>
			<Modal
				isOpen={!!propertiesJobId}
				title="Task Properties"
				onClose={() => setPropertiesJobId(null)}
				footer={
					<button
						className="action-btn"
						onClick={() => setPropertiesJobId(null)}
					>
						Close
					</button>
				}
				size="md"
			>
				{propertiesJobId &&
					(() => {
						const job = jobs[propertiesJobId];
						if (!job) return <p>Task details not found.</p>;

						return (
							<div className="properties-grid">
								<div className="properties-label">Title</div>
								<div className="properties-value" style={{ fontWeight: 700 }}>
									{job.title || "Unknown Title"}
								</div>

								<div className="properties-label">Job ID</div>
								<div
									className="properties-value"
									style={{ fontFamily: "var(--font-mono)" }}
								>
									{job.job_id}
								</div>

								<div className="properties-label">Source URL</div>
								<div
									className="properties-value"
									style={{ color: "var(--status-downloading)" }}
								>
									<a
										href={job.url}
										target="_blank"
										rel="noreferrer"
										style={{ color: "inherit", textDecoration: "none" }}
									>
										{job.url}
									</a>
								</div>

								<div className="properties-label">Status</div>
								<div
									className="properties-value"
									style={{ textTransform: "uppercase", fontWeight: 800 }}
								>
									<span
										className={`status-badge ${job.status}`}
										style={{ border: "none", padding: 0 }}
									>
										{job.status}
									</span>
								</div>

								<div className="properties-label">Progress</div>
								<div className="properties-value">
									{job.progress.toFixed(2)}% (
									{formatBytes(
										job.combined_downloaded_bytes ??
											job.downloaded_bytes + job.audio_downloaded_bytes,
									)}{" "}
									/{" "}
									{formatBytes(
										job.combined_total_bytes ??
											job.total_bytes + job.audio_total_bytes,
									)}
									)
								</div>

								{job.media_type === "torrent" && (
									<>
										<div className="properties-label">Torrent</div>
										<div className="properties-value">
											{job.torrent_peers ?? 0} peers, {job.torrent_seeds ?? 0}{" "}
											seeds, {job.torrent_availability?.toFixed(2) ?? "0.00"}{" "}
											availability
										</div>
										<div className="properties-label">Pieces</div>
										<div className="properties-value">
											{job.torrent_completed_pieces ?? 0} /{" "}
											{job.torrent_piece_count ?? "?"} completed
										</div>
									</>
								)}

								{job.file_path && (
									<>
										<div className="properties-label">Saved Path</div>
										<div
											className="properties-value"
											style={{
												color: "var(--status-completed)",
												fontFamily: "var(--font-mono)",
											}}
										>
											{job.file_path}
										</div>
									</>
								)}

								{job.output_dir && (
									<>
										<div className="properties-label">Output Dir</div>
										<div
											className="properties-value"
											style={{ fontFamily: "var(--font-mono)" }}
										>
											{job.output_dir}
										</div>
									</>
								)}

								{job.error && (
									<>
										<div
											className="properties-label"
											style={{ color: "var(--status-failed)" }}
										>
											Error
										</div>
										<div
											className="properties-value"
											style={{ color: "var(--status-failed)" }}
										>
											{job.error}
										</div>
									</>
								)}
							</div>
						);
					})()}
			</Modal>

			<Modal
				isOpen={!!duplicateJobAlert}
				title="Link Already In List"
				onClose={() => setDuplicateJobAlert(null)}
				footer={
					<>
						<button
							className="action-btn-secondary"
							onClick={() => setDuplicateJobAlert(null)}
						>
							Dismiss
						</button>
						{duplicateJobAlert && (
							<button
								className="action-btn"
								onClick={() => {
									setPropertiesJobId(duplicateJobAlert.jobId);
									setDuplicateJobAlert(null);
								}}
							>
								View Task Details
							</button>
						)}
					</>
				}
				size="sm"
			>
				{duplicateJobAlert && (
					<div
						style={{
							display: "flex",
							flexDirection: "column",
							gap: "14px",
							lineHeight: 1.4,
						}}
					>
						<div style={{ fontSize: "13px", color: "var(--text-secondary)" }}>
							This media link has already been submitted and exists in the task
							registry.
						</div>
						<div
							className="media-info"
							style={{
								background: "rgba(255,255,255,0.01)",
								border: "1px solid var(--border-dim)",
								padding: "12px",
								borderRadius: "var(--radius-md)",
								display: "flex",
								flexDirection: "column",
								gap: "8px",
							}}
						>
							<div style={{ fontWeight: 700, fontSize: "12.5px" }}>
								{duplicateJobAlert.title}
							</div>
							<div
								style={{
									fontSize: "11px",
									color: "var(--text-muted)",
									wordBreak: "break-all",
								}}
							>
								{duplicateJobAlert.url}
							</div>
							<div
								style={{
									fontSize: "11px",
									textTransform: "uppercase",
									fontWeight: 800,
								}}
							>
								Status:{" "}
								<span className={`status-badge ${duplicateJobAlert.status}`}>
									{duplicateJobAlert.status}
								</span>
							</div>
						</div>
					</div>
				)}
			</Modal>

			<Modal
				isOpen={!!duplicateFileAlert}
				title="Duplicate File Detected"
				onClose={() => setDuplicateFileAlert(null)}
				footer={
					<>
						<button
							className="action-btn-secondary"
							onClick={() => setDuplicateFileAlert(null)}
						>
							Cancel
						</button>
						{duplicateFileAlert && (
							<>
								<button
									className="action-btn-secondary"
									onClick={() => {
										proceedWithDownload(
											duplicateFileAlert.jobId,
											selectedFormatId,
											duplicateFileAlert.path,
											"rename",
										);
									}}
								>
									Add Anyway (Auto-Rename)
								</button>
								<button
									className="action-btn"
									onClick={() => {
										proceedWithDownload(
											duplicateFileAlert.jobId,
											selectedFormatId,
											duplicateFileAlert.path,
											"replace",
										);
									}}
								>
									Replace / Overwrite
								</button>
							</>
						)}
					</>
				}
				size="md"
			>
				{duplicateFileAlert && (
					<div
						style={{
							display: "flex",
							flexDirection: "column",
							gap: "14px",
							lineHeight: 1.4,
						}}
					>
						<div style={{ fontSize: "13px", color: "var(--text-secondary)" }}>
							A file with the same name already exists in the target save
							destination.
						</div>
						<div
							style={{
								background: "rgba(255,255,255,0.01)",
								border: "1px solid var(--border-dim)",
								padding: "12px",
								borderRadius: "var(--radius-md)",
								display: "flex",
								flexDirection: "column",
								gap: "6px",
							}}
						>
							<div
								style={{
									fontWeight: 700,
									fontSize: "12.5px",
									wordBreak: "break-all",
								}}
							>
								{duplicateFileAlert.filename}
							</div>
							<div
								style={{
									fontSize: "11px",
									color: "var(--text-muted)",
									wordBreak: "break-all",
								}}
							>
								Path: {duplicateFileAlert.path}
							</div>
						</div>
						<div style={{ fontSize: "12.5px", color: "var(--text-muted)" }}>
							Would you like to overwrite the existing file or download it as a
							new copy with an incremented name?
						</div>
					</div>
				)}
			</Modal>

			<Modal
				isOpen={!!deleteFileConfirm}
				title={isTorrent ? "Confirm Torrent Deletion" : "Confirm File Deletion"}
				onClose={() => setDeleteFileConfirm(null)}
				footer={
					<>
						<button
							className="action-btn-secondary"
							onClick={() => setDeleteFileConfirm(null)}
						>
							Cancel
						</button>
						<button
							className="action-btn"
							style={{
								background: "var(--status-failed)",
								borderColor: "var(--status-failed)",
							}}
							onClick={() => {
								handleConfirmDeleteFile();
							}}
						>
							{isTorrent ? "Delete Folder" : "Delete File"}
						</button>
					</>
				}
				size="sm"
			>
				<p
					style={{
						fontSize: "13px",
						lineHeight: 1.5,
						color: "var(--text-secondary)",
					}}
				>
					{isTorrent
						? "Are you sure you want to delete the downloaded torrent folder and all its contents from your disk? This action is irreversible."
						: "Are you sure you want to delete the downloaded file from your disk? This action is irreversible."}
				</p>
			</Modal>

			<Modal
				isOpen={!!genericAlert}
				title={genericAlert?.title || "Notification"}
				onClose={() => setGenericAlert(null)}
				footer={
					<button className="action-btn" onClick={() => setGenericAlert(null)}>
						OK
					</button>
				}
				size="sm"
			>
				<div
					style={{
						display: "flex",
						flexDirection: "column",
						gap: "10px",
						lineHeight: 1.4,
						fontSize: "13px",
						color: "var(--text-secondary)",
					}}
				>
					<p>{genericAlert?.message}</p>
					{genericAlert?.suggestion && (
						<div
							style={{
								background: "rgba(255,255,255,0.01)",
								border: "1px solid var(--border-dim)",
								padding: "10px",
								borderRadius: "var(--radius-md)",
								fontSize: "11.5px",
								color: "var(--text-muted)",
							}}
						>
							<strong>Suggestion:</strong> {genericAlert.suggestion}
						</div>
					)}
				</div>
			</Modal>

			<Modal
				isOpen={isExtModalOpen}
				title="Install Browser Extension"
				onClose={() => setIsExtModalOpen(false)}
				size="md"
			>
				<div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
					<p
						style={{
							color: "var(--text-secondary)",
							fontSize: "13px",
							lineHeight: "1.5",
						}}
					>
						Select an installed browser to integrate the DownloadAnything media
						sniffer extension:
					</p>

					{isLoadingBrowsers ? (
						<div
							style={{
								display: "flex",
								justifyContent: "center",
								padding: "24px 0",
							}}
						>
							<span style={{ fontSize: "14px", color: "var(--text-muted)" }}>
								Detecting installed browsers...
							</span>
						</div>
					) : (
						<div
							style={{ display: "flex", flexDirection: "column", gap: "12px" }}
						>
							{browsersList.map((browser) => (
								<div
									key={browser.key}
									style={{
										display: "flex",
										justifyContent: "space-between",
										alignItems: "center",
										padding: "16px 20px",
										background: "rgba(255,255,255,0.015)",
										border: "1px solid var(--border-dim)",
										borderRadius: "var(--radius-lg)",
										transition: "var(--transition-smooth)",
									}}
									className="browser-card-row"
								>
									<div
										style={{
											display: "flex",
											flexDirection: "column",
											gap: "6px",
										}}
									>
										<span
											style={{
												fontWeight: "600",
												fontSize: "13.5px",
												color: "#ffffff",
											}}
										>
											{browser.name}
										</span>
										<span
											style={{
												fontSize: "11px",
												color: browser.installed
													? "var(--status-completed)"
													: "var(--status-failed)",
												fontWeight: 700,
												textTransform: "uppercase",
												letterSpacing: "0.03em",
											}}
										>
											{browser.installed ? "● Detected" : "○ Not Detected"}
										</span>
									</div>

									<button
										className="action-btn"
										style={{ padding: "8px 16px", fontSize: "12px" }}
										onClick={() => handleInstallForBrowser(browser)}
										disabled={!browser.installed}
									>
										Install
									</button>
								</div>
							))}
						</div>
					)}
				</div>
			</Modal>

			<Modal
				isOpen={!!urlRefreshJobId}
				title="Download link expired"
				onClose={() => setUrlRefreshJobId(null)}
				size="md"
				footer={
					<button
						type="button"
						className="action-btn-secondary"
						onClick={() => setUrlRefreshJobId(null)}
					>
						Cancel
					</button>
				}
			>
				{urlRefreshJob ? (
					<div
						style={{
							display: "flex",
							flexDirection: "column",
							gap: "14px",
						}}
					>
						<p
							style={{
								fontSize: "13px",
								color: "var(--text-secondary)",
								lineHeight: 1.5,
							}}
						>
							The download link for{" "}
							<strong>{urlRefreshJob.title || "this task"}</strong> has expired.
							Return to the source page in your browser and start the download
							again. The extension will capture the new link and resume this
							task automatically.
						</p>
						{urlRefreshJob.page_url && (
							<a
								href={urlRefreshJob.page_url}
								target="_blank"
								rel="noopener noreferrer"
								style={{
									fontSize: "12px",
									color: "var(--accent)",
									wordBreak: "break-all",
									fontFamily: "monospace",
								}}
							>
								{urlRefreshJob.page_url}
							</a>
						)}
						<p
							style={{
								fontSize: "12px",
								color: "var(--text-muted)",
							}}
						>
							Listening for a new download URL from the extension...
						</p>
					</div>
				) : (
					<p>Task not found.</p>
				)}
			</Modal>
		</>
	);
}
