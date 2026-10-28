//! Exposes PyO3's interpreter cfgs (notably `Py_GIL_DISABLED`) to this crate.

fn main() {
    pyo3_build_config::use_pyo3_cfgs();
}
