use std::path::{Path, PathBuf};

const DEFAULT_API_PORT: u16 = 16801;

/// The webview's ws credentials. The Python backend owns the `settings`
/// row in dax.db — read it live so token rotations land without restart.
pub struct BackendInfo {
    pub token: String,
    pub port: u16,
}

fn db_path() -> PathBuf {
    std::env::var_os("HOME")
        .map(|h| PathBuf::from(h).join("Library/Application Support/dax/dax.db"))
        .unwrap_or_else(|| PathBuf::from("dax.db"))
}

/// Read the settings row. None until the backend has written it (first
/// boot) — the caller falls back to the default port, empty token.
fn read_backend_info(path: &Path) -> Option<BackendInfo> {
    let conn = rusqlite::Connection::open(path).ok()?;
    conn.query_row(
        "SELECT api_token, api_port FROM settings WHERE id = 1",
        [],
        |r| Ok(BackendInfo { token: r.get(0)?, port: r.get(1)? }),
    )
    .ok()
}

pub fn backend_info() -> BackendInfo {
    read_backend_info(&db_path()).unwrap_or(BackendInfo {
        token: String::new(),
        port: DEFAULT_API_PORT,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    #[test]
    fn test_backend_info_defaults_without_db() {
        assert!(read_backend_info(Path::new("/nonexistent/dax.db")).is_none());
    }

    #[test]
    fn test_backend_info_reads_settings_row() {
        let dir = std::env::temp_dir().join(format!("dax-test-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("dax.db");
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), \
             api_token TEXT NOT NULL DEFAULT '', api_port INTEGER NOT NULL DEFAULT 16801)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO settings (id, api_token, api_port) VALUES (1, 'tok-abc', 17777)",
            [],
        )
        .unwrap();
        let b = read_backend_info(&path).unwrap();
        assert_eq!(b.token, "tok-abc");
        assert_eq!(b.port, 17777);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
