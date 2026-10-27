//! PyO3 bridge exposed to Python as `downloadanything.downloadanything_rs`.

use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use pyo3::{PyResult, Python, pyfunction};
use tracing_subscriber::EnvFilter;

/// Fallback filter when neither Python nor `RUST_LOG` supplies one.
const DEFAULT_FILTER: &str = "info,downloadanything_rs=debug";

/// stdout plus, when the app supplies one, an append to its log file.
///
/// The bundled app runs the interpreter with stdio on /dev/null, so the file
/// is the only place these lines survive. Opened per event: Python's rotating
/// handler may have renamed the file since the last line.
struct TeeWriter {
    log: Option<PathBuf>,
}

impl Write for TeeWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if let Some(path) = &self.log
            && let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)
        {
            let _ = f.write_all(buf);
        }
        io::stdout().write_all(buf)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        io::stdout().flush()
    }
}

/// Initialize the tracing subscriber once for the process.
///
/// An invalid filter is ignored rather than panicking: logging configuration
/// must never take the extractor down.
fn init_tracing_with(filter: Option<&str>, log_path: Option<&Path>) {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        let filter = filter
            .and_then(|f| EnvFilter::try_new(f).ok())
            .or_else(|| EnvFilter::try_from_default_env().ok())
            .unwrap_or_else(|| EnvFilter::new(DEFAULT_FILTER));
        let log = log_path.map(Path::to_path_buf);
        let _ = tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_target(true)
            .compact()
            // ANSI would reach the file mirror as escape bytes.
            .with_ansi(false)
            .with_writer(move || TeeWriter { log: log.clone() })
            .try_init();
    });
}

/// Initialize tracing with the filter from `RUST_LOG` or the crate default.
pub(crate) fn init_tracing() {
    init_tracing_with(None, None);
}

/// Process-wide runtime for the blocking Python bridge.
fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("downloadanything_rs needs a tokio runtime for extraction")
    })
}

/// Extract media from URLs via the Rust pipeline.
///
/// Per-URL failures are carried in the returned `PageResult` JSON, never
/// thrown. `options` is camelCase [`crate::config::Options`] JSON.
#[pyfunction]
#[pyo3(name = "extract")]
pub(crate) fn extract_urls(py: Python<'_>, options: &str, urls: Vec<String>) -> PyResult<String> {
    let opts: crate::config::Options = serde_json::from_str(options)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad options json: {e}")))?;
    // Blocking network work — release the GIL so Python threads keep running.
    py.detach(move || -> Result<String, String> {
        init_tracing();
        let cfg = crate::config::Config::from(&opts);
        let net = crate::net::Fetcher::new(&cfg).map_err(|e| e.to_string())?;
        let out = runtime().block_on(crate::api::extract(&net, urls));
        serde_json::to_string(&out).map_err(|e| e.to_string())
    })
    .map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

/// Re-initialize the tracing filter from the Python logging config.
///
/// Called once at startup with the `RUST_LOG` value and log path
/// `downloadanything/log.py` computed.
#[pyfunction]
pub(crate) fn init_logging(filter: &str, log_path: Option<&str>) {
    init_tracing_with(Some(filter), log_path.map(Path::new));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tee_writer_mirrors_to_file() {
        let path =
            std::env::temp_dir().join(format!("downloadanything-tee-{}.log", std::process::id()));
        let mut w = TeeWriter {
            log: Some(path.clone()),
        };
        w.write_all(b"hello\n").unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "hello\n");
        let _ = std::fs::remove_file(&path);
    }
}
