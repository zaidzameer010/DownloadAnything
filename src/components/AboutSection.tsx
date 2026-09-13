import { Alert, Button, ProgressBar, Typography } from "@heroui/react";
import { Squircle } from "@squircle-js/react";
import { Info, Loader2, RefreshCw } from "lucide-react";
import type { Updater } from "../hooks/useUpdater";

export default function AboutSection({ updater }: { updater?: Updater }) {
	if (!updater) {
		return null;
	}
	const canCheck =
		updater.tauriReady &&
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
			<Squircle
				className="flex items-start gap-3 border border-separator bg-surface-secondary p-4"
				cornerRadius={12}
			>
				<Squircle
					className="flex size-9 shrink-0 items-center justify-center bg-accent-soft text-accent"
					cornerRadius={12}
				>
					<Info aria-hidden className="size-4" />
				</Squircle>
				<div className="min-w-0 flex-1">
					<Typography className="font-semibold" type="h6">
						DownloadAnything
					</Typography>
					<Typography color="muted" type="body-sm">
						{updater.currentVersion
							? `Current version: ${updater.currentVersion}`
							: updater.tauriReady
								? "Reading version…"
								: "Current version: not available"}
					</Typography>
					{!updater.tauriReady ? (
						<Typography color="muted" type="body-sm">
							Update checking requires the desktop app.
						</Typography>
					) : null}
				</div>
				<Squircle asChild className="ring-inset" cornerRadius={12}>
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
				</Squircle>
			</Squircle>

			{updater.state.status === "available" ? (
				<Squircle asChild cornerRadius={24}>
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
								<Squircle asChild className="ring-inset" cornerRadius={12}>
									<Button
										className="mt-3 h-8"
										variant="primary"
										size="sm"
										onPress={() => updater.install()}
									>
										Install & restart
									</Button>
								</Squircle>
							</Alert.Description>
						</Alert.Content>
					</Alert>
				</Squircle>
			) : null}

			{updater.state.status === "error" ? (
				<Squircle asChild cornerRadius={24}>
					<Alert status="danger">
						<Alert.Indicator />
						<Alert.Content>
							<Alert.Title>Update check failed</Alert.Title>
							<Alert.Description className="text-muted">
								{updater.state.message}
							</Alert.Description>
						</Alert.Content>
					</Alert>
				</Squircle>
			) : null}

			{updater.state.status === "downloading" ||
			updater.state.status === "installing" ? (
				<Squircle asChild cornerRadius={24}>
					<Alert status="accent">
						<Alert.Indicator />
						<Alert.Content>
							<Alert.Title>
								{updater.state.status === "installing"
									? "Installing update…"
									: "Downloading update…"}
							</Alert.Title>
							<Alert.Description>
								<ProgressBar
									aria-label="Update download progress"
									className="mt-2 w-full max-w-md"
									color="accent"
									size="sm"
									value={progress}
								>
									<ProgressBar.Track className="h-2 overflow-hidden rounded-full bg-surface-tertiary">
										<ProgressBar.Fill className="h-full transition-all" />
									</ProgressBar.Track>
								</ProgressBar>
								{updater.state.status === "downloading" ? (
									<p className="mt-1 text-sm text-muted">
										{Math.round(progress)}% downloaded
									</p>
								) : null}
							</Alert.Description>
						</Alert.Content>
					</Alert>
				</Squircle>
			) : null}
		</div>
	);
}
