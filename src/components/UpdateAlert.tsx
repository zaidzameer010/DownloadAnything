import { Alert, Button, Chip, Typography } from "@heroui/react";
import { Download, RotateCcw, X } from "lucide-react";
import type { UpdaterState, useUpdater } from "../hooks/useUpdater";
import { formatBytes } from "../lib/format";

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
			<Alert className="z-40 border-b border-separator" status="danger">
				<Alert.Indicator />
				<Alert.Content>
					<Alert.Title>Update check failed</Alert.Title>
					<Alert.Description className="flex flex-wrap items-center gap-3">
						<span className="text-sm">{state.message}</span>
						<Button
							className="h-8 gap-2"
							size="sm"
							variant="tertiary"
							onPress={() => checkForUpdates()}
						>
							<RotateCcw className="size-3.5" />
							Retry
						</Button>
					</Alert.Description>
				</Alert.Content>
			</Alert>
		);
	}

	if (state.status === "checking") {
		return (
			<Alert className="z-40 border-b border-separator" status="accent">
				<Alert.Indicator />
				<Alert.Content>
					<Alert.Title>Checking for updates…</Alert.Title>
					<Alert.Description>
						Looking for a newer version on GitHub.
					</Alert.Description>
				</Alert.Content>
			</Alert>
		);
	}

	if (state.status === "downloading" || state.status === "installing") {
		const isInstalling = state.status === "installing";
		return (
			<Alert className="z-40 border-b border-separator" status="accent">
				<Alert.Indicator />
				<Alert.Content>
					<Alert.Title>
						{isInstalling ? "Installing update…" : "Downloading update…"}
					</Alert.Title>
					<Alert.Description>
						<div className="mt-2 h-2 w-full max-w-md overflow-hidden rounded-full bg-surface-tertiary">
							<div
								className="h-full bg-accent transition-all"
								style={{
									width:
										state.status === "downloading" && state.total
											? `${Math.min(100, (state.downloaded / state.total) * 100)}%`
											: "100%",
								}}
							/>
						</div>
						<p className="mt-1 text-sm text-muted">
							{state.status === "downloading"
								? progressText(state)
								: "Almost done"}
						</p>
					</Alert.Description>
				</Alert.Content>
			</Alert>
		);
	}

	if (state.status === "available") {
		const { update } = state;
		return (
			<Alert className="z-40 border-b border-separator" status="accent">
				<Alert.Indicator />
				<Alert.Content className="w-full min-w-0">
					<div className="flex w-full flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
						<div className="min-w-0">
							<Alert.Title className="flex items-center gap-2">
								Update available
								<Chip size="sm" variant="soft">
									{update.version}
								</Chip>
							</Alert.Title>
							<Alert.Description>
								<div className="mt-1 max-w-2xl space-y-2">
									<Typography color="muted" type="body-sm">
										{currentVersion
											? `DownloadAnything ${currentVersion} can be updated to ${update.version}.`
											: `A new version ${update.version} is ready to install.`}
									</Typography>
									{update.body ? (
										<div className="max-h-32 overflow-y-auto rounded-lg border border-separator bg-surface-secondary p-3 text-sm">
											<pre className="whitespace-pre-wrap font-sans text-muted">
												{update.body}
											</pre>
										</div>
									) : null}
								</div>
								<div className="mt-3 flex items-center gap-2">
									<Button
										className="h-8 gap-2"
										variant="primary"
										size="sm"
										onPress={() => install()}
									>
										<Download className="size-3.5" />
										Install & restart
									</Button>
									<Button
										className="h-8"
										size="sm"
										variant="tertiary"
										onPress={() => dismiss()}
									>
										<X className="size-3.5" />
										Later
									</Button>
								</div>
							</Alert.Description>
						</div>
					</div>
				</Alert.Content>
			</Alert>
		);
	}

	return null;
}
