// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// mimalloc beats the system allocator on the allocation-heavy extract/probe
// paths (regex captures, JSON values, per-request buffers).
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn main() {
    app_lib::run();
}
