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
	Tabs,
	TextField,
	Typography,
} from "@heroui/react";
import type { LucideIcon } from "lucide-react";
import {
	Download,
	Folder,
	Globe,
	HardDrive,
	Info,
	Network,
	PenLine,
	Plus,
	Settings2,
	Trash2,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import type { useBackend } from "../hooks/useBackend";
import type { Settings } from "../lib/backend";
import { formatBytes } from "../lib/format";
import AboutSection from "./AboutSection";

type Backend = ReturnType<typeof useBackend>;
type Section =
	| "general"
	| "network"
	| "engines"
	| "postprocessing"
	| "locations"
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
		description: "Limits and access",
		icon: Network,
	},
	{
		id: "engines",
		label: "Engines",
		description: "HTTP and torrent",
		icon: Download,
	},
	{
		id: "postprocessing",
		label: "Post-processing",
		description: "Metadata and output",
		icon: PenLine,
	},
	{
		id: "locations",
		label: "Locations",
		description: "Folders and presets",
		icon: Folder,
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
	children,
}: {
	title: string;
	description: string;
	children: ReactNode;
}) {
	return (
		<Card
			className="gap-6 border border-separator bg-surface p-5 shadow-none sm:p-7"
			variant="default"
		>
			<div className="space-y-1">
				<Typography className="font-semibold" type="h5">
					{title}
				</Typography>
				<Typography color="muted" type="body-sm">
					{description}
				</Typography>
			</div>
			<div className="space-y-6">{children}</div>
		</Card>
	);
}

function EngineCard({
	title,
	description,
	icon: Icon,
	children,
}: {
	title: string;
	description: string;
	icon: LucideIcon;
	children: ReactNode;
}) {
	return (
		<Card
			className="gap-5 border border-separator bg-surface-secondary p-5 shadow-none"
			variant="transparent"
		>
			<div className="flex items-start gap-3">
				<div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
					<Icon aria-hidden className="size-4" />
				</div>
				<div>
					<Typography className="font-semibold" type="h6">
						{title}
					</Typography>
					<Typography className="mt-1" color="muted" type="body-sm">
						{description}
					</Typography>
				</div>
			</div>
			<div className="space-y-5">{children}</div>
		</Card>
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
		<Tabs
			className="w-full"
			orientation="vertical"
			selectedKey={section}
			onSelectionChange={(key) => onChange(String(key) as Section)}
		>
			<Tabs.ListContainer className="bg-transparent p-0">
				<Tabs.List
					aria-label="Settings sections"
					className="w-full flex-col items-stretch gap-1"
				>
					{sections.map(({ id, label, description, icon: Icon }) => (
						<Tabs.Tab
							className="h-auto justify-start gap-3 rounded-xl px-3 py-3 text-start"
							id={id}
							key={id}
						>
							<Icon aria-hidden className="mt-0.5 size-4 shrink-0" />
							<span className="min-w-0">
								<span className="block text-sm font-medium">{label}</span>
								<span className="mt-0.5 block text-xs text-muted">
									{description}
								</span>
							</span>
							<Tabs.Indicator />
						</Tabs.Tab>
					))}
				</Tabs.List>
			</Tabs.ListContainer>
		</Tabs>
	);
}

export default function SettingsPage({ backend }: { backend: Backend }) {
	const { settings, updateSettings, connected } = backend;
	const [section, setSection] = useState<Section>("general");
	const [newPresetName, setNewPresetName] = useState("");
	const [newPresetPath, setNewPresetPath] = useState("");
	const [settingsError, setSettingsError] = useState("");

	if (!settings) {
		return (
			<Card
				className="border border-separator bg-surface p-6 shadow-none"
				variant="default"
			>
				{connected ? (
					<div className="space-y-4">
						<Skeleton className="h-6 w-40 rounded-lg" />
						<Skeleton className="h-12 w-full rounded-xl" />
						<Skeleton className="h-12 w-full rounded-xl" />
					</div>
				) : (
					<Alert status="danger">
						<Alert.Indicator />
						<Alert.Content>
							<Alert.Title>Backend unavailable</Alert.Title>
							<Alert.Description>
								Start backend/server.py to load and edit settings.
							</Alert.Description>
						</Alert.Content>
					</Alert>
				)}
			</Card>
		);
	}

	const set = async (partial: Partial<Settings>, debounceMs = 350) => {
		const result = await updateSettings(partial, debounceMs);
		if (!result.ok) {
			setSettingsError(
				result.error ||
					"Could not save settings. Check the backend connection.",
			);
		} else {
			setSettingsError("");
		}
	};
	const setImmediate = async (partial: Partial<Settings>) => {
		const result = await updateSettings(partial, 0);
		if (!result.ok) {
			setSettingsError(
				result.error ||
					"Could not save settings. Check the backend connection.",
			);
		} else {
			setSettingsError("");
		}
	};
	const addPreset = () => {
		const path = newPresetPath.trim();
		if (!path) return;
		setImmediate({
			presetPaths: [
				...settings.presetPaths,
				{ name: newPresetName.trim() || path, path },
			],
		});
		setNewPresetName("");
		setNewPresetPath("");
	};
	const removePreset = (path: string) =>
		setImmediate({
			presetPaths: settings.presetPaths.filter(
				(preset) => preset.path !== path,
			),
		});

	return (
		<div className="space-y-8">
			<header className="space-y-2">
				<Typography color="muted" type="body-xs">
					WORKSPACE / SETTINGS
				</Typography>
				<div className="flex items-center gap-3">
					<Typography className="font-semibold tracking-tight" type="h2">
						Settings
					</Typography>
					<Chip color="success" size="sm" variant="soft">
						Auto-sync enabled
					</Chip>
				</div>
				<Typography className="max-w-2xl" color="muted" type="body-sm">
					Tune the engines and destinations behind your download queue. Changes
					sync to the backend as you make them.
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
					{section === "general" ? (
						<SectionCard
							description="Set the baseline behavior for the shared queue."
							title="General"
						>
							<SliderSetting
								display={String(settings.maxConcurrentDownloads)}
								label="Concurrent downloads (next restart)"
								maxValue={8}
								minValue={1}
								onChange={(value) => set({ maxConcurrentDownloads: value })}
								value={settings.maxConcurrentDownloads}
							/>
						</SectionCard>
					) : null}

					{section === "network" ? (
						<SectionCard
							description="Control throughput and browser session access."
							title="Network"
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
								placeholder="http://127.0.0.1:8080 (empty = direct)"
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

					{section === "engines" ? (
						<SectionCard
							description="Configure the direct HTTP engine and libtorrent runtime."
							title="Download engines"
						>
							{settingsError ? (
								<Alert className="mb-5" status="danger">
									<Alert.Indicator />
									<Alert.Content>
										<Alert.Title>Settings could not be saved</Alert.Title>
										<Alert.Description>{settingsError}</Alert.Description>
									</Alert.Content>
								</Alert>
							) : null}
							<div className="grid gap-5 xl:grid-cols-2">
								<EngineCard
									description="HTTP, HLS, and DASH fragment fetching."
									icon={Globe}
									title="aria2-next"
								>
									<SliderSetting
										display={String(settings.aria2NextConnections)}
										label="Connections per server"
										maxValue={32}
										minValue={1}
										onChange={(value) => set({ aria2NextConnections: value })}
										value={settings.aria2NextConnections}
									/>
									<SliderSetting
										display={String(settings.aria2NextMaxConcurrent)}
										label="Concurrent downloads"
										maxValue={32}
										minValue={1}
										onChange={(value) => set({ aria2NextMaxConcurrent: value })}
										value={settings.aria2NextMaxConcurrent}
									/>
									<TextSetting
										label="Minimum split size"
										onCommit={(value) =>
											setImmediate({ aria2NextMinSplitSize: value })
										}
										placeholder="1M"
										value={settings.aria2NextMinSplitSize}
									/>
									<SelectSetting
										label="File allocation"
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
													This mode tries to reserve the full file size before
													downloading and can fail with &apos;No space left on
													device&apos; if the destination volume does not have
													enough free space. Set File allocation to
													&quot;none&quot; to avoid the pre-allocation step (the
													final file must still fit).
												</Alert.Description>
											</Alert.Content>
										</Alert>
									) : null}
									<TextSetting
										label="Extra aria2-next arguments"
										onCommit={(value) =>
											setImmediate({ aria2NextExtraArgs: value })
										}
										placeholder="--max-tries=3 --timeout=30"
										value={settings.aria2NextExtraArgs}
									/>
								</EngineCard>
								<EngineCard
									description="Magnet links and torrent jobs in the shared queue."
									icon={HardDrive}
									title="libtorrent"
								>
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
										label="Max connections"
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
									<TextSetting
										label="Upload limit"
										onCommit={(value) =>
											setImmediate({
												torrentUploadLimit: Math.max(0, Number(value) || 0),
											})
										}
										placeholder="0 = unlimited bytes/sec"
										value={String(settings.torrentUploadLimit)}
									/>
									<div className="space-y-2">
										<ToggleSetting
											checked={settings.torrentEnableDht}
											label="Enable DHT"
											onChange={(value) =>
												setImmediate({ torrentEnableDht: value })
											}
										/>
										<ToggleSetting
											checked={settings.torrentEnablePex}
											label="Enable peer exchange"
											onChange={(value) =>
												setImmediate({ torrentEnablePex: value })
											}
										/>
										<ToggleSetting
											checked={settings.torrentEnableLsd}
											label="Enable local peer discovery"
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
								</EngineCard>
							</div>
						</SectionCard>
					) : null}

					{section === "postprocessing" ? (
						<SectionCard
							description="Choose which metadata is embedded and how media is merged."
							title="Post-processing"
						>
							<div className="space-y-2">
								<ToggleSetting
									checked={settings.addMetadata}
									label="Embed metadata"
									onChange={(value) => setImmediate({ addMetadata: value })}
								/>
								<ToggleSetting
									checked={settings.writeThumbnail}
									label="Embed thumbnail"
									onChange={(value) => setImmediate({ writeThumbnail: value })}
								/>
								<ToggleSetting
									checked={settings.writeSubs}
									label="Embed all subtitles"
									onChange={(value) => setImmediate({ writeSubs: value })}
								/>
							</div>
							<SelectSetting
								label="Merge output format"
								onChange={(value) => setImmediate({ mergeOutputFormat: value })}
								options={["mp4", "mkv", "webm", "mov"].map((value) => ({
									label: value,
									value,
								}))}
								value={settings.mergeOutputFormat}
							/>
						</SectionCard>
					) : null}

					{section === "about" ? (
						<SectionCard
							description="Version, update source, and manual update controls."
							title="About"
						>
							<AboutSection />
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
