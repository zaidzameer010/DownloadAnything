//! Tauri shell — spawns the Python backend (src-py) and serves the UI.
//!
//! Release builds run the venv + CPython + downloadanything_rs staged under
//! `src-tauri/resources` by `scripts/bundle-backend.ts`; dev builds run
//! `src-py/.venv`. The UI talks to the backend over one WebSocket
//! (token+port via `backend_info`, which reads the settings row in downloadanything.db
//! the Python backend owns).

pub mod settings;

use serde_json::Value;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
#[cfg(target_os = "macos")]
use tauri::menu::AboutMetadata;
use tauri::menu::{MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::{Emitter, Manager, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

pub struct ShellState {
    pub backend: Mutex<Option<Child>>,
    /// .torrent paths the OS handed us before the webview was listening —
    /// the UI drains them once on mount via `take_pending_torrents`.
    pub pending_torrents: Mutex<Vec<String>>,
}

/// Serializes updater runs: the startup check and the menu item can overlap,
/// and two installers writing the bundle at once would corrupt it.
static UPDATE_CHECK: tauri::async_runtime::Mutex<()> = tauri::async_runtime::Mutex::const_new(());

/// Version installed this session — a second check before restarting would
/// otherwise re-download the same release (the running binary is still old).
static INSTALLED_UPDATE: Mutex<Option<String>> = Mutex::new(None);

#[tauri::command]
fn backend_info() -> Result<Value, String> {
    let b = settings::backend_info();
    Ok(serde_json::json!({
        "token": b.token,
        "port": b.port,
    }))
}

#[tauri::command]
fn restart_app(app: tauri::AppHandle) {
    app.restart();
}

/// Extension pushes land in the webview, which can't unhide the app by itself:
/// show() undoes a macOS Cmd-H / Windows tray hide before the window raises.
#[tauri::command]
fn focus_window(app: tauri::AppHandle) {
    raise_main_window(&app);
}

fn raise_main_window(app: &tauri::AppHandle) {
    // AppHandle::show (unhide the whole app) exists only on macOS; on
    // Windows the window-level show/unminimize below covers it.
    #[cfg(target_os = "macos")]
    let _ = app.show();
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// .torrent paths opened while the webview wasn't listening yet (cold start).
#[tauri::command]
fn take_pending_torrents(state: State<'_, ShellState>) -> Vec<String> {
    std::mem::take(&mut *state.pending_torrents.lock().unwrap())
}

/// Reads an OS-opened .torrent for the inspect card. 8 MiB matches the
/// picked-file limit — real .torrent files are KBs to a few MB.
#[tauri::command]
fn read_torrent_file(path: String) -> Result<Vec<u8>, String> {
    let p = Path::new(&path);
    if !p.extension().is_some_and(|e| e == "torrent") {
        return Err("not a .torrent file".into());
    }
    let meta = std::fs::metadata(p).map_err(|e| format!("could not read {path}: {e}"))?;
    if meta.len() > 8 * 1024 * 1024 {
        return Err("torrent file too large".into());
    }
    std::fs::read(p).map_err(|e| format!("could not read {path}: {e}"))
}

fn queue_torrent_paths(app: &tauri::AppHandle, paths: Vec<String>) {
    if paths.is_empty() {
        return;
    }
    let state: State<'_, ShellState> = app.state();
    state.pending_torrents.lock().unwrap().extend(paths.iter().cloned());
    let _ = app.emit("downloadanything-torrent-files", paths);
    raise_main_window(app);
}

fn is_torrent_path(s: &str) -> bool {
    s.to_ascii_lowercase().ends_with(".torrent")
}

#[derive(serde::Serialize)]
pub struct BrowserInfo {
    id: String,
    name: String,
    path: String,
    running: bool,
}

#[derive(serde::Serialize)]
pub struct ExtensionSetup {
    path: String,
    browsers: Vec<BrowserInfo>,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InstallOutcome {
    session_load: bool,
    needs_restart: bool,
}

/// Dev layout: `../ext`; bundled layout: `$RESOURCE/extension`.
fn extension_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    let path = if cfg!(debug_assertions) {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../ext")
    } else {
        app.path().resource_dir().ok()?.join("extension")
    };
    path.join("manifest.json").is_file().then_some(path)
}

#[tauri::command]
fn extension_setup(app: tauri::AppHandle) -> Result<ExtensionSetup, String> {
    let path =
        extension_path(&app).ok_or("bundled extension not found — run `bun run backend:bundle`")?;
    Ok(ExtensionSetup {
        path: path.to_string_lossy().into_owned(),
        browsers: detect_browsers(),
    })
}

/// Opens the browser's extensions page and reveals the folder the user must
/// pick. Chrome 137+ blocks `--load-extension`; other Chromium browsers still
/// honour it, so a closed browser gets the extension for that session.
#[tauri::command]
fn install_extension(app: tauri::AppHandle, id: String) -> Result<InstallOutcome, String> {
    let path =
        extension_path(&app).ok_or("bundled extension not found — run `bun run backend:bundle`")?;
    let browser = detect_browsers()
        .into_iter()
        .find(|b| b.id == id)
        .ok_or_else(|| format!("browser not found: {id}"))?;
    let session_load = supports_load_extension(&browser);
    if session_load && browser.running {
        return Ok(InstallOutcome {
            session_load,
            needs_restart: true,
        });
    }
    reveal_path(&path)?;
    if session_load {
        launch_with_extension(&browser, &path)?;
    } else {
        launch_extensions_page(&browser)?;
    }
    if let Err(e) = open_extensions_page(&browser) {
        shell_log(&format!("downloadanything: {e}"));
    }
    Ok(InstallOutcome {
        session_load,
        needs_restart: false,
    })
}

/// Quits the browser, then relaunches it with the extension loaded.
#[tauri::command(async)]
fn restart_browser_with_extension(
    app: tauri::AppHandle,
    id: String,
) -> Result<InstallOutcome, String> {
    let path =
        extension_path(&app).ok_or("bundled extension not found — run `bun run backend:bundle`")?;
    let browser = detect_browsers()
        .into_iter()
        .find(|b| b.id == id)
        .ok_or_else(|| format!("browser not found: {id}"))?;
    if !supports_load_extension(&browser) {
        return Err(format!(
            "{} does not allow the extension to be loaded automatically",
            browser.name
        ));
    }
    quit_browser(&browser)?;
    if !wait_for_exit(Path::new(&browser.path), 50) {
        return Err(format!(
            "could not close {} — quit it and try again",
            browser.name
        ));
    }
    launch_with_extension(&browser, &path)?;
    if let Err(e) = open_extensions_page(&browser) {
        shell_log(&format!("downloadanything: {e}"));
    }
    Ok(InstallOutcome {
        session_load: true,
        needs_restart: false,
    })
}

fn launch_extensions_page(browser: &BrowserInfo) -> Result<(), String> {
    Command::new(&browser.path)
        .arg(extensions_url(browser))
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {}: {e}", browser.name))
}

/// Chromium ignores `chrome://` URLs on the launch command line, so macOS
/// opens the page through AppleScript after the process is up.
#[cfg(target_os = "macos")]
fn open_extensions_page(browser: &BrowserInfo) -> Result<(), String> {
    let status = Command::new("osascript")
        .arg("-e")
        .arg(format!(
            "tell application \"{}\" to open location \"{}\"",
            browser.name,
            extensions_url(browser)
        ))
        .status()
        .map_err(|e| {
            format!(
                "could not open the extensions page in {}: {e}",
                browser.name
            )
        })?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| format!("could not open the extensions page in {}", browser.name))
}

#[cfg(not(target_os = "macos"))]
fn open_extensions_page(_browser: &BrowserInfo) -> Result<(), String> {
    Ok(())
}

fn launch_with_extension(browser: &BrowserInfo, path: &Path) -> Result<(), String> {
    Command::new(&browser.path)
        .arg(extensions_url(browser))
        .arg(format!("--load-extension={}", path.display()))
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {}: {e}", browser.name))
}

fn supports_load_extension(browser: &BrowserInfo) -> bool {
    !matches!(
        browser.id.as_str(),
        "chrome" | "chrome-beta" | "chrome-canary"
    )
}

fn extensions_url(browser: &BrowserInfo) -> &'static str {
    match browser.id.as_str() {
        "edge" => "edge://extensions",
        "opera" => "opera://extensions",
        "vivaldi" => "vivaldi://extensions",
        _ => "chrome://extensions",
    }
}

#[cfg(target_os = "macos")]
fn quit_browser(browser: &BrowserInfo) -> Result<(), String> {
    let status = Command::new("osascript")
        .arg("-e")
        .arg(format!("tell application \"{}\" to quit", browser.name))
        .status()
        .map_err(|e| format!("could not quit {}: {e}", browser.name))?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| format!("could not quit {}", browser.name))
}

#[cfg(target_os = "windows")]
fn quit_browser(browser: &BrowserInfo) -> Result<(), String> {
    let exe = Path::new(&browser.path)
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| format!("bad browser path: {}", browser.path))?;
    Command::new("taskkill")
        .args(["/IM", exe])
        .status()
        .map_err(|e| format!("could not quit {}: {e}", browser.name))?;
    Ok(())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn quit_browser(browser: &BrowserInfo) -> Result<(), String> {
    Err(format!(
        "quitting {} is not supported on this platform",
        browser.name
    ))
}

fn wait_for_exit(path: &Path, attempts: u32) -> bool {
    for _ in 0..attempts {
        if !is_browser_running(path) {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    !is_browser_running(path)
}

#[tauri::command]
fn reveal_extension(app: tauri::AppHandle) -> Result<(), String> {
    let path =
        extension_path(&app).ok_or("bundled extension not found — run `bun run backend:bundle`")?;
    reveal_path(&path)
}

fn reveal_path(path: &Path) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg("-R")
            .arg(path)
            .spawn()
            .map_err(|e| format!("could not reveal the extension folder: {e}"))?;
    }
    #[cfg(target_os = "windows")]
    {
        Command::new("explorer")
            .arg(format!("/select,{}", path.display()))
            .spawn()
            .map_err(|e| format!("could not reveal the extension folder: {e}"))?;
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let _ = path;
    Ok(())
}

#[cfg(target_os = "macos")]
fn is_browser_running(path: &Path) -> bool {
    Command::new("pgrep")
        .arg("-f")
        .arg(path)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(target_os = "windows")]
fn is_browser_running(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
        return false;
    };
    Command::new("tasklist")
        .args(["/FI", &format!("IMAGENAME eq {name}"), "/NH"])
        .output()
        .map(|o| {
            String::from_utf8_lossy(&o.stdout)
                .to_ascii_lowercase()
                .contains(&name.to_ascii_lowercase())
        })
        .unwrap_or(false)
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn is_browser_running(_path: &Path) -> bool {
    false
}

#[cfg(target_os = "macos")]
fn detect_browsers() -> Vec<BrowserInfo> {
    const CANDIDATES: &[(&str, &str, &str, &str)] = &[
        (
            "chrome",
            "Google Chrome",
            "Google Chrome.app",
            "Google Chrome",
        ),
        (
            "chrome-beta",
            "Google Chrome Beta",
            "Google Chrome Beta.app",
            "Google Chrome Beta",
        ),
        (
            "chrome-canary",
            "Google Chrome Canary",
            "Google Chrome Canary.app",
            "Google Chrome Canary",
        ),
        ("chromium", "Chromium", "Chromium.app", "Chromium"),
        (
            "brave",
            "Brave Browser",
            "Brave Browser.app",
            "Brave Browser",
        ),
        (
            "edge",
            "Microsoft Edge",
            "Microsoft Edge.app",
            "Microsoft Edge",
        ),
        ("vivaldi", "Vivaldi", "Vivaldi.app", "Vivaldi"),
        ("opera", "Opera", "Opera.app", "Opera"),
    ];
    let mut roots = vec![PathBuf::from("/Applications")];
    if let Some(home) = std::env::var_os("HOME") {
        roots.push(PathBuf::from(home).join("Applications"));
    }
    let mut out = Vec::new();
    for (id, name, app, exe) in CANDIDATES {
        let found = roots.iter().find_map(|root| {
            let exe_path = root.join(app).join("Contents/MacOS").join(exe);
            exe_path.is_file().then_some(exe_path)
        });
        if let Some(path) = found {
            out.push(BrowserInfo {
                id: (*id).to_owned(),
                name: (*name).to_owned(),
                path: path.to_string_lossy().into_owned(),
                running: is_browser_running(&path),
            });
        }
    }
    out
}

#[cfg(target_os = "windows")]
fn detect_browsers() -> Vec<BrowserInfo> {
    const CANDIDATES: &[(&str, &str, &str)] = &[
        (
            "chrome",
            "Google Chrome",
            r"Google\Chrome\Application\chrome.exe",
        ),
        (
            "chrome-beta",
            "Google Chrome Beta",
            r"Google\Chrome Beta\Application\chrome.exe",
        ),
        (
            "chrome-canary",
            "Google Chrome Canary",
            r"Google\Chrome SxS\Application\chrome.exe",
        ),
        ("chromium", "Chromium", r"Chromium\Application\chrome.exe"),
        (
            "brave",
            "Brave Browser",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
        (
            "edge",
            "Microsoft Edge",
            r"Microsoft\Edge\Application\msedge.exe",
        ),
        ("vivaldi", "Vivaldi", r"Vivaldi\Application\vivaldi.exe"),
        ("opera", "Opera", r"Programs\Opera\opera.exe"),
    ];
    let roots: Vec<PathBuf> = ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]
        .iter()
        .filter_map(|key| std::env::var_os(key))
        .map(PathBuf::from)
        .collect();
    let mut out = Vec::new();
    for (id, name, rel) in CANDIDATES {
        let found = roots.iter().find_map(|root| {
            let path = root.join(rel);
            path.is_file().then_some(path)
        });
        if let Some(path) = found {
            out.push(BrowserInfo {
                id: (*id).to_owned(),
                name: (*name).to_owned(),
                path: path.to_string_lossy().into_owned(),
                running: is_browser_running(&path),
            });
        }
    }
    out
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn detect_browsers() -> Vec<BrowserInfo> {
    Vec::new()
}

/// Bundled layout: `$RESOURCE/{backend,venv,python}`; dev layout: `src-py/.venv`.
fn backend_paths(app: &tauri::AppHandle) -> Option<(PathBuf, PathBuf)> {
    let (python, cwd) = if cfg!(debug_assertions) {
        let src_py = Path::new(env!("CARGO_MANIFEST_DIR")).join("../src-py");
        (src_py.join(venv_python(".venv")), src_py)
    } else {
        let res = app.path().resource_dir().ok()?;
        // Windows' venv python is a redirector that resolves its relative
        // `home` against the cwd; Unix runs the staged interpreter directly.
        let cwd = if cfg!(windows) {
            res.join("venv/Scripts")
        } else {
            res.join("backend")
        };
        (res.join(venv_python("venv")), cwd)
    };
    python.is_file().then_some((python, cwd))
}

fn venv_python(venv: &str) -> PathBuf {
    Path::new(venv).join(if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    })
}

/// `aria2-next` is an externalBin, placed next to the main executable.
fn aria2_sidecar() -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "aria2-next.exe"
    } else {
        "aria2-next"
    };
    let path = std::env::current_exe().ok()?.parent()?.join(name);
    path.is_file().then_some(path)
}

/// `downloadanything:` shell lines also land in downloadanything.log — the
/// bundled app has no terminal.
fn shell_log(msg: &str) {
    eprintln!("{msg}");
    let path = settings::log_path();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        let _ = writeln!(f, "{msg}");
    }
}

/// Runs `venv/bin/python -m downloadanything` from the staged backend (or the dev venv).
/// A backend already answering on the port wins: the spawned uvicorn fails to
/// bind and exits — the existing process keeps serving.
fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    let Some((python, cwd)) = backend_paths(app) else {
        shell_log(
            "downloadanything: backend not found — run `bun run backend:bundle` before building",
        );
        return None;
    };
    // Logs visible in dev terminals; silent in the bundled app (downloadanything.log has them).
    let (out, err) = if cfg!(debug_assertions) {
        (Stdio::inherit(), Stdio::inherit())
    } else {
        (Stdio::null(), Stdio::null())
    };
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "downloadanything"])
        .current_dir(&cwd)
        .stdout(out)
        .stderr(err);
    if !cfg!(debug_assertions) {
        // The bundled interpreter is not registered with the OS — point it at
        // the staged prefix and keep the read-only bundle free of .pyc files.
        if let Ok(res) = app.path().resource_dir() {
            cmd.env("PYTHONHOME", res.join("python"))
                .env("PYTHONPATH", res.join("backend"))
                .env("VIRTUAL_ENV", res.join("venv"))
                .env("PYTHONDONTWRITEBYTECODE", "1");
        }
    }
    if let Some(aria2) = aria2_sidecar() {
        cmd.env("ARIA2_NEXT_PATH", aria2);
    }
    match cmd.spawn() {
        Ok(child) => {
            shell_log(&format!(
                "downloadanything: python backend spawned (pid {})",
                child.id()
            ));
            Some(child)
        }
        Err(e) => {
            shell_log(&format!(
                "downloadanything: python backend spawn failed: {e}"
            ));
            None
        }
    }
}

/// Checks the GitHub release feed, downloads and installs a newer build, then
/// emits `downloadanything-update-ready` — the UI banner owns the restart since it kills the
/// Python backend. Returns the installed version, or None when already current.
async fn check_update(app: &tauri::AppHandle) -> Result<Option<String>, String> {
    let _guard = UPDATE_CHECK
        .try_lock()
        .map_err(|_| "an update check is already running".to_string())?;
    let Some(update) = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?
    else {
        return Ok(None);
    };
    let version = update.version.clone();
    if INSTALLED_UPDATE.lock().unwrap().as_deref() == Some(version.as_str()) {
        return Ok(Some(version));
    }
    let notes = update.body.clone();
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| e.to_string())?;
    *INSTALLED_UPDATE.lock().unwrap() = Some(version.clone());
    shell_log(&format!("downloadanything: update {version} installed"));
    let _ = app.emit(
        "downloadanything-update-ready",
        serde_json::json!({ "version": version, "notes": notes }),
    );
    Ok(Some(version))
}

/// Menu "Check for Updates…": a manual check reports its outcome natively —
/// the silent startup check only emits the banner.
async fn check_update_manual(app: tauri::AppHandle) {
    match check_update(&app).await {
        Ok(None) => {
            app.dialog()
                .message("DownloadAnything is up to date.")
                .title("Check for Updates")
                .kind(MessageDialogKind::Info)
                .buttons(MessageDialogButtons::Ok)
                .show(|_| {});
        }
        Ok(Some(version)) => {
            let handle = app.clone();
            app.dialog()
                .message(format!(
                    "DownloadAnything {version} has been installed. Restart to finish updating."
                ))
                .title("Update Installed")
                .kind(MessageDialogKind::Info)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Restart Now".into(),
                    "Later".into(),
                ))
                .show(move |restart| {
                    if restart {
                        handle.restart();
                    }
                });
        }
        Err(e) => {
            shell_log(&format!(
                "downloadanything: manual update check failed: {e}"
            ));
            app.dialog()
                .message(format!("Could not check for updates: {e}"))
                .title("Update Check Failed")
                .kind(MessageDialogKind::Error)
                .buttons(MessageDialogButtons::Ok)
                .show(|_| {});
        }
    }
}

/// macOS keeps the app/Edit/View/Window groups from Tauri's default menu and
/// adds the update item to the app menu; Windows gets File/Help with it.
fn build_menu<R: tauri::Runtime>(app: &tauri::App<R>) -> tauri::Result<tauri::menu::Menu<R>> {
    let check_updates =
        MenuItemBuilder::with_id("check-updates", "Check for Updates…").build(app)?;
    #[cfg(target_os = "macos")]
    let menu = {
        let info = app.package_info();
        let about = AboutMetadata {
            name: Some(info.name.clone()),
            version: Some(info.version.to_string()),
            ..Default::default()
        };
        MenuBuilder::new(app)
            .item(
                &SubmenuBuilder::new(app, info.name.clone())
                    .about(Some(about))
                    .separator()
                    .item(&check_updates)
                    .separator()
                    .services()
                    .separator()
                    .hide()
                    .hide_others()
                    .show_all()
                    .separator()
                    .quit()
                    .build()?,
            )
            .item(
                &SubmenuBuilder::new(app, "Edit")
                    .undo()
                    .redo()
                    .separator()
                    .cut()
                    .copy()
                    .paste()
                    .select_all()
                    .build()?,
            )
            .item(&SubmenuBuilder::new(app, "View").fullscreen().build()?)
            .item(
                &SubmenuBuilder::new(app, "Window")
                    .minimize()
                    .maximize()
                    .separator()
                    .close_window()
                    .build()?,
            )
            .build()?
    };
    #[cfg(not(target_os = "macos"))]
    let menu = MenuBuilder::new(app)
        .item(
            &SubmenuBuilder::new(app, "File")
                .quit_with_text("Exit")
                .build()?,
        )
        .item(
            &SubmenuBuilder::new(app, "Help")
                .item(&check_updates)
                .build()?,
        )
        .build()?;
    Ok(menu)
}

/// No public API reports Full Disk Access; opening a TCC-protected file is the
/// standard probe. The system TCC store always exists; Safari's store is a
/// fallback for a future macOS that moves it.
#[cfg(target_os = "macos")]
fn has_full_disk_access() -> bool {
    std::fs::File::open("/Library/Application Support/com.apple.TCC/TCC.db").is_ok()
        || std::env::var_os("HOME")
            .map(|home| std::fs::read_dir(Path::new(&home).join("Library/Safari")).is_ok())
            .unwrap_or(false)
}

/// System Settings' Full Disk Access pane — macOS 13+ uses the extension id,
/// older releases the legacy preference pane.
#[cfg(target_os = "macos")]
fn fda_pane_url(major: u32) -> &'static str {
    if major >= 13 {
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"
    } else {
        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    }
}

#[cfg(target_os = "macos")]
fn open_full_disk_access_settings() {
    let major = Command::new("/usr/bin/sw_vers")
        .arg("-productVersion")
        .output()
        .ok()
        .and_then(|out| String::from_utf8(out.stdout).ok())
        .and_then(|v| v.trim().split('.').next()?.parse::<u32>().ok())
        .unwrap_or(13);
    if let Err(e) = Command::new("/usr/bin/open")
        .arg(fda_pane_url(major))
        .status()
    {
        shell_log(&format!(
            "downloadanything: opening Full Disk Access settings failed: {e}"
        ));
    }
}

/// Native prompt while the app lacks Full Disk Access — browser cookies from
/// Safari and other protected stores need it. macOS has no API to request it,
/// so the dialog only explains and opens the pane. NSAlert is used directly
/// because Tauri's dialog plugin can't set a custom icon.
#[cfg(target_os = "macos")]
fn prompt_full_disk_access() {
    if has_full_disk_access() {
        return;
    }
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSAlert, NSWorkspace};
    use objc2_foundation::NSString;
    let mtm = MainThreadMarker::new().expect("prompt must run on the main thread");
    let alert = NSAlert::new(mtm);
    alert.setMessageText(&NSString::from_str("Full Disk Access Required"));
    alert.setInformativeText(&NSString::from_str(
        "DownloadAnything reads browser cookies for private or age-gated downloads.\n\nEnable DownloadAnything in Privacy & Security → Full Disk Access, then relaunch.",
    ));
    let icon = NSWorkspace::sharedWorkspace().iconForFile(&NSString::from_str("/"));
    // Soundness: `icon` is a valid retained NSImage that outlives the call.
    unsafe { alert.setIcon(Some(&icon)) };
    alert.addButtonWithTitle(&NSString::from_str("Open Settings"));
    alert.runModal();
    open_full_disk_access_settings();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // A Finder/Dock launch inherits launchd's minimal PATH (/usr/bin:/bin:…),
    // not the login shell's — restore it before the backend spawns so yt-dlp
    // finds deno (YouTube n-challenge) and other user-installed tools.
    if let Err(e) = fix_path_env::fix() {
        shell_log(&format!("downloadanything: PATH fix failed: {e}"));
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .manage(ShellState {
            backend: Mutex::new(None),
            // Cold-start double-click on Windows/Linux delivers the file as
            // argv[1]; macOS uses RunEvent::Opened instead.
            pending_torrents: Mutex::new(
                std::env::args()
                    .skip(1)
                    .filter(|a| is_torrent_path(a))
                    .collect(),
            ),
        })
        .setup(move |app| {
            let child = spawn_backend(app.handle());
            let state: State<'_, ShellState> = app.state();
            *state.backend.lock().unwrap() = child;
            let _ = app.set_menu(build_menu(app)?)?;
            app.on_menu_event(|app, event| {
                if event.id() == "check-updates" {
                    tauri::async_runtime::spawn(check_update_manual(app.clone()));
                }
            });
            #[cfg(target_os = "macos")]
            prompt_full_disk_access();
            // Dev builds aren't installable bundles — skip the download entirely.
            if !cfg!(debug_assertions) {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(e) = check_update(&handle).await {
                        shell_log(&format!("downloadanything: update check failed: {e}"));
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_info,
            restart_app,
            focus_window,
            take_pending_torrents,
            read_torrent_file,
            extension_setup,
            install_extension,
            restart_browser_with_extension,
            reveal_extension
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Opened { urls } = &event {
                let paths = urls
                    .iter()
                    .filter(|u| u.scheme() == "file")
                    .filter_map(|u| u.to_file_path().ok())
                    .map(|p| p.to_string_lossy().into_owned())
                    .filter(|p| is_torrent_path(p))
                    .collect::<Vec<_>>();
                queue_torrent_paths(app, paths);
            }
            if let tauri::RunEvent::Exit = event {
                let state: State<'_, ShellState> = app.state();
                let mut backend = state.backend.lock().unwrap();
                if let Some(child) = backend.as_mut() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;

    #[test]
    fn test_fda_pane_url_switches_at_ventura() {
        assert!(fda_pane_url(12).contains("com.apple.preference.security"));
        assert!(fda_pane_url(13).contains("com.apple.settings.PrivacySecurity.extension"));
        assert!(fda_pane_url(26).contains("com.apple.settings.PrivacySecurity.extension"));
    }

    #[test]
    fn detected_browsers_point_at_real_executables() {
        for browser in detect_browsers() {
            assert!(
                Path::new(&browser.path).is_file(),
                "{} points at {}",
                browser.name,
                browser.path
            );
        }
    }
}
