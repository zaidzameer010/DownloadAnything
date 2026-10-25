//! PyO3 bridge exposed to Python as `dax.dax_rs`.

use std::sync::OnceLock;

use pyo3::{PyResult, Python, pyfunction};
use tracing_subscriber::EnvFilter;

/// Fallback filter when neither Python nor `RUST_LOG` supplies one.
const DEFAULT_FILTER: &str = "info,dax_rs=debug";

/// Initialize the tracing subscriber once for the process.
///
/// An invalid filter is ignored rather than panicking: logging configuration
/// must never take the extractor down.
fn init_tracing_with(filter: Option<&str>) {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        let filter = filter
            .and_then(|f| EnvFilter::try_new(f).ok())
            .or_else(|| EnvFilter::try_from_default_env().ok())
            .unwrap_or_else(|| EnvFilter::new(DEFAULT_FILTER));
        let _ = tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_target(true)
            .compact()
            .try_init();
    });
}

/// Initialize tracing with the filter from `RUST_LOG` or the crate default.
pub(crate) fn init_tracing() {
    init_tracing_with(None);
}

/// Process-wide runtime for the blocking Python bridge.
fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("dax_rs needs a tokio runtime for extraction")
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
/// Called once at startup with the `RUST_LOG` value `dax/log.py` computed.
#[pyfunction]
pub(crate) fn init_logging(filter: &str) {
    init_tracing_with(Some(filter));
}
