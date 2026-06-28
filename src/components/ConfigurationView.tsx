import React, { useState, useEffect, useTransition } from "react";
import { Sliders, Terminal, FolderHeart, Save, Plus, Trash2 } from "lucide-react";
import type { Settings, MergeFormat, CookiesBrowser } from "../types";

interface ConfigurationViewProps {
  readonly settings: Settings;
  readonly saveSettingsAction: (formData: Settings) => void;
  readonly isSaving: boolean;
  readonly saveState: { readonly success: boolean; readonly error: string | null } | null;
  readonly showToast: (msg: string) => void;
}

export function ConfigurationView({
  settings,
  saveSettingsAction,
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
  const [cookiesBrowser, setCookiesBrowser] = useState<CookiesBrowser>("none");
  const [embedThumbnail, setEmbedThumbnail] = useState(false);
  const [embedSubtitles, setEmbedSubtitles] = useState(false);
  const [subtitleLang, setSubtitleLang] = useState("en");
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
    setCookiesBrowser(settings.cookies_from_browser || "none");
    setEmbedThumbnail(!!settings.embed_thumbnail);
    setEmbedSubtitles(!!settings.embed_subtitles);
    setSubtitleLang(settings.subtitle_language || "en");

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
      cookies_from_browser: cookiesBrowser,
      embed_thumbnail: embedThumbnail,
      embed_subtitles: embedSubtitles,
      subtitle_language: subtitleLang.trim(),
      categories: categoriesRecord,
    };

    startTransition(() => {
      saveSettingsAction(payload);
    });
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
          <Sliders size={15} style={{ marginRight: 10 }} /> Configuration
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
                <div className="form-row">
                  <label>Max Concurrent Downloads</label>
                  <input
                    type="number"
                    min="1"
                    max="16"
                    value={concurrency}
                    onChange={(e) => setConcurrency(parseInt(e.target.value, 10) || 3)}
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
                <div className="form-row">
                  <label>Default Path</label>
                  <input
                    type="text"
                    value={defaultPath}
                    onChange={(e) => setDefaultPath(e.target.value)}
                  />
                </div>
              </div>
            )}

            {/* yt-dlp Settings */}
            {activeSubTab === "ytdlp" && (
              <div className="settings-tab-content active" id="stab-ytdlp">
                <div className="form-row">
                  <label>Concurrent Fragments (-N)</label>
                  <input
                    type="number"
                    min="1"
                    max="64"
                    placeholder="e.g. 16 (default)"
                    value={concurrentFragments}
                    onChange={(e) => setConcurrentFragments(parseInt(e.target.value, 10) || 16)}
                  />
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

                <div className="form-row">
                  <label>Load Cookies From Browser</label>
                  <select
                    value={cookiesBrowser}
                    onChange={(e) => setCookiesBrowser(e.target.value as CookiesBrowser)}
                  >
                    <option value="none">None</option>
                    <option value="chrome">Chrome</option>
                    <option value="firefox">Firefox</option>
                    <option value="safari">Safari</option>
                    <option value="edge">Edge</option>
                    <option value="opera">Opera</option>
                    <option value="brave">Brave</option>
                    <option value="vivaldi">Vivaldi</option>
                  </select>
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

                {embedSubtitles && (
                  <div className="form-row" id="subtitle-lang-row">
                    <label>Subtitle Language Code</label>
                    <input
                      type="text"
                      placeholder="e.g. en, es, fr"
                      value={subtitleLang}
                      onChange={(e) => setSubtitleLang(e.target.value)}
                    />
                  </div>
                )}
              </div>
            )}

            {/* Categories Settings */}
            {activeSubTab === "categories" && (
              <div className="settings-tab-content active" id="stab-categories">
                <div className="cat-list" id="cat-list">
                  {categoriesList.map((item) => (
                    <div key={item.id} className="cat-row">
                      <input
                        type="text"
                        className="cat-name"
                        value={item.name}
                        onChange={(e) => handleCategoryNameChange(item.id, e.target.value)}
                      />
                      <input
                        type="text"
                        className="cat-path"
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
            )}

            <button type="submit" className="primary" id="save-settings" disabled={isSaving}>
              <Save size={16} /> {isSaving ? "Saving..." : "Save Settings"}
            </button>
          </form>
        </div>
      </section>
    </div>
  );
}
export default ConfigurationView;
