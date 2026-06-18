use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_updater::UpdaterExt;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_updater::Builder::new().build())
    .plugin(tauri_plugin_dialog::init())
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

      // Spawn the Python sidecar backend.
      // IMPORTANT: `child` must be kept alive for the entire app lifetime —
      // dropping it here would immediately kill the sidecar process.
      let sidecar_command = app.shell().sidecar("main")?;
      let (mut rx, child) = sidecar_command.spawn()?;
      // Store child on app state so it is not dropped
      app.manage(child);

      tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
          match event {
            CommandEvent::Stdout(line_bytes) => {
              let line = String::from_utf8_lossy(&line_bytes);
              log::info!("Sidecar stdout: {}", line);
            }
            CommandEvent::Stderr(line_bytes) => {
              let line = String::from_utf8_lossy(&line_bytes);
              log::error!("Sidecar stderr: {}", line);
            }
            CommandEvent::Terminated(payload) => {
              log::warn!("Sidecar terminated with code: {:?}", payload.code);
            }
            _ => {}
          }
        }
      });

      // Spawn background auto-updater task
      let handle = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        log::info!("Checking for updates...");
        match handle.updater() {
          Ok(updater) => {
            match updater.check().await {
              Ok(Some(update)) => {
                log::info!("Found update: {}. Downloading...", update.version);
                if let Err(e) = update.download_and_install(|chunk_len, total_len| {
                  if let Some(total) = total_len {
                    log::info!("Downloading update: {}/{} bytes", chunk_len, total);
                  }
                }, || {
                  log::info!("Update downloaded, installing...");
                }).await {
                  log::error!("Failed to download and install update: {:?}", e);
                } else {
                  log::info!("Update successfully installed! Relaunching app...");
                  handle.restart();
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
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
