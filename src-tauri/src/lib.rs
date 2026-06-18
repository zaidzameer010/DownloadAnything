use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .setup(|app| {
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

      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
