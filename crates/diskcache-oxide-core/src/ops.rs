//! Cache operations on a [`Store`] (Phase 2 step 3; 04-design.md sections 5-6).
//!
//! Everything here deals in modes and bytes or native numbers; converting Python objects lives in
//! the py crate. Every function takes `now` (seconds since the epoch) from its caller, which reads
//! the clock once per operation before any connection is taken (`clock=` support). Expiry follows
//! diskcache's pinned semantics: an entry is live while `expire_time IS NULL OR expire_time > now`.
//!
//! Values of `spill::THRESHOLD` bytes or more are stored in value files (see `spill`).

use std::time::Duration;

use rusqlite::types::{Value as SqlValue, ValueRef};
use rusqlite::{Connection, OptionalExtension};

use crate::rows::{NewRow, Removed, RowWriter, Stored};
use crate::schema::{KEY_TYPE_STR, MODE_INT, MODE_REAL};
use crate::spill::{self, PendingFile};
use crate::store::{Changed, LockPriority, Store};
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

/// A value removed by [`Store::pop`], still encoded: its mode and an owned copy of what SQLite (or
/// its value file) held.
#[derive(Debug, PartialEq)]
pub struct Popped {
    pub mode: i64,
    pub value: SqlValue,
}

/// One stored version of a key: its row and its store sequence number (D-011, D-013). An overwrite
/// keeps the row but gets a new number, so a version names exactly one value. Only the core makes
/// them, from a row it read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Version {
    id: i64,
    store_seq: i64,
}

/// A live value found by [`Store::get_with`].
#[derive(Debug, PartialEq)]
pub struct Hit<T> {
    /// What `decode` returned.
    pub value: T,
    pub expire_time: Option<f64>,
    /// The version that was decoded, for [`Store::delete_version_nowait`].
    pub version: Version,
}

/// What [`Store::delete_version_nowait`] did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VersionDelete {
    /// The version and its value file are gone.
    Removed,
    /// Already replaced or removed by someone else: nothing to do.
    Gone,
    /// Not tried (it would have had to wait, or isn't allowed here) or it failed: the row is still
    /// there.
    Skipped,
}

/// What [`Store::pop`] did.
#[derive(Debug, PartialEq)]
pub enum Pop {
    /// The key was missing or expired (or its value file was gone, D-013).
    Missing,
    /// The row is gone; here is its value.
    Removed(Popped),
    /// The row's mode was refused, so it was left in place (D-001: a `pickle=False` handle never
    /// destroys a pickled value it can't read).
    Refused { mode: i64 },
}

impl Popped {
    /// The value, borrowed in the shape a read hands out.
    #[must_use]
    pub fn value_ref(&self) -> ValueRef<'_> {
        (&self.value).into()
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
    /// expire time and version, or `None` on a miss. For an inline value `decode` runs while the connection is
    /// held, straight from SQLite's row buffer; it may run more than once. It may call back into the
    /// cache (a pickle hook): that nested operation runs at gate depth > 0, where nothing waits on
    /// this thread, and inside a held transaction it reuses the transaction's connection. A value
    /// file is read with no connection held (see `spill`). For a value file whose mode `refuse`
    /// rejects, `decode` gets `ValueRef::Null` instead of the file, which is never read: the caller
    /// would only turn it away (a pickle on a `pickle=False` handle, an unknown mode).
    pub fn get_with<T>(
        &self,
        timeout: Duration,
        key: &str,
        now: f64,
        refuse: impl Fn(i64) -> bool,
        mut decode: impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<Hit<T>>> {
        let mut row = self.read(timeout, |conn| look_up(conn, key, now, timeout, &mut decode))?;
        for _ in 0..spill::OPTIMISTIC_ATTEMPTS {
            let file = match row {
                Found::Miss => return Ok(None),
                Found::Inline(hit) => return Ok(Some(hit)),
                Found::File(file) if refuse(file.mode) => {
                    return Ok(Some(file.hit(decode(file.mode, ValueRef::Null)?)));
                }
                Found::File(file) => file,
            };
            if let Some(bytes) = spill::read(self.directory(), &file.relative, file.size)? {
                return Ok(Some(file.hit(decode(file.mode, ValueRef::Blob(&bytes))?)));
            }
            // Missing or short: overwritten since the row was read, or truly gone.
            row = self.read(timeout, |conn| look_up(conn, key, now, timeout, &mut decode))?;
            if let Found::File(again) = &row
                && again.version == file.version
            {
                // Same version, no readable file: a true miss (D-013). Remove exactly that version,
                // only if it needs no waiting; a later write's cleanup gets it otherwise.
                let _ = self.delete_version_nowait(file.version);
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
            if refuse(file.mode) {
                return Ok(Some(Err(file)));
            }
            Ok(spill::open(self.directory(), &file.relative)?.map(|(handle, path)| Ok((handle, path, file))))
        })?;
        let (handle, path, file) = match opened {
            None => return Ok(None),
            // Replaced by a value of a refused mode while this reader retried: never read it.
            Some(Err(file)) => return Ok(Some(file.hit(decode(file.mode, ValueRef::Null)?))),
            Some(Ok(opened)) => opened,
        };
        match spill::read_open(handle, &path, file.size)? {
            Some(bytes) => Ok(Some(file.hit(decode(file.mode, ValueRef::Blob(&bytes))?))),
            None => Ok(None),
        }
    }

    /// Deletes exactly `version` (nothing newer, D-013) and its value file, if that needs no waiting
    /// at all: not for the write lock, a connection slot, or another process's lock. Skipped while
    /// this thread holds a write transaction on any cache or is inside any cache operation (gate
    /// depth > 0: the gate is process-wide), and in a child that can't write this file. Like any
    /// operation, it waits while a fork is pending. An error is reported as a skip.
    pub fn delete_version_nowait(&self, version: Version) -> VersionDelete {
        match self.write_nowait(|writer| writer.delete_version(version.id, version.store_seq)) {
            Some(Ok(Some(removed))) => {
                // Never inside a held transaction: its holder keeps a `HeldShare`, so it's at gate
                // depth > 0, where `write_nowait` refuses. This commit is final.
                self.unlink(removed.filename);
                VersionDelete::Removed
            }
            Some(Ok(None)) => VersionDelete::Gone,
            None | Some(Err(_)) => VersionDelete::Skipped,
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
        let result = self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            let before = prelude(tx, key, now, timeout)?;
            let (delta, mut removed) =
                put_value(writer, key, value, pending.as_ref(), now, expire_time(now, expire), before.old())?;
            removed.extend(cull(writer, tx, &before, delta, now, timeout)?);
            Ok(Changed::removing((), removed).creating(pending.as_ref().map(|p| p.relative.clone())))
        });
        settle_with(pending.as_ref(), result, |()| true)?;
        Ok(true)
    }

    /// Stores `value` only if `key` is missing or expired. Returns whether it stored.
    pub fn add(&self, timeout: Duration, key: &str, value: Value<'_>, expire: Option<f64>, now: f64) -> Result<bool> {
        let pending = self.spill_if_large(value)?;
        let result = self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            let before = prelude(tx, key, now, timeout)?;
            if before.live.is_some() {
                return Ok(Changed::removing(false, []));
            }
            let (delta, mut removed) =
                put_value(writer, key, value, pending.as_ref(), now, expire_time(now, expire), before.old())?;
            removed.extend(cull(writer, tx, &before, delta, now, timeout)?);
            Ok(Changed::removing(true, removed).creating(pending.as_ref().map(|p| p.relative.clone())))
        });
        settle_with(pending.as_ref(), result, |added| *added)
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
        self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            if live_row(tx, key, now, timeout)?.is_none() {
                return Ok(Changed::removing(false, []));
            }
            let removed = writer.delete_key(key)?;
            Ok(Changed::removing(removed.is_some(), removed))
        })
    }

    /// Removes the live value of `key` and returns it, still encoded, or `None` if it was missing or
    /// expired. The caller decodes it after the write has committed: decoding a pickle can run
    /// Python (`__setstate__`) that uses this cache, and inside the transaction a write from it
    /// would wait out the whole timeout behind its own thread (roadmap Phase 2.5, item 7).
    ///
    /// A live row whose mode `refuse` rejects is left in place and reported as [`Pop::Refused`].
    /// The check runs inside the transaction, before the delete, so a value the caller can't decode
    /// is never destroyed on its way to an error. `refuse` runs with the write lock and a connection
    /// held, and perhaps more than once: it must be cheap, and must not run Python or use the cache.
    pub fn pop(&self, timeout: Duration, key: &str, now: f64, refuse: impl Fn(i64) -> bool) -> Result<Pop> {
        self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            let found = {
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
                        if refuse(mode) {
                            return Ok(Changed::removing(Pop::Refused { mode }, []));
                        }
                        let filename: Option<String> = row.get(2).map_err(|e| Error::from_sqlite(e, timeout))?;
                        if let Some(relative) = filename {
                            // Under the write lock nothing can replace the file, so read it here. A
                            // missing file acts missing (D-013); the row goes below.
                            let size: i64 = row.get(3).map_err(|e| Error::from_sqlite(e, timeout))?;
                            spill::read(self.directory(), &relative, size)?
                                .map(|bytes| Popped { mode, value: SqlValue::Blob(bytes) })
                        } else {
                            // Copied byte for byte, as `get` borrows them: TEXT goes in as a BLOB
                            // (the codec reads both as raw bytes), so no UTF-8 check can fail here.
                            let value = match row.get_ref(1).map_err(|e| Error::from_sqlite(e, timeout))? {
                                ValueRef::Text(bytes) | ValueRef::Blob(bytes) => SqlValue::Blob(bytes.to_vec()),
                                ValueRef::Integer(n) => SqlValue::Integer(n),
                                ValueRef::Real(x) => SqlValue::Real(x),
                                ValueRef::Null => SqlValue::Null,
                            };
                            Some(Popped { mode, value })
                        }
                    }
                    None => None,
                }
            };
            match found {
                Some(value) => Ok(Changed::removing(Pop::Removed(value), writer.delete_key(key)?)),
                // A spilled row whose file is gone: remove it, and report a miss.
                None if live_row(tx, key, now, timeout)?.is_some() => {
                    Ok(Changed::removing(Pop::Missing, writer.delete_key(key)?))
                }
                None => Ok(Changed::removing(Pop::Missing, [])),
            }
        })
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
        self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            let before = prelude(tx, key, now, timeout)?;
            let (current, expire) = match &before.live {
                Some(row) => (number_of(row)?, row.expire_time),
                None => (default.ok_or(Error::KeyMissing)?, None),
            };
            let next = current.add(delta)?;
            let (delta_size, mut removed) = put_value(writer, key, next.value(), None, now, expire, before.old())?;
            removed.extend(cull(writer, tx, &before, delta_size, now, timeout)?);
            Ok(Changed::removing(next, removed))
        })
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
    Inline(Hit<T>),
    File(FileRow),
}

/// A live row whose value is in a value file.
struct FileRow {
    version: Version,
    mode: i64,
    relative: String,
    size: i64,
    expire_time: Option<f64>,
}

impl FileRow {
    fn hit<T>(&self, value: T) -> Hit<T> {
        Hit { value, expire_time: self.expire_time, version: self.version }
    }
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
    let version = Version { id: row.get(5).map_err(sqlite)?, store_seq: row.get(6).map_err(sqlite)? };
    if let Some(relative) = row.get::<_, Option<String>>(3).map_err(sqlite)? {
        Ok(Found::File(FileRow { version, mode, relative, size: row.get(4).map_err(sqlite)?, expire_time }))
    } else {
        let value = decode(mode, row.get_ref(1).map_err(sqlite)?)?;
        Ok(Found::Inline(Hit { value, expire_time, version }))
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

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use super::*;
    use crate::open::Requested;
    use crate::schema::{MODE_BYTES, MODE_STR};
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions};
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
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
        })
        .unwrap()
    }

    fn text(s: &str) -> Value<'_> {
        Value::Bytes { mode: MODE_STR, data: s.as_bytes() }
    }

    fn get(store: &Store, key: &str, now: f64) -> Option<(Vec<u8>, Option<f64>)> {
        store
            .get_with(
                T,
                key,
                now,
                |_| false,
                |_, v| match v {
                    ValueRef::Blob(b) | ValueRef::Text(b) => Ok(b.to_vec()),
                    ValueRef::Integer(n) => Ok(n.to_string().into_bytes()),
                    ValueRef::Real(x) => Ok(x.to_string().into_bytes()),
                    ValueRef::Null => Ok(Vec::new()),
                },
            )
            .unwrap()
            .map(|hit| (hit.value, hit.expire_time))
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
        assert_eq!(s.pop(T, "k", NOW + 2.0, |_| false).unwrap(), Pop::Missing);

        s.set(T, "j", text("v"), Some(10.0), NOW).unwrap();
        assert!(s.touch(T, "j", None, NOW).unwrap());
        assert_eq!(get(&s, "j", NOW + 1e9).unwrap().1, None, "touch(None) removes the expiry");
        let Pop::Removed(popped) = s.pop(T, "j", NOW, |_| false).unwrap() else { panic!("a live value pops") };
        assert_eq!((popped.mode, popped.value_ref()), (MODE_STR, ValueRef::Blob(b"v")));
        assert!(!s.delete(T, "j", NOW).unwrap());
    }

    #[test]
    fn pop_leaves_a_refused_mode_in_place() {
        let tmp = TempDir::new("ops-pop-refused");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        assert_eq!(s.pop(T, "k", NOW, |mode| mode == MODE_STR).unwrap(), Pop::Refused { mode: MODE_STR });
        assert_eq!(get(&s, "k", NOW).unwrap(), (b"v".to_vec(), None), "the refused row is still there");
        assert!(matches!(s.pop(T, "k", NOW, |_| false).unwrap(), Pop::Removed(_)));
    }

    fn version(store: &Store, key: &str) -> Option<Version> {
        store.get_with(T, key, NOW, |_| false, |_, _| Ok(())).unwrap().map(|hit| hit.version)
    }

    #[test]
    fn a_version_names_exactly_one_stored_value() {
        let tmp = TempDir::new("ops-version");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("a"), None, NOW).unwrap();
        let first = version(&s, "k").unwrap();
        assert_eq!(version(&s, "k"), Some(first), "reading again names the same version");
        s.set(T, "k", text("b"), None, NOW).unwrap();
        let second = version(&s, "k").unwrap();
        assert_ne!(first, second, "an overwrite is a new version");

        assert_eq!(s.delete_version_nowait(first), VersionDelete::Gone, "an older version deletes nothing");
        assert_eq!(get(&s, "k", NOW).unwrap().0, b"b");
        assert_eq!(s.delete_version_nowait(second), VersionDelete::Removed);
        assert!(get(&s, "k", NOW).is_none());
        assert_eq!(s.delete_version_nowait(second), VersionDelete::Gone, "already gone");
        assert_eq!(len(&s), 0);
    }

    #[test]
    fn a_refused_spilled_value_is_never_read() {
        let tmp = TempDir::new("ops-refused-file");
        let s = store(tmp.path(), Requested::default());
        let big = vec![b'x'; spill::THRESHOLD];
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
        let mut seen = Vec::new();
        let hit = s
            .get_with(
                T,
                "k",
                NOW,
                |mode| mode == MODE_BYTES,
                |mode, v| {
                    seen.push((mode, matches!(v, ValueRef::Null)));
                    Ok(())
                },
            )
            .unwrap();
        assert!(hit.is_some());
        assert_eq!(seen, [(MODE_BYTES, true)], "decode saw Null, not the file");
        assert_eq!(value_files(tmp.path()).len(), 1, "and the file is untouched");
    }

    #[test]
    fn deleting_a_spilled_version_removes_its_file() {
        let tmp = TempDir::new("ops-version-file");
        let s = store(tmp.path(), Requested::default());
        let big = vec![b'x'; spill::THRESHOLD];
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
        assert_eq!(value_files(tmp.path()).len(), 1);
        assert_eq!(s.delete_version_nowait(version(&s, "k").unwrap()), VersionDelete::Removed);
        assert!(value_files(tmp.path()).is_empty());
    }

    #[test]
    fn a_spilled_value_whose_file_is_gone_is_a_miss_and_its_row_goes() {
        let tmp = TempDir::new("ops-version-lost-file");
        let s = store(tmp.path(), Requested::default());
        let big = vec![b'x'; spill::THRESHOLD];
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
        for file in value_files(tmp.path()) {
            std::fs::remove_file(file).unwrap();
        }
        assert!(get(&s, "k", NOW).is_none());
        assert_eq!(len(&s), 0, "the row of the lost file was removed (D-013)");
    }

    #[test]
    fn deleting_a_version_never_waits_for_the_write_lock() {
        let tmp = TempDir::new("ops-version-busy");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        let v = version(&s, "k").unwrap();

        // Another thread holds the write lock.
        let (held_tx, held_rx) = std::sync::mpsc::channel();
        let (done_tx, done_rx) = std::sync::mpsc::channel::<()>();
        let holder = {
            let s = Arc::clone(&s);
            std::thread::spawn(move || {
                let held = s.begin_hold(T).unwrap();
                held_tx.send(()).unwrap();
                done_rx.recv().unwrap();
                held.finish(false).unwrap();
            })
        };
        held_rx.recv().unwrap();
        let started = std::time::Instant::now();
        assert_eq!(s.delete_version_nowait(v), VersionDelete::Skipped, "skipped while another thread writes");
        assert!(started.elapsed() < Duration::from_secs(1));
        done_tx.send(()).unwrap();
        holder.join().unwrap();
        assert!(get(&s, "k", NOW).is_some());

        // This thread's own held transaction: skipped too (gate depth > 0).
        let held = s.begin_hold(T).unwrap();
        assert_eq!(s.delete_version_nowait(v), VersionDelete::Skipped);
        held.finish(false).unwrap();
        assert_eq!(s.delete_version_nowait(v), VersionDelete::Removed, "and works once nothing is held");
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

    /// Every value file under `dir`.
    fn value_files(dir: &Path) -> std::collections::BTreeSet<std::path::PathBuf> {
        let mut found = std::collections::BTreeSet::new();
        let mut stack = vec![dir.to_path_buf()];
        while let Some(at) = stack.pop() {
            for entry in std::fs::read_dir(&at).unwrap().map(Result::unwrap) {
                let path = entry.path();
                if path.is_dir() {
                    stack.push(path);
                } else if path.extension().is_some_and(|e| e == "val") {
                    found.insert(path);
                }
            }
        }
        found
    }

    #[test]
    fn value_files_written_by_an_operation_nested_in_a_held_transaction_settle_when_it_ends() {
        // A write nested inside another operation (a pickle hook decoding a value) runs on the lent
        // connection. Its value files must wait for the transaction's end like any other's.
        let big = |byte: u8| vec![byte; spill::THRESHOLD];
        for commit in [false, true] {
            let tmp = TempDir::new("ops-held-files");
            let s = store(tmp.path(), Requested::default());
            s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'x') }, None, NOW).unwrap();
            s.set(T, "trigger", text("t"), None, NOW).unwrap();
            let before = value_files(tmp.path());
            let held = s.begin_hold(T).unwrap();
            s.get_with(
                T,
                "trigger",
                NOW,
                |_| false,
                |_, _| s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'y') }, None, NOW),
            )
            .unwrap();
            assert!(before.is_subset(&value_files(tmp.path())), "a value file went before the transaction ended");
            held.finish(commit).unwrap();
            let after = value_files(tmp.path());
            let expected = if commit { big(b'y') } else { big(b'x') };
            assert_eq!(get(&s, "k", NOW).map(|(v, _)| v), Some(expected), "commit = {commit}");
            assert_eq!(after.len(), 1, "commit = {commit}: a replaced or rolled-back file was left");
            assert_eq!(after == before, !commit, "commit = {commit}");
        }
    }

    #[test]
    fn an_operation_that_fails_partway_inside_a_held_transaction_leaves_nothing_behind() {
        let tmp = TempDir::new("ops-held-savepoint");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        let held = s.begin_hold(T).unwrap();
        let failed = s.write(T, |_, tx| {
            tx.execute("DELETE FROM cache WHERE key = 'k'", []).map_err(|e| Error::from_sqlite(e, T))?;
            Err::<(), _>(Error::NotNumeric)
        });
        assert!(matches!(failed, Err(Error::NotNumeric)));
        s.set(T, "after", text("a"), None, NOW).unwrap();
        held.finish(true).unwrap();
        assert_eq!(get(&s, "k", NOW).map(|(v, _)| v), Some(b"v".to_vec()), "the failed write's delete committed");
        assert_eq!(get(&s, "after", NOW).map(|(v, _)| v), Some(b"a".to_vec()));
    }

    #[test]
    fn a_failed_operation_inside_a_hold_undoes_the_files_of_an_operation_nested_in_it() {
        // The nested set succeeds (its savepoint is released into the outer one), then the outer
        // write fails: ROLLBACK TO undoes the nested row too, so its new file must go and the file
        // it replaced must stay, even though the transaction then commits.
        let big = |byte: u8| vec![byte; spill::THRESHOLD];
        let tmp = TempDir::new("ops-held-nested-savepoint");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'x') }, None, NOW).unwrap();
        let before = value_files(tmp.path());
        let held = s.begin_hold(T).unwrap();
        let failed = s.write(T, |_, _| {
            s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'y') }, None, NOW)?;
            Err::<(), _>(Error::NotNumeric)
        });
        assert!(matches!(failed, Err(Error::NotNumeric)));
        held.finish(true).unwrap();
        assert_eq!(get(&s, "k", NOW).map(|(v, _)| v), Some(big(b'x')));
        assert_eq!(value_files(tmp.path()), before);
    }

    #[test]
    fn a_held_transaction_that_sqlite_rolled_back_is_doomed_and_settles_as_a_rollback() {
        // As after a full disk: SQLite ends the whole transaction on its own. Later writes must
        // not run in autocommit (each would commit by itself), and the end must roll back.
        let big = |byte: u8| vec![byte; spill::THRESHOLD];
        let tmp = TempDir::new("ops-held-doomed");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'x') }, None, NOW).unwrap();
        let before = value_files(tmp.path());
        let held = s.begin_hold(T).unwrap();
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'y') }, None, NOW).unwrap();
        let failed = s.write(T, |_, tx| {
            tx.execute_batch("ROLLBACK").map_err(|e| Error::from_sqlite(e, T))?;
            Err::<(), _>(Error::NotNumeric)
        });
        assert!(matches!(failed, Err(Error::NotNumeric)));
        let later = s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big(b'z') }, None, NOW);
        assert!(matches!(later, Err(Error::TransactionAborted)), "{later:?}");
        assert!(matches!(held.finish(true), Err(Error::TransactionAborted)));
        assert_eq!(get(&s, "k", NOW).map(|(v, _)| v), Some(big(b'x')));
        assert_eq!(value_files(tmp.path()), before);
        s.set(T, "k", text("after"), None, NOW).unwrap();
    }

    #[test]
    fn a_write_that_panics_inside_a_held_transaction_is_undone() {
        let tmp = TempDir::new("ops-held-panic");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        let held = s.begin_hold(T).unwrap();
        let panicked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            s.write(T, |_, tx| -> Result<()> {
                tx.execute("DELETE FROM cache WHERE key = 'k'", []).map_err(|e| Error::from_sqlite(e, T))?;
                panic!("inside a held write");
            })
        }));
        assert!(panicked.is_err());
        held.finish(true).unwrap();
        assert_eq!(get(&s, "k", NOW).map(|(v, _)| v), Some(b"v".to_vec()), "the panicked write's delete committed");
    }
}

#[cfg(test)]
mod reprepare_guard {
    use rusqlite::StatementStatus;

    use super::*;
    use crate::open::Requested;
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions, Store};
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
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
        })
        .unwrap();
        for i in 0..300 {
            let key = format!("s{}", i % 100);
            let now = 1.0 + f64::from(i);
            store.set(timeout, &key, Value::Bytes { mode: 1, data: b"v" }, Some(1_000.0), now).unwrap();
            store.get_with(timeout, &key, now, |_| false, |_, _| Ok(())).unwrap();
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
