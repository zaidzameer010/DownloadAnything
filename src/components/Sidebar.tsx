import { ListVideo, Settings as SettingsIcon, Cpu, Terminal, Activity } from "lucide-react";
import type { Health, Task } from "../types";

interface SidebarProps {
  readonly activeTab: "downloads" | "settings";
  readonly onTabChange: (tab: "downloads" | "settings") => void;
  readonly health: Health;
  readonly tasks: readonly Task[];
}

export function Sidebar({ activeTab, onTabChange, health, tasks }: SidebarProps) {
  const activeCount = tasks.filter((t) => t.status === "downloading").length;

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">
        <svg
          width="22"
          height="22"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
        </svg>
        <span>Acquisition Engine</span>
      </div>

      <div className="sidebar-menu">
        <button
          className={`menu-item ${activeTab === "downloads" ? "active" : ""}`}
          onClick={() => onTabChange("downloads")}
        >
          <ListVideo size={18} />
          <span>Downloads</span>
          {activeCount > 0 && (
            <span className="nav-badge" style={{ display: "inline-flex" }}>
              {activeCount}
            </span>
          )}
        </button>
        <button
          className={`menu-item ${activeTab === "settings" ? "active" : ""}`}
          onClick={() => onTabChange("settings")}
        >
          <SettingsIcon size={18} />
          <span>Configuration</span>
        </button>
      </div>

      <div className="sidebar-footer">
        <div className="meta-item">
          <Cpu size={16} />
          <div className="meta-item-info">
            <label>Workers</label>
            <span>{health.active_workers}</span>
          </div>
        </div>
        <div className="meta-item">
          <Terminal size={16} />
          <div className="meta-item-info">
            <label>yt-dlp</label>
            <span>{health.yt_dlp_version}</span>
          </div>
        </div>
        <div className="meta-item">
          <Activity size={16} />
          <div className="meta-item-info">
            <label>Active Tasks</label>
            <span>{activeCount}</span>
          </div>
        </div>
      </div>
    </nav>
  );
}
export default Sidebar;
