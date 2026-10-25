//! Tauri shell — spawns the Python backend (src-py) and serves the UI.
//!
//! Release builds run the venv + CPython + dax_rs staged under
//! `src-tauri/resources` by `scripts/bundle-backend.ts`; dev builds run
//! `src-py/.venv`. The UI talks to the backend over one WebSocket
//! (token+port via `backend_info`, which reads the settings row in dax.db
//! the Python backend owns).

pub mod settings;

use serde_json::Value;
use std::path::{Path, PathBuf};
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

/// Runs `venv/bin/python -m dax` from the staged backend (or the dev venv).
/// A backend already answering on the port wins: the spawned uvicorn fails to
/// bind and exits — the existing process keeps serving.
fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    let Some((python, cwd)) = backend_paths(app) else {
        eprintln!("dax: backend not found — run `bun run backend:bundle` before building");
        return None;
    };
    // Logs visible in dev terminals; silent in the bundled app (dax.log has them).
    let (out, err) = if cfg!(debug_assertions) {
        (Stdio::inherit(), Stdio::inherit())
    } else {
        (Stdio::null(), Stdio::null())
    };
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "dax"])
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
            let child = spawn_backend(app.handle());
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
