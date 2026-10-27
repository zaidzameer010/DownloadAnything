fn main() {
    // Tauri validates every configured resource path at build time; dev builds
    // do not run backend:bundle, so stage the directories first.
    for name in ["backend", "extension", "venv", "python"] {
        let _ = std::fs::create_dir_all(format!("resources/{name}"));
    }
    tauri_build::build()
}
