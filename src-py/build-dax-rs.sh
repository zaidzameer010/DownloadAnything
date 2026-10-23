#!/bin/sh
# Builds the PyO3 extractor bridge (src-py/dax-core — the vendored custom
# extractor crate) and drops it inside the dax package as
# dax/dax_rs.so. PYO3_PYTHON points at the project venv so pyo3 compiles
# against the interpreter that will import the module.
set -eu
cd "$(dirname "$0")/dax-core"
PYO3_PYTHON="$PWD/../.venv/bin/python" cargo build --release
# Workspace member — cargo emits into the workspace-root target dir.
cp ../../target/release/libdax_rs.dylib ../dax/dax_rs.so.new
mv ../dax/dax_rs.so.new ../dax/dax_rs.so  # atomic swap — a running process keeps its old inode
