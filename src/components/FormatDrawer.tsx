import { Download, FileDown, FolderOpen, Music, Video } from "lucide-react";
import { useEffect, useState } from "react";
import type { UseDownloaderReturn } from "../hooks/useDownloader";
import { formatBytes, formatDuration } from "../utils";
import { Modal } from "./Modals";

interface FormatDrawerProps {
	downloader: UseDownloaderReturn;
}

const ENGINE_BADGES: Record<string, { label: string; cls: string }> = {
	stream: { label: "Stream", cls: "stream" },
	torrent: { label: "Torrent", cls: "torrent" },
	ytdlp: { label: "yt-dlp", cls: "ytdlp" },
};

export function FormatDrawer({ downloader }: FormatDrawerProps) {
	const {
		showFormatDrawer,
		setShowFormatDrawer,
		probedInfo,
		setProbedInfo,
		selectedFormatId,
		setSelectedFormatId,
		categories,
		selectedCategoryPath,
		setSelectedCategoryPath,
		drawerCustomPath,
		setDrawerCustomPath,
		fetchDirectory,
		handleChooseFormat,
		inputUrl,
	} = downloader;

	const [activeTab, setActiveTab] = useState<"video" | "audio">("video");

	useEffect(() => {
		if (!probedInfo) return;
		const activeFormats = probedInfo.formats.filter((fmt) => {
			const isVideo = (fmt.height && fmt.height > 0) || fmt.formatId === "best";
			return activeTab === "video" ? isVideo : !isVideo;
		});
		const isCurrentSelectedInTab = activeFormats.some(
			(f) => f.formatId === selectedFormatId,
		);
		if (!isCurrentSelectedInTab && activeFormats.length > 0) {
			setSelectedFormatId(activeFormats[0].formatId);
		}
	}, [activeTab, probedInfo, selectedFormatId, setSelectedFormatId]);

	const isOpen = showFormatDrawer && !!probedInfo;

	return (
		<Modal
			isOpen={isOpen}
			title="Configure Download Option"
			onClose={() => {
				setShowFormatDrawer(false);
				setProbedInfo(null);
			}}
			size="lg"
			footer={
				<>
					<button
						className="action-btn-secondary"
						onClick={() => {
							setShowFormatDrawer(false);
							setProbedInfo(null);
						}}
					>
						Cancel
					</button>
					<button className="action-btn" onClick={handleChooseFormat}>
						<span>Download Now</span>
						<Download size={14} />
					</button>
				</>
			}
		>
			{probedInfo && (
				<div className="format-drawer-body">
					<div className="media-meta-card">
						<div className="meta-thumbnail-wrapper">
							{probedInfo.thumbnail ? (
								<img src={probedInfo.thumbnail} alt="Cover" />
							) : (
								<div
									style={{
										display: "flex",
										alignItems: "center",
										justifyContent: "center",
										height: "100%",
										color: "var(--text-muted)",
									}}
								>
									<FileDown size={32} />
								</div>
							)}
							{probedInfo.duration ? (
								<span className="duration-badge">
									{formatDuration(probedInfo.duration)}
								</span>
							) : null}
						</div>
						<div className="meta-info">
							<div className="meta-title" title={probedInfo.title}>
								{probedInfo.title}
							</div>
							<div className="meta-badges-row">
								{probedInfo.uploader && (
									<span className="meta-badge-chip">
										Uploader: {probedInfo.uploader}
									</span>
								)}
								{probedInfo.duration && (
									<span className="meta-badge-chip">
										Duration: {formatDuration(probedInfo.duration)}
									</span>
								)}
								{probedInfo.mediaType && (
									<span
										className={`meta-badge-chip engine ${ENGINE_BADGES[probedInfo.mediaType]?.cls ?? "ytdlp"}`}
									>
										{ENGINE_BADGES[probedInfo.mediaType]?.label ?? "yt-dlp"}
									</span>
								)}
							</div>
							<div className="meta-url">{inputUrl}</div>
						</div>
					</div>

					{probedInfo.mediaType === "torrent" && probedInfo.torrent ? (
						<div className="torrent-metadata-panel">
							<div className="torrent-summary-grid">
								<span>Size</span>
								<strong>{formatBytes(probedInfo.torrent.totalSize)}</strong>
								<span>Pieces</span>
								<strong>{probedInfo.torrent.pieceCount}</strong>
								<span>Piece length</span>
								<strong>{formatBytes(probedInfo.torrent.pieceLength)}</strong>
							</div>
						</div>
					) : (
						<div style={{ marginTop: "20px" }}>
							<div className="format-section-title">
								Available Target Streams
							</div>

							<div className="modal-tabs">
								<button
									type="button"
									className={`modal-tab-btn ${activeTab === "video" ? "active" : ""}`}
									onClick={() => setActiveTab("video")}
								>
									<Video size={13} />
									Video
								</button>
								<button
									type="button"
									className={`modal-tab-btn ${activeTab === "audio" ? "active" : ""}`}
									onClick={() => setActiveTab("audio")}
								>
									<Music size={13} />
									Audio
								</button>
							</div>

							<div className="format-list">
								<div className="format-list-header">
									<div className="format-list-radio-col"></div>
									<div className="format-list-label-col">Stream Name</div>
									<div className="format-list-ext-col">Ext</div>
									<div className="format-list-size-col">Est. Size</div>
									<div className="format-list-badges-col">Details</div>
								</div>
								{probedInfo.formats
									.filter((fmt) => {
										const isVideo =
											(fmt.height && fmt.height > 0) || fmt.formatId === "best";
										return activeTab === "video" ? isVideo : !isVideo;
									})
									.map((fmt) => {
										const isSelected = selectedFormatId === fmt.formatId;
										return (
											<div
												key={fmt.formatId}
												className={`format-list-row ${isSelected ? "selected" : ""}`}
												onClick={() => setSelectedFormatId(fmt.formatId)}
											>
												<div className="format-list-radio-col">
													<input
														type="radio"
														name="format-choice"
														checked={isSelected}
														onChange={() => setSelectedFormatId(fmt.formatId)}
													/>
												</div>
												<div className="format-list-label-col">
													<span className="format-list-label" title={fmt.label}>
														{fmt.label}
													</span>
												</div>
												<div className="format-list-ext-col">
													<span className="format-list-ext">
														{fmt.ext.toUpperCase()}
													</span>
												</div>
												<div className="format-list-size-col">
													<span className="format-list-size">
														{fmt.estSizeBytes
															? formatBytes(fmt.estSizeBytes)
															: "\u2014"}
													</span>
												</div>
												<div className="format-list-badges-col">
													{fmt.hdr && (
														<span className="format-badge hdr">HDR</span>
													)}
													{fmt.isCombined && (
														<span
															className="format-badge-pill video"
															title="Multiplexed video and audio"
														>
															Muxed
														</span>
													)}
													{fmt.codecFamily && (
														<span
															className="format-badge codec"
															title={fmt.codecFamily}
														>
															{fmt.codecFamily}
														</span>
													)}
												</div>
											</div>
										);
									})}
								{probedInfo.formats.filter((fmt) => {
									const isVideo =
										(fmt.height && fmt.height > 0) || fmt.formatId === "best";
									return activeTab === "video" ? isVideo : !isVideo;
								}).length === 0 && (
									<div
										style={{
											padding: "24px",
											textAlign: "center",
											color: "var(--text-muted)",
											fontSize: "13px",
										}}
									>
										No formats discovered for this tab.
									</div>
								)}
							</div>
						</div>
					)}

					<div className="drawer-options-row" style={{ marginTop: "16px" }}>
						<div className="form-field" style={{ marginBottom: 0 }}>
							<label htmlFor="drawerCategory">Save to Preset Category</label>
							<div style={{ display: "flex", gap: "8px" }}>
								<select
									id="drawerCategory"
									className="form-select"
									value={
										categories.find((c) => c.path === selectedCategoryPath)
											?.path || ""
									}
									onChange={(e) => {
										setSelectedCategoryPath(e.target.value);
										setDrawerCustomPath("");
									}}
									style={{ flex: 1 }}
								>
									{categories.map((c, i) => (
										<option key={i} value={c.path}>
											{c.name} ({c.path})
										</option>
									))}
								</select>
								<button
									className="action-btn-secondary"
									onClick={() =>
										fetchDirectory(
											drawerCustomPath || selectedCategoryPath,
											"drawer",
										)
									}
									title="Browse Location..."
								>
									<FolderOpen size={14} />
								</button>
							</div>
							{drawerCustomPath && (
								<div
									style={{
										display: "flex",
										justifyContent: "space-between",
										alignItems: "center",
										fontSize: "11.5px",
										color: "var(--text-secondary)",
										marginTop: "8px",
										background: "rgba(255, 255, 255, 0.02)",
										border: "1px solid var(--border-dim)",
										borderRadius: "var(--radius-sm)",
										padding: "6px 10px",
									}}
								>
									<span style={{ wordBreak: "break-all" }}>
										Custom Location:{" "}
										<strong style={{ color: "#ffffff" }}>
											{drawerCustomPath}
										</strong>
									</span>
									<button
										style={{
											background: "none",
											border: "none",
											color: "var(--status-failed)",
											cursor: "pointer",
											fontSize: "11px",
											fontWeight: 600,
											padding: "0 0 0 8px",
										}}
										onClick={() => setDrawerCustomPath("")}
									>
										Clear
									</button>
								</div>
							)}
						</div>
					</div>
				</div>
			)}
		</Modal>
	);
}
