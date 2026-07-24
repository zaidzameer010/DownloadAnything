import {
	Cpu,
	FolderOpen,
	Globe,
	Play,
	Puzzle,
	Settings,
	Trash2,
} from "lucide-react";
import type {
	DownloaderSettings,
	UseDownloaderReturn,
} from "../hooks/useDownloader";

interface SettingsPanelProps {
	downloader: UseDownloaderReturn;
}

function GeneralSettings({ downloader }: { downloader: UseDownloaderReturn }) {
	const {
		mergeFormat,
		setMergeFormat,
		cookiesFromBrowser,
		setCookiesFromBrowser,
		embedThumbnail,
		setEmbedThumbnail,
		embedSubs,
		setEmbedSubs,
		subtitlesLangs,
		setSubtitlesLangs,
		pushSettings,
	} = downloader;

	return (
		<div className="general-preferences-layout">
			<section className="settings-card">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<Settings size={18} />
						<span>Output Preferences</span>
					</h2>
				</div>

				<div className="settings-group">
					<div className="form-field">
						<label htmlFor="mergeFormat">Merged Output Format</label>
						<select
							id="mergeFormat"
							className="form-select"
							value={mergeFormat}
							onChange={(e) => {
								setMergeFormat(e.target.value);
								pushSettings({ mergeFormat: e.target.value });
							}}
						>
							<option value="mkv">MKV (Broad Codec Support)</option>
							<option value="mp4">MP4 (Merge MP4 container)</option>
						</select>
					</div>

					<div className="form-field">
						<label htmlFor="cookiesFromBrowser">
							Native Cookies Browser Source
						</label>
						<select
							id="cookiesFromBrowser"
							className="form-select"
							value={cookiesFromBrowser}
							onChange={(e) => {
								setCookiesFromBrowser(e.target.value);
								pushSettings({ cookiesFromBrowser: e.target.value });
							}}
						>
							<option value="none">None (Bypassed)</option>
							<option value="chrome">Chrome</option>
							<option value="firefox">Firefox</option>
							<option value="safari">Safari</option>
							<option value="edge">Edge</option>
							<option value="brave">Brave</option>
							<option value="opera">Opera</option>
							<option value="vivaldi">Vivaldi</option>
						</select>
					</div>

					<div className="checkboxes-vertical-group">
						<label className="checkbox-row custom-toggle-row">
							<input
								type="checkbox"
								checked={embedThumbnail}
								onChange={(e) => {
									setEmbedThumbnail(e.target.checked);
									pushSettings({ embedThumbnail: e.target.checked });
								}}
							/>
							<span>Embed Album Art / Thumbnail</span>
						</label>

						<label className="checkbox-row custom-toggle-row">
							<input
								type="checkbox"
								checked={embedSubs}
								onChange={(e) => {
									setEmbedSubs(e.target.checked);
									pushSettings({ embedSubs: e.target.checked });
								}}
							/>
							<span>Extract & Embed Subtitles</span>
						</label>
					</div>

					{embedSubs && (
						<div className="form-field subtitle-langs-field animate-fade-in-up">
							<label htmlFor="subtitlesLangs">
								Embed Subtitles Languages (comma separated, e.g. en,es or all)
							</label>
							<input
								id="subtitlesLangs"
								type="text"
								className="form-input"
								value={subtitlesLangs}
								onChange={(e) => setSubtitlesLangs(e.target.value)}
								onBlur={(e) => pushSettings({ subtitlesLangs: e.target.value })}
								placeholder="all"
							/>
						</div>
					)}
				</div>
			</section>
		</div>
	);
}

function CategoriesSettings({
	downloader,
}: {
	downloader: UseDownloaderReturn;
}) {
	const {
		categories,
		newCatName,
		setNewCatName,
		newCatPath,
		setNewCatPath,
		handleAddCategory,
		handleDeleteCategory,
		fetchDirectory,
	} = downloader;

	return (
		<div className="preset-paths-layout">
			<div className="settings-card form-section">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<FolderOpen size={18} />
						<span>Create Preset Path</span>
					</h2>
				</div>
				<div className="add-cat-form-fields">
					<div className="form-field">
						<label htmlFor="catNameInput">Category Name</label>
						<input
							id="catNameInput"
							type="text"
							placeholder="e.g. Movies, Music, Lectures"
							className="form-input"
							value={newCatName}
							onChange={(e) => setNewCatName(e.target.value)}
						/>
					</div>

					<div className="form-field">
						<label>Selected Destination Path</label>
						<div style={{ display: "flex", gap: "8px" }}>
							<input
								type="text"
								className="form-input"
								value={newCatPath}
								onChange={(e) => setNewCatPath(e.target.value)}
								placeholder="Select a folder path..."
								style={{ flex: 1 }}
							/>
							<button
								className="action-btn-secondary"
								style={{ padding: "0 12px" }}
								onClick={() => fetchDirectory(newCatPath, "new_category")}
							>
								<FolderOpen size={14} />
							</button>
						</div>
					</div>

					<div
						style={{
							display: "flex",
							justifyContent: "flex-end",
							marginTop: "8px",
						}}
					>
						<button
							className="action-btn"
							onClick={handleAddCategory}
							disabled={!newCatName.trim() || !newCatPath.trim()}
						>
							<span>Save Category</span>
						</button>
					</div>
				</div>
			</div>

			<div className="settings-card list-section">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<FolderOpen size={18} />
						<span>Preset Directories</span>
					</h2>
				</div>
				<p className="section-subtitle">
					Define default download paths. These presets populate the dashboard
					drawer and browser extension dropdown.
				</p>

				<div className="categories-list">
					{categories.map((cat, idx) => (
						<div key={idx} className="category-item-row animate-fade-in-up">
							<div className="category-details">
								<span className="category-name">{cat.name}</span>
								<span className="category-path" title={cat.path}>
									{cat.path}
								</span>
							</div>
							{cat.name !== "Default" ? (
								<button
									className="delete-cat-btn"
									onClick={() => handleDeleteCategory(cat.name)}
									title="Delete Category"
								>
									<Trash2 size={13} />
								</button>
							) : (
								<span
									style={{
										fontSize: "9px",
										color: "var(--text-muted)",
										fontWeight: 800,
										paddingRight: "8px",
										letterSpacing: "0.05em",
									}}
								>
									SYSTEM
								</span>
							)}
						</div>
					))}
				</div>
			</div>
		</div>
	);
}

function EnginesSettings({ downloader }: { downloader: UseDownloaderReturn }) {
	const {
		concurrentFragmentDownloads,
		setConcurrentFragmentDownloads,
		downloadRetries,
		setDownloadRetries,
		fragmentRetries,
		setFragmentRetries,
		rateLimit,
		setRateLimit,
		ffmpegLocation,
		setFfmpegLocation,
		useAria2Next,
		setUseAria2Next,
		aria2NextMaxConnections,
		setAria2NextMaxConnections,
		aria2NextConcurrentDownloads,
		setAria2NextConcurrentDownloads,
		aria2NextSplit,
		setAria2NextSplit,
		aria2NextMinSplitSize,
		setAria2NextMinSplitSize,
		aria2NextPreallocate,
		setAria2NextPreallocate,
		aria2NextCheckCertificate,
		setAria2NextCheckCertificate,
		aria2NextAlwaysResume,
		setAria2NextAlwaysResume,
		pushSettings,
	} = downloader;

	return (
		<div className="engines-layout">
			<section className="settings-card">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<Globe size={18} />
						<span>yt-dlp Core Tuning</span>
					</h2>
				</div>

				<div className="settings-group">
					<div className="sliders-grid">
						<div className="form-field">
							<div className="label-row">
								<label htmlFor="concurrentFragmentDownloads">
									Concurrent Fragment Downloads
								</label>
								<span className="slider-value tabular-nums">
									{concurrentFragmentDownloads}
								</span>
							</div>
							<input
								id="concurrentFragmentDownloads"
								type="range"
								min="1"
								max="16"
								className="form-input-slider"
								value={concurrentFragmentDownloads}
								onChange={(e) => {
									const v = parseInt(e.target.value);
									setConcurrentFragmentDownloads(v);
									pushSettings({ concurrentFragmentDownloads: v });
								}}
							/>
						</div>
						<div className="form-field">
							<div className="label-row">
								<label htmlFor="downloadRetries">Download Retries</label>
								<span className="slider-value tabular-nums">
									{downloadRetries}
								</span>
							</div>
							<input
								id="downloadRetries"
								type="range"
								min="0"
								max="30"
								className="form-input-slider"
								value={downloadRetries}
								onChange={(e) => {
									const v = parseInt(e.target.value);
									setDownloadRetries(v);
									pushSettings({ downloadRetries: v });
								}}
							/>
						</div>
						<div className="form-field">
							<div className="label-row">
								<label htmlFor="fragmentRetries">Fragment Retries</label>
								<span className="slider-value tabular-nums">
									{fragmentRetries}
								</span>
							</div>
							<input
								id="fragmentRetries"
								type="range"
								min="0"
								max="30"
								className="form-input-slider"
								value={fragmentRetries}
								onChange={(e) => {
									const v = parseInt(e.target.value);
									setFragmentRetries(v);
									pushSettings({ fragmentRetries: v });
								}}
							/>
						</div>
					</div>

					<div className="form-field">
						<label htmlFor="rateLimit">Download Speed Limit</label>
						<input
							id="rateLimit"
							type="text"
							className="form-input"
							value={rateLimit}
							onChange={(e) => setRateLimit(e.target.value)}
							onBlur={(e) => pushSettings({ rateLimit: e.target.value })}
							placeholder="Unlimited (e.g. 50K, 1M, 5M)"
						/>
					</div>

					<div className="form-field">
						<label htmlFor="ffmpegLocation">Custom FFmpeg Binary Path</label>
						<input
							id="ffmpegLocation"
							type="text"
							className="form-input"
							value={ffmpegLocation}
							onChange={(e) => setFfmpegLocation(e.target.value)}
							onBlur={(e) => pushSettings({ ffmpegLocation: e.target.value })}
							placeholder="System default path (leave empty)"
						/>
					</div>
				</div>
			</section>

			<section className="settings-card">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<Play size={18} />
						<span>aria2-next Configuration</span>
					</h2>
				</div>
				<div className="settings-group">
					<label className="checkbox-row custom-toggle-row">
						<input
							type="checkbox"
							checked={useAria2Next}
							onChange={(e) => {
								setUseAria2Next(e.target.checked);
								pushSettings({ useAria2Next: e.target.checked });
							}}
						/>
						<span>Enable aria2-next External Downloader</span>
					</label>

					{useAria2Next && (
						<div className="aria2-next-subpanel animate-fade-in-up">
							<div className="sliders-grid">
								<div className="form-field">
									<div className="label-row">
										<label htmlFor="aria2NextConcurrentDownloads">
											Max Concurrent Downloads
										</label>
										<span className="slider-value tabular-nums">
											{aria2NextConcurrentDownloads}
										</span>
									</div>
									<input
										id="aria2NextConcurrentDownloads"
										type="range"
										min="1"
										max="10"
										className="form-input-slider"
										value={aria2NextConcurrentDownloads}
										onChange={(e) => {
											const v = parseInt(e.target.value);
											setAria2NextConcurrentDownloads(v);
											pushSettings({ aria2NextConcurrentDownloads: v });
										}}
									/>
								</div>
								<div className="form-field">
									<div className="label-row">
										<label htmlFor="aria2NextMaxConnections">
											Max Connections Per Server
										</label>
										<span className="slider-value tabular-nums">
											{aria2NextMaxConnections}
										</span>
									</div>
									<input
										id="aria2NextMaxConnections"
										type="range"
										min="1"
										max="64"
										className="form-input-slider"
										value={aria2NextMaxConnections}
										onChange={(e) => {
											const v = parseInt(e.target.value);
											setAria2NextMaxConnections(v);
											pushSettings({ aria2NextMaxConnections: v });
										}}
									/>
								</div>
								<div className="form-field">
									<div className="label-row">
										<label htmlFor="aria2NextSplit">
											Max Split Connections Per File
										</label>
										<span className="slider-value tabular-nums">
											{aria2NextSplit}
										</span>
									</div>
									<input
										id="aria2NextSplit"
										type="range"
										min="1"
										max="64"
										className="form-input-slider"
										value={aria2NextSplit}
										onChange={(e) => {
											const v = parseInt(e.target.value);
											setAria2NextSplit(v);
											pushSettings({ aria2NextSplit: v });
										}}
									/>
								</div>
							</div>

							<div className="form-field" style={{ marginTop: "8px" }}>
								<label htmlFor="aria2NextMinSplitSize">
									Minimum Split Size
								</label>
								<select
									id="aria2NextMinSplitSize"
									className="form-select"
									value={aria2NextMinSplitSize}
									onChange={(e) => {
										setAria2NextMinSplitSize(e.target.value);
										pushSettings({ aria2NextMinSplitSize: e.target.value });
									}}
								>
									<option value="1M">1 MB (Aggressive splitting)</option>
									<option value="5M">5 MB</option>
									<option value="10M">10 MB</option>
									<option value="20M">20 MB</option>
									<option value="50M">50 MB</option>
								</select>
							</div>

							<div className="checkboxes-grid">
								<label className="checkbox-row custom-toggle-row">
									<input
										type="checkbox"
										checked={aria2NextPreallocate}
										onChange={(e) => {
											setAria2NextPreallocate(e.target.checked);
											pushSettings({ aria2NextPreallocate: e.target.checked });
										}}
									/>
									<span>Pre-allocate File Space</span>
								</label>
								<label className="checkbox-row custom-toggle-row">
									<input
										type="checkbox"
										checked={aria2NextCheckCertificate}
										onChange={(e) => {
											setAria2NextCheckCertificate(e.target.checked);
											pushSettings({
												aria2NextCheckCertificate: e.target.checked,
											});
										}}
									/>
									<span>Validate SSL Certificates</span>
								</label>
								<label className="checkbox-row custom-toggle-row">
									<input
										type="checkbox"
										checked={aria2NextAlwaysResume}
										onChange={(e) => {
											setAria2NextAlwaysResume(e.target.checked);
											pushSettings({ aria2NextAlwaysResume: e.target.checked });
										}}
									/>
									<span>Always Resume Downloads</span>
								</label>
							</div>
						</div>
					)}
				</div>
			</section>
		</div>
	);
}

function TorrentSettings({ downloader }: { downloader: UseDownloaderReturn }) {
	const {
		torrentEnabled,
		setTorrentEnabled,
		torrentMaxActive,
		setTorrentMaxActive,
		torrentDownloadLimit,
		setTorrentDownloadLimit,
		torrentUploadLimit,
		setTorrentUploadLimit,
		torrentSeedRatio,
		setTorrentSeedRatio,
		torrentPeerLimit,
		setTorrentPeerLimit,
		torrentUploadPeerLimit,
		setTorrentUploadPeerLimit,
		pushSettings,
	} = downloader;
	const field = (
		id: string,
		label: string,
		value: number,
		setValue: (value: number) => void,
		min = 0,
		max = 10000,
	) => (
		<div className="form-field">
			<label htmlFor={id}>{label}</label>
			<input
				id={id}
				className="form-input"
				type="number"
				min={min}
				max={max}
				value={value}
				onChange={(event) => {
					const next = Number(event.target.value);
					setValue(next);
					pushSettings({ [id]: next } as Partial<DownloaderSettings>);
				}}
			/>
		</div>
	);
	return (
		<div className="engines-layout">
			<section className="settings-card">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<Cpu size={18} />
						<span>libtorrent Engine</span>
					</h2>
				</div>
				<div className="settings-group">
					<label className="checkbox-row custom-toggle-row">
						<input
							type="checkbox"
							checked={torrentEnabled}
							onChange={(event) => {
								setTorrentEnabled(event.target.checked);
								pushSettings({ torrentEnabled: event.target.checked });
							}}
						/>
						<span>Enable torrent downloads</span>
					</label>
					<div className="sliders-grid">
						{field(
							"torrentMaxActive",
							"Max Active Torrents",
							torrentMaxActive,
							setTorrentMaxActive,
							1,
							32,
						)}
						{field(
							"torrentPeerLimit",
							"Peer Connection Limit",
							torrentPeerLimit,
							setTorrentPeerLimit,
							1,
							2000,
						)}
						{field(
							"torrentUploadPeerLimit",
							"Upload Peer Limit",
							torrentUploadPeerLimit,
							setTorrentUploadPeerLimit,
							1,
							500,
						)}
					</div>
					<div className="sliders-grid">
						{field(
							"torrentDownloadLimit",
							"Download Limit (KiB/s, 0 = unlimited)",
							torrentDownloadLimit,
							setTorrentDownloadLimit,
						)}
						{field(
							"torrentUploadLimit",
							"Upload Limit (KiB/s, 0 = unlimited)",
							torrentUploadLimit,
							setTorrentUploadLimit,
						)}
					</div>
					<div className="sliders-grid">
						{field(
							"torrentSeedRatio",
							"Seed Ratio (0 = disabled)",
							torrentSeedRatio,
							setTorrentSeedRatio,
							0,
							100,
						)}
					</div>
				</div>
			</section>
		</div>
	);
}

function IntegrationsSettings({
	downloader,
}: {
	downloader: UseDownloaderReturn;
}) {
	const { handleInstallExtensionClick } = downloader;

	return (
		<div className="integrations-layout">
			<section className="settings-card">
				<div className="section-header-row">
					<h2 className="section-title-large">
						<Puzzle size={18} />
						<span>Browser Extension Setup</span>
					</h2>
				</div>
				<div className="settings-group">
					<p className="section-subtitle">
						Intercept standard browser downloads and sniff media streams
						automatically by installing the DownloadAnything browser extension.
					</p>

					<div className="browser-instructions">
						<div className="step-item">
							<span className="step-badge">1</span>
							<p>
								Click the button below to initiate extension detection on your
								system.
							</p>
						</div>
						<div className="step-item">
							<span className="step-badge">2</span>
							<p>
								Select your active browser profile from the detected browsers
								list to automatically load the extension bundle.
							</p>
						</div>
					</div>

					<button
						className="action-btn extension-primary-btn"
						onClick={handleInstallExtensionClick}
					>
						<span>Detect and Install Extension</span>
						<Puzzle size={14} />
					</button>
				</div>
			</section>
		</div>
	);
}

export function SettingsPanel({ downloader }: SettingsPanelProps) {
	const { activeSettingsSection, setActiveSettingsSection } = downloader;

	return (
		<div className="unified-settings-container animate-fade-in-up">
			<aside className="settings-sub-sidebar">
				<button
					className={`settings-sub-btn ${activeSettingsSection === "general" ? "active" : ""}`}
					onClick={() => setActiveSettingsSection("general")}
				>
					<Settings size={14} />
					<span>General Preferences</span>
				</button>
				<button
					className={`settings-sub-btn ${activeSettingsSection === "categories" ? "active" : ""}`}
					onClick={() => setActiveSettingsSection("categories")}
				>
					<FolderOpen size={14} />
					<span>Preset Paths</span>
				</button>
				<button
					className={`settings-sub-btn ${activeSettingsSection === "engines" ? "active" : ""}`}
					onClick={() => setActiveSettingsSection("engines")}
				>
					<Cpu size={14} />
					<span>Downloader Engines</span>
				</button>
				<button
					className={`settings-sub-btn ${activeSettingsSection === "torrent" ? "active" : ""}`}
					onClick={() => setActiveSettingsSection("torrent")}
				>
					<Cpu size={14} />
					<span>Torrent Engine</span>
				</button>
				<button
					className={`settings-sub-btn ${activeSettingsSection === "integrations" ? "active" : ""}`}
					onClick={() => setActiveSettingsSection("integrations")}
				>
					<Puzzle size={14} />
					<span>Browser Extensions</span>
				</button>
			</aside>

			<div className="settings-content-pane">
				{activeSettingsSection === "general" && (
					<GeneralSettings downloader={downloader} />
				)}
				{activeSettingsSection === "categories" && (
					<CategoriesSettings downloader={downloader} />
				)}
				{activeSettingsSection === "engines" && (
					<EnginesSettings downloader={downloader} />
				)}
				{activeSettingsSection === "torrent" && (
					<TorrentSettings downloader={downloader} />
				)}
				{activeSettingsSection === "integrations" && (
					<IntegrationsSettings downloader={downloader} />
				)}
			</div>
		</div>
	);
}
