import { FileDown, Settings } from "lucide-react";
import type { UseDownloaderReturn } from "../hooks/useDownloader";

interface SidebarProps {
	downloader: UseDownloaderReturn;
}

export function Sidebar({ downloader }: SidebarProps) {
	const {
		activeTab,
		setActiveTab,
		setActiveSettingsSection,
		isConnected,
		serverInfo,
		fetchSettings,
		fetchCategories,
	} = downloader;

	return (
		<aside className="sidebar">
			<div className="sidebar-top">
				<nav className="sidebar-section">
					<h2 className="sidebar-title">Downloads</h2>
					<button
						type="button"
						className={`tab-btn ${activeTab === "downloads" ? "active" : ""}`}
						onClick={() => setActiveTab("downloads")}
					>
						<FileDown size={16} />
						<span>Dashboard</span>
					</button>

					<h2 className="sidebar-title">Configuration</h2>
					<button
						type="button"
						className={`tab-btn ${activeTab === "settings" ? "active" : ""}`}
						onClick={() => {
							setActiveTab("settings");
							setActiveSettingsSection("general");
							fetchSettings();
							fetchCategories();
						}}
					>
						<Settings size={16} />
						<span>Settings</span>
					</button>
				</nav>
			</div>

			<div className="sidebar-footer">
				<div className="server-status-pill">
					<div className="status-indicator-row-minimal">
						<span
							className={`pulse-dot ${isConnected ? "online" : "offline"}`}
						></span>
						<span>{isConnected ? "Server Connected" : "Server Offline"}</span>
					</div>
					{isConnected && serverInfo && (
						<div className="system-specs-mini">
							<span
								title={`yt-dlp version: ${serverInfo.ytDlpVersion || "unknown"}`}
							>
								yt-dlp v
								{(serverInfo.ytDlpVersion || "").substring(0, 8) || "..."}
							</span>
							<span>FFmpeg: {serverInfo.ffmpegAvailable ? "OK" : "ERR"}</span>
						</div>
					)}
				</div>
			</div>
		</aside>
	);
}
