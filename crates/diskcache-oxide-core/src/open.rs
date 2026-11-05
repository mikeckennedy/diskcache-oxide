//! Opening a cache directory without writing when nothing changed (D-019, 04-design.md section 3).
//!
//! Every step reads first and writes only on a mismatch, so reopening an existing cache with the
//! same settings leaves the database untouched: a peer's `PRAGMA data_version` doesn't move.
//! Racing creators of a new database are safe because creation re-checks `user_version` under
//! `BEGIN IMMEDIATE`.

use std::io::ErrorKind;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{Connection, TransactionBehavior};

use crate::lockwait;
use crate::schema::{CREATE, DB_FILENAME, DEFAULT_CULL_LIMIT, DEFAULT_SIZE_LIMIT, FORMAT_VERSION, MIN_SIZE_LIMIT};
use crate::{Error, Result};

/// Settings a caller passed explicitly. `None` keeps whatever is stored (or the default for a new
/// cache), so a reopen that doesn't mention a setting never overwrites another opener's value.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Requested {
    pub size_limit: Option<i64>,
    pub cull_limit: Option<i64>,
}

/// The persisted limits in effect after opening.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    pub size_limit: i64,
    pub cull_limit: i64,
}

impl Requested {
    /// Rejects values the cache can't honor (D-005: a `size_limit` under `MIN_SIZE_LIMIT`).
    pub fn validate(&self) -> Result<()> {
        if let Some(limit) = self.size_limit
            && limit < MIN_SIZE_LIMIT
        {
            return Err(Error::InvalidSetting(format!(
                "size_limit must be at least {MIN_SIZE_LIMIT} bytes, got {limit}"
            )));
        }
        if let Some(limit) = self.cull_limit
            && limit < 0
        {
            return Err(Error::InvalidSetting(format!("cull_limit must not be negative, got {limit}")));
        }
        Ok(())
    }
}

/// The database file inside `directory`.
#[must_use]
pub fn db_path(directory: &Path) -> PathBuf {
    directory.join(DB_FILENAME)
}

/// Creates `directory` as owner-only (0700) if it doesn't exist (D-015). Missing parents are
/// created with the process umask, like `os.makedirs`; an existing directory is left alone.
pub fn ensure_directory(directory: &Path) -> Result<()> {
    let io = |source| Error::Io { path: directory.to_path_buf(), source };
    if let Some(parent) = directory.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent).map_err(io)?;
    }
    match std::fs::DirBuilder::new().mode(0o700).create(directory) {
        Ok(()) => Ok(()),
        // Another opener (thread or process) won the race, or it already existed.
        Err(e) if e.kind() == ErrorKind::AlreadyExists && directory.is_dir() => Ok(()),
        Err(e) => Err(io(e)),
    }
}

/// Opens one connection to the database and applies the per-connection settings. Writes nothing.
pub fn connect(directory: &Path, timeout: Duration) -> Result<Connection> {
    let path = db_path(directory);
    let mut conn = Connection::open(&path).map_err(|e| Error::from_sqlite(e, timeout))?;
    // Busy waits happen only inside an open `AttachedWindow`; see `lockwait`.
    lockwait::install(&mut conn).map_err(|e| Error::from_sqlite(e, timeout))?;
    // Room for every statement the core caches (about 30), so a mixed workload never evicts one
    // and re-parses it. rusqlite's default is 16.
    conn.set_prepared_statement_cache_capacity(64);
    // A connection setting: it writes nothing.
    conn.execute_batch("PRAGMA synchronous = NORMAL").map_err(|e| Error::from_sqlite(e, timeout))?;
    // Query planner stability guarantee: plans never depend on bound values. Without it, SQLite
    // marks a statement whose plan looked at a parameter (our `expire_time` comparisons, via the
    // partial `cache_expire` index) as expired whenever its bindings are cleared, which rusqlite's
    // statement cache does on every reuse, so those statements were recompiled on every call (about
    // 30% of a `set`). Queries that use the partial index say `expire_time IS NOT NULL` explicitly.
    conn.set_db_config(rusqlite::config::DbConfig::SQLITE_DBCONFIG_ENABLE_QPSG, true)
        .map_err(|e| Error::from_sqlite(e, timeout))?;
    Ok(conn)
}

/// Brings the database to format version 1 with the requested settings, writing only what differs.
/// Returns the limits in effect.
pub fn prepare(conn: &mut Connection, requested: Requested, timeout: Duration) -> Result<Limits> {
    requested.validate()?;
    let sqlite = |e| Error::from_sqlite(e, timeout);

    ensure_wal(conn, timeout)?;

    let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0)).map_err(sqlite)?;
    if version == 0 {
        create_if_new(conn, requested, timeout)?;
    } else if version != FORMAT_VERSION {
        return Err(Error::FormatMismatch { found: version, expected: FORMAT_VERSION, path: None });
    }

    let stored = read_limits(conn, timeout)?;
    let wanted = Limits {
        size_limit: requested.size_limit.unwrap_or(stored.size_limit),
        cull_limit: requested.cull_limit.unwrap_or(stored.cull_limit),
    };
    if wanted != stored {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(sqlite)?;
        tx.execute(
            "UPDATE totals SET size_limit = ?1, cull_limit = ?2 WHERE only = 1",
            (wanted.size_limit, wanted.cull_limit),
        )
        .map_err(sqlite)?;
        tx.commit().map_err(sqlite)?;
    }
    Ok(wanted)
}

/// Reads the persisted limits.
pub fn read_limits(conn: &Connection, timeout: Duration) -> Result<Limits> {
    conn.query_row("SELECT size_limit, cull_limit FROM totals WHERE only = 1", [], |row| {
        Ok(Limits { size_limit: row.get(0)?, cull_limit: row.get(1)? })
    })
    .map_err(|e| Error::from_sqlite(e, timeout))
}

/// Switches to WAL only if the database isn't in WAL mode already, then verifies it (WAL is
/// required; e.g. some network filesystems refuse it). Runs before any schema transaction, so a
/// new database is created in WAL mode from the start.
fn ensure_wal(conn: &Connection, timeout: Duration) -> Result<()> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let mode: String = conn.query_row("PRAGMA journal_mode", [], |row| row.get(0)).map_err(sqlite)?;
    if mode.eq_ignore_ascii_case("wal") {
        return Ok(());
    }
    let mode: String = conn.query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0)).map_err(sqlite)?;
    if mode.eq_ignore_ascii_case("wal") { Ok(()) } else { Err(Error::NotWal { mode }) }
}

/// Creates the schema if this connection is the first to get the write lock on a new database.
fn create_if_new(conn: &mut Connection, requested: Requested, timeout: Duration) -> Result<()> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(sqlite)?;
    // Another opener may have created it while this one waited for the lock.
    let version: i64 = tx.query_row("PRAGMA user_version", [], |row| row.get(0)).map_err(sqlite)?;
    match version {
        0 => {
            tx.execute_batch(CREATE).map_err(sqlite)?;
            tx.execute(
                "INSERT INTO totals (only, count, size, next_seq, size_limit, cull_limit) VALUES (1, 0, 0, 1, ?1, ?2)",
                (
                    requested.size_limit.unwrap_or(DEFAULT_SIZE_LIMIT),
                    requested.cull_limit.unwrap_or(DEFAULT_CULL_LIMIT),
                ),
            )
            .map_err(sqlite)?;
            tx.execute_batch(&format!("PRAGMA user_version = {FORMAT_VERSION}")).map_err(sqlite)?;
            tx.commit().map_err(sqlite)
        }
        FORMAT_VERSION => Ok(()),
        found => Err(Error::FormatMismatch { found, expected: FORMAT_VERSION, path: None }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);

    fn open(dir: &Path, requested: Requested) -> (Connection, Limits) {
        ensure_directory(dir).unwrap();
        let mut conn = connect(dir, T).unwrap();
        let limits = prepare(&mut conn, requested, T).unwrap();
        (conn, limits)
    }

    fn data_version(conn: &Connection) -> i64 {
        conn.query_row("PRAGMA data_version", [], |r| r.get(0)).unwrap()
    }

    #[test]
    fn new_database_gets_defaults_wal_and_the_format_version() {
        let tmp = TempDir::new("open-new");
        let (conn, limits) = open(tmp.path(), Requested::default());
        assert_eq!(limits, Limits { size_limit: DEFAULT_SIZE_LIMIT, cull_limit: DEFAULT_CULL_LIMIT });
        let mode: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
        assert_eq!(mode, "wal");
        let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        assert_eq!(version, FORMAT_VERSION);
    }

    #[test]
    fn directory_is_created_owner_only_and_existing_ones_are_left_alone() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = TempDir::new("open-mode");
        let dir = tmp.path().join("parent").join("cache");
        ensure_directory(&dir).unwrap();
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);

        let shared = tmp.path().join("shared");
        std::fs::create_dir(&shared).unwrap();
        std::fs::set_permissions(&shared, std::fs::Permissions::from_mode(0o770)).unwrap();
        ensure_directory(&shared).unwrap();
        assert_eq!(std::fs::metadata(&shared).unwrap().permissions().mode() & 0o777, 0o770);
    }

    #[test]
    fn reopening_with_the_same_or_no_settings_writes_nothing() {
        let tmp = TempDir::new("open-nowrite");
        let requested = Requested { size_limit: Some(5 << 20), cull_limit: Some(3) };
        drop(open(tmp.path(), requested));

        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let (_same, limits) = open(tmp.path(), requested);
        let (_unspecified, unspecified_limits) = open(tmp.path(), Requested::default());
        assert_eq!(data_version(&peer), before, "an unchanged reopen must not write");
        assert_eq!(limits, Limits { size_limit: 5 << 20, cull_limit: 3 });
        assert_eq!(unspecified_limits, limits, "None keeps the stored values");
    }

    #[test]
    fn an_explicit_new_setting_is_written_once() {
        let tmp = TempDir::new("open-change");
        drop(open(tmp.path(), Requested::default()));
        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let (_conn, limits) = open(tmp.path(), Requested { size_limit: Some(2 << 20), cull_limit: None });
        assert_ne!(data_version(&peer), before);
        assert_eq!(limits, Limits { size_limit: 2 << 20, cull_limit: DEFAULT_CULL_LIMIT });
    }

    #[test]
    fn an_empty_immediate_transaction_does_not_bump_a_peers_data_version() {
        // The spilled-value locked fallback relies on this (04-design.md section 4).
        let tmp = TempDir::new("open-empty-tx");
        let (mut conn, _) = open(tmp.path(), Requested::default());
        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();
        let _: i64 = tx.query_row("SELECT count FROM totals", [], |r| r.get(0)).unwrap();
        tx.commit().unwrap();
        assert_eq!(data_version(&peer), before);
    }

    #[test]
    fn size_limit_below_the_minimum_is_rejected() {
        let tmp = TempDir::new("open-min");
        ensure_directory(tmp.path()).unwrap();
        let mut conn = connect(tmp.path(), T).unwrap();
        let err =
            prepare(&mut conn, Requested { size_limit: Some(MIN_SIZE_LIMIT - 1), cull_limit: None }, T).unwrap_err();
        assert!(matches!(err, Error::InvalidSetting(_)), "{err:?}");
        assert!(prepare(&mut conn, Requested { size_limit: Some(MIN_SIZE_LIMIT), cull_limit: None }, T).is_ok());
    }

    #[test]
    fn a_newer_format_is_refused_without_writing() {
        let tmp = TempDir::new("open-newer");
        drop(open(tmp.path(), Requested::default()));
        connect(tmp.path(), T).unwrap().execute_batch("PRAGMA user_version = 2").unwrap();
        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let mut conn = connect(tmp.path(), T).unwrap();
        let err = prepare(&mut conn, Requested { size_limit: Some(9 << 20), cull_limit: None }, T).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found: 2, expected: 1, .. }), "{err:?}");
        assert_eq!(data_version(&peer), before);
    }

    #[test]
    fn racing_creators_all_succeed_with_one_schema() {
        let tmp = TempDir::new("open-race");
        let dir = tmp.path().join("cache");
        let barrier = std::sync::Barrier::new(8);
        std::thread::scope(|s| {
            for _ in 0..8 {
                s.spawn(|| {
                    barrier.wait();
                    ensure_directory(&dir).unwrap();
                    // Even connecting can report busy while another opener switches the new
                    // database to WAL, so the whole open is one retried attempt (as `Store` does).
                    crate::lockwait::retry(T, |_| prepare(&mut connect(&dir, T)?, Requested::default(), T)).unwrap();
                });
            }
        });
        let conn = connect(&dir, T).unwrap();
        let rows: i64 = conn.query_row("SELECT COUNT(*) FROM totals", [], |r| r.get(0)).unwrap();
        assert_eq!(rows, 1);
    }
}
