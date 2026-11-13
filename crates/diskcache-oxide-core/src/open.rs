//! Opening a cache directory without writing when nothing changed (D-019, 04-design.md section 3).
//!
//! Every step reads first and writes only on a mismatch, so reopening an existing cache with the
//! same settings leaves the database untouched: a peer's `PRAGMA data_version` doesn't move.
//! Racing creators of a new database are safe because creation re-checks `user_version` under
//! `BEGIN IMMEDIATE`.

use std::fs::DirBuilder;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior};

use crate::fanout::{HASH_IDENTITY, MAX_SHARDS, MIN_SHARDS, parse_decimal};
use crate::schema::{
    CREATE, DB_FILENAME, DEFAULT_CULL_LIMIT, DEFAULT_SIZE_LIMIT, EvictionPolicy, FORMAT_VERSION, MIN_SIZE_LIMIT,
};
use crate::{Error, Result};
use crate::{lockwait, sqlite};

/// Settings a caller passed explicitly. `None` keeps whatever is stored (or the default for a new
/// cache), so a reopen that doesn't mention a setting never overwrites another opener's value.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Requested {
    pub size_limit: Option<i64>,
    pub cull_limit: Option<i64>,
    pub eviction_policy: Option<EvictionPolicy>,
}

/// The persisted limits in effect after opening. Write paths read the policy inside their own
/// transaction; this copy is for the attributes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    pub size_limit: i64,
    pub cull_limit: i64,
    pub eviction_policy: EvictionPolicy,
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

/// What a database is to the cache that opens it: a plain `Cache`'s own, or one shard of a
/// `FanoutCache` (plan 13, section 1).
///
/// A shard's database carries a stamp, the `settings` row `('fanout', '<index>/<count>/<hash
/// identity>')`, written once when the shard is created and never rewritten. Every open compares
/// the stamp with the role it asked for, so a renamed, copied or mixed-up shard directory, a second
/// handle with another shard count, or a plain `Cache` pointed at a shard, is refused rather than
/// reading or writing the wrong keys. The hash identity is part of the stamp: a stamp naming
/// another hash isn't a shard this build can open, under any role.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Role {
    /// A plain `Cache`: the database has no stamp.
    #[default]
    Plain,
    /// Shard `index` of a `FanoutCache` with `count` shards, under [`HASH_IDENTITY`].
    Shard { index: u32, count: u32 },
}

/// The `settings` row that holds a shard's stamp.
const ROLE_SETTING: &str = "fanout";

impl Role {
    /// Rejects a shard role the format can't hold: a count outside `MIN_SHARDS..=MAX_SHARDS`, or
    /// an index not below the count.
    pub fn validate(self) -> Result<()> {
        match self {
            Self::Plain => Ok(()),
            Self::Shard { index, count } if (MIN_SHARDS..=MAX_SHARDS).contains(&count) && index < count => Ok(()),
            Self::Shard { index, count } => Err(Error::InvalidSetting(format!(
                "a shard needs 0 <= index < count and {MIN_SHARDS} <= count <= {MAX_SHARDS}, got shard {index} of {count}"
            ))),
        }
    }

    /// The stamp a new database with this role gets, or `None` for a plain cache (no row).
    #[must_use]
    pub fn stamp(self) -> Option<String> {
        match self {
            Self::Plain => None,
            Self::Shard { index, count } => Some(format!("{index}/{count}/{HASH_IDENTITY}")),
        }
    }

    /// Parses a stamp, accepting only the exact rendering [`Role::stamp`] gives some valid shard
    /// role: decimal digits with no sign, padding or leading zeros, and this build's hash identity.
    #[must_use]
    pub fn from_stamp(stamp: &str) -> Option<Self> {
        let mut parts = stamp.splitn(3, '/');
        let index = parse_decimal(parts.next()?)?;
        let count = parse_decimal(parts.next()?)?;
        let role = Self::Shard { index, count };
        let canonical = role.validate().is_ok() && parts.next() == Some(HASH_IDENTITY);
        canonical.then_some(role)
    }

    /// Plain words for an error message.
    fn describe(self) -> String {
        match self {
            Self::Plain => "a plain Cache".to_owned(),
            Self::Shard { index, count } => format!("shard {index} of {count} of a FanoutCache"),
        }
    }
}

/// Refuses an open as `wanted` of a database whose stamp says it is `found` (`path` names the
/// database in the error). Equal roles pass.
pub(crate) fn check_role(path: &Path, wanted: Role, found: Role) -> Result<()> {
    let reason = match (wanted, found) {
        _ if wanted == found => return Ok(()),
        (Role::Plain, Role::Shard { .. }) => format!(
            "this database is {}; open the FanoutCache's root directory instead of one of its shards",
            found.describe()
        ),
        (Role::Shard { .. }, Role::Plain) => format!(
            "this database belongs to a plain Cache (it has no FanoutCache shard stamp), so it can't be used as {}; \
             a FanoutCache needs a directory of its own",
            wanted.describe()
        ),
        _ => format!(
            "this database is {}, but it was opened as {} (a renamed or copied shard directory, or a root whose \
             manifest was replaced); changing the shard count needs a new directory",
            found.describe(),
            wanted.describe()
        ),
    };
    Err(Error::RoleMismatch { path: path.to_path_buf(), reason })
}

/// Reads the database's stamp, as a [`Role`]. A read, so an unchanged open still writes nothing
/// (D-019). A stamp this build can't parse (another hash, say) is refused under every role.
pub fn read_role(conn: &Connection, timeout: Duration) -> Result<Role> {
    let stamp: Option<rusqlite::types::Value> = conn
        .query_row("SELECT value FROM settings WHERE name = ?1", [ROLE_SETTING], |row| row.get(0))
        .optional()
        .map_err(|e| Error::from_sqlite(e, timeout))?;
    let parsed = match &stamp {
        None => return Ok(Role::Plain),
        Some(rusqlite::types::Value::Text(text)) => Role::from_stamp(text),
        Some(_) => None,
    };
    parsed.ok_or_else(|| Error::RoleMismatch {
        path: database_path(conn),
        reason: format!("this database has an unrecognized FanoutCache shard stamp {stamp:?}"),
    })
}

/// The file `conn` has open, for error messages.
fn database_path(conn: &Connection) -> PathBuf {
    conn.path().map(PathBuf::from).unwrap_or_default()
}

/// The database file inside `directory`.
#[must_use]
pub fn db_path(directory: &Path) -> PathBuf {
    directory.join(DB_FILENAME)
}

/// A non-recursive `DirBuilder` for a directory the cache owns: owner-only (0700) on Unix (D-015).
/// Windows has no mode bits, so there the directory inherits the default ACL of its parent.
#[must_use]
pub(crate) fn owner_only_dir_builder() -> DirBuilder {
    #[cfg_attr(not(unix), expect(unused_mut))]
    let mut builder = DirBuilder::new();
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder
}

/// Creates a shard directory as owner-only (0700 on Unix) if it doesn't exist, but never its
/// parent: a root deleted while its shards were opening gives `NotFound` instead of being
/// re-created without a manifest, with the old count's shards in it (plan 15, R3).
pub fn ensure_shard_directory(directory: &Path) -> Result<()> {
    match owner_only_dir_builder().create(directory) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == ErrorKind::AlreadyExists && directory.is_dir() => Ok(()),
        Err(source) => Err(Error::Io { path: directory.to_path_buf(), source }),
    }
}

/// Creates `directory` as owner-only (0700 on Unix) if it doesn't exist (D-015). Missing parents are
/// created with the process umask, like `os.makedirs`; an existing directory is left alone. For a
/// plain cache's directory or a `FanoutCache` root; a shard uses [`ensure_shard_directory`].
///
/// A versioned subdirectory (`diskcache-oxide-format-N`, plan 26) never creates its parent: a cache
/// directory deleted while a cache was moving into it gives `NotFound`, rather than coming back with
/// umask permissions in place of the owner-only ones it had.
pub fn ensure_directory(directory: &Path) -> Result<()> {
    let io = |source| Error::Io { path: directory.to_path_buf(), source };
    if crate::layout::is_format_dir(directory) {
        return ensure_shard_directory(directory);
    }
    if let Some(parent) = directory.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent).map_err(io)?;
    }
    match owner_only_dir_builder().create(directory) {
        Ok(()) => Ok(()),
        // Another opener (thread or process) won the race, or it already existed.
        Err(e) if e.kind() == ErrorKind::AlreadyExists && directory.is_dir() => Ok(()),
        Err(e) => Err(io(e)),
    }
}

/// Opens one connection to the database and applies the per-connection settings. Writes nothing.
pub fn connect(directory: &Path, timeout: Duration) -> Result<Connection> {
    let path = db_path(directory);
    let mut conn = sqlite::open(&path).map_err(|e| Error::from_sqlite(e, timeout))?;
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

/// A connection to `directory`'s database if it exists, as [`connect`] makes but without its
/// statement-cache and planner settings, and never creating the file: `Ok(None)` if there's no
/// database to open (plan 26, review M2). For reading another cache's format, which must never leave
/// a database behind.
pub fn connect_existing(directory: &Path, timeout: Duration) -> Result<Option<Connection>> {
    let path = db_path(directory);
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX | OpenFlags::SQLITE_OPEN_URI;
    let mut conn = match sqlite::open_with_flags(&path, flags) {
        Ok(conn) => conn,
        Err(e) if e.sqlite_error_code() == Some(rusqlite::ErrorCode::CannotOpen) => return Ok(None),
        Err(e) => return Err(Error::from_sqlite(e, timeout)),
    };
    lockwait::install(&mut conn).map_err(|e| Error::from_sqlite(e, timeout))?;
    Ok(Some(conn))
}

/// Brings the database to the current format version ([`FORMAT_VERSION`]) with the requested settings, writing only what differs,
/// after checking that its stamp matches `role` (see [`Role`]). A new database gets `role`'s stamp;
/// a refused open writes nothing. Returns the limits in effect.
pub fn prepare(conn: &mut Connection, requested: Requested, role: Role, timeout: Duration) -> Result<Limits> {
    prepare_creating(conn, requested, role, DEFAULT_SIZE_LIMIT, timeout)
}

/// [`prepare`], giving a database it creates `new_size_limit` when `requested` names no
/// `size_limit`. A `FanoutCache` shard's share of the default total (plan 13, section 4); an
/// existing database keeps whatever it stores.
pub fn prepare_creating(
    conn: &mut Connection,
    requested: Requested,
    role: Role,
    new_size_limit: i64,
    timeout: Duration,
) -> Result<Limits> {
    requested.validate()?;
    if new_size_limit < MIN_SIZE_LIMIT {
        return Err(Error::InvalidSetting(format!(
            "a new database's size_limit must be at least {MIN_SIZE_LIMIT} bytes, got {new_size_limit}"
        )));
    }
    role.validate()?;
    let sqlite = |e| Error::from_sqlite(e, timeout);

    // The format first, before `ensure_wal` can touch the file: a database in another format is
    // refused having run nothing but this read, so a newer format that isn't in WAL mode is never
    // switched to it, nor waited on for the lock the switch would need (plan 26, M2). A new database
    // reads 0 and is still created in WAL mode, because `ensure_wal` runs before the schema.
    let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0)).map_err(sqlite)?;
    if version != 0 && version != FORMAT_VERSION {
        return Err(Error::FormatMismatch {
            found: version,
            expected: FORMAT_VERSION,
            path: Some(database_path(conn)),
        });
    }

    ensure_wal(conn, timeout)?;

    if version == 0 {
        create_if_new(conn, requested, role, new_size_limit, timeout)?;
    }
    // On every open, not only a new one: a database another creator won the race for, or one made
    // by a plain `Cache` before this shard opened (which has no stamp, and is never adopted).
    check_role(&database_path(conn), role, read_role(conn, timeout)?)?;

    let stored = read_limits(conn, timeout)?;
    let wanted = Limits {
        size_limit: requested.size_limit.unwrap_or(stored.size_limit),
        cull_limit: requested.cull_limit.unwrap_or(stored.cull_limit),
        eviction_policy: requested.eviction_policy.unwrap_or(stored.eviction_policy),
    };
    if wanted != stored {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(sqlite)?;
        tx.execute(
            "UPDATE state SET size_limit = ?1, cull_limit = ?2, eviction_policy = ?3 WHERE only = 1",
            (wanted.size_limit, wanted.cull_limit, wanted.eviction_policy.code()),
        )
        .map_err(sqlite)?;
        tx.commit().map_err(sqlite)?;
    }
    Ok(wanted)
}

/// Reads the persisted limits.
pub fn read_limits(conn: &Connection, timeout: Duration) -> Result<Limits> {
    let (size_limit, cull_limit, policy): (i64, i64, i64) = conn
        .query_row("SELECT size_limit, cull_limit, eviction_policy FROM state WHERE only = 1", [], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .map_err(|e| Error::from_sqlite(e, timeout))?;
    Ok(Limits { size_limit, cull_limit, eviction_policy: EvictionPolicy::try_from(policy)? })
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

/// Creates the schema, with `role`'s stamp, if this connection is the first to get the write lock on
/// a new database. The stamp goes in the same transaction, so racing creators see it or nothing.
fn create_if_new(
    conn: &mut Connection,
    requested: Requested,
    role: Role,
    new_size_limit: i64,
    timeout: Duration,
) -> Result<()> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(sqlite)?;
    // Another opener may have created it while this one waited for the lock.
    let version: i64 = tx.query_row("PRAGMA user_version", [], |row| row.get(0)).map_err(sqlite)?;
    match version {
        0 => {
            tx.execute_batch(CREATE).map_err(sqlite)?;
            tx.execute(
                "INSERT INTO state (only, count, size, next_seq, size_limit, cull_limit, eviction_policy)
                 VALUES (1, 0, 0, 1, ?1, ?2, ?3)",
                (
                    requested.size_limit.unwrap_or(new_size_limit),
                    requested.cull_limit.unwrap_or(DEFAULT_CULL_LIMIT),
                    requested.eviction_policy.unwrap_or_default().code(),
                ),
            )
            .map_err(sqlite)?;
            if let Some(stamp) = role.stamp() {
                tx.execute("INSERT INTO settings (name, value) VALUES (?1, ?2)", (ROLE_SETTING, stamp))
                    .map_err(sqlite)?;
            }
            tx.execute_batch(&format!("PRAGMA user_version = {FORMAT_VERSION}")).map_err(sqlite)?;
            tx.commit().map_err(sqlite)
        }
        FORMAT_VERSION => Ok(()),
        found => Err(Error::FormatMismatch { found, expected: FORMAT_VERSION, path: Some(database_path(&tx)) }),
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
        let limits = prepare(&mut conn, requested, Role::Plain, T).unwrap();
        (conn, limits)
    }

    fn data_version(conn: &Connection) -> i64 {
        conn.query_row("PRAGMA data_version", [], |r| r.get(0)).unwrap()
    }

    #[test]
    fn new_database_gets_defaults_wal_and_the_format_version() {
        let tmp = TempDir::new("open-new");
        let (conn, limits) = open(tmp.path(), Requested::default());
        assert_eq!(
            limits,
            Limits {
                size_limit: DEFAULT_SIZE_LIMIT,
                cull_limit: DEFAULT_CULL_LIMIT,
                eviction_policy: crate::schema::EvictionPolicy::LeastRecentlyStored
            }
        );
        let mode: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
        assert_eq!(mode, "wal");
        let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        assert_eq!(version, FORMAT_VERSION);
    }

    #[cfg(unix)]
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
        let requested = Requested { size_limit: Some(5 << 20), cull_limit: Some(3), eviction_policy: None };
        drop(open(tmp.path(), requested));

        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let (_same, limits) = open(tmp.path(), requested);
        let (_unspecified, unspecified_limits) = open(tmp.path(), Requested::default());
        assert_eq!(data_version(&peer), before, "an unchanged reopen must not write");
        assert_eq!(
            limits,
            Limits {
                size_limit: 5 << 20,
                cull_limit: 3,
                eviction_policy: crate::schema::EvictionPolicy::LeastRecentlyStored
            }
        );
        assert_eq!(unspecified_limits, limits, "None keeps the stored values");
    }

    #[test]
    fn an_explicit_new_setting_is_written_once() {
        let tmp = TempDir::new("open-change");
        drop(open(tmp.path(), Requested::default()));
        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let (_conn, limits) =
            open(tmp.path(), Requested { size_limit: Some(2 << 20), cull_limit: None, eviction_policy: None });
        assert_ne!(data_version(&peer), before);
        assert_eq!(
            limits,
            Limits {
                size_limit: 2 << 20,
                cull_limit: DEFAULT_CULL_LIMIT,
                eviction_policy: crate::schema::EvictionPolicy::LeastRecentlyStored
            }
        );
    }

    #[test]
    fn an_empty_immediate_transaction_does_not_bump_a_peers_data_version() {
        // The spilled-value locked fallback relies on this (04-design.md section 4).
        let tmp = TempDir::new("open-empty-tx");
        let (mut conn, _) = open(tmp.path(), Requested::default());
        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();
        let _: i64 = tx.query_row("SELECT count FROM state", [], |r| r.get(0)).unwrap();
        tx.commit().unwrap();
        assert_eq!(data_version(&peer), before);
    }

    #[test]
    fn size_limit_below_the_minimum_is_rejected() {
        let tmp = TempDir::new("open-min");
        ensure_directory(tmp.path()).unwrap();
        let mut conn = connect(tmp.path(), T).unwrap();
        let err = prepare(
            &mut conn,
            Requested { size_limit: Some(MIN_SIZE_LIMIT - 1), cull_limit: None, eviction_policy: None },
            Role::Plain,
            T,
        )
        .unwrap_err();
        assert!(matches!(err, Error::InvalidSetting(_)), "{err:?}");
        assert!(
            prepare(
                &mut conn,
                Requested { size_limit: Some(MIN_SIZE_LIMIT), cull_limit: None, eviction_policy: None },
                Role::Plain,
                T
            )
            .is_ok()
        );
    }

    #[test]
    fn a_newer_format_is_refused_without_writing() {
        let tmp = TempDir::new("open-newer");
        drop(open(tmp.path(), Requested::default()));
        connect(tmp.path(), T).unwrap().execute_batch("PRAGMA user_version = 4").unwrap();
        let peer = connect(tmp.path(), T).unwrap();
        let before = data_version(&peer);
        let mut conn = connect(tmp.path(), T).unwrap();
        let err = prepare(
            &mut conn,
            Requested { size_limit: Some(9 << 20), cull_limit: None, eviction_policy: None },
            Role::Plain,
            T,
        )
        .unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found: 4, expected: 3, .. }), "{err:?}");
        assert_eq!(data_version(&peer), before);
    }

    #[test]
    fn a_newer_format_outside_wal_mode_is_refused_without_being_switched_to_wal() {
        // An older build meeting a newer cache must refuse it (and so redirect) before `ensure_wal`
        // runs `PRAGMA journal_mode = WAL` on a file it can't read (plan 26, M2).
        let tmp = TempDir::new("open-newer-not-wal");
        drop(open(tmp.path(), Requested::default()));
        {
            let raw = connect(tmp.path(), T).unwrap();
            let mode: String = raw.query_row("PRAGMA journal_mode = DELETE", [], |r| r.get(0)).unwrap();
            assert_eq!(mode, "delete");
            raw.execute_batch("PRAGMA user_version = 4").unwrap();
        }
        let db = db_path(tmp.path());
        let before = std::fs::read(&db).unwrap();
        let mut conn = connect(tmp.path(), T).unwrap();
        let err = prepare(&mut conn, Requested::default(), Role::Plain, T).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found: 4, expected: 3, .. }), "{err:?}");
        let mode: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
        assert_eq!(mode, "delete", "a refused open must not switch the database to WAL");
        drop(conn);
        assert!(!db.with_extension("sqlite-wal").exists(), "a refused open must not create a WAL");
        assert_eq!(std::fs::read(&db).unwrap(), before, "a refused open must not write");
    }

    #[test]
    fn a_format_1_cache_is_refused() {
        // Format 1 kept value files two levels deep (plan 17). Refused here; the caller redirects (plan 26).
        let tmp = TempDir::new("open-format-1");
        drop(open(tmp.path(), Requested::default()));
        connect(tmp.path(), T).unwrap().execute_batch("PRAGMA user_version = 1").unwrap();
        let mut conn = connect(tmp.path(), T).unwrap();
        let err = prepare(&mut conn, Requested::default(), Role::Plain, T).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found: 1, expected: 3, .. }), "{err:?}");
    }

    #[test]
    fn a_format_2_cache_is_refused_with_a_message_saying_what_to_do() {
        // Format 2 had no tag or eviction policy columns (plan 24, decision 22). Refused here; the
        // caller redirects (plan 26).
        let tmp = TempDir::new("open-format-2");
        drop(open(tmp.path(), Requested::default()));
        connect(tmp.path(), T).unwrap().execute_batch("PRAGMA user_version = 2").unwrap();
        let mut conn = connect(tmp.path(), T).unwrap();
        let err = prepare(&mut conn, Requested::default(), Role::Plain, T).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found: 2, expected: 3, path: Some(_) }), "{err:?}");
        let message = err.to_string();
        assert!(message.contains("diskcache-oxide.sqlite") && message.contains("delete"), "{message}");
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
                    crate::lockwait::retry(T, |_| {
                        prepare(&mut connect(&dir, T)?, Requested::default(), Role::Plain, T)
                    })
                    .unwrap();
                });
            }
        });
        let conn = connect(&dir, T).unwrap();
        let rows: i64 = conn.query_row("SELECT COUNT(*) FROM state", [], |r| r.get(0)).unwrap();
        assert_eq!(rows, 1);
    }

    fn open_as(dir: &Path, role: Role) -> Result<Limits> {
        ensure_directory(dir).unwrap();
        prepare(&mut connect(dir, T)?, Requested::default(), role, T)
    }

    fn stamp_row(dir: &Path) -> Option<String> {
        connect(dir, T)
            .unwrap()
            .query_row("SELECT value FROM settings WHERE name = 'fanout'", [], |r| r.get(0))
            .optional()
            .unwrap()
    }

    #[test]
    fn a_new_shard_is_stamped_once_and_a_plain_cache_is_not() {
        let tmp = TempDir::new("open-stamp");
        open_as(&tmp.path().join("003"), Role::Shard { index: 3, count: 8 }).unwrap();
        assert_eq!(stamp_row(&tmp.path().join("003")).as_deref(), Some("3/8/xxh3-64 seed=0 range=mulhi"));
        open_as(&tmp.path().join("plain"), Role::Plain).unwrap();
        assert_eq!(stamp_row(&tmp.path().join("plain")), None);

        // Reopening the shard as itself writes nothing (D-019), and the row is never rewritten.
        let dir = tmp.path().join("003");
        let peer = connect(&dir, T).unwrap();
        let before = data_version(&peer);
        open_as(&dir, Role::Shard { index: 3, count: 8 }).unwrap();
        assert_eq!(data_version(&peer), before);
    }

    #[test]
    fn stamps_round_trip_and_only_the_canonical_rendering_parses() {
        for role in [
            Role::Shard { index: 0, count: 1 },
            Role::Shard { index: 7, count: 8 },
            Role::Shard { index: 255, count: 256 },
        ] {
            assert_eq!(Role::from_stamp(&role.stamp().unwrap()), Some(role));
        }
        assert_eq!(Role::Plain.stamp(), None);
        for bad in [
            "",
            "1/8",
            "1/8/",
            "01/8/xxh3-64 seed=0 range=mulhi",
            "+1/8/xxh3-64 seed=0 range=mulhi",
            " 1/8/xxh3-64 seed=0 range=mulhi",
            "1/8/xxh3-64 seed=0 range=mulhi ",
            "1/8/xxh3-64 seed=1 range=mulhi",
            "8/8/xxh3-64 seed=0 range=mulhi",
            "0/0/xxh3-64 seed=0 range=mulhi",
            "0/257/xxh3-64 seed=0 range=mulhi",
            "1/8/xxh3-64 seed=0 range=mulhi/x",
        ] {
            assert_eq!(Role::from_stamp(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn out_of_range_shard_roles_are_invalid_settings() {
        let tmp = TempDir::new("open-role-range");
        for role in [
            Role::Shard { index: 8, count: 8 },
            Role::Shard { index: 0, count: 0 },
            Role::Shard { index: 0, count: 257 },
        ] {
            let err = open_as(tmp.path(), role).unwrap_err();
            assert!(matches!(err, Error::InvalidSetting(_)), "{err:?}");
        }
        // Refused before anything was created.
        let version: i64 = connect(tmp.path(), T).unwrap().query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        assert_eq!(version, 0);
    }

    #[test]
    fn a_role_that_differs_from_the_stamp_is_refused_without_writing() {
        let tmp = TempDir::new("open-role-refused");
        let shard = tmp.path().join("001");
        open_as(&shard, Role::Shard { index: 1, count: 8 }).unwrap();
        let plain = tmp.path().join("plain");
        open_as(&plain, Role::Plain).unwrap();
        let peers = [(connect(&shard, T).unwrap(), &shard), (connect(&plain, T).unwrap(), &plain)];
        let before: Vec<i64> = peers.iter().map(|(c, _)| data_version(c)).collect();

        let cases = [
            (&shard, Role::Plain, "open the FanoutCache's root directory"),
            (&shard, Role::Shard { index: 0, count: 8 }, "opened as shard 0 of 8"),
            (&shard, Role::Shard { index: 1, count: 4 }, "opened as shard 1 of 4"),
            // A database a plain Cache created is never adopted as a shard.
            (&plain, Role::Shard { index: 0, count: 8 }, "belongs to a plain Cache"),
        ];
        for (dir, role, says) in cases {
            // With and without explicit settings: neither may write.
            for requested in
                [Requested::default(), Requested { size_limit: Some(9 << 20), cull_limit: None, eviction_policy: None }]
            {
                let err = prepare(&mut connect(dir, T).unwrap(), requested, role, T).unwrap_err();
                assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");
                assert!(err.to_string().contains(says), "{err}");
            }
        }
        let after: Vec<i64> = peers.iter().map(|(c, _)| data_version(c)).collect();
        assert_eq!(after, before, "a refused open must not write");
        assert_eq!(stamp_row(&plain), None, "the plain database was not adopted");
    }

    #[test]
    fn an_unrecognized_stamp_is_refused_under_every_role() {
        let tmp = TempDir::new("open-role-foreign");
        open_as(tmp.path(), Role::Shard { index: 1, count: 8 }).unwrap();
        connect(tmp.path(), T)
            .unwrap()
            .execute("UPDATE settings SET value = '1/8/some-future-hash' WHERE name = 'fanout'", [])
            .unwrap();
        for role in [Role::Plain, Role::Shard { index: 1, count: 8 }] {
            let err = open_as(tmp.path(), role).unwrap_err();
            assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");
            assert!(err.to_string().contains("unrecognized FanoutCache shard stamp"), "{err}");
        }
        connect(tmp.path(), T).unwrap().execute("UPDATE settings SET value = 18 WHERE name = 'fanout'", []).unwrap();
        assert!(matches!(open_as(tmp.path(), Role::Plain), Err(Error::RoleMismatch { .. })));
    }

    #[test]
    fn racing_shard_creators_agree_on_one_stamp() {
        let tmp = TempDir::new("open-role-race");
        let dir = tmp.path().join("002");
        let barrier = std::sync::Barrier::new(8);
        let results: Vec<Result<Limits>> = std::thread::scope(|s| {
            let handles: Vec<_> = (0..8u32)
                .map(|i| {
                    let (barrier, dir) = (&barrier, &dir);
                    s.spawn(move || {
                        // Half ask for 2 of 4 shards, half for 2 of 8: one count wins the stamp.
                        let role = Role::Shard { index: 2, count: if i % 2 == 0 { 4 } else { 8 } };
                        barrier.wait();
                        ensure_directory(dir).unwrap();
                        crate::lockwait::retry(T, |_| prepare(&mut connect(dir, T)?, Requested::default(), role, T))
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let stamp = stamp_row(&dir).unwrap();
        let winner = Role::from_stamp(&stamp).unwrap();
        let Role::Shard { count: won, .. } = winner else { unreachable!() };
        for (i, result) in (0..8u32).zip(&results) {
            let asked = if i % 2 == 0 { 4 } else { 8 };
            match result {
                Ok(_) => assert_eq!(asked, won),
                Err(err) => {
                    assert_ne!(asked, won);
                    assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");
                }
            }
        }
    }
}
