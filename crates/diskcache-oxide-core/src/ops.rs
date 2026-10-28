//! Cache operations on a [`Store`] (Phase 2 step 3; 04-design.md sections 5-6).
//!
//! Everything here deals in modes and bytes or native numbers; converting Python objects lives in
//! the py crate. Every function takes `now` (seconds since the epoch) from its caller, which reads
//! the clock once per operation before any connection is taken (`clock=` support). Expiry follows
//! diskcache's pinned semantics: an entry is live while `expire_time IS NULL OR expire_time > now`.
//!
//! Values of `spill::THRESHOLD` bytes or more are stored in value files (see `spill`).

use std::time::Duration;

use rusqlite::types::ValueRef;
use rusqlite::{Connection, OptionalExtension};

use crate::rows::{NewRow, Removed, RowWriter, Stored};
use crate::schema::{KEY_TYPE_STR, MODE_INT, MODE_REAL};
use crate::spill::{self, PendingFile};
use crate::store::Store;
use crate::{Error, Result};

/// A value to store.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Value<'a> {
    /// Opaque bytes in `mode` (raw str, raw bytes or a serialized value).
    Bytes { mode: i64, data: &'a [u8] },
    /// A native integer (mode 6).
    Int(i64),
    /// A native finite float (mode 7).
    Real(f64),
}

impl Value<'_> {
    fn mode(&self) -> i64 {
        match self {
            Value::Bytes { mode, .. } => *mode,
            Value::Int(_) => MODE_INT,
            Value::Real(_) => MODE_REAL,
        }
    }

    fn stored(&self) -> Stored<'_> {
        match *self {
            Value::Bytes { data, .. } => Stored::Blob(data),
            Value::Int(n) => Stored::Int(n),
            Value::Real(x) => Stored::Real(x),
        }
    }

    /// Bytes this value adds to `volume()` beyond the database pages. Inline values are already
    /// counted by `page_count × page_size`, so they add nothing here; only value files (step 5) do.
    #[allow(clippy::unused_self)] // Becomes non-trivial when values spill to files.
    fn size(&self) -> i64 {
        0
    }
}

/// A number for `incr`/`decr`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Number {
    Int(i64),
    Real(f64),
}

impl Number {
    fn add(self, other: Number) -> Result<Number> {
        match (self, other) {
            (Number::Int(a), Number::Int(b)) => a.checked_add(b).map(Number::Int).ok_or(Error::Overflow),
            #[allow(clippy::cast_precision_loss)] // Python's int + float semantics.
            (Number::Int(a), Number::Real(b)) => Ok(Number::Real(a as f64 + b)),
            #[allow(clippy::cast_precision_loss)]
            (Number::Real(a), Number::Int(b)) => Ok(Number::Real(a + b as f64)),
            (Number::Real(a), Number::Real(b)) => Ok(Number::Real(a + b)),
        }
    }

    fn value(self) -> Value<'static> {
        match self {
            Number::Int(n) => Value::Int(n),
            Number::Real(x) => Value::Real(x),
        }
    }
}

/// Seconds-from-now to an absolute expire time. `None` never expires; zero or negative is already
/// expired, as in diskcache.
fn expire_time(now: f64, expire: Option<f64>) -> Option<f64> {
    expire.map(|seconds| now + seconds)
}

impl Store {
    /// Calls `decode(mode, value)` on the live value of `key` and returns its result with the entry's
    /// expire time, or `None` on a miss. For an inline value `decode` runs while the connection is
    /// held, straight from SQLite's row buffer, so it must not call back into the cache; it may run
    /// more than once. A value file is read with no connection held (see `spill`).
    pub fn get_with<T>(
        &self,
        timeout: Duration,
        key: &str,
        now: f64,
        mut decode: impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<(T, Option<f64>)>> {
        let mut row = self.read(timeout, |conn| look_up(conn, key, now, timeout, &mut decode))?;
        for _ in 0..spill::OPTIMISTIC_ATTEMPTS {
            let file = match row {
                Found::Miss => return Ok(None),
                Found::Inline(value, expire) => return Ok(Some((value, expire))),
                Found::File(file) => file,
            };
            if let Some(bytes) = spill::read(self.directory(), &file.relative, file.size)? {
                return Ok(Some((decode(file.mode, ValueRef::Blob(&bytes))?, file.expire_time)));
            }
            // Missing or short: overwritten since the row was read, or truly gone.
            row = self.read(timeout, |conn| look_up(conn, key, now, timeout, &mut decode))?;
            if let Found::File(again) = &row
                && again.id == file.id
                && again.store_seq == file.store_seq
            {
                // Same version, no readable file: a true miss (D-013). Remove exactly that version,
                // only if it needs no waiting; a later write's cleanup gets it otherwise.
                if let Some(Ok(Some(removed))) =
                    self.write_nowait(|writer| writer.delete_version(file.id, file.store_seq))
                {
                    unlink(self, [removed]);
                }
                return Ok(None);
            }
        }
        // A writer keeps replacing the value faster than this reader can open it (D-016): take the
        // row and open its file under the write lock, then read with the lock released. Skipped in a
        // child that can't write this database (D-020): it reports a miss instead.
        if self.check_writable().is_err() {
            return Ok(None);
        }
        let opened = self.write(timeout, |_, conn| {
            let Some(file) = file_row(conn, key, now, timeout)? else { return Ok(None) };
            Ok(spill::open(self.directory(), &file.relative)?.map(|(handle, path)| (handle, path, file)))
        })?;
        let Some((handle, path, file)) = opened else { return Ok(None) };
        match spill::read_open(handle, &path, file.size)? {
            Some(bytes) => Ok(Some((decode(file.mode, ValueRef::Blob(&bytes))?, file.expire_time))),
            None => Ok(None),
        }
    }

    /// Rows stored, including expired ones not yet swept (as diskcache counts). O(1): `totals`.
    pub fn len(&self, timeout: Duration) -> Result<i64> {
        self.read(timeout, |conn| {
            conn.prepare_cached("SELECT count FROM totals WHERE only = 1")
                .and_then(|mut stmt| stmt.query_row([], |r| r.get(0)))
                .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    /// The database pages in use plus the value files; see `volume_in`.
    pub fn volume(&self, timeout: Duration) -> Result<i64> {
        self.read(timeout, |conn| {
            let size: i64 = conn
                .prepare_cached("SELECT size FROM totals WHERE only = 1")
                .and_then(|mut stmt| stmt.query_row([], |r| r.get(0)))
                .map_err(|e| Error::from_sqlite(e, timeout))?;
            volume_in(conn, size, timeout)
        })
    }

    /// Whether `key` is live at `now`.
    pub fn contains(&self, timeout: Duration, key: &str, now: f64) -> Result<bool> {
        self.read(timeout, |conn| {
            conn.prepare_cached(
                "SELECT 1 FROM cache WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)",
            )
            .and_then(|mut stmt| stmt.exists((KEY_TYPE_STR, key, now)))
            .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    /// Stores `value` under `key`, then culls. Always returns `Ok(true)` on success, like diskcache.
    pub fn set(&self, timeout: Duration, key: &str, value: Value<'_>, expire: Option<f64>, now: f64) -> Result<bool> {
        let pending = self.spill_if_large(value)?;
        let result = self.write(timeout, |writer, tx| {
            let before = prelude(tx, key, now, timeout)?;
            let (delta, mut removed) =
                put_value(writer, key, value, pending.as_ref(), now, expire_time(now, expire), before.old())?;
            removed.extend(cull(writer, tx, &before, delta, now, timeout)?);
            Ok(removed)
        });
        let removed = settle_with(pending.as_ref(), result, |_| true)?;
        unlink(self, removed);
        Ok(true)
    }

    /// Stores `value` only if `key` is missing or expired. Returns whether it stored.
    pub fn add(&self, timeout: Duration, key: &str, value: Value<'_>, expire: Option<f64>, now: f64) -> Result<bool> {
        let pending = self.spill_if_large(value)?;
        let result = self.write(timeout, |writer, tx| {
            let before = prelude(tx, key, now, timeout)?;
            if before.live.is_some() {
                return Ok((false, Vec::new()));
            }
            let (delta, mut removed) =
                put_value(writer, key, value, pending.as_ref(), now, expire_time(now, expire), before.old())?;
            removed.extend(cull(writer, tx, &before, delta, now, timeout)?);
            Ok((true, removed))
        });
        let (added, removed) = settle_with(pending.as_ref(), result, |(added, _)| *added)?;
        unlink(self, removed);
        Ok(added)
    }

    /// Writes a value file for a large inline value, before any lock or connection is taken.
    fn spill_if_large(&self, value: Value<'_>) -> Result<Option<PendingFile>> {
        match value {
            Value::Bytes { data, .. } if data.len() >= spill::THRESHOLD => {
                PendingFile::write(self.directory(), data).map(Some)
            }
            _ => Ok(None),
        }
    }

    /// Deletes `key`. Returns whether a live entry was deleted; an expired entry acts missing.
    pub fn delete(&self, timeout: Duration, key: &str, now: f64) -> Result<bool> {
        let removed = self.write(timeout, |writer, tx| {
            if live_row(tx, key, now, timeout)?.is_none() {
                return Ok(None);
            }
            writer.delete_key(key)
        })?;
        let deleted = removed.is_some();
        unlink(self, removed);
        Ok(deleted)
    }

    /// Removes the live value of `key` and returns `decode(mode, value)` of it, or `None` if it was
    /// missing or expired.
    pub fn pop_with<T>(
        &self,
        timeout: Duration,
        key: &str,
        now: f64,
        mut decode: impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<T>> {
        let (value, removed) = self.write(timeout, |writer, tx| {
            let decoded = {
                let mut stmt = tx
                    .prepare_cached(
                        "SELECT mode, value, filename, size FROM cache
                         WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)",
                    )
                    .map_err(|e| Error::from_sqlite(e, timeout))?;
                let mut rows = stmt.query((KEY_TYPE_STR, key, now)).map_err(|e| Error::from_sqlite(e, timeout))?;
                match rows.next().map_err(|e| Error::from_sqlite(e, timeout))? {
                    Some(row) => {
                        let mode: i64 = row.get(0).map_err(|e| Error::from_sqlite(e, timeout))?;
                        let filename: Option<String> = row.get(2).map_err(|e| Error::from_sqlite(e, timeout))?;
                        match filename {
                            // Under the write lock nothing can replace the file, so read it here.
                            Some(relative) => {
                                let size: i64 = row.get(3).map_err(|e| Error::from_sqlite(e, timeout))?;
                                match spill::read(self.directory(), &relative, size)? {
                                    Some(bytes) => Some(decode(mode, ValueRef::Blob(&bytes))?),
                                    None => None, // A missing file acts missing (D-013); the row goes below.
                                }
                            }
                            None => Some(decode(mode, row.get_ref(1).map_err(|e| Error::from_sqlite(e, timeout))?)?),
                        }
                    }
                    None => None,
                }
            };
            match decoded {
                Some(value) => Ok((Some(value), writer.delete_key(key)?)),
                // A spilled row whose file is gone: remove it, and report a miss.
                None if live_row(tx, key, now, timeout)?.is_some() => Ok((None, writer.delete_key(key)?)),
                None => Ok((None, None)),
            }
        })?;
        unlink(self, removed);
        Ok(value)
    }

    /// Sets a live entry's expiry (`None` = never) without counting as a store. Returns whether `key`
    /// was live; an expired entry is not revived.
    pub fn touch(&self, timeout: Duration, key: &str, expire: Option<f64>, now: f64) -> Result<bool> {
        self.write(timeout, |writer, _| writer.set_expire_time(key, now, expire_time(now, expire)))
    }

    /// Adds `delta` to the number stored under `key` and returns the result.
    ///
    /// - A live native number keeps its expiry; the update counts as a store (new `store_seq`).
    /// - A missing or expired key starts from `default` and gets **no expiry** (diskcache's pinned
    ///   behavior), or fails with [`Error::KeyMissing`] when `default` is `None`.
    /// - Any other stored value fails with [`Error::NotNumeric`]; a 64-bit overflow with
    ///   [`Error::Overflow`]. Both leave the value unchanged.
    pub fn incr(
        &self,
        timeout: Duration,
        key: &str,
        delta: Number,
        default: Option<Number>,
        now: f64,
    ) -> Result<Number> {
        let (result, removed) = self.write(timeout, |writer, tx| {
            let before = prelude(tx, key, now, timeout)?;
            let (current, expire) = match &before.live {
                Some(row) => (number_of(row)?, row.expire_time),
                None => (default.ok_or(Error::KeyMissing)?, None),
            };
            let next = current.add(delta)?;
            let (delta_size, mut removed) = put_value(writer, key, next.value(), None, now, expire, before.old())?;
            removed.extend(cull(writer, tx, &before, delta_size, now, timeout)?);
            Ok((next, removed))
        })?;
        unlink(self, removed);
        Ok(result)
    }
}

/// Stores `value` (pointing the row at `pending`'s value file if it was spilled), given the stored
/// version `old` read in this transaction. Returns the change in `totals.size` and the replaced
/// version.
fn put_value(
    writer: &RowWriter<'_>,
    key: &str,
    value: Value<'_>,
    pending: Option<&PendingFile>,
    now: f64,
    expire_time: Option<f64>,
    old: Option<(i64, Option<String>)>,
) -> Result<(i64, Vec<Removed>)> {
    let old_size = old.as_ref().map_or(0, |(size, _)| *size);
    let row = match (pending, value) {
        (Some(file), Value::Bytes { mode, data }) => {
            file.commit_name()?;
            let size = i64::try_from(data.len()).unwrap_or(i64::MAX);
            NewRow { key, mode, value: Stored::File(&file.relative), size, store_time: now, expire_time }
        }
        _ => {
            NewRow { key, mode: value.mode(), value: value.stored(), size: value.size(), store_time: now, expire_time }
        }
    };
    let (_seq, replaced) = writer.upsert_after(&row, old)?;
    Ok((row.size - old_size, replaced.into_iter().collect()))
}

/// Finishes a spilled write: if it failed, or didn't use the value file, the file is removed.
fn settle_with<T>(pending: Option<&PendingFile>, result: Result<T>, used: impl FnOnce(&T) -> bool) -> Result<T> {
    if let Some(file) = pending {
        match &result {
            Ok(value) if used(value) => {}
            _ => file.discard(),
        }
    }
    result
}

/// What a lookup found: a decoded inline value, a value file still to read, or nothing.
enum Found<T> {
    Miss,
    Inline(T, Option<f64>),
    File(FileRow),
}

/// A live row whose value is in a value file.
struct FileRow {
    id: i64,
    store_seq: i64,
    mode: i64,
    relative: String,
    size: i64,
    expire_time: Option<f64>,
}

const LOOKUP_SQL: &str = "SELECT mode, value, expire_time, filename, size, id, store_seq FROM cache
     WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)";

fn look_up<T>(
    conn: &Connection,
    key: &str,
    now: f64,
    timeout: Duration,
    decode: &mut impl FnMut(i64, ValueRef<'_>) -> Result<T>,
) -> Result<Found<T>> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let mut stmt = conn.prepare_cached(LOOKUP_SQL).map_err(sqlite)?;
    let mut rows = stmt.query((KEY_TYPE_STR, key, now)).map_err(sqlite)?;
    let Some(row) = rows.next().map_err(sqlite)? else { return Ok(Found::Miss) };
    let mode: i64 = row.get(0).map_err(sqlite)?;
    let expire_time: Option<f64> = row.get(2).map_err(sqlite)?;
    match row.get::<_, Option<String>>(3).map_err(sqlite)? {
        Some(relative) => Ok(Found::File(FileRow {
            id: row.get(5).map_err(sqlite)?,
            store_seq: row.get(6).map_err(sqlite)?,
            mode,
            relative,
            size: row.get(4).map_err(sqlite)?,
            expire_time,
        })),
        None => Ok(Found::Inline(decode(mode, row.get_ref(1).map_err(sqlite)?)?, expire_time)),
    }
}

/// The live row for `key` if its value is in a file (for the locked fallback).
fn file_row(conn: &Connection, key: &str, now: f64, timeout: Duration) -> Result<Option<FileRow>> {
    let mut never = |_: i64, _: ValueRef<'_>| Ok(());
    match look_up(conn, key, now, timeout, &mut never)? {
        Found::File(file) => Ok(Some(file)),
        Found::Inline(..) | Found::Miss => Ok(None),
    }
}

/// Everything a write needs to know before it writes, in one statement: the stored version of the
/// key (live or not), the limits and totals, and whether any row has expired. Separate statements
/// here cost several microseconds per `set`.
struct Prelude {
    cull_limit: i64,
    size_limit: i64,
    /// `totals.size`: bytes in value files.
    size: i64,
    any_expired: bool,
    /// The stored version's `(size, filename)`, live or expired.
    stored: Option<(i64, Option<String>)>,
    /// The stored version, if it's live at `now`.
    live: Option<LiveRow>,
}

impl Prelude {
    fn old(&self) -> Option<(i64, Option<String>)> {
        self.stored.clone()
    }
}

const PRELUDE_SQL: &str = "
SELECT t.cull_limit, t.size_limit, t.size, 0,
       EXISTS (SELECT 1 FROM cache WHERE expire_time IS NOT NULL AND expire_time < ?3),
       c.id, c.size, c.filename, c.mode, c.value, c.expire_time
FROM totals AS t
LEFT JOIN cache AS c ON c.key_type = ?1 AND c.key = ?2
WHERE t.only = 1";

fn prelude(conn: &Connection, key: &str, now: f64, timeout: Duration) -> Result<Prelude> {
    conn.prepare_cached(PRELUDE_SQL)
        .and_then(|mut stmt| {
            stmt.query_row((KEY_TYPE_STR, key, now), |r| {
                let size: i64 = r.get(2)?;
                let stored = match r.get::<_, Option<i64>>(5)? {
                    Some(_) => Some((r.get::<_, i64>(6)?, r.get::<_, Option<String>>(7)?)),
                    None => None,
                };
                let expire_time: Option<f64> = r.get(10)?;
                let live = match (&stored, expire_time) {
                    (None, _) => None,
                    (Some(_), Some(at)) if at <= now => None,
                    (Some(_), _) => {
                        let (int, real) = match r.get_ref(9)? {
                            ValueRef::Integer(n) => (Some(n), None),
                            ValueRef::Real(x) => (None, Some(x)),
                            _ => (None, None),
                        };
                        Some(LiveRow { mode: r.get(8)?, int, real, expire_time })
                    }
                };
                Ok(Prelude { cull_limit: r.get(0)?, size_limit: r.get(1)?, size, any_expired: r.get(4)?, stored, live })
            })
        })
        .map_err(|e| Error::from_sqlite(e, timeout))
}

/// One shared budget of `cull_limit` rows per call, as in diskcache: expired rows first (earliest
/// expiry first), then, only if the cache is at or over `size_limit`, the least recently stored.
/// Uses the numbers the prelude read before this write, plus the write's own size change.
fn cull(
    writer: &RowWriter<'_>,
    tx: &Connection,
    before: &Prelude,
    size_delta: i64,
    now: f64,
    timeout: Duration,
) -> Result<Vec<Removed>> {
    if before.cull_limit <= 0 {
        return Ok(Vec::new());
    }
    let mut removed = if before.any_expired { writer.delete_expired(now, before.cull_limit)? } else { Vec::new() };
    let budget = before.cull_limit - i64::try_from(removed.len()).unwrap_or(i64::MAX);
    if budget > 0 && over_limit(tx, before.size + size_delta, before.size_limit, timeout)? {
        removed.extend(writer.delete_oldest(budget)?);
    }
    Ok(removed)
}

/// Whether the cache is at or over `size_limit`. Free pages only lower the volume, so the exact
/// figure (with `freelist_count`) is only computed when the whole file would already be over.
fn over_limit(tx: &Connection, size: i64, size_limit: i64, timeout: Duration) -> Result<bool> {
    let (pages, page_size) = pages_and_size(tx, timeout)?;
    if pages * page_size + size < size_limit {
        return Ok(false);
    }
    Ok(volume_in(tx, size, timeout)? >= size_limit)
}

/// `(page_count − freelist_count) × page_size + totals.size`: the database pages in use plus the
/// value files. Freed pages are left out: deleting rows never shrinks the file (the pages go on
/// SQLite's freelist and get reused), so counting them would keep a full cache "over the limit"
/// forever and culling could never converge (D-006 covers the difference from diskcache).
pub(crate) fn volume_in(tx: &Connection, size: i64, timeout: Duration) -> Result<i64> {
    let (pages, page_size) = pages_and_size(tx, timeout)?;
    Ok((pages - pragma(tx, "PRAGMA freelist_count", timeout)?) * page_size + size)
}

/// `page_count` and `page_size`, from cached plain PRAGMA statements. (The `pragma_*` table-valued
/// functions re-prepare their PRAGMA on every call, which showed up as SQL parsing on every `set`.)
fn pages_and_size(tx: &Connection, timeout: Duration) -> Result<(i64, i64)> {
    Ok((pragma(tx, "PRAGMA page_count", timeout)?, pragma(tx, "PRAGMA page_size", timeout)?))
}

fn pragma(tx: &Connection, sql: &str, timeout: Duration) -> Result<i64> {
    tx.prepare_cached(sql)
        .and_then(|mut stmt| stmt.query_row([], |r| r.get(0)))
        .map_err(|e| Error::from_sqlite(e, timeout))
}

struct LiveRow {
    mode: i64,
    int: Option<i64>,
    real: Option<f64>,
    expire_time: Option<f64>,
}

fn live_row(tx: &Connection, key: &str, now: f64, timeout: Duration) -> Result<Option<LiveRow>> {
    tx.prepare_cached(
        "SELECT mode, value, expire_time FROM cache
         WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)",
    )
    .and_then(|mut stmt| {
        stmt.query_row((KEY_TYPE_STR, key, now), |r| {
            let mode: i64 = r.get(0)?;
            let (int, real) = match r.get_ref(1)? {
                ValueRef::Integer(n) => (Some(n), None),
                ValueRef::Real(x) => (None, Some(x)),
                _ => (None, None),
            };
            Ok(LiveRow { mode, int, real, expire_time: r.get(2)? })
        })
        .optional()
    })
    .map_err(|e| Error::from_sqlite(e, timeout))
}

fn number_of(row: &LiveRow) -> Result<Number> {
    match (row.mode, row.int, row.real) {
        (MODE_INT, Some(n), _) => Ok(Number::Int(n)),
        (MODE_REAL, _, Some(x)) => Ok(Number::Real(x)),
        _ => Err(Error::NotNumeric),
    }
}

/// Unlinks the value files of removed rows, after their transaction committed. A file that's
/// already gone is fine. Nothing spills until step 5, so this is usually empty.
fn unlink(store: &Store, removed: impl IntoIterator<Item = Removed>) {
    for filename in removed.into_iter().filter_map(|r| r.filename) {
        let _ = std::fs::remove_file(store.directory().join(filename));
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use super::*;
    use crate::open::Requested;
    use crate::schema::{MODE_BYTES, MODE_STR};
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, OpenOptions};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);
    const NOW: f64 = 1_000_000.0;

    fn store(dir: &Path, requested: Requested) -> Arc<Store> {
        Store::open(&OpenOptions {
            directory: dir.to_path_buf(),
            timeout: T,
            requested,
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
        })
        .unwrap()
    }

    fn text(s: &str) -> Value<'_> {
        Value::Bytes { mode: MODE_STR, data: s.as_bytes() }
    }

    fn get(store: &Store, key: &str, now: f64) -> Option<(Vec<u8>, Option<f64>)> {
        store
            .get_with(T, key, now, |_, v| match v {
                ValueRef::Blob(b) | ValueRef::Text(b) => Ok(b.to_vec()),
                ValueRef::Integer(n) => Ok(n.to_string().into_bytes()),
                ValueRef::Real(x) => Ok(x.to_string().into_bytes()),
                ValueRef::Null => Ok(Vec::new()),
            })
            .unwrap()
    }

    fn len(store: &Store) -> i64 {
        store
            .read(T, |c| {
                c.query_row("SELECT count FROM totals", [], |r| r.get(0)).map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap()
    }

    #[test]
    fn expiry_boundaries_match_diskcache() {
        let tmp = TempDir::new("ops-expiry");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), Some(10.0), NOW).unwrap();
        assert!(get(&s, "k", NOW + 9.999).is_some());
        assert!(get(&s, "k", NOW + 10.0).is_none(), "a miss at exactly the expire time");
        s.set(T, "zero", text("v"), Some(0.0), NOW).unwrap();
        assert!(get(&s, "zero", NOW).is_none());
        assert_eq!(get(&s, "k", NOW).unwrap().1, Some(NOW + 10.0));
        assert_eq!(len(&s), 2, "expired rows still count until swept");
    }

    #[test]
    fn add_succeeds_only_over_missing_or_expired() {
        let tmp = TempDir::new("ops-add");
        let s = store(tmp.path(), Requested::default());
        assert!(s.add(T, "k", text("a"), Some(5.0), NOW).unwrap());
        assert!(!s.add(T, "k", text("b"), None, NOW).unwrap());
        assert_eq!(get(&s, "k", NOW).unwrap().0, b"a");
        assert!(s.add(T, "k", text("c"), None, NOW + 6.0).unwrap(), "add over an expired key succeeds");
        assert_eq!(get(&s, "k", NOW + 6.0).unwrap(), (b"c".to_vec(), None));
    }

    #[test]
    fn delete_pop_and_touch_treat_expired_as_missing() {
        let tmp = TempDir::new("ops-missing");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), Some(1.0), NOW).unwrap();
        assert!(!s.delete(T, "k", NOW + 2.0).unwrap());
        assert!(!s.touch(T, "k", Some(100.0), NOW + 2.0).unwrap(), "touch doesn't revive");
        assert!(get(&s, "k", NOW + 2.0).is_none());
        let popped = s.pop_with(T, "k", NOW + 2.0, |_, _| Ok(())).unwrap();
        assert!(popped.is_none());

        s.set(T, "j", text("v"), Some(10.0), NOW).unwrap();
        assert!(s.touch(T, "j", None, NOW).unwrap());
        assert_eq!(get(&s, "j", NOW + 1e9).unwrap().1, None, "touch(None) removes the expiry");
        assert_eq!(s.pop_with(T, "j", NOW, |m, _| Ok(m)).unwrap(), Some(MODE_STR));
        assert!(!s.delete(T, "j", NOW).unwrap());
    }

    #[test]
    fn incr_semantics_match_the_pinned_behavior() {
        let tmp = TempDir::new("ops-incr");
        let s = store(tmp.path(), Requested::default());
        // A missing key starts from the default, with no expiry.
        assert_eq!(s.incr(T, "c", Number::Int(1), Some(Number::Int(0)), NOW).unwrap(), Number::Int(1));
        assert_eq!(get(&s, "c", NOW).unwrap().1, None);
        // A live counter keeps its expiry.
        s.add(T, "w", Value::Int(0), Some(60.0), NOW).unwrap();
        s.incr(T, "w", Number::Int(5), Some(Number::Int(0)), NOW + 1.0).unwrap();
        assert_eq!(get(&s, "w", NOW + 1.0).unwrap(), (b"5".to_vec(), Some(NOW + 60.0)));
        // An expired counter restarts with no expiry.
        assert_eq!(s.incr(T, "w", Number::Int(1), Some(Number::Int(0)), NOW + 61.0).unwrap(), Number::Int(1));
        assert_eq!(get(&s, "w", NOW + 61.0).unwrap().1, None);
        // default=None on a missing key, non-numbers and overflow are errors that change nothing.
        assert!(matches!(s.incr(T, "nope", Number::Int(1), None, NOW), Err(Error::KeyMissing)));
        assert_eq!(len(&s), 2);
        s.set(T, "s", Value::Bytes { mode: MODE_BYTES, data: b"x" }, None, NOW).unwrap();
        assert!(matches!(s.incr(T, "s", Number::Int(1), Some(Number::Int(0)), NOW), Err(Error::NotNumeric)));
        s.set(T, "max", Value::Int(i64::MAX), None, NOW).unwrap();
        assert!(matches!(s.incr(T, "max", Number::Int(1), Some(Number::Int(0)), NOW), Err(Error::Overflow)));
        assert_eq!(get(&s, "max", NOW).unwrap().0, i64::MAX.to_string().into_bytes());
        // Floats work, and int + float is a float.
        s.set(T, "f", Value::Real(2.5), None, NOW).unwrap();
        assert_eq!(s.incr(T, "f", Number::Int(1), None, NOW).unwrap(), Number::Real(3.5));
    }

    #[test]
    fn incr_counts_as_a_store() {
        let tmp = TempDir::new("ops-incr-seq");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        let seq = |s: &Store| -> i64 {
            s.read(T, |c| {
                c.query_row("SELECT store_seq FROM cache WHERE key = 'c'", [], |r| r.get(0))
                    .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap()
        };
        let before = seq(&s);
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        assert!(seq(&s) > before);
    }

    #[test]
    fn set_sweeps_expired_rows_within_the_cull_limit() {
        // Pinned: with no size pressure, one set removes at most cull_limit (10) expired rows.
        let tmp = TempDir::new("ops-sweep");
        let s = store(tmp.path(), Requested::default());
        for i in 0..15 {
            s.set(T, &format!("e{i}"), text("v"), Some(1.0), NOW + f64::from(i) * 0.001).unwrap();
        }
        s.set(T, "fresh", text("v"), None, NOW + 10.0).unwrap();
        assert_eq!(len(&s), 6);
    }

    #[test]
    fn culling_shares_one_budget_and_evicts_least_recently_stored() {
        let tmp = TempDir::new("ops-cull");
        let s = store(tmp.path(), Requested { size_limit: Some(1 << 20), cull_limit: Some(3) });
        let blob = vec![0u8; 60_000];
        for i in 0..30 {
            s.set(T, &format!("k{i:02}"), Value::Bytes { mode: MODE_BYTES, data: &blob }, None, NOW + f64::from(i))
                .unwrap();
        }
        // Over the limit, each set evicts up to 3 rows, oldest stored first; the newest survive.
        let keys: Vec<String> = s
            .read(T, |c| {
                let mut stmt =
                    c.prepare("SELECT key FROM cache ORDER BY store_seq").map_err(|e| Error::from_sqlite(e, T))?;
                stmt.query_map([], |r| r.get(0)).and_then(Iterator::collect).map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap();
        assert!(keys.len() < 30, "nothing was culled");
        assert_eq!(keys.last().map(String::as_str), Some("k29"));
        let first: usize = keys[0][1..].parse().unwrap();
        assert!(
            keys.iter().map(|k| k[1..].parse::<usize>().unwrap()).eq(first..30),
            "evicted out of store order: {keys:?}"
        );
    }
}

#[cfg(test)]
mod reprepare_guard {
    use rusqlite::StatementStatus;

    use super::*;
    use crate::open::Requested;
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, OpenOptions, Store};
    use crate::testutil::TempDir;

    /// Regression guard: cached statements must not be recompiled on every call. Without the query
    /// planner stability guarantee (see `open::connect`), rusqlite's cache clearing the bindings of
    /// a statement whose plan looked at a parameter expired it each time, about 30% of a `set`.
    #[test]
    fn hot_statements_are_never_reprepared() {
        let tmp = TempDir::new("reprepare-guard");
        let timeout = Duration::from_secs(5);
        let store = Store::open(&OpenOptions {
            directory: tmp.path().to_path_buf(),
            timeout,
            requested: Requested::default(),
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
        })
        .unwrap();
        for i in 0..300 {
            let key = format!("s{}", i % 100);
            let now = 1.0 + f64::from(i);
            store.set(timeout, &key, Value::Bytes { mode: 1, data: b"v" }, Some(1_000.0), now).unwrap();
            store.get_with(timeout, &key, now, |_, _| Ok(())).unwrap();
        }
        let reprepares = store
            .read(timeout, |conn| {
                let count = |sql: &str| -> Result<i32> {
                    let stmt = conn.prepare_cached(sql).map_err(|e| Error::from_sqlite(e, timeout))?;
                    Ok(stmt.get_status(StatementStatus::RePrepare))
                };
                Ok((count(PRELUDE_SQL)?, count(LOOKUP_SQL)?))
            })
            .unwrap();
        assert_eq!(reprepares, (0, 0), "(prelude, lookup) were recompiled");
    }
}
