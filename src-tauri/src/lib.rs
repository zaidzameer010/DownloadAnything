use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;

#[cfg(desktop)]
use tauri::{Emitter, Manager, RunEvent};
#[cfg(desktop)]
use tauri::path::BaseDirectory;

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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_updater::Builder::new().build())
    .plugin(tauri_plugin_process::init())
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

        if let Some(path) = resolve_user_path() {
          command.env("PATH", &path);
          log::info!("Using login-shell PATH for backend");
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
      }

      Ok(())
    })
    .build(tauri::generate_context!())
    .expect("error while building tauri application")
    .run(|app, event| {
      #[cfg(desktop)]
      match event {
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
