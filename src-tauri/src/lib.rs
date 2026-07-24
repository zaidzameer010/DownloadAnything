use tauri::Manager;
#[cfg(not(debug_assertions))]
use tauri::Emitter;
use tauri::path::BaseDirectory;
use tauri_plugin_updater::UpdaterExt;

#[cfg(not(debug_assertions))]
struct BackendChild(std::sync::Mutex<std::process::Child>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_updater::Builder::new().build())
    .plugin(tauri_plugin_dialog::init())
    .invoke_handler(tauri::generate_handler![
      detect_installed_browsers,
      install_extension_for_browser
    ])
    .setup(|app| {
      // Build application menu
      let default_menu = tauri::menu::Menu::default(app.handle())?;
      let check_updates_item = tauri::menu::MenuItemBuilder::new("Check for Updates...")
          .id("check-updates")
          .build(app.handle())?;

      let mut added = false;
      if let Ok(items) = default_menu.items() {
          if cfg!(target_os = "macos") {
              // On macOS, insert into the first submenu (the App menu)
              if let Some(tauri::menu::MenuItemKind::Submenu(app_submenu)) = items.first() {
                  let _ = app_submenu.prepend(&check_updates_item);
                  let _ = app_submenu.prepend(&tauri::menu::PredefinedMenuItem::separator(app.handle())?);
                  added = true;
              }
          } else {
              // On Windows, look for a submenu titled "Help"
              for item in &items {
                  if let tauri::menu::MenuItemKind::Submenu(submenu) = item {
                      if let Ok(title) = submenu.text() {
                          if title.to_lowercase().contains("help") {
                              let _ = submenu.prepend(&check_updates_item);
                              let _ = submenu.prepend(&tauri::menu::PredefinedMenuItem::separator(app.handle())?);
                              added = true;
                              break;
                          }
                      }
                  }
              }
          }
      }

      if !added {
          let updates_menu = tauri::menu::SubmenuBuilder::new(app.handle(), "Help")
              .item(&check_updates_item)
              .build()?;
          let _ = default_menu.append(&updates_menu);
      }

      app.set_menu(default_menu)?;
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }

      // Spawn the Python backend in release mode
      #[cfg(not(debug_assertions))]
      {
        let binary_name = if cfg!(target_os = "windows") { "main.exe" } else { "main" };
        let backend_path = app
          .path()
          .resolve(format!("backend/{}", binary_name), BaseDirectory::Resource)?;

        if backend_path.exists() {
          use std::process::{Command, Stdio};
          use std::io::{BufReader, BufRead};

          let mut cmd = Command::new(&backend_path);
          cmd.env("DOWNLOADER_VERSION", app.package_info().version.to_string());
          #[cfg(target_os = "windows")]
          {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
          }

          let mut child = cmd
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

          let stdout = child.stdout.take().ok_or("Failed to open backend stdout")?;
          let stderr = child.stderr.take().ok_or("Failed to open backend stderr")?;

          let app_handle = app.handle().clone();
          tauri::async_runtime::spawn(async move {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
              if let Ok(line) = line {
                log::info!("Backend stdout: {}", line);
                let _ = app_handle.emit("backend-log", line);
              }
            }
          });

          let app_handle = app.handle().clone();
          tauri::async_runtime::spawn(async move {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
              if let Ok(line) = line {
                log::error!("Backend stderr: {}", line);
                let _ = app_handle.emit("backend-log", line);
              }
            }
          });

          app.manage(BackendChild(std::sync::Mutex::new(child)));
        } else {
          log::error!("Backend binary not found at {:?}", backend_path);
        }
      }

      // Spawn background auto-updater task (non-blocking dialogs)
      let handle = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        log::info!("Checking for updates...");
        match handle.updater() {
          Ok(updater) => {
            match updater.check().await {
              Ok(Some(update)) => {
                log::info!("Found update: {}. Prompting user...", update.version);
                use tauri_plugin_dialog::DialogExt;
                let message = format!(
                    "A new version ({}) is available. Would you like to download and install it now?",
                    update.version
                );
                let (tx, rx) = tokio::sync::oneshot::channel::<bool>();
                handle.dialog()
                    .message(&message)
                    .title("Update Available")
                    .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                    .buttons(tauri_plugin_dialog::MessageDialogButtons::YesNo)
                    .show(move |confirmed| {
                      let _ = tx.send(confirmed);
                    });
                let confirmed = rx.await.unwrap_or(false);

                if confirmed {
                    log::info!("User confirmed update. Downloading and installing...");
                    if let Err(e) = update.download_and_install(|chunk_len, total_len| {
                      if let Some(total) = total_len {
                        log::info!("Downloading update: {}/{} bytes", chunk_len, total);
                      }
                    }, || {
                      log::info!("Update downloaded, installing...");
                    }).await {
                      log::error!("Failed to download and install update: {:?}", e);
                      let (txe, rxe) = tokio::sync::oneshot::channel::<bool>();
                      handle.dialog()
                          .message(format!("Failed to install update: {:?}", e))
                          .title("Update Error")
                          .kind(tauri_plugin_dialog::MessageDialogKind::Error)
                          .show(move |_| { let _ = txe.send(true); });
                      let _ = rxe.await;
                    } else {
                      log::info!("Update successfully installed! Relaunching app...");
                      let (txs, rxs) = tokio::sync::oneshot::channel::<bool>();
                      handle.dialog()
                          .message("Update installed successfully. The application will now restart.")
                          .title("Update Success")
                          .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                          .show(move |_| { let _ = txs.send(true); });
                      let _ = rxs.await;
                      handle.restart();
                    }
                }
              }
              Ok(None) => {
                log::info!("No updates available.");
              }
              Err(e) => {
                log::error!("Failed to check for updates: {:?}", e);
              }
            }
          }
          Err(e) => {
            log::error!("Failed to get updater instance: {:?}", e);
          }
        }
      });

      Ok(())
    })
    .on_menu_event(|app_handle, event| {
        if event.id().as_ref() == "check-updates" {
            let handle = app_handle.clone();
            tauri::async_runtime::spawn(async move {
                use tauri_plugin_dialog::DialogExt;
                use tauri_plugin_updater::UpdaterExt;

                async fn ask_yes_no(handle: &tauri::AppHandle, title: &str, message: &str) -> bool {
                    let (tx, rx) = tokio::sync::oneshot::channel::<bool>();
                    handle.dialog()
                        .message(message)
                        .title(title)
                        .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                        .buttons(tauri_plugin_dialog::MessageDialogButtons::YesNo)
                        .show(move |confirmed| { let _ = tx.send(confirmed); });
                    rx.await.unwrap_or(false)
                }

                async fn show_info(handle: &tauri::AppHandle, title: &str, message: &str) {
                    let (tx, rx) = tokio::sync::oneshot::channel::<bool>();
                    handle.dialog()
                        .message(message)
                        .title(title)
                        .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                        .show(move |_| { let _ = tx.send(true); });
                    let _ = rx.await;
                }

                async fn show_error(handle: &tauri::AppHandle, title: &str, message: &str) {
                    let (tx, rx) = tokio::sync::oneshot::channel::<bool>();
                    handle.dialog()
                        .message(message)
                        .title(title)
                        .kind(tauri_plugin_dialog::MessageDialogKind::Error)
                        .show(move |_| { let _ = tx.send(true); });
                    let _ = rx.await;
                }

                match handle.updater() {
                    Ok(updater) => {
                        match updater.check().await {
                            Ok(Some(update)) => {
                                let message = format!(
                                    "A new version ({}) is available. Would you like to download and install it now?",
                                    update.version
                                );
                                let confirmed = ask_yes_no(&handle, "Update Available", &message).await;

                                if confirmed {
                                    if let Err(e) = update.download_and_install(|_, _| {}, || {}).await {
                                        show_error(&handle, "Update Error", &format!("Failed to install update: {:?}", e)).await;
                                    } else {
                                        show_info(&handle, "Update Success", "Update installed successfully. The application will now restart.").await;
                                        handle.restart();
                                    }
                                }
                            }
                            Ok(None) => {
                                show_info(&handle, "No Updates Available", "You are already running the latest version.").await;
                            }
                            Err(e) => {
                                show_error(&handle, "Update Error", &format!("Failed to check for updates: {:?}", e)).await;
                            }
                        }
                    }
                    Err(e) => {
                        show_error(&handle, "Update Error", &format!("Failed to retrieve updater instance: {:?}", e)).await;
                    }
                }
            });
        }
    })
    .build(tauri::generate_context!())
    .expect("error while building tauri application")
    .run(|_app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            #[cfg(not(debug_assertions))]
            if let Some(child_state) = _app_handle.try_state::<BackendChild>() {
                if let Ok(mut child) = child_state.0.lock() {
                    // Graceful stop: try SIGTERM on Unix, wait briefly, then SIGKILL.
                    #[cfg(unix)]
                    {
                        let pid = child.id();
                        if pid != 0 {
                            unsafe {
                                libc::kill(pid as libc::pid_t, libc::SIGTERM);
                            }
                        }
                    }
                    #[cfg(not(unix))]
                    {
                        let _ = child.kill();
                    }
                    match child.try_wait() {
                        Ok(Some(_)) => {}
                        Ok(None) => {
                            // Wait up to ~2s for exit.
                            for _ in 0..20 {
                                std::thread::sleep(std::time::Duration::from_millis(100));
                                if let Ok(Some(_)) = child.try_wait() {
                                    break;
                                }
                            }
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                        Err(_) => {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        }
    });
}

#[derive(serde::Serialize)]
pub struct BrowserInfo {
  name: String,
  key: String,
  installed: bool,
  extensions_url: String,
}

struct BrowserDef {
  name: &'static str,
  key: &'static str,
  macos_app_names: &'static [&'static str],
  windows_exe_paths: &'static [&'static str],
  extensions_url: &'static str,
}

const BROWSERS: [BrowserDef; 4] = [
  BrowserDef {
    name: "Google Chrome",
    key: "chrome",
    macos_app_names: &["Google Chrome"],
    windows_exe_paths: &[
      "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
      "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ],
    extensions_url: "chrome://extensions",
  },
  BrowserDef {
    name: "Brave Browser",
    key: "brave",
    macos_app_names: &["Brave Browser"],
    windows_exe_paths: &[
      "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
      "C:\\Program Files (x86)\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
    ],
    extensions_url: "brave://extensions",
  },
  BrowserDef {
    name: "Mozilla Firefox",
    key: "firefox",
    macos_app_names: &["Firefox"],
    windows_exe_paths: &[
      "C:\\Program Files\\Mozilla Firefox\\firefox.exe",
      "C:\\Program Files (x86)\\Mozilla Firefox\\firefox.exe",
    ],
    extensions_url: "about:debugging",
  },
  BrowserDef {
    name: "Ark",
    key: "ark",
    macos_app_names: &["Ark", "Ark Browser", "Arc", "Arc Browser"],
    windows_exe_paths: &[
      "C:\\Program Files\\Ark\\Application\\ark.exe",
      "C:\\Program Files (x86)\\Ark\\Application\\ark.exe",
      "C:\\Program Files\\Arc\\Application\\arc.exe",
      "C:\\Program Files (x86)\\Arc\\Application\\arc.exe",
    ],
    extensions_url: "chrome://extensions",
  },
];

fn find_macos_browser_name<'a>(names: &'a [&'a str]) -> Option<&'a str> {
  names.iter().find(|name| {
    std::path::PathBuf::from(format!("/Applications/{}.app", name)).exists()
  }).copied()
}

fn find_windows_browser_path<'a>(paths: &'a [&'a str]) -> Option<&'a str> {
  paths.iter().find(|path| std::path::Path::new(path).exists()).copied()
}

fn get_extension_persistent_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
  let app_dir = app
    .path()
    .app_data_dir()
    .map_err(|err| err.to_string())?;
  
  let ext_dir = app_dir.join("extension");
  std::fs::create_dir_all(&ext_dir).map_err(|err| err.to_string())?;
  Ok(ext_dir)
}

fn copy_dir_all(src: impl AsRef<std::path::Path>, dst: impl AsRef<std::path::Path>) -> std::io::Result<()> {
  std::fs::create_dir_all(&dst)?;
  for entry in std::fs::read_dir(src)? {
    let entry = entry?;
    let ty = entry.file_type()?;
    if ty.is_dir() {
      copy_dir_all(entry.path(), dst.as_ref().join(entry.file_name()))?;
    } else {
      std::fs::copy(entry.path(), dst.as_ref().join(entry.file_name()))?;
    }
  }
  Ok(())
}

fn copy_to_clipboard(text: &str) -> Result<(), String> {
  #[cfg(target_os = "macos")]
  {
    use std::io::Write;
    let mut child = std::process::Command::new("pbcopy")
      .stdin(std::process::Stdio::piped())
      .spawn()
      .map_err(|err| err.to_string())?;
    
    if let Some(mut stdin) = child.stdin.take() {
      stdin.write_all(text.as_bytes()).map_err(|err| err.to_string())?;
    }
    child.wait().map_err(|err| err.to_string())?;
  }

  #[cfg(target_os = "windows")]
  {
    use std::io::Write;
    let mut child = std::process::Command::new("clip")
      .stdin(std::process::Stdio::piped())
      .spawn()
      .map_err(|err| err.to_string())?;
    
    if let Some(mut stdin) = child.stdin.take() {
      stdin.write_all(text.as_bytes()).map_err(|err| err.to_string())?;
    }
    child.wait().map_err(|err| err.to_string())?;
  }

  Ok(())
}

#[tauri::command]
fn detect_installed_browsers() -> Result<Vec<BrowserInfo>, String> {
  let mut browsers = Vec::new();

  for def in &BROWSERS {
    let installed = if cfg!(target_os = "macos") {
      find_macos_browser_name(def.macos_app_names).is_some()
    } else if cfg!(target_os = "windows") {
      find_windows_browser_path(def.windows_exe_paths).is_some()
    } else {
      false
    };

    browsers.push(BrowserInfo {
      name: def.name.to_string(),
      key: def.key.to_string(),
      installed,
      extensions_url: def.extensions_url.to_string(),
    });
  }

  Ok(browsers)
}

#[tauri::command]
fn install_extension_for_browser(app: tauri::AppHandle, browser_key: String) -> Result<String, String> {
  let extension_src = app
    .path()
    .resolve("browser-extension", BaseDirectory::Resource)
    .map_err(|err| err.to_string())?;

  let extension_dst = get_extension_persistent_path(&app)?;

  copy_dir_all(&extension_src, &extension_dst).map_err(|err| format!("Failed to copy files: {}", err))?;

  let dst_path_str = extension_dst.to_string_lossy().to_string();

  copy_to_clipboard(&dst_path_str)?;

  let def = BROWSERS.iter().find(|d| d.key == browser_key).ok_or("Unknown browser")?;

  #[cfg(target_os = "macos")]
  {
    if let Some(app_name) = find_macos_browser_name(def.macos_app_names) {
      let mut cmd = std::process::Command::new("open");
      cmd.arg("-a").arg(app_name).arg("--args").arg(def.extensions_url);
      if def.key != "firefox" {
        cmd.arg(format!("--load-extension={}", dst_path_str));
      }
      let _ = cmd.spawn();
    }
  }

  #[cfg(target_os = "windows")]
  {
    if let Some(exe_path) = find_windows_browser_path(def.windows_exe_paths) {
      let mut cmd = std::process::Command::new(exe_path);
      cmd.arg(def.extensions_url);
      if def.key != "firefox" {
        cmd.arg(format!("--load-extension={}", dst_path_str));
      }
      let _ = cmd.spawn();
    }
  }

  Ok(dst_path_str)
}

