//! The on-disk format, version 1 (04-design.md section 3).
//!
//! A format change never migrates in place: it gets a new database filename, so old and new
//! workers overlapping during a deploy each see only their own file. `user_version` stays as a
//! safety check against a mislabeled file.

/// Database filename for format version 1 (D-002: never diskcache's `cache.db`).
pub const DB_FILENAME: &str = "diskcache-oxide.sqlite";

/// `PRAGMA user_version` of a format-1 database.
pub const FORMAT_VERSION: i64 = 1;

/// `size_limit` of a new cache, in bytes (diskcache's default).
pub const DEFAULT_SIZE_LIMIT: i64 = 1 << 30;

/// `cull_limit` of a new cache, in rows per call (diskcache's default).
pub const DEFAULT_CULL_LIMIT: i64 = 10;

/// The smallest accepted `size_limit` (D-005).
pub const MIN_SIZE_LIMIT: i64 = 1 << 20;

/// `key_type` of a `str` key. Other key types are reserved for Phase 6a.
pub const KEY_TYPE_STR: i64 = 1;

/// Creates every table and index plus the single `totals` row. Runs once, inside the creating
/// connection's `BEGIN IMMEDIATE`, after `user_version` was re-checked under the lock.
pub(crate) const CREATE: &str = "
CREATE TABLE cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_type INTEGER NOT NULL,
    key TEXT NOT NULL,
    mode INTEGER NOT NULL,
    value BLOB,
    filename TEXT,
    size INTEGER NOT NULL,
    store_time REAL NOT NULL,
    expire_time REAL,
    store_seq INTEGER NOT NULL
);
CREATE UNIQUE INDEX cache_key ON cache (key_type, key);
CREATE INDEX cache_store_seq ON cache (store_seq);
CREATE INDEX cache_expire ON cache (expire_time) WHERE expire_time IS NOT NULL;
CREATE INDEX cache_filename ON cache (filename) WHERE filename IS NOT NULL;
CREATE TABLE settings (name TEXT PRIMARY KEY, value) WITHOUT ROWID;
CREATE TABLE totals (
    only INTEGER PRIMARY KEY CHECK (only = 1),
    count INTEGER NOT NULL,
    size INTEGER NOT NULL,
    next_seq INTEGER NOT NULL,
    size_limit INTEGER NOT NULL,
    cull_limit INTEGER NOT NULL
);
";

/// Value modes (04-design.md section 5). The core stores modes 1-5 as opaque bytes; 6 and 7 are
/// native SQLite numbers so `incr` can work on them inside a write transaction.
pub const MODE_STR: i64 = 1;
pub const MODE_BYTES: i64 = 2;
pub const MODE_MSGPACK: i64 = 3;
pub const MODE_JSON: i64 = 4;
pub const MODE_PICKLE: i64 = 5;
pub const MODE_INT: i64 = 6;
pub const MODE_REAL: i64 = 7;
