use tauri::Manager;
#[cfg(not(debug_assertions))]
use tauri::Emitter;
use tauri::path::BaseDirectory;
use tauri_plugin_updater::UpdaterExt;

#[cfg(not(debug_assertions))]
struct BackendChild(std::sync::Mutex<std::process::Child>);

#[tauri::command]
fn open_browser_extension_folder(app: tauri::AppHandle) -> Result<(), String> {
  let extension_path = app
    .path()
    .resolve("browser-extension", BaseDirectory::Resource)
    .map_err(|err| err.to_string())?;
  let extension_path = extension_path.to_string_lossy().to_string();
  let command = if cfg!(target_os = "macos") {
    std::process::Command::new("open").arg(&extension_path).spawn()
  } else if cfg!(target_os = "windows") {
    std::process::Command::new("explorer").arg(&extension_path).spawn()
  } else {
    std::process::Command::new("xdg-open").arg(&extension_path).spawn()
  };
  command.map_err(|err| err.to_string())?;
  Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_updater::Builder::new().build())
    .plugin(tauri_plugin_dialog::init())
    .invoke_handler(tauri::generate_handler![
      open_browser_extension_folder,
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
              // On Windows/Linux, look for a submenu titled "Help"
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

      // Spawn background auto-updater task
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
                let confirmed = handle.dialog()
                    .message(&message)
                    .title("Update Available")
                    .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                    .buttons(tauri_plugin_dialog::MessageDialogButtons::YesNo)
                    .blocking_show();

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
                      let _ = handle.dialog()
                          .message(&format!("Failed to install update: {:?}", e))
                          .title("Update Error")
                          .kind(tauri_plugin_dialog::MessageDialogKind::Error)
                          .blocking_show();
                    } else {
                      log::info!("Update successfully installed! Relaunching app...");
                      let _ = handle.dialog()
                          .message("Update installed successfully. The application will now restart.")
                          .title("Update Success")
                          .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                          .blocking_show();
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

                match handle.updater() {
                    Ok(updater) => {
                        match updater.check().await {
                            Ok(Some(update)) => {
                                let message = format!(
                                    "A new version ({}) is available. Would you like to download and install it now?",
                                    update.version
                                );
                                let confirmed = handle.dialog()
                                    .message(&message)
                                    .title("Update Available")
                                    .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                                    .buttons(tauri_plugin_dialog::MessageDialogButtons::YesNo)
                                    .blocking_show();

                                if confirmed {
                                    if let Err(e) = update.download_and_install(|_, _| {}, || {}).await {
                                        let _ = handle.dialog()
                                            .message(&format!("Failed to install update: {:?}", e))
                                            .title("Update Error")
                                            .kind(tauri_plugin_dialog::MessageDialogKind::Error)
                                            .blocking_show();
                                    } else {
                                        let _ = handle.dialog()
                                            .message("Update installed successfully. The application will now restart.")
                                            .title("Update Success")
                                            .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                                            .blocking_show();
                                        let _ = handle.restart();
                                    }
                                }
                            }
                            Ok(None) => {
                                let _ = handle.dialog()
                                    .message("You are already running the latest version.")
                                    .title("No Updates Available")
                                    .kind(tauri_plugin_dialog::MessageDialogKind::Info)
                                    .blocking_show();
                            }
                            Err(e) => {
                                let _ = handle.dialog()
                                    .message(&format!("Failed to check for updates: {:?}", e))
                                    .title("Update Error")
                                    .kind(tauri_plugin_dialog::MessageDialogKind::Error)
                                    .blocking_show();
                            }
                        }
                    }
                    Err(e) => {
                        let _ = handle.dialog()
                            .message(&format!("Failed to retrieve updater instance: {:?}", e))
                            .title("Update Error")
                            .kind(tauri_plugin_dialog::MessageDialogKind::Error)
                            .blocking_show();
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
                    let _ = child.kill();
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

  #[cfg(target_os = "linux")]
  {
    use std::io::Write;
    if let Ok(mut child) = std::process::Command::new("xclip")
      .arg("-selection")
      .arg("clipboard")
      .stdin(std::process::Stdio::piped())
      .spawn()
    {
      if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(text.as_bytes());
      }
      let _ = child.wait();
    }
  }

  Ok(())
}

#[tauri::command]
fn detect_installed_browsers() -> Result<Vec<BrowserInfo>, String> {
  let mut browsers = Vec::new();

  // Chrome
  let chrome_installed = if cfg!(target_os = "macos") {
    std::path::Path::new("/Applications/Google Chrome.app").exists()
  } else if cfg!(target_os = "windows") {
    std::path::Path::new("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe").exists()
      || std::path::Path::new("C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe").exists()
  } else {
    std::process::Command::new("which").arg("google-chrome").output().is_ok()
  };

  browsers.push(BrowserInfo {
    name: "Google Chrome".to_string(),
    key: "chrome".to_string(),
    installed: chrome_installed,
    extensions_url: "chrome://extensions".to_string(),
  });

  // Edge
  let edge_installed = if cfg!(target_os = "macos") {
    std::path::Path::new("/Applications/Microsoft Edge.app").exists()
  } else if cfg!(target_os = "windows") {
    std::path::Path::new("C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe").exists()
  } else {
    std::process::Command::new("which").arg("microsoft-edge").output().is_ok()
  };

  browsers.push(BrowserInfo {
    name: "Microsoft Edge".to_string(),
    key: "edge".to_string(),
    installed: edge_installed,
    extensions_url: "edge://extensions".to_string(),
  });

  // Brave
  let brave_installed = if cfg!(target_os = "macos") {
    std::path::Path::new("/Applications/Brave Browser.app").exists()
  } else if cfg!(target_os = "windows") {
    std::path::Path::new("C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe").exists()
  } else {
    std::process::Command::new("which").arg("brave-browser").output().is_ok()
  };

  browsers.push(BrowserInfo {
    name: "Brave Browser".to_string(),
    key: "brave".to_string(),
    installed: brave_installed,
    extensions_url: "brave://extensions".to_string(),
  });

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

  #[cfg(target_os = "macos")]
  {
    let app_name = match browser_key.as_str() {
      "chrome" => "Google Chrome",
      "edge" => "Microsoft Edge",
      "brave" => "Brave Browser",
      _ => "Google Chrome",
    };
    let extensions_url = match browser_key.as_str() {
      "chrome" => "chrome://extensions",
      "edge" => "edge://extensions",
      "brave" => "brave://extensions",
      _ => "chrome://extensions",
    };
    let _ = std::process::Command::new("open")
      .arg("-a")
      .arg(app_name)
      .arg(extensions_url)
      .arg("--args")
      .arg(format!("--load-extension={}", dst_path_str))
      .spawn();
  }

  #[cfg(target_os = "windows")]
  {
    let exec_path = match browser_key.as_str() {
      "chrome" => vec![
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
      ],
      "edge" => vec!["C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"],
      "brave" => vec!["C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"],
      _ => vec!["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"],
    };
    let extensions_url = match browser_key.as_str() {
      "chrome" => "chrome://extensions",
      "edge" => "edge://extensions",
      "brave" => "brave://extensions",
      _ => "chrome://extensions",
    };
    
    let mut launched = false;
    for path in exec_path {
      if std::path::Path::new(path).exists() {
        let _ = std::process::Command::new(path)
          .arg(extensions_url)
          .arg(format!("--load-extension={}", dst_path_str))
          .spawn();
        launched = true;
        break;
      }
    }
    
    if !launched {
      let _ = std::process::Command::new("cmd")
        .args(&["/C", "start", extensions_url])
        .spawn();
    }
  }

  #[cfg(target_os = "linux")]
  {
    let exec_name = match browser_key.as_str() {
      "chrome" => "google-chrome",
      "edge" => "microsoft-edge",
      "brave" => "brave-browser",
      _ => "google-chrome",
    };
    let extensions_url = match browser_key.as_str() {
      "chrome" => "chrome://extensions",
      "edge" => "edge://extensions",
      "brave" => "brave://extensions",
      _ => "chrome://extensions",
    };
    let _ = std::process::Command::new(exec_name)
      .arg(extensions_url)
      .arg(format!("--load-extension={}", dst_path_str))
      .spawn();
  }

  Ok(dst_path_str)
}

