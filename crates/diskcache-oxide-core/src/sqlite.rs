//! The bundled SQLite: the one entry point every connection is opened through, and facts about the
//! library compiled into this build.

#[cfg(windows)]
use std::borrow::Cow;
use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

use crate::sqlite_mutex;
use crate::{Error, Result};

/// Opens a connection to the database at `path`, read-write and created if missing.
///
/// Every SQLite open in both crates, tests included, goes through this module (pinned by
/// `tests::every_open_goes_through_the_entry_point`), because each one first installs this crate's
/// mutex methods ([`sqlite_mutex::ensure_installed`]). A direct rusqlite open would initialize
/// SQLite itself, and one racing the install is heap corruption, not a fallback.
pub fn open(path: &Path) -> rusqlite::Result<Connection> {
    open_with_flags(path, OpenFlags::default())
}

/// [`open`], with explicit open flags.
pub fn open_with_flags(path: &Path, flags: OpenFlags) -> rusqlite::Result<Connection> {
    sqlite_mutex::ensure_installed();
    #[cfg(windows)]
    return Connection::open_with_flags_and_vfs(reachable(path), flags, WINDOWS_VFS);
    #[cfg(not(windows))]
    Connection::open_with_flags(path, flags)
}

/// Windows: SQLite's bundled `win32-longpath` VFS, for every connection (plan 25, R-L10). It is the
/// default `win32` VFS's code (the same files, locks and shared memory, so it shares a database with
/// any other SQLite on the machine) with a path limit of 65,534 bytes instead of 1,040: past that,
/// `win32` truncates the full path and the pager refuses to open the database.
#[cfg(windows)]
const WINDOWS_VFS: &std::ffi::CStr = c"win32-longpath";

/// The path to give SQLite for `path` (plan 25, R-L10). Windows: SQLite hands the name to
/// `CreateFileW` as it is, which stops at `MAX_PATH` (260) unless the machine enables long paths
/// (off by default) and the program declares itself long-path aware. A path whose absolute form,
/// with SQLite's longest suffix (`-journal`), wouldn't fit becomes a verbatim `\\?\` path, which
/// has no such limit. Shorter ones are left as they are, so messages show the caller's path.
#[cfg(windows)]
fn reachable(path: &Path) -> Cow<'_, Path> {
    const MAX_PATH: usize = 260;
    const LONGEST_SUFFIX: usize = "-journal".len();
    let Ok(absolute) = std::path::absolute(path) else { return Cow::Borrowed(path) };
    let Some(text) = absolute.to_str() else { return Cow::Borrowed(path) };
    if text.encode_utf16().count() + LONGEST_SUFFIX < MAX_PATH || text.starts_with(r"\\?\") {
        return Cow::Borrowed(path);
    }
    let verbatim = match text.strip_prefix(r"\\") {
        Some(unc) => format!(r"\\?\UNC\{unc}"),
        None => format!(r"\\?\{text}"),
    };
    Cow::Owned(verbatim.into())
}

/// [`open`], for a private in-memory database.
pub fn open_in_memory() -> rusqlite::Result<Connection> {
    sqlite_mutex::ensure_installed();
    Connection::open_in_memory()
}

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
        let conn = open_in_memory()?;
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

    /// Every SQLite open in `crates/` goes through this module, which installs the mutex methods
    /// first, and no other code calls SQLite's C API directly: several raw calls initialize SQLite
    /// (`sqlite3_vfs_find`, `sqlite3_soft_heap_limit64`, `sqlite3_randomness`,
    /// `sqlite3_mutex_alloc`), which is the same race as a direct open. Never widen the allowlist
    /// casually: a call racing the install corrupts the heap.
    #[test]
    fn every_open_goes_through_the_entry_point() {
        /// (file, the only raw calls it may make). An empty list allows the whole file.
        const ALLOWED: &[(&str, &[&str])] = &[
            ("diskcache-oxide-core/src/sqlite.rs", &[]),
            ("diskcache-oxide-core/src/sqlite_mutex.rs", &[]),
            // `_sqlite_memory` (a test hook): the memory counters don't initialize SQLite.
            ("diskcache-oxide-py/src/lib.rs", &["sqlite3_memory_used", "sqlite3_memory_highwater"]),
            // The install test checks that config is refused after init; neither call initializes
            // (`sqlite3_mutex_methods` is the type GETMUTEX writes to).
            (
                "diskcache-oxide-core/tests/sqlite_mutex.rs",
                &["sqlite3_config", "sqlite3_threadsafe", "sqlite3_mutex_methods"],
            ),
        ];
        fn allowed(path: &Path, code: &str) -> bool {
            ALLOWED.iter().any(|(file, calls)| {
                path.ends_with(file)
                    && (calls.is_empty()
                        || code.match_indices("sqlite3_").all(|(i, _)| calls.iter().any(|c| code[i..].starts_with(c))))
            })
        }
        fn scan(dir: &Path, found: &mut Vec<String>, seen: &mut usize) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    if path.file_name().is_some_and(|n| n != "target") {
                        scan(&path, found, seen);
                    }
                } else if path.extension().is_some_and(|e| e == "rs") {
                    let text = std::fs::read_to_string(&path).unwrap();
                    for (n, line) in text.lines().enumerate() {
                        let code = line.trim_start();
                        let raw = code.contains("Connection::open")
                            || code.contains("ffi::sqlite3_")
                            || code.contains("sqlite3_open");
                        if !raw || code.starts_with("//") {
                            continue;
                        }
                        *seen += 1;
                        if !allowed(&path, code) {
                            found.push(format!("{}:{}: {code}", path.display(), n + 1));
                        }
                    }
                }
            }
        }
        let crates = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let (mut stray, mut seen) = (Vec::new(), 0);
        scan(crates, &mut stray, &mut seen);
        assert!(stray.is_empty(), "open SQLite through diskcache_oxide_core::sqlite::open*, not directly: {stray:#?}");
        assert!(seen >= 5, "the scan should see the allowed calls, {seen} seen");
    }
}
