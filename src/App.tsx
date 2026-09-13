import { Card, Tabs, Typography } from "@heroui/react";
import { Squircle, SquircleNoScript } from "@squircle-js/react";
import { Activity, Download, Settings2, Wifi, WifiOff } from "lucide-react";
import { useMemo, useState } from "react";
import DownloadsPage from "./components/DownloadsPage";
import SettingsPage from "./components/SettingsPage";
import SquircleChip from "./components/SquircleChip";
import { useBackend } from "./hooks/useBackend";
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
						<Squircle
							asChild
							className="ring-inset"
							cornerRadius={12}
							key={key}
						>
							<Tabs.Tab
								className="justify-start gap-3 px-3 py-2.5 text-sm font-medium transition-colors hover:text-foreground hover:bg-surface-secondary-hover active:bg-surface-secondary-active active:scale-[0.98] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
								id={key}
							>
								<Icon aria-hidden className="size-4 shrink-0" />
								{label}
								<Squircle asChild cornerRadius={12}>
									<Tabs.Indicator />
								</Squircle>
							</Tabs.Tab>
						</Squircle>
					))}
				</Tabs.List>
			</Tabs.ListContainer>
		</Tabs>
	);
}

function App() {
	const backend = useBackend();
	const [page, setPage] = useState<Page>("downloads");

	const activeJobs = useMemo(
		() =>
			backend.jobs.filter(
				(job) => job.status === "downloading" || job.status === "queued",
			).length,
		[backend.jobs],
	);

	return (
		<div className="dark h-dvh w-full overflow-hidden bg-background text-foreground">
			<SquircleNoScript />
			<Squircle asChild cornerRadius={6}>
				<a
					className="sr-only z-50 bg-accent px-4 py-2 text-accent-foreground focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
					href="#main-content"
				>
					Skip to main content
				</a>
			</Squircle>

			<div className="flex h-full w-full flex-col md:flex-row overflow-hidden">
				<aside className="hidden w-72 shrink-0 flex-col border-e border-separator bg-surface-secondary px-5 py-6 md:flex h-full overflow-y-auto">
					<div className="flex items-start gap-3">
						<Squircle
							className="flex size-10 items-center justify-center bg-accent text-accent-foreground shadow-sm"
							cornerRadius={16}
						>
							<Download aria-hidden className="size-5" />
						</Squircle>
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

					<Squircle asChild cornerRadius={24}>
						<Card
							className="mt-auto gap-4 border border-separator bg-surface-tertiary p-4 shadow-sm"
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
								<SquircleChip
									className="tabular-nums"
									color={backend.connected ? "success" : "default"}
									size="sm"
									variant="soft"
								>
									{activeJobs} active
								</SquircleChip>
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
								<Typography
									className="tabular-nums"
									color="muted"
									type="body-xs"
								>
									{backend.jobs.length} total jobs
								</Typography>
							</div>
						</Card>
					</Squircle>
				</aside>

				<div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
					<header className="border-b border-separator bg-background/95 px-4 py-4 backdrop-blur md:hidden">
						<div className="mb-4 flex items-center justify-between gap-3">
							<div className="flex min-w-0 items-center gap-3">
								<Squircle
									className="flex size-9 shrink-0 items-center justify-center bg-accent text-accent-foreground"
									cornerRadius={12}
								>
									<Download aria-hidden className="size-4" />
								</Squircle>
								<div className="min-w-0">
									<Typography className="truncate font-semibold" type="h6">
										DownloadAnything
									</Typography>
									<Typography color="muted" type="body-xs">
										{backend.connected ? "Backend online" : "Backend offline"}
									</Typography>
								</div>
							</div>
							<SquircleChip
								className="tabular-nums"
								color={backend.connected ? "success" : "default"}
								size="sm"
								variant="soft"
							>
								{activeJobs} active
							</SquircleChip>
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
