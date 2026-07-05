import { useState, useEffect } from "react";
import { WifiOff, Info, Check } from "lucide-react";
import Sidebar from "./components/Sidebar";
import DownloadsView from "./components/DownloadsView";
import ConfigurationView from "./components/ConfigurationView";
import { useDownloadEngine } from "./hooks/useDownloadEngine";

export function App() {
  const engine = useDownloadEngine();

  // Toast State
  const [toastMessage, setToastMessage] = useState("");
  const [showToast, setShowToast] = useState(false);

  // Startup Toast State
  const [showStartupToast, setShowStartupToast] = useState(false);
  const [startupSuccess, setStartupSuccess] = useState(false);
  const [startupTimer, setStartupTimer] = useState<ReturnType<typeof setTimeout> | null>(null);

  const triggerToast = (msg: string) => {
    setToastMessage(msg);
    setShowToast(true);
  };

  // Automatically hide regular toast
  useEffect(() => {
    if (showToast) {
      const timer = setTimeout(() => {
        setShowToast(false);
      }, 2200);
      return () => clearTimeout(timer);
    }
  }, [showToast]);

  // Monitor backend ready state for startup log toast
  const isReady = engine.backend.ready;
  const logs = engine.backend.logs;

  useEffect(() => {
    if (!isReady) {
      // Backend is launching
      setShowStartupToast(true);
      setStartupSuccess(false);
      if (startupTimer) {
        clearTimeout(startupTimer);
        setStartupTimer(null);
      }
    } else {
      // Backend has transitioned to ready
      setStartupSuccess(true);
      const timer = setTimeout(() => {
        setShowStartupToast(false);
      }, 2500);
      setStartupTimer(timer);
    }

    return () => {
      if (startupTimer) {
        clearTimeout(startupTimer);
      }
    };
  }, [isReady]);

  // Determine last log to display
  let lastLog = "Initializing sidecar process...";
  if (logs.length > 0) {
    const rawLog = logs[logs.length - 1] ?? "";
    lastLog = rawLog.replace(/^Sidecar stdout: /, "").replace(/^Sidecar stderr: /, "");
    if (lastLog.length > 60) {
      lastLog = lastLog.substring(0, 57) + "...";
    }
  }

  // Connection banner logic
  const showOfflineBanner = !engine.online && isReady;

  return (
    <>
      {/* React 19 Document Metadata hoisting */}
      <title>DownloadAnything</title>
      <meta name="description" content="DownloadAnything - A powerful self-hosted download manager and media acquisition engine." />

      <div className="app-layout">
        <Sidebar
          activeTab={engine.activeTab}
          onTabChange={engine.setActiveTab}
          health={engine.health}
          tasks={engine.rawTasks}
        />

        <main className="main-content">
          {showOfflineBanner && (
            <div className="offline-banner visible" id="offline-banner">
              <WifiOff size={18} />
              Connection lost. Trying to reconnect to FastAPI engine...
            </div>
          )}

          {engine.activeTab === "downloads" ? (
            <DownloadsView
              tasks={engine.tasks}
              settings={engine.settings}
              pauseTasks={engine.pauseTasks}
              resumeTasks={engine.resumeTasks}
              revealTask={engine.revealTask}
              deleteTasks={engine.deleteTasks}
              clearCompleted={engine.clearCompleted}
              showToast={triggerToast}
            />
          ) : (
            <ConfigurationView
              settings={engine.settings}
              saveSettingsAction={engine.saveSettingsAction}
              openBrowserExtensionFolder={engine.openBrowserExtensionFolder}
              isSaving={engine.isSaving}
              saveState={engine.saveState}
              showToast={triggerToast}
            />
          )}
        </main>
      </div>

      {/* Regular toast alerts */}
      <div className={`toast ${showToast ? "show" : ""}`} id="toast">
        <Info size={18} />
        <span id="toast-text">{toastMessage}</span>
      </div>

      {/* Backend Startup log status toast */}
      {showStartupToast && (
        <div className={`startup-toast visible ${startupSuccess ? "success" : ""}`} id="startup-toast">
          <div className="startup-toast-header">
            {startupSuccess ? (
              <div className="startup-toast-success-icon">
                <Check size={14} />
              </div>
            ) : (
              <div className="startup-toast-spinner" />
            )}
            <span className="startup-toast-title">
              {startupSuccess ? "Engine Ready" : "Engine Launching..."}
            </span>
          </div>
          <div className="startup-toast-body" id="startup-toast-log">
            {startupSuccess ? "Connected to core services." : lastLog}
          </div>
        </div>
      )}
    </>
  );
}
export default App;
