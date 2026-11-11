use std::path::PathBuf;
use std::time::Duration;

use rusqlite::ErrorCode;

/// Every failure the storage engine reports. The py crate maps each variant to a Python exception.
///
/// There is deliberately no `From<rusqlite::Error>`: every rusqlite error goes through
/// [`Error::from_sqlite`], so a plain `?` can't skip the busy classification.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum Error {
    /// A lock stayed held, or a fork stayed pending, for longer than the cache's timeout.
    #[error("timed out after {seconds} s waiting for a lock or a pending fork")]
    Timeout { seconds: f64 },

    #[error("I/O error on {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    /// The disk or quota is full (`SQLITE_FULL`, or `ENOSPC` from a value-file write).
    #[error("disk full: {source}")]
    DiskFull {
        #[source]
        source: Box<dyn std::error::Error + Send + Sync>,
    },

    /// SQLite couldn't read or write the database files: `SQLITE_IOERR` and its extended codes,
    /// `SQLITE_CANTOPEN`, `SQLITE_PERM` and `SQLITE_NOLFS`.
    #[error("SQLite I/O error: {source}")]
    SqliteIo {
        /// The OS error behind it, where known. rusqlite has no safe `sqlite3_system_errno`, so
        /// [`Error::from_sqlite`] leaves it `None`.
        errno: Option<i32>,
        #[source]
        source: rusqlite::Error,
    },

    /// Any other SQLite failure.
    #[error("SQLite error: {0}")]
    Sqlite(#[source] rusqlite::Error),

    /// The database was written by a format version this build doesn't know.
    /// `path` names the file that declared it when that isn't the database itself (a `FanoutCache`
    /// manifest).
    #[error("cache format version {found} is not supported (this build uses version {expected}){}", path_note(.path.as_ref()))]
    FormatMismatch { found: i64, expected: i64, path: Option<PathBuf> },

    /// A `FanoutCache` root's manifest isn't the exact bytes this build writes (plan 13, section 1):
    /// missing lines, extra whitespace, a shard count out of range, an oversized file. Python
    /// `FormatMismatchError`.
    #[error("invalid fan-out manifest {path}: {reason}")]
    ManifestInvalid { path: PathBuf, reason: String },

    /// A directory holds the other kind of cache than the one opening it (plan 13, section 1): a
    /// plain `Cache` on a `FanoutCache` root or shard, a `FanoutCache` on a plain `Cache`'s directory,
    /// or a shard opened as another index or count than its stamp says (a renamed or copied shard
    /// directory). Python `LayoutMismatchError`.
    #[error("{path}: {reason}")]
    RoleMismatch { path: PathBuf, reason: String },

    /// A `FanoutCache` was opened with a `shards=` that differs from the count its root records.
    /// Python `LayoutMismatchError`, like [`Error::RoleMismatch`].
    #[error(
        "this FanoutCache has {stored} shards but shards={requested} was requested; changing the count needs a new directory"
    )]
    ShardCountMismatch { stored: u32, requested: u32 },

    /// The `Caches` registry's open (`ShardRequest::ForNewRoot`) kept a root's stored count, but a
    /// setting that fits the registered count doesn't fit the stored one (a `size_limit` or
    /// `max_connections` too small to split over `stored` shards). Python `LayoutMismatchError`, so
    /// the registry serves its reported `NullCache` (plan 15, R1).
    #[error(
        "this FanoutCache has {stored} shards but is registered with shards={requested}, and its settings don't fit {stored} shards: {reason}; delete the directory to apply shards={requested}, or raise the setting"
    )]
    StoredShardsRefuseSettings { stored: u32, requested: u32, reason: String },

    /// SQLite refused WAL mode, e.g. on a network filesystem.
    #[error("the cache requires WAL journal mode, but SQLite is using {mode:?}")]
    NotWal { mode: String },

    #[error("the cache is closed")]
    Closed,

    /// A forked child tried to use a transaction it inherited from its parent.
    #[error("this transaction was inherited across fork() and can't be used in the child")]
    ForkPoisoned,

    /// A write in a child forked while its parent thread held a transaction on this database
    /// (D-020). The inherited SQLite lock table means it could never succeed, so it fails at once.
    #[error("this database can't be written in a process forked inside a transaction")]
    Unwritable,

    /// A call that can't run inside a held transaction, naming the call and why (Python
    /// `RuntimeError`).
    #[error("{0}")]
    InsideTransaction(&'static str),

    /// A stored key that isn't one this build can read: a damaged page or file.
    #[error("a stored key is damaged: {0}")]
    CorruptKey(crate::key::KeyError),

    /// `incr`/`decr` with `default=None` on a missing or expired key (Python `KeyError`).
    #[error("key not found")]
    KeyMissing,

    /// `incr`/`decr` on a value that isn't a native int or float (Python `TypeError`).
    #[error("the stored value is not a number")]
    NotNumeric,

    /// `incr`/`decr` past the 64-bit integer range (Python `OverflowError`), as diskcache does.
    #[error("integer overflow")]
    Overflow,

    /// A setting the cache can't honor, such as a `size_limit` under the minimum (D-005).
    #[error("{0}")]
    InvalidSetting(String),

    /// The calling thread tried to start a write transaction while it already holds one.
    #[error("this thread already holds a write transaction on this cache")]
    NestedTransaction,

    /// A write transaction was started inside another operation or another cache's transaction
    /// (gate depth > 0) while this cache's write lock was taken. Waiting there could deadlock (two
    /// threads each holding one cache and wanting the other's, or a pending fork), so it fails at
    /// once. Never retry it in a loop: the lock's holder may be waiting on this thread. Python
    /// `Timeout`, so existing handlers still catch it.
    #[error("this cache's write lock is taken, and it can't be waited for inside another transaction")]
    WriteLockBusyInTransaction,

    /// The calling thread tried to end a write transaction it doesn't hold.
    #[error("this thread holds no write transaction on this cache")]
    NoTransaction,

    /// A held write transaction was ended from inside one of its own operations (a pickle hook
    /// that leaves the `with` block, say). It can't end there; it's rolled back when that
    /// operation returns.
    #[error(
        "a write transaction can't end inside one of its own operations; it is rolled back when that operation returns"
    )]
    EndInsideOperation,

    /// A held write transaction can't continue: SQLite rolled it back after an error (a full disk,
    /// an I/O error), or an operation that failed inside it couldn't be undone. Nothing in it
    /// commits; it can only be ended, which rolls back.
    #[error("the write transaction was rolled back after an error; nothing in it can commit")]
    TransactionAborted,
}

/// The " (in <path>)" suffix of a [`Error::FormatMismatch`] that came from a file other than the database.
fn path_note(path: Option<&PathBuf>) -> String {
    path.map_or_else(String::new, |p| format!(" (in {})", p.display()))
}

impl Error {
    /// Classifies a rusqlite error: busy or locked becomes [`Error::Timeout`] (SQLite only reports
    /// them once the busy handler has waited out `timeout`), `SQLITE_FULL` [`Error::DiskFull`], the
    /// I/O family [`Error::SqliteIo`], and anything else [`Error::Sqlite`].
    #[must_use]
    pub fn from_sqlite(error: rusqlite::Error, timeout: Duration) -> Self {
        match error.sqlite_error_code() {
            Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked) => {
                Self::Timeout { seconds: timeout.as_secs_f64() }
            }
            Some(ErrorCode::DiskFull) => Self::DiskFull { source: Box::new(error) },
            Some(
                ErrorCode::SystemIoFailure
                | ErrorCode::CannotOpen
                | ErrorCode::PermissionDenied
                | ErrorCode::NoLargeFileSupport,
            ) => Self::SqliteIo { errno: None, source: error },
            _ => Self::Sqlite(error),
        }
    }

    /// Whether this says the cache is unavailable right now, rather than that the call was wrong.
    /// `Cache(durable=True)` answers an operational failure on a single-key call as a miss.
    ///
    /// Exhaustive on purpose: a new variant has to be decided here, and can't fall into either side
    /// by default.
    #[must_use]
    pub fn is_operational(&self) -> bool {
        match self {
            Self::Timeout { .. }
            | Self::DiskFull { .. }
            | Self::SqliteIo { .. }
            | Self::Unwritable
            | Self::ForkPoisoned => true,
            Self::Io { .. } | Self::Sqlite(_) => !self.is_out_of_memory(),
            // Only an open reports the first four, and the constructor always raises. The rest are
            // bugs in the caller, or would break its transaction's atomicity.
            Self::FormatMismatch { .. }
            | Self::ManifestInvalid { .. }
            | Self::RoleMismatch { .. }
            | Self::ShardCountMismatch { .. }
            | Self::StoredShardsRefuseSettings { .. }
            | Self::NotWal { .. }
            | Self::InvalidSetting(_)
            | Self::Closed
            | Self::KeyMissing
            | Self::NotNumeric
            | Self::Overflow
            | Self::NestedTransaction
            | Self::WriteLockBusyInTransaction
            | Self::NoTransaction
            | Self::EndInsideOperation
            | Self::TransactionAborted
            // Damage, not an outage: never answered as a miss.
            | Self::CorruptKey(_)
            | Self::InsideTransaction(_) => false,
        }
    }

    /// `SQLITE_NOMEM`, or no memory for a value file: Python `MemoryError`, which is never answered
    /// as a miss.
    #[must_use]
    pub fn is_out_of_memory(&self) -> bool {
        match self {
            Self::Sqlite(e) => e.sqlite_error_code() == Some(ErrorCode::OutOfMemory),
            Self::Io { source, .. } => source.kind() == std::io::ErrorKind::OutOfMemory,
            _ => false,
        }
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;

#[cfg(test)]
mod tests {
    use rusqlite::ffi;

    use super::*;

    fn failure(code: i32) -> rusqlite::Error {
        rusqlite::Error::SqliteFailure(ffi::Error::new(code), None)
    }

    #[test]
    fn sqlite_errors_are_classified() {
        let timeout = Duration::from_secs(3);
        assert!(matches!(Error::from_sqlite(failure(ffi::SQLITE_BUSY), timeout), Error::Timeout { .. }));
        assert!(matches!(Error::from_sqlite(failure(ffi::SQLITE_LOCKED), timeout), Error::Timeout { .. }));
        assert!(matches!(Error::from_sqlite(failure(ffi::SQLITE_BUSY_SNAPSHOT), timeout), Error::Timeout { .. }));
        assert!(matches!(Error::from_sqlite(failure(ffi::SQLITE_FULL), timeout), Error::DiskFull { .. }));
        assert!(matches!(
            Error::from_sqlite(failure(ffi::SQLITE_IOERR_WRITE), timeout),
            Error::SqliteIo { errno: None, .. }
        ));
        assert!(matches!(Error::from_sqlite(failure(ffi::SQLITE_CANTOPEN), timeout), Error::SqliteIo { .. }));
        assert!(matches!(Error::from_sqlite(failure(ffi::SQLITE_CORRUPT), timeout), Error::Sqlite(_)));
        assert!(matches!(Error::from_sqlite(rusqlite::Error::QueryReturnedNoRows, timeout), Error::Sqlite(_)));
    }

    #[test]
    fn operational_failures_are_told_from_bugs() {
        let timeout = Duration::from_secs(3);
        let sqlite = |code| Error::from_sqlite(failure(code), timeout);
        let operational = [
            Error::Timeout { seconds: 1.0 },
            Error::Io { path: PathBuf::from("x"), source: std::io::ErrorKind::PermissionDenied.into() },
            sqlite(ffi::SQLITE_BUSY),
            sqlite(ffi::SQLITE_FULL),
            sqlite(ffi::SQLITE_IOERR_WRITE),
            sqlite(ffi::SQLITE_CANTOPEN),
            sqlite(ffi::SQLITE_CORRUPT),
            sqlite(ffi::SQLITE_NOTADB),
            Error::Unwritable,
            Error::ForkPoisoned,
        ];
        for error in &operational {
            assert!(error.is_operational(), "{error:?}");
            assert!(!error.is_out_of_memory(), "{error:?}");
        }
        let raised = [
            sqlite(ffi::SQLITE_NOMEM),
            Error::Io { path: PathBuf::from("x"), source: std::io::ErrorKind::OutOfMemory.into() },
            Error::FormatMismatch { found: 2, expected: 1, path: None },
            Error::ManifestInvalid { path: PathBuf::from("x"), reason: "y".into() },
            Error::RoleMismatch { path: PathBuf::from("x"), reason: "y".into() },
            Error::ShardCountMismatch { stored: 8, requested: 16 },
            Error::StoredShardsRefuseSettings { stored: 8, requested: 4, reason: "x".into() },
            Error::NotWal { mode: "delete".into() },
            Error::Closed,
            Error::KeyMissing,
            Error::NotNumeric,
            Error::Overflow,
            Error::InvalidSetting("x".into()),
            Error::NestedTransaction,
            Error::InsideTransaction("x"),
            Error::WriteLockBusyInTransaction,
            Error::NoTransaction,
            Error::EndInsideOperation,
            Error::TransactionAborted,
        ];
        for error in &raised {
            assert!(!error.is_operational(), "{error:?}");
        }
        assert!(sqlite(ffi::SQLITE_NOMEM).is_out_of_memory());
    }
}
