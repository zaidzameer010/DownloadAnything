import React, { useState, useEffect, useTransition } from "react";
import { Sliders, Terminal, FolderHeart, Save, Plus, Trash2 } from "lucide-react";
import type { Settings, MergeFormat } from "../types";

interface ConfigurationViewProps {
  readonly settings: Settings;
  readonly saveSettingsAction: (formData: Settings) => void;
  readonly openBrowserExtensionFolder: () => Promise<void>;
  readonly isSaving: boolean;
  readonly saveState: { readonly success: boolean; readonly error: string | null } | null;
  readonly showToast: (msg: string) => void;
}

export function ConfigurationView({
  settings,
  saveSettingsAction,
  openBrowserExtensionFolder,
  isSaving,
  saveState,
  showToast,
}: ConfigurationViewProps) {
  const [activeSubTab, setActiveSubTab] = useState<"general" | "ytdlp" | "categories">("general");

  // Form State
  const [concurrency, setConcurrency] = useState(3);
  const [mergeFormat, setMergeFormat] = useState<MergeFormat>("mp4");
  const [defaultPath, setDefaultPath] = useState("");
  const [concurrentFragments, setConcurrentFragments] = useState(16);
  const [speedLimit, setSpeedLimit] = useState<string>("");
  const [proxy, setProxy] = useState("");
  const [embedThumbnail, setEmbedThumbnail] = useState(false);
  const [embedSubtitles, setEmbedSubtitles] = useState(false);
  const [subtitleLang, setSubtitleLang] = useState("en");
  const [enableDownloadInterception, setEnableDownloadInterception] = useState(true);
  const [interceptMediaOnly, setInterceptMediaOnly] = useState(false);
  const [useExternalDownloader, setUseExternalDownloader] = useState(true);
  const [cookiesFromBrowser, setCookiesFromBrowser] = useState("");
  const [cookiefilePath, setCookiefilePath] = useState("");
  const [aria2MaxConnectionPerServer, setAria2MaxConnectionPerServer] = useState(16);
  const [aria2Split, setAria2Split] = useState(16);
  const [aria2MaxConcurrentDownloads, setAria2MaxConcurrentDownloads] = useState(16);
  const [aria2MinSplitSize, setAria2MinSplitSize] = useState("1M");
  const [aria2CheckCertificate, setAria2CheckCertificate] = useState(false);
  const [categoriesList, setCategoriesList] = useState<{ id: string; name: string; path: string }[]>([]);

  const [, startTransition] = useTransition();

  // Populate state from settings on mount or settings change
  useEffect(() => {
    setConcurrency(settings.max_concurrent_downloads || 3);
    setMergeFormat(settings.merge_output_format || "mp4");
    setDefaultPath(settings.default_download_path || "");
    setConcurrentFragments(settings.concurrent_fragments || 16);

    const bytes = settings.rate_limit_bytes_per_sec || 0;
    setSpeedLimit(bytes ? String(Math.round(bytes / 1024)) : "");

    setProxy(settings.proxy || "");
    setEmbedThumbnail(!!settings.embed_thumbnail);
    setEmbedSubtitles(!!settings.embed_subtitles);
    setSubtitleLang(settings.subtitle_language || "en");
    setEnableDownloadInterception(settings.enable_download_interception !== false);
    setInterceptMediaOnly(!!settings.intercept_media_only);
    setUseExternalDownloader(settings.use_external_downloader !== false);
    setCookiesFromBrowser(settings.cookies_from_browser || "");
    setCookiefilePath(settings.cookiefile_path || "");
    setAria2MaxConnectionPerServer(settings.aria2_max_connection_per_server ?? 16);
    setAria2Split(settings.aria2_split ?? 16);
    setAria2MaxConcurrentDownloads(settings.aria2_max_concurrent_downloads ?? 16);
    setAria2MinSplitSize(settings.aria2_min_split_size || "1M");
    setAria2CheckCertificate(!!settings.aria2_check_certificate);

    const list = Object.entries(settings.categories || {}).map(([name, path], idx) => ({
      id: `cat-${idx}-${Date.now()}`,
      name,
      path,
    }));
    setCategoriesList(list);
  }, [settings]);

  // Handle Save State notifications
  useEffect(() => {
    if (saveState) {
      if (saveState.success) {
        showToast("Settings saved");
      } else if (saveState.error) {
        showToast(saveState.error);
      }
    }
  }, [saveState, showToast]);

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    const speedLimitKb = parseFloat(speedLimit) || 0;
    const rateLimitBytes = speedLimitKb > 0 ? speedLimitKb * 1024 : 0;

    const categoriesRecord: Record<string, string> = {};
    categoriesList.forEach((item) => {
      const name = item.name.trim();
      if (name) {
        categoriesRecord[name] = item.path;
      }
    });

    const payload: Settings = {
      max_concurrent_downloads: concurrency,
      merge_output_format: mergeFormat,
      default_download_path: defaultPath,
      concurrent_fragments: concurrentFragments,
      rate_limit_bytes_per_sec: rateLimitBytes,
      proxy: proxy.trim(),
      embed_thumbnail: embedThumbnail,
      embed_subtitles: embedSubtitles,
      subtitle_language: subtitleLang.trim(),
      categories: categoriesRecord,
      enable_download_interception: enableDownloadInterception,
      intercept_media_only: interceptMediaOnly,
      use_external_downloader: useExternalDownloader,
      cookies_from_browser: cookiesFromBrowser,
      cookiefile_path: cookiefilePath,
      aria2_max_connection_per_server: aria2MaxConnectionPerServer,
      aria2_split: aria2Split,
      aria2_max_concurrent_downloads: aria2MaxConcurrentDownloads,
      aria2_min_split_size: aria2MinSplitSize.trim(),
      aria2_check_certificate: aria2CheckCertificate,
    };

    startTransition(() => {
      saveSettingsAction(payload);
    });
  };

  const handleOpenExtensionFolder = async () => {
    try {
      await openBrowserExtensionFolder();
      showToast("Opened the bundled browser extension folder");
    } catch (error) {
      console.error("Failed to open browser extension folder:", error);
      showToast("Failed to open the bundled browser extension folder");
    }
  };

  // Categories Operations
  const handleAddCategory = () => {
    const count = categoriesList.length + 1;
    const newName = `Category${count}`;
    setCategoriesList((prev) => [
      ...prev,
      {
        id: `cat-new-${Date.now()}-${Math.random()}`,
        name: newName,
        path: defaultPath,
      },
    ]);
  };

  const handleCategoryNameChange = (id: string, newName: string) => {
    setCategoriesList((prev) =>
      prev.map((item) => (item.id === id ? { ...item, name: newName } : item))
    );
  };

  const handleCategoryPathChange = (id: string, newPath: string) => {
    setCategoriesList((prev) =>
      prev.map((item) => (item.id === id ? { ...item, path: newPath } : item))
    );
  };

  const handleDeleteCategory = (id: string) => {
    setCategoriesList((prev) => prev.filter((item) => item.id !== id));
  };

  return (
    <div className="view-panel active" id="view-settings">
      <section className="card settings-card">
        <h2>
          <Sliders size={15} /> Configuration
        </h2>

        <div className="settings-layout">
          {/* Left Panel Tabs */}
          <div className="settings-tabs">
            <button
              className={`settings-tab-btn ${activeSubTab === "general" ? "active" : ""}`}
              onClick={() => setActiveSubTab("general")}
            >
              <Sliders size={14} /> General
            </button>
            <button
              className={`settings-tab-btn ${activeSubTab === "ytdlp" ? "active" : ""}`}
              onClick={() => setActiveSubTab("ytdlp")}
            >
              <Terminal size={14} /> yt-dlp Settings
            </button>
            <button
              className={`settings-tab-btn ${activeSubTab === "categories" ? "active" : ""}`}
              onClick={() => setActiveSubTab("categories")}
            >
              <FolderHeart size={14} /> Categories
            </button>
          </div>

          {/* Right Panel Content */}
          <form className="settings-content" onSubmit={handleSave}>
            {/* General Settings */}
            {activeSubTab === "general" && (
              <div className="settings-tab-content active" id="stab-general">
                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Browser Extension</h3>
                    <p className="settings-group-desc">
                      Open the bundled extension folder, load it unpacked in Chrome, Edge, or Brave, then enable private mode and file URL access in the browser's extension page.
                    </p>
                  </div>
                  <div className="form-row">
                    <label>Install Extension</label>
                    <button type="button" className="ghost" onClick={handleOpenExtensionFolder}>
                      Open bundled extension folder
                    </button>
                  </div>
                  <div className="field-desc">
                    On macOS this opens Finder. On Windows this opens File Explorer. After loading the unpacked folder, enable the extension in incognito/private windows and allow access to file URLs from the browser's extension details page.
                  </div>
                </div>

                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Download Limits &amp; Queues</h3>
                    <p className="settings-group-desc">Manage concurrency and allocation limits</p>
                  </div>
                  <div className="form-row">
                    <label>Max Concurrent Downloads</label>
                    <div style={{ display: "flex", alignItems: "center", gap: "12px", width: "100%" }}>
                      <input
                        type="range"
                        min="1"
                        max="32"
                        value={concurrency}
                        onChange={(e) => setConcurrency(parseInt(e.target.value, 10) || 3)}
                        style={{ flex: 1 }}
                      />
                      <span className="slider-value" style={{ minWidth: "24px", textAlign: "right", fontWeight: "600", fontSize: "13px", color: "var(--text)" }}>{concurrency}</span>
                    </div>
                  </div>
                </div>

                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Storage &amp; Formats</h3>
                    <p className="settings-group-desc">Configure default destination path and fallback containers</p>
                  </div>
                  <div className="form-row">
                    <label>Default Path</label>
                    <input
                      type="text"
                      value={defaultPath}
                      onChange={(e) => setDefaultPath(e.target.value)}
                    />
                  </div>
                  <div className="form-row">
                    <label>Merge Container</label>
                    <select
                      value={mergeFormat}
                      onChange={(e) => setMergeFormat(e.target.value as MergeFormat)}
                    >
                      <option value="mp4">MP4</option>
                      <option value="mkv">MKV</option>
                      <option value="webm">WebM</option>
                    </select>
                  </div>
                </div>

              </div>
            )}

            {/* yt-dlp Settings */}
            {activeSubTab === "ytdlp" && (
              <div className="settings-tab-content active" id="stab-ytdlp">
                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Network &amp; Speed</h3>
                    <p className="settings-group-desc">Tune performance, connection settings, and transfer limits</p>
                  </div>
                  <div className="form-row">
                    <label>Concurrent Fragments (-N)</label>
                    <div style={{ display: "flex", alignItems: "center", gap: "12px", width: "100%" }}>
                      <input
                        type="range"
                        min="1"
                        max="32"
                        value={concurrentFragments}
                        onChange={(e) => setConcurrentFragments(parseInt(e.target.value, 10) || 16)}
                        style={{ flex: 1 }}
                      />
                      <span className="slider-value" style={{ minWidth: "24px", textAlign: "right", fontWeight: "600", fontSize: "13px", color: "var(--text)" }}>{concurrentFragments}</span>
                    </div>
                  </div>

                  <div className="form-row">
                    <label>Speed Limit (KB/s)</label>
                    <input
                      type="number"
                      min="0"
                      placeholder="e.g. 1024 (0 for unlimited)"
                      value={speedLimit}
                      onChange={(e) => setSpeedLimit(e.target.value)}
                    />
                  </div>

                  <div className="form-row">
                    <label>Proxy Server</label>
                    <input
                      type="text"
                      placeholder="e.g. socks5://127.0.0.1:1080"
                      value={proxy}
                      onChange={(e) => setProxy(e.target.value)}
                    />
                  </div>

                  <div className="toggle-row">
                    <div className="toggle-info">
                      <span className="toggle-label">Use External Downloader (aria2c)</span>
                      <p>Use aria2c for multi-connection fragment downloads</p>
                    </div>
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={useExternalDownloader}
                        onChange={(e) => setUseExternalDownloader(e.target.checked)}
                      />
                      <span className="slider"></span>
                    </label>
                  </div>

                  {useExternalDownloader && (
                    <div className="aria2-settings-container" style={{
                      marginTop: "16px",
                      padding: "16px",
                      background: "rgba(255,255,255,0.02)",
                      border: "1px solid rgba(255,255,255,0.06)",
                      borderRadius: "8px",
                      display: "flex",
                      flexDirection: "column",
                      gap: "12px"
                    }}>
                      <h4 style={{ margin: 0, fontSize: "13px", fontWeight: "600", color: "#f5f5f7" }}>aria2c Downloader Parameters</h4>
                      
                      <div className="form-row">
                        <label>Max Connections Per Server (-x)</label>
                        <div style={{ display: "flex", alignItems: "center", gap: "12px", width: "100%" }}>
                          <input
                            type="range"
                            min="1"
                            max="32"
                            value={aria2MaxConnectionPerServer}
                            onChange={(e) => setAria2MaxConnectionPerServer(parseInt(e.target.value, 10) || 16)}
                            style={{ flex: 1 }}
                          />
                          <span className="slider-value" style={{ minWidth: "24px", textAlign: "right", fontWeight: "600", fontSize: "13px", color: "var(--text)" }}>{aria2MaxConnectionPerServer}</span>
                        </div>
                      </div>

                      <div className="form-row">
                        <label>Split Connections (-s)</label>
                        <div style={{ display: "flex", alignItems: "center", gap: "12px", width: "100%" }}>
                          <input
                            type="range"
                            min="1"
                            max="32"
                            value={aria2Split}
                            onChange={(e) => setAria2Split(parseInt(e.target.value, 10) || 16)}
                            style={{ flex: 1 }}
                          />
                          <span className="slider-value" style={{ minWidth: "24px", textAlign: "right", fontWeight: "600", fontSize: "13px", color: "var(--text)" }}>{aria2Split}</span>
                        </div>
                      </div>

                      <div className="form-row">
                        <label>Max Concurrent Connections (-j)</label>
                        <div style={{ display: "flex", alignItems: "center", gap: "12px", width: "100%" }}>
                          <input
                            type="range"
                            min="1"
                            max="32"
                            value={aria2MaxConcurrentDownloads}
                            onChange={(e) => setAria2MaxConcurrentDownloads(parseInt(e.target.value, 10) || 16)}
                            style={{ flex: 1 }}
                          />
                          <span className="slider-value" style={{ minWidth: "24px", textAlign: "right", fontWeight: "600", fontSize: "13px", color: "var(--text)" }}>{aria2MaxConcurrentDownloads}</span>
                        </div>
                      </div>

                      <div className="form-row">
                        <label>Min Split Size (-k)</label>
                        <input
                          type="text"
                          placeholder="e.g. 1M or 5M"
                          value={aria2MinSplitSize}
                          onChange={(e) => setAria2MinSplitSize(e.target.value)}
                        />
                      </div>

                      <div className="toggle-row" style={{ padding: 0 }}>
                        <div className="toggle-info">
                          <span className="toggle-label" style={{ fontSize: "12px" }}>Verify SSL Certificate</span>
                        </div>
                        <label className="toggle">
                          <input
                            type="checkbox"
                            checked={aria2CheckCertificate}
                            onChange={(e) => setAria2CheckCertificate(e.target.checked)}
                          />
                          <span className="slider"></span>
                        </label>
                      </div>
                    </div>
                  )}
                </div>

                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Authentication &amp; Access</h3>
                    <p className="settings-group-desc">Bypass login screens or access protected video links</p>
                  </div>
                  <div className="form-row">
                    <label>Cookies from Browser</label>
                    <select
                      value={cookiesFromBrowser}
                      onChange={(e) => setCookiesFromBrowser(e.target.value)}
                    >
                      <option value="">None (Don't extract cookies)</option>
                      <option value="chrome">Chrome</option>
                      <option value="firefox">Firefox</option>
                      <option value="safari">Safari</option>
                      <option value="edge">Edge</option>
                      <option value="opera">Opera</option>
                      <option value="brave">Brave</option>
                      <option value="vivaldi">Vivaldi</option>
                    </select>
                  </div>

                  <div className="form-row">
                    <label>Cookies File Path</label>
                    <input
                      type="text"
                      placeholder="e.g. /path/to/cookies.txt"
                      value={cookiefilePath}
                      onChange={(e) => setCookiefilePath(e.target.value)}
                    />
                  </div>
                </div>

                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Post-Processing &amp; Media</h3>
                    <p className="settings-group-desc">Embed artwork and download subtitle files</p>
                  </div>
                  <div className="toggle-row">
                    <div className="toggle-info">
                      <span className="toggle-label">Embed Thumbnail</span>
                      <p>Embed the best video thumbnail artwork as video cover art</p>
                    </div>
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={embedThumbnail}
                        onChange={(e) => setEmbedThumbnail(e.target.checked)}
                      />
                      <span className="slider"></span>
                    </label>
                  </div>

                  <div className="toggle-row">
                    <div className="toggle-info">
                      <span className="toggle-label">Embed Subtitles</span>
                      <p>Download subtitles and embed them directly into video file</p>
                    </div>
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={embedSubtitles}
                        onChange={(e) => setEmbedSubtitles(e.target.checked)}
                      />
                      <span className="slider"></span>
                    </label>
                  </div>

                  <div className={`form-row ${!embedSubtitles ? "disabled" : ""}`}>
                    <label>Subtitle Language Code</label>
                    <span className="field-desc">ISO language code for subtitles (e.g. en, es, fr)</span>
                    <input
                      type="text"
                      placeholder="e.g. en, es, fr"
                      value={subtitleLang}
                      disabled={!embedSubtitles}
                      onChange={(e) => setSubtitleLang(e.target.value)}
                    />
                  </div>
                </div>
              </div>
            )}

            {/* Categories Settings */}
            {activeSubTab === "categories" && (
              <div className="settings-tab-content active" id="stab-categories">
                <div className="settings-group">
                  <div>
                    <h3 className="settings-group-title">Dynamic Folder Routing</h3>
                    <p className="settings-group-desc">Route downloads to custom folders based on category tags</p>
                  </div>
                  <div className="cat-list" id="cat-list">
                    {categoriesList.map((item) => (
                      <div key={item.id} className="cat-row">
                        <input
                          type="text"
                          className="cat-name"
                          placeholder="Category Name"
                          value={item.name}
                          onChange={(e) => handleCategoryNameChange(item.id, e.target.value)}
                        />
                        <input
                          type="text"
                          className="cat-path"
                          placeholder="/absolute/path"
                          value={item.path}
                          onChange={(e) => handleCategoryPathChange(item.id, e.target.value)}
                        />
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => handleDeleteCategory(item.id)}
                          title="Delete Category"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="ghost"
                    id="add-cat"
                    onClick={handleAddCategory}
                  >
                    <Plus size={16} /> Add Category
                  </button>
                </div>
              </div>
            )}

            <button type="submit" className="ghost" id="save-settings" disabled={isSaving}>
              <Save size={16} /> {isSaving ? "Saving..." : "Save Settings"}
            </button>
          </form>
        </div>
      </section>
    </div>
  );
}
export default ConfigurationView;
