import { getVersion } from "@tauri-apps/api/app";
import { relaunch } from "@tauri-apps/plugin-process";
import {
	check,
	type DownloadEvent,
	type Update,
} from "@tauri-apps/plugin-updater";
import { useCallback, useEffect, useRef, useState } from "react";

export type UpdaterState =
	| { status: "idle" }
	| { status: "checking" }
	| { status: "available"; update: Update }
	| {
			status: "downloading";
			update: Update;
			downloaded: number;
			total?: number;
	  }
	| { status: "installing"; update: Update }
	| { status: "error"; message: string };

export function useUpdater() {
	const [currentVersion, setCurrentVersion] = useState<string | null>(null);
	const [state, setState] = useState<UpdaterState>({ status: "idle" });
	const [dismissed, setDismissed] = useState(false);
	const mounted = useRef(true);

	useEffect(() => {
		mounted.current = true;
		return () => {
			mounted.current = false;
		};
	}, []);

	const setSafe = useCallback((next: UpdaterState) => {
		if (mounted.current) setState(next);
	}, []);

	const checkForUpdates = useCallback(async () => {
		setDismissed(false);
		setSafe({ status: "checking" });
		try {
			const result = await check({ timeout: 30000 });
			if (result) {
				setSafe({ status: "available", update: result });
				return result;
			}
			setSafe({ status: "idle" });
			return null;
		} catch (err) {
			const message = err instanceof Error ? err.message : String(err);
			setSafe({ status: "error", message });
			return null;
		}
	}, [setSafe]);

	const install = useCallback(async () => {
		const update = state.status === "available" ? state.update : undefined;
		if (!update) return;

		setSafe({ status: "downloading", update, downloaded: 0 });

		try {
			await update.downloadAndInstall((progress: DownloadEvent) => {
				switch (progress.event) {
					case "Started":
						setState({
							status: "downloading",
							update,
							downloaded: 0,
							total: progress.data.contentLength,
						});
						break;
					case "Progress":
						setState((prev) => {
							if (prev.status !== "downloading") return prev;
							return {
								...prev,
								downloaded: prev.downloaded + progress.data.chunkLength,
							};
						});
						break;
					case "Finished":
						setState({ status: "installing", update });
						break;
				}
			});

			await relaunch();
		} catch (err) {
			const message = err instanceof Error ? err.message : String(err);
			setSafe({ status: "error", message });
		}
	}, [state, setSafe]);

	useEffect(() => {
		let active = true;
		let timeoutId: ReturnType<typeof setTimeout> | null = null;

		async function init() {
			try {
				const version = await getVersion();
				if (active) setCurrentVersion(version);
			} catch (err) {
				console.warn("Failed to read app version:", err);
			}

			// Only auto-check on packaged builds. In dev the updater config is
			// usually a placeholder and the bundle is not signed, so auto-checks
			// just spam the user with 404 / key errors.
			if (import.meta.env.DEV) {
				return;
			}

			timeoutId = setTimeout(() => {
				if (active) checkForUpdates();
			}, 2000);
		}

		init();
		return () => {
			active = false;
			if (timeoutId) clearTimeout(timeoutId);
		};
	}, [checkForUpdates]);

	return {
		currentVersion,
		state,
		dismissed,
		checkForUpdates,
		install,
		dismiss: () => setDismissed(true),
	};
}
