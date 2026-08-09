import { Alert, Button, Typography } from "@heroui/react";
import { Info, Loader2, RefreshCw } from "lucide-react";
import { useUpdater } from "../hooks/useUpdater";

export default function AboutSection() {
	const updater = useUpdater();

	const canCheck =
		updater.state.status !== "checking" &&
		updater.state.status !== "downloading" &&
		updater.state.status !== "installing";

	const progress =
		updater.state.status === "downloading" && updater.state.total
			? Math.min(100, (updater.state.downloaded / updater.state.total) * 100)
			: updater.state.status === "installing"
				? 100
				: 0;

	return (
		<div className="space-y-4">
			<div className="flex items-start gap-3 rounded-xl border border-separator bg-surface-secondary p-4">
				<div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
					<Info aria-hidden className="size-4" />
				</div>
				<div className="min-w-0 flex-1">
					<Typography className="font-semibold" type="h6">
						DownloadAnything
					</Typography>
					<Typography color="muted" type="body-sm">
						{updater.currentVersion
							? `Current version: ${updater.currentVersion}`
							: "Reading version…"}
					</Typography>
				</div>
				<Button
					className="h-8 gap-2"
					isDisabled={!canCheck}
					size="sm"
					onPress={() => updater.checkForUpdates()}
				>
					{updater.state.status === "checking" ? (
						<Loader2 aria-hidden className="size-3.5 animate-spin" />
					) : (
						<RefreshCw aria-hidden className="size-3.5" />
					)}
					{updater.state.status === "checking"
						? "Checking…"
						: "Check for updates"}
				</Button>
			</div>

			{updater.state.status === "available" ? (
				<Alert status="accent">
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>
							Update {updater.state.update.version} available
						</Alert.Title>
						<Alert.Description>
							{updater.state.update.body ? (
								<pre className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap font-sans text-sm text-muted">
									{updater.state.update.body}
								</pre>
							) : (
								<span className="text-muted">No release notes provided.</span>
							)}
							<Button
								className="mt-3 h-8"
								variant="primary"
								size="sm"
								onPress={() => updater.install()}
							>
								Install & restart
							</Button>
						</Alert.Description>
					</Alert.Content>
				</Alert>
			) : null}

			{updater.state.status === "error" ? (
				<Alert status="danger">
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>Update check failed</Alert.Title>
						<Alert.Description className="text-muted">
							{updater.state.message}
						</Alert.Description>
					</Alert.Content>
				</Alert>
			) : null}

			{updater.state.status === "downloading" ||
			updater.state.status === "installing" ? (
				<Alert status="accent">
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>
							{updater.state.status === "installing"
								? "Installing update…"
								: "Downloading update…"}
						</Alert.Title>
						<Alert.Description>
							<div className="mt-2 h-2 w-full max-w-md overflow-hidden rounded-full bg-surface-tertiary">
								<div
									className="h-full bg-accent transition-all"
									style={{ width: `${progress}%` }}
								/>
							</div>
							{updater.state.status === "downloading" ? (
								<p className="mt-1 text-sm text-muted">
									{Math.round(progress)}% downloaded
								</p>
							) : null}
						</Alert.Description>
					</Alert.Content>
				</Alert>
			) : null}
		</div>
	);
}
