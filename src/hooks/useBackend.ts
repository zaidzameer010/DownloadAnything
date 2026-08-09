import { useCallback, useEffect, useRef, useState } from "react";
import { backend, type Job, type Settings } from "../lib/backend";

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

	const debounceTimers = useRef(
		new Map<string, ReturnType<typeof setTimeout>>(),
	);
	useEffect(() => {
		const timers = debounceTimers.current;
		return () => {
			for (const timer of timers.values()) clearTimeout(timer);
		};
	}, []);

	const updateSettings = useCallback(
		async (partial: Partial<Settings>, debounceMs = 0) => {
			// Optimistic local update; the server broadcast reconciles afterwards.
			setSettings((prev) => (prev ? { ...prev, ...partial } : prev));
			const send = () => backend.setSettings(partial);
			const key = Object.keys(partial).join(",");
			const existing = debounceTimers.current.get(key);
			if (existing) clearTimeout(existing);
			if (debounceMs > 0) {
				return new Promise<{ ok?: boolean; error?: string }>((resolve) => {
					debounceTimers.current.set(
						key,
						setTimeout(async () => {
							const result = await send();
							resolve(result);
						}, debounceMs),
					);
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
		download: backend.download.bind(backend),
		jobAction: backend.jobAction.bind(backend),
		clearFinished: backend.clearFinished.bind(backend),
		reveal: backend.reveal.bind(backend),
	};
}
