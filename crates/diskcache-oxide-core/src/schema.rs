//! The on-disk format, version 3 (04-design.md section 3; plan 24, release 3).
//!
//! A format change never migrates in place: it keeps the database filename and raises
//! `user_version`. A build never reads or writes another format's database: since plan 26 it opens a
//! cache of its own in the directory's `diskcache-oxide-format-N` subdirectory instead
//! ([`crate::layout`]), and `remove_old_formats()` ([`crate::cleanup`]) removes the old one once
//! its workers are gone. (Before plan 26 the other format was refused and caches were deleted on
//! upgrade, with every worker stopped: Michael, 2026-11-06, and decision 22 of plan 24.)
//!
//! Frozen in every future format, or builds of two formats would disagree about where a cache
//! lives: [`DB_FILENAME`], `PRAGMA user_version` as the format, the manifest's first two lines,
//! the subdirectory's name and the rule in [`crate::layout::resolve`]. A change to the manifest's
//! format also raises [`FORMAT_VERSION`], so the subdirectory names one format for both. Version 2
//! moved value files one level deep (plan 17); version 3 adds tags and the eviction policy. A new
//! filename is no option even for a change confined to the database: two databases in one directory
//! would share its value folders, and `check(fix=True)` removes value files its own database doesn't
//! name.
//!
//! Version 3 also defends against an older worker that outlives the deletion and opens the new file
//! by path (a pooled connection, or a closed handle used again): it would never check the format
//! again. Formats 1 and 2 kept their counts and limits in a table named `totals`, which every write,
//! delete, pop and maintenance call of theirs reads or updates; version 3 names it `state`, so each
//! of those fails with "no such table" and writes nothing (architect, release 3's review, M1: under
//! the old name a 0.1.2 `cull()` emptied a 'none' cache). `tag_type` is `NOT NULL` as well, so an
//! older insert fails on its own. Only an older `touch` and reads still reach the new file, which
//! breaks nothing.

/// Database filename (D-002: never diskcache's `cache.db`).
pub const DB_FILENAME: &str = "diskcache-oxide.sqlite";

/// `PRAGMA user_version` of a current database. Version 1 kept value files two levels deep; version 2
/// had no tags or eviction policy.
pub const FORMAT_VERSION: i64 = 3;

/// `tag_type` of an untagged row (`tag` is then NULL). A tag is stored as a key is: `KEY_TYPE_STR`
/// for a `str`, `KEY_TYPE_TYPED` for any other key type.
pub const TAG_TYPE_NONE: i64 = 0;

/// How a cache makes room when it's over `size_limit`, as stored in `state.eviction_policy`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EvictionPolicy {
    /// Evict the least recently stored entries (the default).
    #[default]
    LeastRecentlyStored,
    /// Never evict for size; expired entries are still removed.
    None,
}

impl EvictionPolicy {
    /// The stored code. 0 is never one, so a zeroed value can't quietly mean "never evict".
    #[must_use]
    pub fn code(self) -> i64 {
        match self {
            Self::LeastRecentlyStored => 1,
            Self::None => 2,
        }
    }

    /// diskcache's name for the policy.
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::LeastRecentlyStored => "least-recently-stored",
            Self::None => "none",
        }
    }
}

impl TryFrom<i64> for EvictionPolicy {
    type Error = crate::Error;

    fn try_from(code: i64) -> crate::Result<Self> {
        match code {
            1 => Ok(Self::LeastRecentlyStored),
            2 => Ok(Self::None),
            other => Err(crate::Error::InvalidSetting(format!("the stored eviction_policy {other} is unknown"))),
        }
    }
}

/// `size_limit` of a new cache, in bytes (diskcache's default).
pub const DEFAULT_SIZE_LIMIT: i64 = 1 << 30;

/// `cull_limit` of a new cache, in rows per call (diskcache's default).
pub const DEFAULT_CULL_LIMIT: i64 = 10;

/// The smallest accepted `size_limit` (D-005).
pub const MIN_SIZE_LIMIT: i64 = 1 << 20;

/// `key_type` of a `str` key, stored as TEXT.
pub const KEY_TYPE_STR: i64 = 1;

/// `key_type` of every other key, stored as a BLOB in the format `crate::key` defines.
pub const KEY_TYPE_TYPED: i64 = 2;

/// Creates every table and index plus the single `state` row. Runs once, inside the creating
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
    store_seq INTEGER NOT NULL,
    tag_type INTEGER NOT NULL,
    tag TEXT,
    CHECK ((tag_type = 0) = (tag IS NULL))
);
CREATE UNIQUE INDEX cache_key ON cache (key_type, key);
CREATE INDEX cache_store_seq ON cache (store_seq);
CREATE INDEX cache_expire ON cache (expire_time) WHERE expire_time IS NOT NULL;
CREATE INDEX cache_filename ON cache (filename) WHERE filename IS NOT NULL;
CREATE INDEX cache_tag ON cache (tag_type, tag, store_seq) WHERE tag IS NOT NULL;
CREATE TABLE settings (name TEXT PRIMARY KEY, value) WITHOUT ROWID;
CREATE TABLE state (
    only INTEGER PRIMARY KEY CHECK (only = 1),
    count INTEGER NOT NULL,
    size INTEGER NOT NULL,
    next_seq INTEGER NOT NULL,
    size_limit INTEGER NOT NULL,
    cull_limit INTEGER NOT NULL,
    eviction_policy INTEGER NOT NULL CHECK (eviction_policy IN (1, 2))
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
