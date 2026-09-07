import { useCallback, useEffect, useRef, useState } from "react";
import { backend, type Job, type Settings } from "../lib/backend";

type PendingSettingsUpdate = {
	timer: ReturnType<typeof setTimeout>;
	resolve: (result: { ok?: boolean; error?: string }) => void;
};

/**
 * Subscribes to the backend singleton and exposes connection state, the live
 * job queue and settings snapshot, plus realtime settings mutation
 * (optimistic + debounced so sliders/text inputs don't flood the socket).
 */
export function useBackend() {
	const [connected, setConnected] = useState(backend.connected);
	const [settings, setSettings] = useState<Settings | null>(backend.settings);
	const [jobs, setJobs] = useState<Job[]>(backend.jobs);

	useEffect(() => {
		backend.connect();
		const sync = () => {
			setConnected(backend.connected);
			setSettings(backend.settings);
			setJobs(backend.jobs);
		};
		sync();
		return backend.subscribe(sync);
	}, []);

	const debounceTimers = useRef(new Map<string, PendingSettingsUpdate>());
	useEffect(() => {
		const timers = debounceTimers.current;
		return () => {
			for (const pending of timers.values()) {
				clearTimeout(pending.timer);
				pending.resolve({ ok: true });
			}
			timers.clear();
		};
	}, []);

	const updateSettings = useCallback(
		async (partial: Partial<Settings>, debounceMs = 0) => {
			// Optimistic local update; the server broadcast reconciles afterwards.
			setSettings((prev) => (prev ? { ...prev, ...partial } : prev));
			const send = async () => {
				const result = await backend.setSettings(partial);
				if (result.error) setSettings(backend.settings);
				return result;
			};
			const key = Object.keys(partial).sort().join(",");
			const existing = debounceTimers.current.get(key);
			if (existing) {
				clearTimeout(existing.timer);
				existing.resolve({ ok: true });
				debounceTimers.current.delete(key);
			}
			if (debounceMs > 0) {
				return new Promise<{ ok?: boolean; error?: string }>((resolve) => {
					let pending: PendingSettingsUpdate;
					pending = {
						timer: setTimeout(async () => {
							if (debounceTimers.current.get(key) !== pending) return;
							debounceTimers.current.delete(key);
							resolve(await send());
						}, debounceMs),
						resolve,
					};
					debounceTimers.current.set(key, pending);
				});
			}
			return send();
		},
		[],
	);

	return {
		connected,
		settings,
		jobs,
		updateSettings,
		probe: backend.probe.bind(backend),
		cancelProbe: backend.cancelProbe.bind(backend),
		download: backend.download.bind(backend),
		jobAction: backend.jobAction.bind(backend),
		clearFinished: backend.clearFinished.bind(backend),
		reveal: backend.reveal.bind(backend),
	};
}
