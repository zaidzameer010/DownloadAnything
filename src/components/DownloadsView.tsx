import React, { useState, useEffect, useRef, useTransition } from "react";
import { createPortal } from "react-dom";
import {
  ListVideo,
  Search,
  Trash2,
  Clock,
  ArrowDownToLine,
  Check,
  AlertTriangle,
  Ban,
  Pause,
  Play,
  Loader2,
  FolderOpen,
  FileX,
  File,
  RefreshCw,
  Copy,
  Info,
} from "lucide-react";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  createColumnHelper,
} from "@tanstack/react-table";
import type { RowSelectionState, Row } from "@tanstack/react-table";
import type { Task, Settings, TaskStatus } from "../types";
import { fmtBytes, fmtSpeed, fmtETA } from "../utils/format";

interface DownloadsViewProps {
  readonly tasks: readonly Task[];
  readonly settings: Settings;
  readonly pauseTasks: (ids: readonly string[]) => void;
  readonly resumeTasks: (ids: readonly string[]) => void;
  readonly revealTask: (id: string) => Promise<void>;
  readonly deleteTasks: (ids: readonly string[], deleteFile: boolean) => void;
  readonly clearCompleted: () => Promise<number>;
  readonly showToast: (msg: string) => void;
}

interface ContextMenuState {
  readonly taskId: string;
  readonly x: number;
  readonly y: number;
}

interface DeleteModalState {
  readonly isOpen: boolean;
  readonly taskIds: readonly string[];
  readonly isBulk: boolean;
}

interface PropertiesModalState {
  readonly isOpen: boolean;
  readonly taskId: string | null;
}

const columnHelper = createColumnHelper<Task>();

const classNameMap: Record<string, string> = {
  select: "checkbox-col",
  progress: "progress-col",
  title: "title-col",
  size: "size-col",
  speed: "speed-col",
  eta: "eta-col",
  status: "status-col",
  location: "location-col",
};

export function DownloadsView({
  tasks,
  settings,
  pauseTasks,
  resumeTasks,
  revealTask,
  deleteTasks,
  clearCompleted,
  showToast,
}: DownloadsViewProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [deleteModal, setDeleteModal] = useState<DeleteModalState>({
    isOpen: false,
    taskIds: [],
    isBulk: false,
  });
  const [propertiesModal, setPropertiesModal] = useState<PropertiesModalState>({
    isOpen: false,
    taskId: null,
  });

  const [, startTransition] = useTransition();
  const contextMenuRef = useRef<HTMLDivElement>(null);

  // Close context menu on click outside
  useEffect(() => {
    const handleOutsideClick = (e: MouseEvent) => {
      if (contextMenuRef.current && !contextMenuRef.current.contains(e.target as Node)) {
        setContextMenu(null);
      }
    };
    if (contextMenu) {
      document.addEventListener("click", handleOutsideClick);
    }
    return () => {
      document.removeEventListener("click", handleOutsideClick);
    };
  }, [contextMenu]);

  // Clean up selected task IDs that no longer exist
  useEffect(() => {
    const currentIds = new Set(tasks.map((t) => t.task_id));
    let changed = false;
    const nextSelection = { ...rowSelection };
    for (const id of Object.keys(rowSelection)) {
      if (!currentIds.has(id)) {
        delete nextSelection[id];
        changed = true;
      }
    }
    if (changed) {
      setRowSelection(nextSelection);
    }
  }, [tasks, rowSelection]);

  // Categories list
  const categories = Object.keys(settings.categories || {});

  // Filtering logic
  let filteredTasks = tasks;
  if (categoryFilter !== "all") {
    filteredTasks = filteredTasks.filter((t) => t.category === categoryFilter);
  }
  if (searchQuery.trim()) {
    const query = searchQuery.toLowerCase().trim();
    filteredTasks = filteredTasks.filter((t) => {
      const title = (t.title || "").toLowerCase();
      const url = (t.url || "").toLowerCase();
      return title.includes(query) || url.includes(query);
    });
  }

  // Column definitions for TanStack Table
  const columns = React.useMemo(() => [
    columnHelper.display({
      id: "select",
      header: ({ table }) => (
        <input
          type="checkbox"
          id="select-all-tasks"
          checked={table.getIsAllRowsSelected()}
          ref={(input) => {
            if (input) {
              input.indeterminate = table.getIsSomeRowsSelected();
            }
          }}
          onChange={table.getToggleAllRowsSelectedHandler()}
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          className="task-checkbox"
          checked={row.getIsSelected()}
          onChange={row.getToggleSelectedHandler()}
        />
      ),
    }),
    columnHelper.accessor("progress", {
      id: "progress",
      header: "Progress",
      cell: ({ row }) => {
        const t = row.original;
        const r = 17;
        const c = 2 * Math.PI * r;
        const progressVal = t.progress || 0;
        const off = c - (progressVal / 100) * c;

        return t.status === "completed" ? (
          "—"
        ) : (
          <div className="progress-ring">
            <svg width="38" height="38">
              <circle
                className="bg"
                cx="19"
                cy="19"
                r={r}
                fill="none"
                strokeWidth="3"
              />
              <circle
                className="fg"
                cx="19"
                cy="19"
                r={r}
                fill="none"
                strokeWidth="3"
                strokeDasharray={c}
                strokeDashoffset={off}
              />
            </svg>
            <span>{Math.round(progressVal)}%</span>
          </div>
        );
      },
    }),
    columnHelper.accessor((row) => row.title || row.url, {
      id: "title",
      header: "Title",
      cell: ({ row }) => {
        const t = row.original;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", overflow: "hidden" }}>
            <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {t.title || t.url}
            </span>
            {t.format_id && t.format_id.includes("+ba") && (
              <span
                className="badge"
                style={{
                  fontSize: "9px",
                  padding: "2px 6px",
                  background: "rgba(255,255,255,0.08)",
                  color: "#a0a0b0",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: "4px",
                  flexShrink: 0,
                  fontWeight: 600,
                  letterSpacing: "0.5px"
                }}
                title="Audio track is being merged natively"
              >
                + AUDIO
              </span>
            )}
          </div>
        );
      },
    }),
    columnHelper.accessor((row) => row.total_bytes || row.downloaded_bytes, {
      id: "size",
      header: "Size",
      cell: ({ row }) => {
        const t = row.original;
        return fmtBytes(t.total_bytes || t.downloaded_bytes);
      },
    }),
    columnHelper.accessor("speed", {
      id: "speed",
      header: "Speed",
      cell: ({ getValue }) => fmtSpeed(getValue()),
    }),
    columnHelper.accessor("eta", {
      id: "eta",
      header: "ETA",
      cell: ({ getValue }) => fmtETA(getValue()),
    }),
    columnHelper.accessor("status", {
      id: "status",
      header: "Status",
      cell: ({ row }) => {
        const t = row.original;
        const statusClass = t.status.replace(/\s+/g, "-");

        let statusText: string = t.status;
        let badgeIcon = <></>;
        const PROCESSING_STATUSES: readonly string[] = ["stitching", "embedding", "finalizing"];

        if (t.status === "downloading") {
          badgeIcon = <ArrowDownToLine size={11} style={{ marginRight: 3 }} />;
          if (t.fragment_index !== undefined && t.fragment_index !== null) {
            if (t.fragment_count) {
              statusText = `downloading (${t.fragment_index}/${t.fragment_count})`;
            } else {
              statusText = `downloading (frag ${t.fragment_index})`;
            }
          }
        } else if (t.status === "completed") {
          badgeIcon = <Check size={11} style={{ marginRight: 3 }} />;
        } else if (t.status === "error") {
          badgeIcon = <AlertTriangle size={11} style={{ marginRight: 3 }} />;
        } else if (t.status === "queued") {
          badgeIcon = <Clock size={11} style={{ marginRight: 3 }} />;
        } else if (t.status === "cancelled") {
          badgeIcon = <Ban size={11} style={{ marginRight: 3 }} />;
        } else if (t.status === "paused") {
          badgeIcon = <Pause size={11} style={{ marginRight: 3 }} />;
        } else if (PROCESSING_STATUSES.includes(t.status)) {
          badgeIcon = (
            <Loader2 className="spin" size={11} style={{ marginRight: 3, animation: "spin 1.5s linear infinite" }} />
          );
          const labels: Record<string, string> = {
            stitching: "Stitching",
            embedding: "Embedding",
            finalizing: "Finalizing",
          };
          statusText = labels[t.status] || t.status;
        }

        return (
          <span className={`badge ${statusClass}`}>
            {badgeIcon}
            {statusText}
          </span>
        );
      },
    }),
    columnHelper.accessor((row) => row.final_path || row.custom_path, {
      id: "location",
      header: "Location",
      cell: ({ row }) => {
        const t = row.original;
        let loc =
          t.final_path ||
          t.custom_path ||
          settings.categories[t.category] ||
          "—";
        if (loc && loc !== "—" && loc === t.final_path) {
          const sep = loc.includes("\\") ? "\\" : "/";
          const parts = loc.split(sep);
          parts.pop();
          loc = parts.join(sep) + sep;
        }
        return (
          <span title={loc}>
            {loc}
          </span>
        );
      },
    }),
  ], [settings]);

  const table = useReactTable({
    data: filteredTasks as Task[],
    columns,
    state: {
      rowSelection,
    },
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.task_id,
    enableRowSelection: true,
  });

  const rowClick = (e: React.MouseEvent, row: Row<Task>) => {
    const target = e.target as HTMLElement;
    if (
      target.closest("input") ||
      target.closest("button") ||
      target.closest("a") ||
      target.classList.contains("task-checkbox")
    ) {
      return;
    }
    row.toggleSelected();
  };

  // Context Menu handlers
  const handleContextMenu = (e: React.MouseEvent, taskId: string) => {
    e.preventDefault();
    setContextMenu({
      taskId,
      x: e.clientX,
      y: e.clientY,
    });
  };

  const handleContextAction = (action: () => void) => {
    action();
    setContextMenu(null);
  };

  // Confirm and execute single/bulk delete
  const triggerDeleteConfirm = (taskIds: readonly string[], isBulk: boolean) => {
    setDeleteModal({
      isOpen: true,
      taskIds,
      isBulk,
    });
  };

  const executeDelete = () => {
    const ids = deleteModal.taskIds;
    deleteTasks(ids, true);
    showToast(
      deleteModal.isBulk ? `Deleting files for ${ids.length} tasks` : "Deleting task and file"
    );
    setRowSelection((prev) => {
      const next = { ...prev };
      ids.forEach((id) => delete next[id]);
      return next;
    });
    setDeleteModal({ isOpen: false, taskIds: [], isBulk: false });
  };

  // Bulk actions handlers
  const handleBulkPause = () => {
    const ids = Object.keys(rowSelection);
    pauseTasks(ids);
    showToast(`Paused ${ids.length} downloads`);
    setRowSelection({});
  };

  const handleBulkResume = () => {
    const ids = Object.keys(rowSelection);
    resumeTasks(ids);
    showToast(`Resumed ${ids.length} downloads`);
    setRowSelection({});
  };

  const handleBulkRemove = () => {
    const ids = Object.keys(rowSelection);
    deleteTasks(ids, false);
    showToast(`Removed ${ids.length} tasks from queue`);
    setRowSelection({});
  };

  const handleClearCompleted = async () => {
    try {
      const count = await clearCompleted();
      if (count > 0) {
        showToast(`Cleared ${count} completed tasks`);
      } else {
        showToast("No completed tasks to clear");
      }
    } catch (e) {
      showToast("Failed to clear tasks");
    }
  };

  // Bulk actions toolbar display logic
  const showBulkActions = Object.keys(rowSelection).length > 0;
  const selectedTasksList = tasks.filter((t) => rowSelection[t.task_id]);
  const hasActiveSelected = selectedTasksList.some(
    (t) => t.status === "downloading" || t.status === "queued"
  );
  const hasPausedSelected = selectedTasksList.some(
    (t) =>
      t.status === "paused" ||
      t.status === "cancelled" ||
      t.status === "error"
  );

  return (
    <div className="view-panel active" id="view-downloads">
      <section className="card">
        <div className="download-header-actions">
          <h2>
            <ListVideo size={15} style={{ marginRight: 10 }} /> Downloads
          </h2>
          <div className="header-search-bar">
            <Search className="search-icon" size={16} />
            <input
              type="text"
              id="task-search"
              placeholder="Search downloads by title or URL..."
              autoComplete="off"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>
        </div>

        <div className="download-filters">
          <div className="filter-group" id="download-category-filters">
            <button
              className={`filter-btn ${categoryFilter === "all" ? "active" : ""}`}
              onClick={() => setCategoryFilter("all")}
            >
              All
            </button>
            {categories.map((cat) => (
              <button
                key={cat}
                className={`filter-btn ${categoryFilter === cat ? "active" : ""}`}
                onClick={() => setCategoryFilter(cat)}
              >
                {cat}
              </button>
            ))}
          </div>

          <button className="ghost danger" id="clear-completed-btn" onClick={handleClearCompleted}>
            <Trash2 size={14} /> Clear Completed
          </button>
        </div>

        <table>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id} className={classNameMap[header.id]}>
                    {flexRender(
                      header.column.columnDef.header,
                      header.getContext()
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody id="task-body">
            {table.getRowModel().rows.length === 0 ? (
              <tr>
                <td
                  colSpan={8}
                  style={{
                    textAlign: "center",
                    color: "var(--muted)",
                    padding: 40,
                    fontWeight: 500,
                  }}
                >
                  {tasks.length === 0 ? "No downloads in queue" : "No matching downloads found"}
                </td>
              </tr>
            ) : (
              table.getRowModel().rows.map((row) => {
                const isSelected = row.getIsSelected();
                const rowClass = isSelected ? "selected" : "";
                return (
                  <tr
                    key={row.id}
                    className={rowClass}
                    onClick={(e) => rowClick(e, row)}
                    onContextMenu={(e) => handleContextMenu(e, row.id)}
                    style={{ cursor: "pointer" }}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className={classNameMap[cell.column.id]}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </section>

      {/* Floating Context Menu */}
      {contextMenu && createPortal(
        <div
          ref={contextMenuRef}
          className="context-menu"
          style={{
            display: "flex",
            left: contextMenu.x,
            top: contextMenu.y,
          }}
        >
          {(() => {
            const task = tasks.find((t) => t.task_id === contextMenu.taskId);
            if (!task) return null;

            const isDownloading = task.status === "downloading" || task.status === "queued";
            const isPaused = task.status === "paused";

            return (
              <>
                {isDownloading && (
                  <button
                    className="context-menu-item"
                    onClick={() => handleContextAction(() => pauseTasks([task.task_id]))}
                  >
                    <Pause size={14} /> Pause
                  </button>
                )}
                {isPaused && (
                  <button
                    className="context-menu-item"
                    onClick={() => handleContextAction(() => resumeTasks([task.task_id]))}
                  >
                    <Play size={14} /> Resume
                  </button>
                )}
                {(task.status === "cancelled" || task.status === "error") && (
                  <button
                    className="context-menu-item"
                    onClick={() => handleContextAction(() => resumeTasks([task.task_id]))}
                  >
                    <RefreshCw size={14} /> Restart / Resume
                  </button>
                )}
                <button
                  className="context-menu-item"
                  onClick={() =>
                    handleContextAction(async () => {
                      try {
                        await revealTask(task.task_id);
                      } catch {
                        showToast("Failed to reveal folder");
                      }
                    })
                  }
                >
                  <FolderOpen size={14} /> Reveal in Finder
                </button>
                <button
                  className="context-menu-item"
                  onClick={() =>
                    handleContextAction(() => {
                      setPropertiesModal({
                        isOpen: true,
                        taskId: task.task_id,
                      });
                    })
                  }
                >
                  <Info size={14} /> Properties
                </button>
                <div className="context-menu-divider" />
                <button
                  className="context-menu-item"
                  onClick={() =>
                    handleContextAction(() => {
                      deleteTasks([task.task_id], false);
                      showToast("Task removed from list");
                    })
                  }
                >
                  <Trash2 size={14} /> Remove from list
                </button>
                <button
                  className="context-menu-item danger"
                  onClick={() =>
                    handleContextAction(() => triggerDeleteConfirm([task.task_id], false))
                  }
                >
                  <FileX size={14} /> Delete File
                </button>
              </>
            );
          })()}
        </div>,
        document.body
      )}

      {/* Bulk Actions Dock */}
      {createPortal(
        <div className={`bulk-actions-bar ${showBulkActions ? "show" : ""}`}>
          <span className="selection-count">{Object.keys(rowSelection).length} selected</span>
          <div className="bulk-buttons">
            {hasActiveSelected && (
              <button className="ghost" onClick={handleBulkPause} title="Pause selected">
                <Pause size={14} /> Pause
              </button>
            )}
            {!hasActiveSelected && hasPausedSelected && (
              <button className="ghost" onClick={handleBulkResume} title="Resume/Retry selected">
                <Play size={14} /> Resume
              </button>
            )}
            <button className="ghost" onClick={handleBulkRemove} title="Remove selected from list">
              <Trash2 size={14} /> Remove
            </button>
            <button
              className="ghost danger"
              onClick={() => triggerDeleteConfirm(Object.keys(rowSelection), true)}
              title="Delete selected files from disk"
            >
              <FileX size={14} /> Delete Files
            </button>
          </div>
        </div>,
        document.body
      )}

      {/* Delete Confirmation Warning Modal */}
      {deleteModal.isOpen && createPortal(
        <div className="onboarding-overlay" style={{ display: "flex" }}>
          <div className="onboarding-card delete-confirm-card">
            <div className="delete-confirm-header">
              <AlertTriangle className="warning-icon" size={24} style={{ color: "var(--danger)" }} />
              <h2>Permanently Delete Files</h2>
            </div>
            <p className="delete-confirm-text">
              {deleteModal.isBulk
                ? `Are you sure you want to permanently delete files for ${deleteModal.taskIds.length} selected tasks?`
                : "Are you sure you want to permanently delete the downloaded file from disk?"}
            </p>

            <div className="delete-files-list" style={{ display: "flex" }}>
              {deleteModal.taskIds.map((id) => {
                const task = tasks.find((t) => t.task_id === id);
                const title = task ? task.title : "Unknown File";
                return (
                  <div key={id} className="delete-files-list-item">
                    <File size={14} />
                    <span>{title || "Download Task"}</span>
                  </div>
                );
              })}
            </div>

            <div className="delete-confirm-actions">
              <button
                className="ghost"
                onClick={() => setDeleteModal({ isOpen: false, taskIds: [], isBulk: false })}
              >
                Cancel
              </button>
              <button className="primary danger" onClick={executeDelete}>
                Delete Permanently
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* Properties Modal */}
      {propertiesModal.isOpen && propertiesModal.taskId && createPortal(
        <div
          className="onboarding-overlay"
          style={{ display: "flex" }}
          onClick={(e) => {
            if (e.target === e.currentTarget) {
              setPropertiesModal({ isOpen: false, taskId: null });
            }
          }}
        >
          <div className="onboarding-card properties-modal-card">
            {(() => {
              const task = tasks.find((t) => t.task_id === propertiesModal.taskId);
              if (!task) return null;

              const handleCopy = (text: string, label: string) => {
                navigator.clipboard.writeText(text);
                showToast(`${label} copied to clipboard`);
              };

              return (
                <>
                  <div className="properties-header">
                    <h2>
                      <Info size={18} style={{ color: "var(--info)" }} /> Task Properties
                    </h2>
                  </div>

                  <div className="properties-content">
                    <div className="title-row">
                      <span className="property-label">Title</span>
                      <div className="property-value" style={{ fontWeight: 600, color: "var(--text)", wordBreak: "break-word" }}>
                        {task.title || "Untitled Download"}
                      </div>
                    </div>

                    <div className="properties-grid">
                      <div className="property-item">
                        <span className="property-label">Task ID</span>
                        <div className="property-value">
                          <code>{task.task_id}</code>
                          <button
                            className="ghost-icon-btn"
                            title="Copy Task ID"
                            onClick={() => handleCopy(task.task_id, "Task ID")}
                          >
                            <Copy size={12} />
                          </button>
                        </div>
                      </div>

                      <div className="property-item">
                        <span className="property-label">Status</span>
                        <div className="property-value">
                          <span className={`badge ${task.status.replace(/\s+/g, "-")}`}>
                            {task.status}
                          </span>
                        </div>
                      </div>

                      <div className="property-item full-width">
                        <span className="property-label">Source URL</span>
                        <div className="property-value url-val">
                          <a href={task.url} target="_blank" rel="noreferrer" title={task.url}>
                            {task.url}
                          </a>
                          <button
                            className="ghost-icon-btn"
                            title="Copy URL"
                            onClick={() => handleCopy(task.url, "URL")}
                          >
                            <Copy size={12} />
                          </button>
                        </div>
                      </div>

                      <div className="property-item">
                        <span className="property-label">Category</span>
                        <div className="property-value">{task.category || "Default"}</div>
                      </div>

                      <div className="property-item">
                        <span className="property-label">Format ID</span>
                        <div className="property-value">
                          <code>{task.format_id || "default"}</code>
                        </div>
                      </div>

                      <div className="property-item">
                        <span className="property-label">Media Type</span>
                        <div className="property-value">
                          {task.is_stream ? "Segmented Stream" : task.is_video ? "Video" : "Audio / File"}
                        </div>
                      </div>

                      {task.page_title && task.page_title !== task.title && (
                        <div className="property-item full-width">
                          <span className="property-label">Original Page Title</span>
                          <div className="property-value" style={{ wordBreak: "break-word" }}>{task.page_title}</div>
                        </div>
                      )}

                      {task.started_at && task.started_at > 0 && (
                        <div className="property-item">
                          <span className="property-label">Started At</span>
                          <div className="property-value">
                            {new Date(task.started_at * 1000).toLocaleString()}
                          </div>
                        </div>
                      )}

                      {task.finished_at && task.finished_at > 0 && (
                        <div className="property-item">
                          <span className="property-label">Finished At</span>
                          <div className="property-value">
                            {new Date(task.finished_at * 1000).toLocaleString()}
                          </div>
                        </div>
                      )}

                      {task.status === "error" && task.error && (
                        <div className="property-item full-width error-val">
                          <span className="property-label" style={{ color: "var(--danger)" }}>Error Message</span>
                          <div className="property-value" style={{ color: "var(--danger)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                            {task.error}
                          </div>
                        </div>
                      )}

                      {task.status !== "completed" && (
                        <>
                          <div className="property-item full-width">
                            <span className="property-label">Progress</span>
                            <div className="property-value progress-value-container">
                              <div className="properties-progress-bar-bg">
                                <div
                                  className="properties-progress-bar-fg"
                                  style={{ width: `${task.progress || 0}%` }}
                                />
                              </div>
                              <span className="progress-percentage">{Math.round(task.progress || 0)}%</span>
                            </div>
                          </div>

                          <div className="property-item">
                            <span className="property-label">Speed</span>
                            <div className="property-value">{fmtSpeed(task.speed)}</div>
                          </div>

                          <div className="property-item">
                            <span className="property-label">ETA</span>
                            <div className="property-value">{fmtETA(task.eta)}</div>
                          </div>
                        </>
                      )}

                      <div className="property-item">
                        <span className="property-label">File Size</span>
                        <div className="property-value">
                          {task.status === "completed"
                            ? fmtBytes(task.total_bytes || task.downloaded_bytes)
                            : `${fmtBytes(task.downloaded_bytes)} of ${fmtBytes(task.total_bytes || 0)}`}
                        </div>
                      </div>

                      <div className="property-item full-width">
                        <span className="property-label">Destination Path</span>
                        <div className="property-value path-val">
                          <span title={task.final_path || task.custom_path || "—"}>
                            {task.final_path || task.custom_path || "—"}
                          </span>
                          {(task.final_path || task.custom_path) && (
                            <button
                              className="ghost-icon-btn"
                              title="Copy Path"
                              onClick={() => handleCopy(task.final_path || task.custom_path || "", "Path")}
                            >
                              <Copy size={12} />
                            </button>
                          )}
                        </div>
                      </div>

                      {task.fragment_index !== undefined && task.fragment_index !== null && (
                        <div className="property-item">
                          <span className="property-label">Fragments</span>
                          <div className="property-value">
                            {task.fragment_count
                              ? `${task.fragment_index} / ${task.fragment_count}`
                              : `Fragment ${task.fragment_index}`}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="properties-actions">
                    <button
                      className="primary"
                      onClick={() => setPropertiesModal({ isOpen: false, taskId: null })}
                    >
                      Close
                    </button>
                  </div>
                </>
              );
            })()}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
export default DownloadsView;
