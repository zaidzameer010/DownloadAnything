use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

#[cfg(desktop)]
static DEPENDENCIES_CHECKED: AtomicBool = AtomicBool::new(false);

#[cfg(desktop)]
use tauri::{Emitter, Manager, RunEvent};
#[cfg(desktop)]
use tauri::menu::{MenuBuilder, SubmenuBuilder};
#[cfg(desktop)]
use tauri::path::BaseDirectory;
#[cfg(desktop)]
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

#[cfg(desktop)]
struct BackendProcess(Mutex<Option<std::process::Child>>);

#[tauri::command]
fn get_cli_torrent_file() -> Option<String> {
  let args: Vec<String> = std::env::args().collect();
  for arg in args.iter().skip(1) {
    let lower = arg.to_lowercase();
    if lower.ends_with(".torrent") || (arg.starts_with("file://") && lower.ends_with(".torrent")) {
      return Some(arg.clone());
    }
  }
  None
}

/// Resolve the user's login-shell PATH so the backend can find external
/// executables (e.g. Node for yt-dlp's n-challenge solver) when the app
/// is launched from the GUI, where macOS/Linux provide only a minimal PATH.
#[cfg(desktop)]
fn resolve_user_path() -> Option<String> {
  #[cfg(not(unix))]
  return None;

  #[cfg(unix)]
  {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let output = std::process::Command::new(&shell)
      .args(["-l", "-c", "env"])
      .output()
      .ok()?;
    let text = String::from_utf8(output.stdout).ok()?;
    text
      .lines()
      .find_map(|line| line.strip_prefix("PATH="))
      .map(|path| path.to_string())
  }
}

#[cfg(desktop)]
fn find_aria2_sidecar(app_dir: &Path) -> Option<PathBuf> {
  let is_windows = cfg!(windows);
  let entries = std::fs::read_dir(app_dir).ok()?;
  for entry in entries.flatten() {
    let path = entry.path();
    if !path.is_file() {
      continue;
    }
    let name = path.file_name()?.to_str()?.to_lowercase();
    if !name.starts_with("aria2-next") {
      continue;
    }
    if is_windows {
      if !name.ends_with(".exe") {
        continue;
      }
    } else if name.ends_with(".exe") {
      continue;
    }
    return Some(path);
  }
  None
}

#[cfg(desktop)]
fn command_exists(name: &str, extra_path: Option<&str>) -> bool {
  let is_windows = cfg!(windows);
  let mut candidates = Vec::new();

  #[cfg(unix)]
  {
    candidates.push(PathBuf::from(format!("/opt/homebrew/bin/{}", name)));
    candidates.push(PathBuf::from(format!("/usr/local/bin/{}", name)));
    candidates.push(PathBuf::from(format!("/usr/bin/{}", name)));
    candidates.push(PathBuf::from(format!("/bin/{}", name)));
  }

  let mut all_paths = Vec::new();
  if let Some(p) = extra_path {
    all_paths.push(p.to_string());
  }
  if let Ok(p) = std::env::var("PATH") {
    all_paths.push(p);
  }

  let separator = if is_windows { ';' } else { ':' };
  for path_env in all_paths {
    for dir in path_env.split(separator) {
      let trimmed = dir.trim();
      if trimmed.is_empty() {
        continue;
      }
      let dir_path = Path::new(trimmed);
      if is_windows {
        candidates.push(dir_path.join(format!("{}.exe", name)));
        candidates.push(dir_path.join(name));
      } else {
        candidates.push(dir_path.join(name));
      }
    }
  }

  for candidate in candidates {
    if candidate.is_file() {
      return true;
    }
  }

  false
}

#[cfg(desktop)]
fn find_homebrew() -> Option<PathBuf> {
  let candidates = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"];
  for p in candidates {
    let path = PathBuf::from(p);
    if path.is_file() {
      return Some(path);
    }
  }
  None
}

#[cfg(desktop)]
fn ensure_desktop_dependencies<R: tauri::Runtime>(app: &tauri::AppHandle<R>, user_path: Option<&str>) {
  if DEPENDENCIES_CHECKED.swap(true, Ordering::SeqCst) {
    return;
  }

  let has_ffmpeg = command_exists("ffmpeg", user_path);
  let has_deno = command_exists("deno", user_path);

  if has_ffmpeg && has_deno {
    log::info!("Required dependencies (ffmpeg and deno) are already installed.");
    return;
  }

  let mut missing = Vec::new();
  if !has_ffmpeg {
    missing.push("ffmpeg");
  }
  if !has_deno {
    missing.push("deno");
  }

  let missing_names = missing.join(" and ");
  let pm_name = if cfg!(target_os = "macos") {
    if find_homebrew().is_none() {
      "Homebrew (will be installed automatically)"
    } else {
      "Homebrew"
    }
  } else if cfg!(windows) {
    "winget"
  } else {
    "package manager"
  };
  let message = format!(
    "DownloadAnything requires {} for media remuxing and extraction.\n\nWould you like to install {} now using {}?",
    missing_names, missing_names, pm_name
  );

  let confirmed = app.dialog()
    .message(&message)
    .title("Dependencies Required")
    .kind(MessageDialogKind::Info)
    .buttons(MessageDialogButtons::OkCancelCustom("Install".to_string(), "Skip".to_string()))
    .blocking_show();

  if !confirmed {
    log::warn!("User skipped installation of {}", missing_names);
    return;
  }

  #[cfg(target_os = "macos")]
  {
    let missing_str = missing.join(", ");
    let brew_cmd = format!(
      "clear && \
       echo '==================================================' && \
       echo '  DownloadAnything - Dependency Setup' && \
       echo '==================================================' && \
       echo 'Installing required dependencies: {}' && \
       echo '--------------------------------------------------' && \
       if ! command -v brew >/dev/null 2>&1 && [ ! -f /opt/homebrew/bin/brew ] && [ ! -f /usr/local/bin/brew ]; then \
         echo 'Homebrew is not installed. Installing Homebrew first (https://brew.sh)...' && \
         echo 'Please enter your password if prompted by the installer.' && \
         echo '' && \
         /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\" ; \
         if [ -f /opt/homebrew/bin/brew ]; then \
           eval \"$(/opt/homebrew/bin/brew shellenv)\" ; \
         elif [ -f /usr/local/bin/brew ]; then \
           eval \"$(/usr/local/bin/brew shellenv)\" ; \
         fi ; \
       fi ; \
       BREW_BIN=\"brew\" && \
       if ! command -v \"$BREW_BIN\" >/dev/null 2>&1 ; then \
         if [ -f /opt/homebrew/bin/brew ]; then \
           BREW_BIN=\"/opt/homebrew/bin/brew\" ; \
         elif [ -f /usr/local/bin/brew ]; then \
           BREW_BIN=\"/usr/local/bin/brew\" ; \
         fi ; \
       fi ; \
       echo '' && \
       if ! command -v \"$BREW_BIN\" >/dev/null 2>&1 ; then \
         echo 'Homebrew could not be located. Please install Homebrew manually from https://brew.sh' ; \
       else \
         echo 'Installing: {} via Homebrew...' && \
         \"$BREW_BIN\" install {} ; \
       fi ; \
       echo '' && \
       echo '--------------------------------------------------' && \
       echo 'Installation finished.' && \
       echo '' && \
       exit",
      missing_str,
      missing_str,
      missing.join(" ")
    );
    let script = format!("tell application \"Terminal\" to do script \"{}\"", brew_cmd.replace('\\', "\\\\").replace('"', "\\\""));
    if let Err(err) = Command::new("/usr/bin/osascript")
      .args(["-e", "tell application \"Terminal\" to activate", "-e", &script])
      .spawn()
    {
      log::error!("Failed to launch Terminal for Homebrew installation: {err}");
    }
  }

  #[cfg(windows)]
  {
    let mut winget_cmds = Vec::new();
    for item in &missing {
      if *item == "ffmpeg" {
        winget_cmds.push("winget install --exact --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements");
      } else if *item == "deno" {
        winget_cmds.push("winget install --exact --id denoland.deno --accept-source-agreements --accept-package-agreements");
      }
    }
    let missing_str = missing.join(", ");
    let cmd_str = format!(
      "cls & \
       echo ================================================== & \
       echo   DownloadAnything - Dependency Setup & \
       echo ================================================== & \
       echo Installing required dependencies: {} & \
       echo Package Manager: winget & \
       echo Please wait, this may take a few moments... & \
       echo -------------------------------------------------- & \
       echo. & \
       {} & \
       echo. & \
       echo -------------------------------------------------- & \
       echo Installation finished. & \
       pause > nul & \
       exit",
      missing_str,
      winget_cmds.join(" & ")
    );
    let _ = Command::new("cmd.exe")
      .args(["/c", "start", "DownloadAnything - Dependency Setup", "cmd.exe", "/c", &cmd_str])
      .spawn();
  }
}

#[cfg(target_os = "macos")]
fn has_full_disk_access() -> bool {
  const TCC_DATABASE: &str = "/Library/Application Support/com.apple.TCC/TCC.db";
  std::fs::File::open(TCC_DATABASE).is_ok()
}

#[cfg(target_os = "macos")]
fn show_full_disk_access_dialog<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
  if has_full_disk_access() {
    return;
  }

  app.dialog()
    .message(
      "Full Disk Access required to read browser cookies. enable DownloadAnything.",
    )
    .kind(MessageDialogKind::Warning)
    .title("Full Disk Access Required")
    .buttons(MessageDialogButtons::OkCustom(
      "Open System Settings".to_string(),
    ))
    .show(|open_settings| {
      if open_settings {
        if let Err(error) = Command::new("/usr/bin/open")
          .arg("x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles")
          .spawn()
        {
          log::warn!("Unable to open Full Disk Access settings: {error}");
        }
      }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_updater::Builder::new().build())
    .plugin(tauri_plugin_process::init())
    .plugin(tauri_plugin_dialog::init())
    .invoke_handler(tauri::generate_handler![get_cli_torrent_file])
    .setup(|app| {
      #[cfg(desktop)]
      {
        let backend_dir = app.path().resolve("downloadanything-backend", BaseDirectory::Resource)?;
        let program = if cfg!(windows) {
          "downloadanything-backend.exe"
        } else {
          "downloadanything-backend"
        };
        let backend_path = backend_dir.join(program);
        if !backend_path.exists() {
          return Err(format!("Backend executable not found at {}", backend_path.display()).into());
        }

        let mut command = Command::new(&backend_path);
        command.stdout(Stdio::null()).stderr(Stdio::null());

        let user_path = resolve_user_path();

        #[cfg(target_os = "macos")]
        {
          if has_full_disk_access() {
            ensure_desktop_dependencies(app.handle(), user_path.as_deref());
          } else {
            show_full_disk_access_dialog(app.handle());
          }
        }

        #[cfg(not(target_os = "macos"))]
        ensure_desktop_dependencies(app.handle(), user_path.as_deref());

        #[allow(unused_mut)] // mutated only in the unix cfg block below
        let mut final_path = user_path.unwrap_or_default();
        #[cfg(unix)]
        {
          let standard_paths = ["/opt/homebrew/bin", "/usr/local/bin"];
          for sp in standard_paths {
            if !final_path.split(':').any(|p| p == sp) {
              if !final_path.is_empty() {
                final_path = format!("{}:{}", sp, final_path);
              } else {
                final_path = sp.to_string();
              }
            }
          }
        }
        if !final_path.is_empty() {
          command.env("PATH", &final_path);
          log::info!("Using enhanced PATH for backend: {}", final_path);
        }

        if let Some(app_dir) = std::env::current_exe()?.parent() {
          if let Some(aria2_path) = find_aria2_sidecar(app_dir) {
            command.env("ARIA2_NEXT_PATH", &aria2_path);
            log::info!("Using aria2-next sidecar at {}", aria2_path.display());
          } else {
            log::warn!("aria2-next sidecar not found in {}", app_dir.display());
          }
        }

        let child = command.spawn()?;
        app.manage(BackendProcess(Mutex::new(Some(child))));

        // Handle .torrent files passed via CLI (Windows) or at cold start (macOS).
        if let Some(path) = get_cli_torrent_file() {
          let _ = app.emit("open-torrent-file", &path);
        }

        // Build the app menu. On macOS this becomes the app-wide menubar;
        // on Windows/Linux it is shown on the main window.
        let menu = MenuBuilder::new(app)
          .item(
            &SubmenuBuilder::new(app, "DownloadAnything")
              .about(None)
              .text("check-for-updates", "Check for Updates")
              .build()?
          )
          .build()?;
        app.set_menu(menu)?;
      }

      Ok(())
    })
    .on_window_event(|_window, _event| {
      #[cfg(target_os = "macos")]
      if let tauri::WindowEvent::Focused(true) = _event {
        if has_full_disk_access() && !DEPENDENCIES_CHECKED.load(Ordering::SeqCst) {
          let user_path = resolve_user_path();
          ensure_desktop_dependencies(_window.app_handle(), user_path.as_deref());
        }
      }
    })
    .on_menu_event(|app, event| {
      if event.id() == "check-for-updates" {
        let _ = app.emit("check-for-updates", ());
      }
    })
    .build(tauri::generate_context!())
    .expect("error while building tauri application")
    .run(|app, event| {
      #[cfg(desktop)]
      match event {
        // macOS file-association event.
        #[cfg(target_os = "macos")]
        RunEvent::Opened { urls } => {
          for url in urls {
            let path_str = url.to_string();
            if path_str.to_lowercase().ends_with(".torrent") {
              let _ = app.emit("open-torrent-file", &path_str);
            }
          }
        }
        RunEvent::Exit => {
          if let Some(state) = app.try_state::<BackendProcess>() {
            if let Ok(mut process) = state.0.lock() {
              if let Some(mut child) = process.take() {
                let _ = child.kill();
              }
            }
          }
        }
        _ => {}
      }
    });
}
