//! Facts about the SQLite library compiled into this build.

use std::time::Duration;

use rusqlite::Connection;

use crate::{Error, Result};

/// The SQLite version the bundled library was compiled from (the `SQLITE_VERSION` macro).
#[must_use]
pub fn compile_version() -> &'static str {
    rusqlite::ffi::SQLITE_VERSION.to_str().unwrap_or("unknown")
}

/// The version reported at runtime by `sqlite3_libversion()`. It differs from
/// [`compile_version`] only if the system SQLite was linked instead of the bundled one.
#[must_use]
pub fn runtime_version() -> &'static str {
    rusqlite::version()
}

/// The compile-time options of the linked SQLite, as reported by `PRAGMA compile_options`.
pub fn compile_options() -> Result<Vec<String>> {
    let read = || -> rusqlite::Result<Vec<String>> {
        let conn = Connection::open_in_memory()?;
        let mut stmt = conn.prepare("PRAGMA compile_options")?;
        stmt.query_map([], |row| row.get::<_, String>(0))?.collect()
    };
    // An in-memory database has no lock to wait for.
    read().map_err(|e| Error::from_sqlite(e, Duration::ZERO))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_matches_the_bundled_library() {
        assert_eq!(runtime_version(), compile_version());
    }

    #[test]
    fn compile_options_are_listed() {
        let options = compile_options().unwrap();
        assert!(options.iter().any(|o| o.starts_with("THREADSAFE=")), "{options:?}");
    }
}
