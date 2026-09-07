import {
	Alert,
	Button,
	Card,
	Chip,
	Input,
	Label,
	ListBox,
	Select,
	Skeleton,
	Slider,
	Switch,
	TextField,
	Typography,
} from "@heroui/react";
import type { LucideIcon } from "lucide-react";
import {
	Folder,
	Globe,
	HardDrive,
	Info,
	Network,
	PenLine,
	Plus,
	Puzzle,
	Settings2,
	Trash2,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import type { useBackend } from "../hooks/useBackend";
import { useUpdater } from "../hooks/useUpdater";
import { backend, type Settings } from "../lib/backend";
import { formatBytes } from "../lib/format";
import AboutSection from "./AboutSection";

type Backend = ReturnType<typeof useBackend>;


type Section =
	| "general"
	| "network"
	| "media"
	| "aria2"
	| "torrent"
	| "locations"
	| "extension"
	| "about";

type Option = { value: string; label: string };

const sections: {
	id: Section;
	label: string;
	description: string;
	icon: LucideIcon;
}[] = [
	{
		id: "general",
		label: "General",
		description: "Queue behavior",
		icon: Settings2,
	},
	{
		id: "network",
		label: "Network",
		description: "Limits & access",
		icon: Network,
	},
	{
		id: "media",
		label: "Media & Subtitles",
		description: "Formats & metadata",
		icon: PenLine,
	},
	{
		id: "aria2",
		label: "aria2-next",
		description: "Direct HTTP engine",
		icon: Globe,
	},
	{
		id: "torrent",
		label: "libtorrent",
		description: "BitTorrent P2P engine",
		icon: HardDrive,
	},
	{
		id: "locations",
		label: "Locations",
		description: "Folders and presets",
		icon: Folder,
	},
	{
		id: "extension",
		label: "Extension",
		description: "Browser integration",
		icon: Puzzle,
	},
	{
		id: "about",
		label: "About",
		description: "Version and updates",
		icon: Info,
	},
];

function TextSetting({
	label,
	value,
	placeholder,
	onCommit,
	mono = true,
}: {
	label: string;
	value: string;
	placeholder?: string;
	onCommit: (value: string) => void;
	mono?: boolean;
}) {
	return (
		<TextField
			className="w-full gap-1.5"
			fullWidth
			value={value}
			onChange={onCommit}
		>
			<Label>{label}</Label>
			<Input
				className={mono ? "font-mono text-sm" : "text-sm"}
				placeholder={placeholder}
			/>
		</TextField>
	);
}

type InstalledBrowser = {
	id: string;
	name: string;
	executable: string;
};

function ExtensionSection() {
	const [browsers, setBrowsers] = useState<InstalledBrowser[]>([]);
	const [extensionPath, setExtensionPath] = useState("");
	const [busyId, setBusyId] = useState<string | null>(null);
	const [status, setStatus] = useState<{
		id: string;
		ok: boolean;
		message: string;
	} | null>(null);

	const load = useCallback(async () => {
		const res = await backend.listBrowsers();
		if (res.browsers) setBrowsers(res.browsers);
		if (res.extensionPath) setExtensionPath(res.extensionPath);
	}, []);

	useEffect(() => {
		void load();
	}, [load]);

	const install = async (browserId: string) => {
		setBusyId(browserId);
		setStatus(null);
		const res = await backend.installExtension(browserId);
		setStatus({
			id: browserId,
			ok: Boolean(res.ok),
			message: res.ok
				? (res.message ?? "Launched with the extension.")
				: (res.error ?? "Install failed"),
		});
		setBusyId(null);
		void load();
	};

	return (
		<SectionCard
			description="Load the DownloadAnything extension into an installed Chromium browser. A running browser is gracefully quit and relaunched with the extension."
			title="Browser Extension"
		>
			<div className="space-y-2">
				{browsers.length ? (
					browsers.map((browser) => (
						<div
							className="flex items-center justify-between gap-4 rounded-xl border border-separator bg-surface-secondary px-4 py-3"
							key={browser.id}
						>
							<div className="min-w-0">
								<Typography className="font-medium" type="body-sm">
									{browser.name}
								</Typography>
								<Typography
									className="mt-1 truncate font-mono text-xs"
									color="muted"
									type="body-xs"
								>
									{browser.executable}
								</Typography>
							</div>
							<Button
								isDisabled={busyId !== null}
								onPress={() => install(browser.id)}
								size="sm"
							>
								{busyId === browser.id ? "Installing…" : "Install"}
							</Button>
						</div>
					))
				) : (
					<Typography color="muted" type="body-sm">
						No supported Chromium browsers found on this system.
					</Typography>
				)}
			</div>
			{status ? (
				<Alert status={status.ok ? "success" : "danger"}>
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>
							{status.ok ? "Install started" : "Install failed"}
						</Alert.Title>
						<Alert.Description>{status.message}</Alert.Description>
					</Alert.Content>
				</Alert>
			) : null}
			{extensionPath ? (
				<Typography className="font-mono" color="muted" type="body-xs">
					Extension source: {extensionPath}
				</Typography>
			) : null}
			<Typography color="muted" type="body-xs">
				The extension loads for this browser session only. To keep it, enable
				Developer mode on chrome://extensions and use &quot;Load unpacked&quot;
				pointing at the extension folder above.
			</Typography>
		</SectionCard>
	);
}

function SelectSetting({
	label,
	value,
	options,
	onChange,
}: {
	label: string;
	value: string;
	options: Option[];
	onChange: (value: string) => void;
}) {
	return (
		<Select
			className="w-full"
			value={value}
			onChange={(next) => onChange(String(next))}
		>
			<Label>{label}</Label>
			<Select.Trigger>
				<Select.Value />
				<Select.Indicator />
			</Select.Trigger>
			<Select.Popover>
				<ListBox>
					{options.map((option) => (
						<ListBox.Item
							id={option.value}
							key={option.value}
							textValue={option.label}
						>
							{option.label}
							<ListBox.ItemIndicator />
						</ListBox.Item>
					))}
				</ListBox>
			</Select.Popover>
		</Select>
	);
}

function SliderSetting({
	label,
	value,
	minValue,
	maxValue,
	step = 1,
	display,
	onChange,
}: {
	label: string;
	value: number;
	minValue: number;
	maxValue: number;
	step?: number;
	display: string;
	onChange: (value: number) => void;
}) {
	return (
		<Slider
			className="w-full"
			maxValue={maxValue}
			minValue={minValue}
			step={step}
			value={value}
			onChange={(next) => onChange(typeof next === "number" ? next : next[0])}
		>
			<div className="flex items-center justify-between gap-3">
				<Label>{label}</Label>
				<Slider.Output className="tabular-nums text-xs text-muted">
					{display}
				</Slider.Output>
			</div>
			<Slider.Track>
				<Slider.Fill />
				<Slider.Thumb />
			</Slider.Track>
		</Slider>
	);
}

function ToggleSetting({
	label,
	checked,
	onChange,
}: {
	label: string;
	checked: boolean;
	onChange: (value: boolean) => void;
}) {
	return (
		<Switch
			className="w-full cursor-pointer"
			isSelected={checked}
			onChange={onChange}
		>
			<Switch.Content className="flex w-full items-center justify-between gap-4 rounded-xl border border-separator bg-surface-secondary px-4 py-3 transition-colors hover:bg-surface-secondary-hover">
				<Label className="cursor-pointer text-sm font-medium">{label}</Label>
				<Switch.Control>
					<Switch.Thumb />
				</Switch.Control>
			</Switch.Content>
		</Switch>
	);
}

function SectionCard({
	title,
	description,
	icon: Icon,
	children,
}: {
	title: string;
	description: string;
	icon?: LucideIcon;
	children: ReactNode;
}) {
	return (
		<div className="space-y-4 rounded-2xl border border-separator bg-surface-secondary/60 p-4 sm:p-5">
			{Icon && (
				<div className="flex items-start gap-3">
					<div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-surface-tertiary text-foreground">
						<Icon className="size-5" />
					</div>
					<div>
						<Typography className="font-medium" type="body-sm">
							{title}
						</Typography>
						<Typography color="muted" type="body-xs">
							{description}
						</Typography>
					</div>
				</div>
			)}
			<div className="space-y-4">{children}</div>
		</div>
	);
}

function SettingsNavigation({
	section,
	onChange,
}: {
	section: Section;
	onChange: (section: Section) => void;
}) {
	return (
		<div className="grid gap-1">
			{sections.map((item) => {
				const Icon = item.icon;
				const active = section === item.id;
				return (
					<button
						className={`flex w-full items-center gap-3 rounded-xl px-3.5 py-2.5 text-left text-sm transition-colors ${
							active
								? "bg-surface-tertiary font-medium text-foreground"
								: "text-muted hover:bg-surface-secondary-hover hover:text-foreground"
						}`}
						key={item.id}
						onClick={() => onChange(item.id)}
						type="button"
					>
						<Icon
							aria-hidden
							className={`size-4 shrink-0 ${active ? "text-accent font-semibold" : ""}`}
						/>
						<span className="truncate">{item.label}</span>
					</button>
				);
			})}
		</div>
	);
}

export default function SettingsPage({ backend }: { backend: Backend }) {
	const updater = useUpdater();
	const { settings, updateSettings } = backend;
	const [section, setSection] = useState<Section>("general");
	const [newPresetName, setNewPresetName] = useState("");
	const [newPresetPath, setNewPresetPath] = useState("");
	const [localError, setLocalError] = useState<string | null>(null);

	if (!settings) {
		return (
			<div className="p-6">
				<Skeleton className="h-64 rounded-2xl" />
			</div>
		);
	}

	const set = async (patch: Partial<Settings>, debounceMs = 350) => {
		const res = await updateSettings(patch, debounceMs);
		if (res?.error) {
			setLocalError(res.error);
		} else {
			setLocalError(null);
		}
	};

	const setImmediate = async (patch: Partial<Settings>) => {
		const res = await updateSettings(patch, 0);
		if (res?.error) {
			setLocalError(res.error);
		} else {
			setLocalError(null);
		}
	};

	const addPreset = () => {
		if (!newPresetName.trim() || !newPresetPath.trim()) return;
		const next = [
			...settings.presetPaths.filter((p) => p.path !== newPresetPath.trim()),
			{ name: newPresetName.trim(), path: newPresetPath.trim() },
		];
		setImmediate({ presetPaths: next });
		setNewPresetName("");
		setNewPresetPath("");
	};

	const removePreset = (path: string) => {
		setImmediate({
			presetPaths: settings.presetPaths.filter((p) => p.path !== path),
		});
	};

	const settingsError = localError;

	return (
		<div className="space-y-6">
			<header className="space-y-1">
				<div className="flex items-center gap-3">
					<Typography className="font-bold tracking-tight" type="h3">
						Settings
					</Typography>
					<Chip color="success" size="sm" variant="soft">
						Auto-sync enabled
					</Chip>
				</div>
				<Typography className="max-w-2xl" color="muted" type="body-sm">
					Tune queue behavior, network speed, engine configurations, and
					download paths.
				</Typography>
			</header>
			<div className="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)] lg:items-start">
				<Card
					className="border border-separator bg-surface-secondary p-2 shadow-none lg:sticky lg:top-6"
					variant="transparent"
				>
					<SettingsNavigation onChange={setSection} section={section} />
				</Card>
				<div className="min-w-0">
					{settingsError ? (
						<Alert className="mb-5" status="danger">
							<Alert.Indicator />
							<Alert.Content>
								<Alert.Title>Settings could not be saved</Alert.Title>
								<Alert.Description>{settingsError}</Alert.Description>
							</Alert.Content>
						</Alert>
					) : null}

					{section === "general" ? (
						<SectionCard
							description="Set baseline queue limits and default destination directory."
							title="General"
						>
							<SliderSetting
								display={String(settings.maxConcurrentDownloads)}
								label="Concurrent downloads (shared limit for all engines)"
								maxValue={8}
								minValue={1}
								onChange={(value) => set({ maxConcurrentDownloads: value })}
								value={settings.maxConcurrentDownloads}
							/>
							<TextSetting
								label="Default download directory"
								onCommit={(value) => set({ downloadDir: value })}
								value={settings.downloadDir}
							/>
						</SectionCard>
					) : null}

					{section === "network" ? (
						<SectionCard
							description="Control network throughput, proxies, and browser cookies."
							title="Network & Connections"
						>
							<div className="grid gap-6 md:grid-cols-3">
								<SliderSetting
									display={
										settings.rateLimit > 0
											? `${formatBytes(settings.rateLimit)}/s`
											: "Unlimited"
									}
									label="Rate limit"
									maxValue={50 * 1024 * 1024}
									minValue={0}
									onChange={(value) => set({ rateLimit: value })}
									step={512 * 1024}
									value={settings.rateLimit}
								/>
								<SliderSetting
									display={String(settings.concurrentFragments)}
									label="Concurrent fragments"
									maxValue={16}
									minValue={1}
									onChange={(value) => set({ concurrentFragments: value })}
									value={settings.concurrentFragments}
								/>
								<SliderSetting
									display={String(settings.retries)}
									label="Retries"
									maxValue={20}
									minValue={0}
									onChange={(value) => set({ retries: value })}
									value={settings.retries}
								/>
							</div>
							<TextSetting
								label="Proxy"
								onCommit={(value) => set({ proxy: value })}
								placeholder="http://127.0.0.1:8080 or socks5://127.0.0.1:1080"
								value={settings.proxy}
							/>
							<SelectSetting
								label="Cookies from browser"
								onChange={(value) =>
									setImmediate({
										cookiesFromBrowser: value === "__disabled" ? "" : value,
									})
								}
								options={[
									{ label: "Disabled", value: "__disabled" },
									{ label: "Chrome", value: "chrome" },
									{ label: "Firefox", value: "firefox" },
									{ label: "Safari", value: "safari" },
									{ label: "Edge", value: "edge" },
									{ label: "Brave", value: "brave" },
								]}
								value={settings.cookiesFromBrowser || "__disabled"}
							/>
						</SectionCard>
					) : null}

					{section === "media" ? (
						<SectionCard
							description="Configure media container formats, metadata embedding, and subtitles."
							title="Media & Subtitles"
						>
							<SelectSetting
								label="Merge output format"
								onChange={(value) => setImmediate({ mergeOutputFormat: value })}
								options={["mkv", "mp4", "webm", "mov"].map((value) => ({
									label: value.toUpperCase(),
									value,
								}))}
								value={settings.mergeOutputFormat}
							/>
							<div className="space-y-2">
								<ToggleSetting
									checked={settings.multiResolutionExtractor}
									label="Extract multiple resolutions from direct media"
									onChange={(value) =>
										setImmediate({ multiResolutionExtractor: value })
									}
								/>
								<ToggleSetting
									checked={settings.addMetadata}
									label="Embed video metadata (title, uploader, date)"
									onChange={(value) => setImmediate({ addMetadata: value })}
								/>
								<ToggleSetting
									checked={settings.writeThumbnail}
									label="Embed thumbnail artwork as cover"
									onChange={(value) => setImmediate({ writeThumbnail: value })}
								/>
								<ToggleSetting
									checked={settings.writeSubs}
									label="Embed official creator subtitles"
									onChange={(value) => setImmediate({ writeSubs: value })}
								/>
								<ToggleSetting
									checked={settings.writeAutoSubs}
									label="Embed auto-generated AI subtitles"
									onChange={(value) => setImmediate({ writeAutoSubs: value })}
								/>
							</div>
							<TextSetting
								label="Subtitle languages"
								onCommit={(value) => setImmediate({ subLangs: value })}
								placeholder="en,es,ja,all"
								value={settings.subLangs}
							/>
						</SectionCard>
					) : null}

					{section === "aria2" ? (
						<SectionCard
							description="Fine-tune the aria2-next multi-connection HTTP downloader."
							title="aria2-next Downloader"
						>
							<div className="grid gap-6 md:grid-cols-2">
								<SliderSetting
									display={String(settings.aria2NextConnections)}
									label="Connections per file"
									maxValue={64}
									minValue={1}
									onChange={(value) => set({ aria2NextConnections: value })}
									value={settings.aria2NextConnections}
								/>
								<SliderSetting
									display={`${settings.aria2NextTimeout}s`}
									label="Connection & socket timeout"
									maxValue={300}
									minValue={5}
									onChange={(value) => set({ aria2NextTimeout: value })}
									value={settings.aria2NextTimeout}
								/>
							</div>
							<TextSetting
								label="Piece length"
								onCommit={(value) =>
									setImmediate({ aria2NextMinSplitSize: value })
								}
								placeholder="1M"
								value={settings.aria2NextMinSplitSize}
							/>
							<SelectSetting
								label="File allocation mode"
								onChange={(value) =>
									setImmediate({ aria2NextFileAllocation: value })
								}
								options={["none", "trunc", "prealloc", "falloc"].map(
									(value) => ({ label: value, value }),
								)}
								value={settings.aria2NextFileAllocation}
							/>
							{settings.aria2NextFileAllocation !== "none" ? (
								<Alert status="warning">
									<Alert.Indicator />
									<Alert.Content>
										<Alert.Title>
											Upfront disk allocation is enabled
										</Alert.Title>
										<Alert.Description>
											Reserves full file size upfront. May fail if free disk
											space is insufficient. Set to &quot;none&quot; to allocate
											dynamically.
										</Alert.Description>
									</Alert.Content>
								</Alert>
							) : null}
							<TextSetting
								label="Per-download rate limit (bytes/sec, 0 = unlimited)"
								onCommit={(value) =>
									setImmediate({
										aria2NextMaxDownloadLimit: Math.max(0, Number(value) || 0),
									})
								}
								placeholder="0 = unlimited"
								value={String(settings.aria2NextMaxDownloadLimit)}
							/>
							<TextSetting
								label="Extra aria2-next arguments"
								onCommit={(value) =>
									setImmediate({ aria2NextExtraArgs: value })
								}
								placeholder="--max-tries=10 --retry-wait=2"
								value={settings.aria2NextExtraArgs}
							/>
						</SectionCard>
					) : null}

					{section === "torrent" ? (
						<SectionCard
							description="Configure BitTorrent session tuning, encryption, and P2P protocols."
							title="libtorrent Engine"
						>
							<div className="grid gap-6 md:grid-cols-3">
								<SliderSetting
									display={String(settings.torrentListenPort)}
									label="Listen port"
									maxValue={65535}
									minValue={1}
									onChange={(value) => set({ torrentListenPort: value })}
									value={settings.torrentListenPort}
								/>
								<SliderSetting
									display={String(settings.torrentMaxConnections)}
									label="Max peer connections"
									maxValue={1000}
									minValue={10}
									onChange={(value) => set({ torrentMaxConnections: value })}
									value={settings.torrentMaxConnections}
								/>
								<SliderSetting
									display={`${settings.torrentMetadataTimeout}s`}
									label="Metadata timeout"
									maxValue={300}
									minValue={15}
									onChange={(value) => set({ torrentMetadataTimeout: value })}
									value={settings.torrentMetadataTimeout}
								/>
							</div>
							<div className="grid gap-6 md:grid-cols-2">
								<TextSetting
									label="Download rate limit (bytes/sec)"
									onCommit={(value) =>
										setImmediate({
											torrentDownloadLimit: Math.max(0, Number(value) || 0),
										})
									}
									placeholder="0 = unlimited"
									value={String(settings.torrentDownloadLimit)}
								/>
								<TextSetting
									label="Upload rate limit (bytes/sec)"
									onCommit={(value) =>
										setImmediate({
											torrentUploadLimit: Math.max(0, Number(value) || 0),
										})
									}
									placeholder="0 = unlimited"
									value={String(settings.torrentUploadLimit)}
								/>
							</div>
							<div className="space-y-2">
								<ToggleSetting
									checked={settings.torrentSequentialDownload}
									label="Sequential downloading (stream video as it downloads)"
									onChange={(value) =>
										setImmediate({ torrentSequentialDownload: value })
									}
								/>
								<ToggleSetting
									checked={settings.torrentForceEncryption}
									label="Require BitTorrent protocol encryption (PE forced)"
									onChange={(value) =>
										setImmediate({ torrentForceEncryption: value })
									}
								/>
							</div>
							<div className="space-y-2">
								<Typography className="font-medium" type="body-sm">
									P2P Discovery Protocols
								</Typography>
								<div className="grid gap-2 sm:grid-cols-2">
									<ToggleSetting
										checked={settings.torrentEnableDht}
										label="Enable DHT"
										onChange={(value) =>
											setImmediate({ torrentEnableDht: value })
										}
									/>
									<ToggleSetting
										checked={settings.torrentEnablePex}
										label="Enable peer exchange (PEX)"
										onChange={(value) =>
											setImmediate({ torrentEnablePex: value })
										}
									/>
									<ToggleSetting
										checked={settings.torrentEnableLsd}
										label="Enable local peer discovery (LSD)"
										onChange={(value) =>
											setImmediate({ torrentEnableLsd: value })
										}
									/>
									<ToggleSetting
										checked={settings.torrentEnableUpnp}
										label="Enable UPnP"
										onChange={(value) =>
											setImmediate({ torrentEnableUpnp: value })
										}
									/>
									<ToggleSetting
										checked={settings.torrentEnableNatpmp}
										label="Enable NAT-PMP"
										onChange={(value) =>
											setImmediate({ torrentEnableNatpmp: value })
										}
									/>
								</div>
							</div>
						</SectionCard>
					) : null}

					{section === "extension" ? <ExtensionSection /> : null}

					{section === "about" ? (
						<SectionCard
							description="Version, update source, and manual update controls."
							title="About"
						>
							<AboutSection updater={updater} />
						</SectionCard>
					) : null}

					{section === "locations" ? (
						<SectionCard
							description="Choose where new files land and expose shortcuts to the browser extension."
							title="Download locations"
						>
							<TextSetting
								label="Default download directory"
								onCommit={(value) => set({ downloadDir: value })}
								value={settings.downloadDir}
							/>
							<div className="space-y-4">
								<div>
									<Typography className="font-medium" type="body-sm">
										Preset paths
									</Typography>
									<Typography className="mt-1" color="muted" type="body-xs">
										These destinations appear in the extension picker.
									</Typography>
								</div>
								<div className="space-y-2">
									{settings.presetPaths.length ? (
										settings.presetPaths.map((preset) => (
											<div
												className="flex items-center justify-between gap-4 rounded-xl border border-separator bg-surface-secondary px-4 py-3"
												key={preset.path}
											>
												<div className="min-w-0">
													<Typography className="font-medium" type="body-sm">
														{preset.name}
													</Typography>
													<Typography
														className="mt-1 truncate font-mono text-xs"
														color="muted"
														type="body-xs"
													>
														{preset.path}
													</Typography>
												</div>
												<Button
													aria-label={`Remove ${preset.name}`}
													isIconOnly
													size="sm"
													variant="tertiary"
													onPress={() => removePreset(preset.path)}
												>
													<Trash2 aria-hidden className="size-4 text-danger" />
												</Button>
											</div>
										))
									) : (
										<Typography color="muted" type="body-sm">
											No preset paths yet.
										</Typography>
									)}
								</div>
								<div className="grid gap-3 md:grid-cols-[1fr_1.4fr_auto] md:items-end">
									<TextField
										fullWidth
										value={newPresetName}
										onChange={setNewPresetName}
									>
										<Label>Name</Label>
										<Input placeholder="Movies" />
									</TextField>
									<TextField
										fullWidth
										value={newPresetPath}
										onChange={setNewPresetPath}
									>
										<Label>Absolute path</Label>
										<Input
											className="font-mono text-sm"
											placeholder="/Users/name/Downloads/Movies"
										/>
									</TextField>
									<Button
										className="min-h-10"
										isDisabled={!newPresetPath.trim()}
										onPress={addPreset}
									>
										<Plus aria-hidden className="size-4" />
										Add preset
									</Button>
								</div>
							</div>
						</SectionCard>
					) : null}
				</div>
			</div>
		</div>
	);
}
