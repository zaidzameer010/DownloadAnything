import { getVersion } from "@tauri-apps/api/app";
import { isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
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

export type Updater = ReturnType<typeof useUpdater>;

export function useUpdater() {
	const [currentVersion, setCurrentVersion] = useState<string | null>(null);
	const [state, setState] = useState<UpdaterState>({ status: "idle" });
	const [dismissed, setDismissed] = useState(false);
	const [tauriReady, setTauriReady] = useState(() => isTauri());
	const mounted = useRef(true);
	const updateRef = useRef<Update | null>(null);

	useEffect(() => {
		mounted.current = true;
		return () => {
			mounted.current = false;
		};
	}, []);

	// Tauri's injected globals can be briefly unavailable on first paint.
	// Retry once after a short delay so we don't disable the updater in a
	// running Tauri app.
	useEffect(() => {
		if (tauriReady) return;
		const id = setTimeout(() => setTauriReady(isTauri()), 150);
		return () => clearTimeout(id);
	}, [tauriReady]);

	const setSafe = useCallback((next: UpdaterState) => {
		if (mounted.current) setState(next);
	}, []);

	const closeUpdate = useCallback(async (update = updateRef.current) => {
		if (!update) return;
		if (updateRef.current === update) updateRef.current = null;
		try {
			await update.close();
		} catch (err) {
			console.warn("Failed to close updater resource:", err);
		}
	}, []);

	useEffect(() => {
		return () => {
			const update = updateRef.current;
			updateRef.current = null;
			if (update) void update.close().catch(() => undefined);
		};
	}, []);

	const checkForUpdates = useCallback(async () => {
		if (!tauriReady) {
			return null;
		}

		setDismissed(false);
		setSafe({ status: "checking" });
		await closeUpdate();
		try {
			const result = await check({ timeout: 30000 });
			if (result) {
				if (!mounted.current) {
					await result.close();
					return null;
				}
				updateRef.current = result;
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
	}, [tauriReady, setSafe, closeUpdate]);

	const checkForUpdatesRef = useRef(checkForUpdates);
	useEffect(() => {
		checkForUpdatesRef.current = checkForUpdates;
	}, [checkForUpdates]);

	const install = useCallback(async () => {
		const update = state.status === "available" ? state.update : undefined;
		if (!update) return;

		setSafe({ status: "downloading", update, downloaded: 0 });

		try {
			await update.downloadAndInstall((progress: DownloadEvent) => {
				if (!mounted.current) return;
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

			await closeUpdate(update);
			await relaunch();
		} catch (err) {
			await closeUpdate(update);
			const message = err instanceof Error ? err.message : String(err);
			setSafe({ status: "error", message });
		}
	}, [state, setSafe, closeUpdate]);

	useEffect(() => {
		let active = true;
		let timeoutId: ReturnType<typeof setTimeout> | null = null;

		async function init() {
			if (!tauriReady) {
				return;
			}

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
				if (active) checkForUpdatesRef.current();
			}, 2000);
		}

		init();
		return () => {
			active = false;
			if (timeoutId) clearTimeout(timeoutId);
		};
	}, [tauriReady]);

	// Listen for the native "Check for Updates" menu item.
	useEffect(() => {
		if (!tauriReady) return;

		let active = true;
		let unlisten: (() => void) | undefined;
		const setup = async () => {
			try {
				const stop = await listen("check-for-updates", () => {
					checkForUpdatesRef.current();
				});
				if (active) unlisten = stop;
				else stop();
			} catch (err) {
				console.warn("Failed to register update menu listener:", err);
			}
		};
		void setup();

		return () => {
			active = false;
			unlisten?.();
		};
	}, [tauriReady]);

	return {
		currentVersion,
		state,
		dismissed,
		tauriReady,
		checkForUpdates,
		install,
		dismiss: () => setDismissed(true),
	};
}
