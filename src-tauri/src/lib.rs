//! Tauri shell — spawns the Python backend (src-py) and serves the UI.
//!
//! The Python backend is the live implementation; the UI talks to it over
//! one WebSocket (token+port via `backend_info`, which reads the settings
//! row in dax.db the Python backend owns).

pub mod settings;

use serde_json::Value;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::{Emitter, Manager, State};
use tauri_plugin_updater::UpdaterExt;

pub struct ShellState {
    pub backend: Mutex<Option<Child>>,
}

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

/// Spawns `src-py/.venv/bin/python -m dax` (the backend binds 127.0.0.1:<apiPort>).
/// A backend already answering on the port wins: the spawned uvicorn fails to
/// bind and exits — the existing process keeps serving.
fn spawn_backend() -> Option<Child> {
    let src_py = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../src-py");
    let python = src_py.join(".venv/bin/python");
    if !python.is_file() {
        eprintln!("dax: python venv missing, backend not spawned: {}", python.display());
        return None;
    }
    // Logs visible in dev terminals; silent in the bundled app (dax.log has them).
    let (out, err) = if cfg!(debug_assertions) {
        (Stdio::inherit(), Stdio::inherit())
    } else {
        (Stdio::null(), Stdio::null())
    };
    match Command::new(&python)
        .args(["-m", "dax"])
        .current_dir(&src_py)
        .stdout(out)
        .stderr(err)
        .spawn()
    {
        Ok(child) => {
            eprintln!("dax: python backend spawned (pid {})", child.id());
            Some(child)
        }
        Err(e) => {
            eprintln!("dax: python backend spawn failed: {e}");
            None
        }
    }
}

/// Background updater: checks the GitHub release feed, downloads and installs
/// a newer build, then emits `dax-update-ready` — the UI banner owns the
/// restart since it kills the Python backend.
async fn check_update(app: tauri::AppHandle) -> tauri_plugin_updater::Result<()> {
    let Some(update) = app.updater()?.check().await? else {
        return Ok(());
    };
    let version = update.version.clone();
    let notes = update.body.clone();
    update.download_and_install(|_, _| {}, || {}).await?;
    eprintln!("dax: update {version} installed");
    let _ = app.emit(
        "dax-update-ready",
        serde_json::json!({ "version": version, "notes": notes }),
    );
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(ShellState {
            backend: Mutex::new(None),
        })
        .setup(move |app| {
            let child = spawn_backend();
            let state: State<'_, ShellState> = app.state();
            *state.backend.lock().unwrap() = child;
            // Dev builds aren't installable bundles — skip the download entirely.
            if !cfg!(debug_assertions) {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(e) = check_update(handle).await {
                        eprintln!("dax: update check failed: {e}");
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_info, restart_app])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
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
