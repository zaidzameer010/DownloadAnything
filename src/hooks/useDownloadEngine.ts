import { useState, useEffect, useRef, useOptimistic, useTransition, useActionState } from "react";
import type { Task, Settings, Health, BackendState, TaskStatus, CookiesBrowser, MergeFormat } from "../types";

const WS_URL = "ws://127.0.0.1:8000/ws/progress";
const WS_REQUEST_TIMEOUT_MS = 30_000;

interface PendingRequest {
  readonly resolve: (value: any) => void;
  readonly reject: (error: Error) => void;
  readonly timeoutId: ReturnType<typeof setTimeout>;
}

export function useDownloadEngine() {
  const [tasks, setTasks] = useState<readonly Task[]>([]);
  const [settings, setSettings] = useState<Settings>({
    max_concurrent_downloads: 3,
    merge_output_format: "mp4",
    default_download_path: "",
    concurrent_fragments: 16,
    rate_limit_bytes_per_sec: 0,
    proxy: "",
    cookies_from_browser: "none",
    embed_thumbnail: false,
    embed_subtitles: false,
    subtitle_language: "en",
    categories: {},
  });
  const [health, setHealth] = useState<Health>({
    active_workers: "—",
    yt_dlp_version: "—",
  });
  const [online, setOnline] = useState(true);
  const [activeTab, setActiveTab] = useState<"downloads" | "settings">("downloads");

  // Backend state
  const isTauri = typeof window !== "undefined" && !!(window as any).__TAURI__;
  const [backendReady, setBackendReady] = useState(!isTauri);
  const [backendLogs, setBackendLogs] = useState<readonly string[]>([]);

  // WebSocket Ref
  const wsRef = useRef<WebSocket | null>(null);
  const pendingRequestsRef = useRef<Map<string, PendingRequest>>(new Map());
  const requestCounterRef = useRef(0);

  // Transitions for actions
  const [, startTransition] = useTransition();

  // Optimistic UI updates
  type OptimisticAction =
    | { readonly type: "status"; readonly task_id: string; readonly status: TaskStatus }
    | { readonly type: "delete"; readonly task_id: string }
    | { readonly type: "clear-completed" };

  const [optimisticTasks, setOptimisticTasks] = useOptimistic(
    tasks,
    (state: readonly Task[], action: OptimisticAction): readonly Task[] => {
      switch (action.type) {
        case "status":
          return state.map((t) =>
            t.task_id === action.task_id ? { ...t, status: action.status } : t
          );
        case "delete":
          return state.filter((t) => t.task_id !== action.task_id);
        case "clear-completed":
          return state.filter((t) => t.status !== "completed");
        default:
          return state;
      }
    }
  );

  // Connect WebSocket function
  const connect = useRef<() => void>(() => {});

  connect.current = () => {
    if (wsRef.current) {
      wsRef.current.close();
    }

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setOnline(true);
      setBackendReady(true);
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "tasks") {
          setTasks(msg.data || []);
          if (msg.health) {
            setHealth(msg.health);
          }
          if (msg.settings) {
            setSettings(msg.settings);
          }
          setOnline(true);
        } else if (msg.type === "response") {
          const { request_id, ok, data, error } = msg;
          const pending = pendingRequestsRef.current.get(request_id);
          if (pending) {
            clearTimeout(pending.timeoutId);
            pendingRequestsRef.current.delete(request_id);
            if (ok) {
              pending.resolve(data);
            } else {
              pending.reject(new Error(error || "Request failed"));
            }
          }
        }
      } catch (e) {
        console.error("WS message parse error:", e);
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      setOnline(false);
      setHealth({ active_workers: "offline", yt_dlp_version: "—" });

      // Reject all in-flight requests so callers never hang indefinitely
      for (const [, pending] of pendingRequestsRef.current) {
        clearTimeout(pending.timeoutId);
        pending.reject(new Error("WebSocket disconnected"));
      }
      pendingRequestsRef.current.clear();

      // Retry connection
      setTimeout(() => connect.current(), 2500);
    };

    ws.onerror = () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  };

  // Setup WebSocket connection and Tauri listeners on mount
  useEffect(() => {
    connect.current();

    // Tauri Log Listener
    let unlisten: (() => void) | null = null;
    if (isTauri && (window as any).__TAURI__.event) {
      (window as any).__TAURI__.event.listen("backend-log", (event: { payload: string }) => {
        setBackendLogs((prev) => {
          const next = [...prev, event.payload];
          if (next.length > 100) {
            next.shift();
          }
          return next;
        });
      }).then((fn: () => void) => {
        unlisten = fn;
      });
    }

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (unlisten) {
        unlisten();
      }
    };
  }, [isTauri]);

  // Helper to send request over WebSocket
  const sendWSRequest = <T = any>(action: string, payload: unknown = {}): Promise<T> => {
    return new Promise((resolve, reject) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        reject(new Error("WebSocket is not connected"));
        return;
      }

      const requestId = `web-${Date.now()}-${requestCounterRef.current++}`;

      const timeoutId = setTimeout(() => {
        if (pendingRequestsRef.current.has(requestId)) {
          pendingRequestsRef.current.delete(requestId);
          reject(new Error("Request timed out"));
        }
      }, WS_REQUEST_TIMEOUT_MS);

      pendingRequestsRef.current.set(requestId, { resolve, reject, timeoutId });
      ws.send(JSON.stringify({ action, request_id: requestId, payload }));
    });
  };

  // Actions
  const pauseTasks = (ids: readonly string[]) => {
    startTransition(async () => {
      ids.forEach((id) => setOptimisticTasks({ type: "status", task_id: id, status: "paused" }));
      try {
        await Promise.all(ids.map((id) => sendWSRequest("pause", { task_id: id })));
      } catch (e) {
        console.error("Failed to pause tasks:", e);
      }
    });
  };

  const resumeTasks = (ids: readonly string[]) => {
    startTransition(async () => {
      ids.forEach((id) => setOptimisticTasks({ type: "status", task_id: id, status: "downloading" }));
      try {
        await Promise.all(ids.map((id) => sendWSRequest("resume", { task_id: id })));
      } catch (e) {
        console.error("Failed to resume tasks:", e);
      }
    });
  };

  const revealTask = async (id: string) => {
    try {
      await sendWSRequest("reveal", { task_id: id });
    } catch (e) {
      console.error("Failed to reveal task:", e);
      throw e;
    }
  };

  const deleteTasks = (ids: readonly string[], deleteFile = false) => {
    startTransition(async () => {
      ids.forEach((id) => setOptimisticTasks({ type: "delete", task_id: id }));
      try {
        await Promise.all(
          ids.map((id) => sendWSRequest("delete", { task_id: id, delete_file: deleteFile }))
        );
      } catch (e) {
        console.error("Failed to delete tasks:", e);
      }
    });
  };

  const clearCompleted = () => {
    const completedTasks = tasks.filter((t) => t.status === "completed");
    if (completedTasks.length === 0) return Promise.resolve(0);

    return new Promise<number>((resolve, reject) => {
      startTransition(async () => {
        setOptimisticTasks({ type: "clear-completed" });
        try {
          let count = 0;
          await Promise.all(
            completedTasks.map(async (t) => {
              try {
                await sendWSRequest("delete", { task_id: t.task_id, delete_file: false });
                count++;
              } catch (err) {
                console.error(`Failed to clear task ${t.task_id}`, err);
              }
            })
          );
          resolve(count);
        } catch (e) {
          reject(e);
        }
      });
    });
  };

  const saveSettings = async (newSettings: Settings): Promise<Settings> => {
    try {
      const saved: Settings = await sendWSRequest("save_settings", newSettings);
      setSettings(saved);
      return saved;
    } catch (e) {
      console.error("Failed to save settings:", e);
      throw e;
    }
  };

  // React 19 useActionState form action logic for saving settings
  const [saveState, saveSettingsAction, isSaving] = useActionState(
    async (_state: { success: boolean; error: string | null } | null, formData: Settings) => {
      try {
        await saveSettings(formData);
        return { success: true, error: null };
      } catch (e: any) {
        return { success: false, error: e.message || "Failed to save settings" };
      }
    },
    null
  );

  return {
    tasks: optimisticTasks,
    rawTasks: tasks,
    settings,
    setSettings,
    health,
    online,
    activeTab,
    setActiveTab,
    backend: {
      ready: backendReady,
      logs: backendLogs,
    },
    pauseTasks,
    resumeTasks,
    revealTask,
    deleteTasks,
    clearCompleted,
    saveSettingsAction,
    isSaving,
    saveState,
  };
}
export type DownloadEngine = ReturnType<typeof useDownloadEngine>;
