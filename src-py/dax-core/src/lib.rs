//! PyO3 bridge — exposes the custom Rust page extractor to the dax
//! Python backend as `dax.dax_rs` (built by src-py/build-dax-rs.sh).

pub mod api;
pub mod config;
pub mod error;
pub mod extract;
pub mod input;
pub mod model;
pub mod net;
mod pipeline;

use std::sync::OnceLock;

use pyo3::prelude::*;

/// One-time tracing subscriber setup; the filter comes from the Python side
/// (init_logging) or RUST_LOG when extract runs first.
fn init_tracing_with(filter: Option<&str>) {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        use tracing_subscriber::EnvFilter;
        let filter = filter
            .map(EnvFilter::new)
            .or_else(|| EnvFilter::try_from_default_env().ok())
            .unwrap_or_else(|| EnvFilter::new("info,dax_rs=debug"));
        let _ = tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_target(true)
            .compact()
            .try_init();
    });
}

/// Initialize the tracing subscriber once for the process.
pub fn init_tracing() {
    init_tracing_with(None);
}

fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("tokio runtime for dax_rs")
    })
}

/// Extract media from URLs via the custom Rust pipeline. Per-URL failures
/// are carried in the returned PageResult JSON, never thrown.
#[pyfunction]
#[pyo3(name = "extract")]
fn extract_urls(py: Python<'_>, options: &str, urls: Vec<String>) -> PyResult<String> {
    let opts: config::Options = serde_json::from_str(options)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad options json: {e}")))?;
    // Blocking network work — released GIL so Python threads keep running.
    let json = py
        .detach(move || -> Result<String, String> {
            init_tracing();
            let cfg = config::Config::from(&opts);
            let net = net::NetEngine::new(&cfg).map_err(|e| e.to_string())?;
            let out = runtime().block_on(api::extract(&cfg, &net, urls));
            serde_json::to_string(&out).map_err(|e| e.to_string())
        })
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    Ok(json)
}

/// Re-initialize the tracing filter from the Python logging config
/// (called once at startup with the RUST_LOG value log.py computed).
#[pyfunction]
fn init_logging(filter: &str) {
    init_tracing_with(Some(filter));
}

#[pymodule]
mod dax_rs {
    #[pymodule_export]
    use super::extract_urls;
    #[pymodule_export]
    use super::init_logging;
}
