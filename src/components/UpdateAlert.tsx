import { Alert, Button, ProgressBar, Typography } from "@heroui/react";
import { Squircle } from "@squircle-js/react";
import { Download, RotateCcw, X } from "lucide-react";
import type { UpdaterState, useUpdater } from "../hooks/useUpdater";
import { formatBytes } from "../lib/format";
import SquircleChip from "./SquircleChip";

type UpdateAlertProps = ReturnType<typeof useUpdater>;

function progressText(state: UpdaterState) {
	if (state.status === "downloading") {
		const pct =
			state.total && state.total > 0
				? Math.round((state.downloaded / state.total) * 100)
				: 0;
		return `${pct}% (${formatBytes(state.downloaded)} / ${formatBytes(state.total)})`;
	}
	return "";
}

export default function UpdateAlert({
	currentVersion,
	state,
	dismissed,
	checkForUpdates,
	install,
	dismiss,
}: UpdateAlertProps) {
	if (dismissed) return null;

	if (state.status === "error") {
		return (
			<Squircle asChild cornerRadius={24}>
				<Alert className="z-40 border-b border-separator" status="danger">
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>Update check failed</Alert.Title>
						<Alert.Description className="flex flex-wrap items-center gap-3">
							<span className="text-sm">{state.message}</span>
							<Squircle asChild className="ring-inset" cornerRadius={12}>
								<Button
									className="h-8 gap-2"
									size="sm"
									variant="tertiary"
									onPress={() => checkForUpdates()}
								>
									<RotateCcw className="size-3.5" />
									Retry
								</Button>
							</Squircle>
						</Alert.Description>
					</Alert.Content>
				</Alert>
			</Squircle>
		);
	}

	if (state.status === "checking") {
		return (
			<Squircle asChild cornerRadius={24}>
				<Alert className="z-40 border-b border-separator" status="accent">
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>Checking for updates…</Alert.Title>
						<Alert.Description>
							Looking for a newer version on GitHub.
						</Alert.Description>
					</Alert.Content>
				</Alert>
			</Squircle>
		);
	}

	if (state.status === "downloading" || state.status === "installing") {
		const isInstalling = state.status === "installing";
		const pct =
			state.status === "downloading" && state.total
				? Math.min(100, (state.downloaded / state.total) * 100)
				: 100;
		return (
			<Squircle asChild cornerRadius={24}>
				<Alert className="z-40 border-b border-separator" status="accent">
					<Alert.Indicator />
					<Alert.Content>
						<Alert.Title>
							{isInstalling ? "Installing update…" : "Downloading update…"}
						</Alert.Title>
						<Alert.Description>
							<ProgressBar
								aria-label="Update download progress"
								className="mt-2 w-full max-w-md"
								color="accent"
								size="sm"
								value={pct}
							>
								<ProgressBar.Track className="h-2 overflow-hidden rounded-full bg-surface-tertiary">
									<ProgressBar.Fill className="h-full transition-all" />
								</ProgressBar.Track>
							</ProgressBar>
							<p className="mt-1 text-sm text-muted">
								{state.status === "downloading"
									? progressText(state)
									: "Almost done"}
							</p>
						</Alert.Description>
					</Alert.Content>
				</Alert>
			</Squircle>
		);
	}

	if (state.status === "available") {
		const { update } = state;
		return (
			<Squircle asChild cornerRadius={24}>
				<Alert className="z-40 border-b border-separator" status="accent">
					<Alert.Indicator />
					<Alert.Content className="w-full min-w-0">
						<div className="flex w-full flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
							<div className="min-w-0">
								<Alert.Title className="flex items-center gap-2">
									Update available
									<SquircleChip size="sm" variant="soft">
										{update.version}
									</SquircleChip>
								</Alert.Title>
								<Alert.Description>
									<div className="mt-1 max-w-2xl space-y-2">
										<Typography color="muted" type="body-sm">
											{currentVersion
												? `DownloadAnything ${currentVersion} can be updated to ${update.version}.`
												: `A new version ${update.version} is ready to install.`}
										</Typography>
										{update.body ? (
											<Squircle
												className="max-h-32 overflow-y-auto border border-separator bg-surface-secondary p-3 text-sm"
												cornerRadius={8}
											>
												<pre className="whitespace-pre-wrap font-sans text-muted">
													{update.body}
												</pre>
											</Squircle>
										) : null}
									</div>
									<div className="mt-3 flex items-center gap-2">
										<Squircle asChild className="ring-inset" cornerRadius={12}>
											<Button
												className="h-8 gap-2"
												variant="primary"
												size="sm"
												onPress={() => install()}
											>
												<Download className="size-3.5" />
												Install & restart
											</Button>
										</Squircle>
										<Squircle asChild className="ring-inset" cornerRadius={12}>
											<Button
												className="h-8"
												size="sm"
												variant="tertiary"
												onPress={() => dismiss()}
											>
												<X className="size-3.5" />
												Later
											</Button>
										</Squircle>
									</div>
								</Alert.Description>
							</div>
						</div>
					</Alert.Content>
				</Alert>
			</Squircle>
		);
	}

	return null;
}
