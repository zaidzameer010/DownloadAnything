import { Card, Chip, Tabs, Typography } from "@heroui/react";
import { Activity, Download, Settings2, Wifi, WifiOff } from "lucide-react";
import { useState } from "react";
import DownloadsPage from "./components/DownloadsPage";
import SettingsPage from "./components/SettingsPage";
import UpdateAlert from "./components/UpdateAlert";
import { useBackend } from "./hooks/useBackend";
import { useUpdater } from "./hooks/useUpdater";
import { BACKEND_URL } from "./lib/backend";

type Page = "downloads" | "settings";

const navigation = [
	{ key: "downloads" as const, label: "Downloads", icon: Download },
	{ key: "settings" as const, label: "Settings", icon: Settings2 },
];

function Navigation({
	page,
	onPageChange,
	orientation,
}: {
	page: Page;
	onPageChange: (page: Page) => void;
	orientation: "horizontal" | "vertical";
}) {
	return (
		<Tabs
			className={orientation === "vertical" ? "w-full" : "w-full"}
			orientation={orientation}
			selectedKey={page}
			onSelectionChange={(key) => onPageChange(String(key) as Page)}
		>
			<Tabs.ListContainer className="bg-transparent p-0">
				<Tabs.List
					aria-label="Main navigation"
					className={
						orientation === "vertical"
							? "w-full flex-col items-stretch gap-1"
							: "w-full gap-1 overflow-x-auto"
					}
				>
					{navigation.map(({ key, label, icon: Icon }) => (
						<Tabs.Tab
							className="justify-start gap-3 rounded-xl px-3 py-2.5 text-sm font-medium"
							id={key}
							key={key}
						>
							<Icon aria-hidden className="size-4 shrink-0" />
							{label}
							<Tabs.Indicator />
						</Tabs.Tab>
					))}
				</Tabs.List>
			</Tabs.ListContainer>
		</Tabs>
	);
}

function App() {
	const backend = useBackend();
	const updater = useUpdater();
	const [page, setPage] = useState<Page>("downloads");

	const activeJobs = backend.jobs.filter(
		(job) =>
			!job.parentId &&
			["queued", "downloading", "postprocessing"].includes(job.status),
	).length;

	return (
		<div className="dark h-dvh w-full overflow-hidden bg-background text-foreground">
			<a
				className="sr-only z-50 rounded-md bg-accent px-4 py-2 text-accent-foreground focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
				href="#main-content"
			>
				Skip to main content
			</a>

			<div className="flex h-full w-full flex-col md:flex-row overflow-hidden">
				<aside className="hidden w-72 shrink-0 flex-col border-e border-separator bg-surface-secondary px-5 py-6 md:flex h-full overflow-y-auto">
					<div className="flex items-start gap-3">
						<div className="flex size-10 items-center justify-center rounded-2xl bg-accent text-accent-foreground shadow-sm">
							<Download aria-hidden className="size-5" />
						</div>
						<div className="min-w-0">
							<Typography
								className="truncate font-semibold tracking-tight"
								type="h5"
							>
								DownloadAnything
							</Typography>
						</div>
					</div>

					<div className="mt-12">
						<Typography
							className="mb-3 px-2 text-[11px] font-semibold tracking-[0.16em] uppercase"
							color="muted"
							type="body-xs"
						>
							Workspace
						</Typography>
						<Navigation
							onPageChange={setPage}
							orientation="vertical"
							page={page}
						/>
					</div>

					<Card
						className="mt-auto gap-4 border border-separator bg-surface p-4 shadow-none"
						variant="transparent"
					>
						<div className="flex items-center justify-between gap-3">
							<div className="flex items-center gap-2">
								{backend.connected ? (
									<Wifi aria-hidden className="size-4 text-success" />
								) : (
									<WifiOff aria-hidden className="size-4 text-muted" />
								)}
								<Typography className="font-medium" type="body-sm">
									{backend.connected ? "Backend online" : "Backend offline"}
								</Typography>
							</div>
							<Chip
								className="tabular-nums"
								color={backend.connected ? "success" : "default"}
								size="sm"
								variant="soft"
							>
								{activeJobs} active
							</Chip>
						</div>
						<Typography
							className="truncate font-mono text-[11px]"
							color="muted"
							type="body-xs"
						>
							{BACKEND_URL}
						</Typography>
						<div className="flex items-center gap-2 text-muted">
							<Activity aria-hidden className="size-3.5" />
							<Typography className="tabular-nums" color="muted" type="body-xs">
								{backend.jobs.length} total jobs
							</Typography>
						</div>
					</Card>
				</aside>

				<div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
					<UpdateAlert {...updater} />
					<header className="border-b border-separator bg-background/95 px-4 py-4 backdrop-blur md:hidden">
						<div className="mb-4 flex items-center justify-between gap-3">
							<div className="flex min-w-0 items-center gap-3">
								<div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent text-accent-foreground">
									<Download aria-hidden className="size-4" />
								</div>
								<div className="min-w-0">
									<Typography className="truncate font-semibold" type="h6">
										DownloadAnything
									</Typography>
									<Typography color="muted" type="body-xs">
										{backend.connected ? "Backend online" : "Backend offline"}
									</Typography>
								</div>
							</div>
							<Chip
								className="tabular-nums"
								color={backend.connected ? "success" : "default"}
								size="sm"
								variant="soft"
							>
								{activeJobs} active
							</Chip>
						</div>
						<Navigation
							onPageChange={setPage}
							orientation="horizontal"
							page={page}
						/>
					</header>

					<main
						className="min-w-0 flex-1 overflow-y-auto bg-background px-4 py-6 sm:px-6 lg:px-10 lg:py-10"
						id="main-content"
					>
						<div className="mx-auto w-full max-w-360">
							{page === "downloads" ? (
								<DownloadsPage backend={backend} />
							) : (
								<SettingsPage backend={backend} />
							)}
						</div>
					</main>
				</div>
			</div>
		</div>
	);
}

export default App;
