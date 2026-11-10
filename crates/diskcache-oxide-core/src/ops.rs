//! Cache operations on a [`Store`] (Phase 2 step 3; 04-design.md sections 5-6).
//!
//! Everything here deals in modes and bytes or native numbers; converting Python objects lives in
//! the py crate. Every function takes `now` (seconds since the epoch) from its caller, which reads
//! the clock once per operation before any connection is taken (`clock=` support). Expiry follows
//! diskcache's pinned semantics: an entry is live while `expire_time IS NULL OR expire_time > now`.
//!
//! Values of `spill::THRESHOLD` bytes or more are stored in value files (see `spill`).

use std::fs::File;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use rusqlite::types::{Value as SqlValue, ValueRef};
use rusqlite::{Connection, OptionalExtension};

use crate::key::Key;
#[cfg(test)]
use crate::rows::UPDATE_NUMBER_SQL;
use crate::rows::{NewRow, Removed, RemovedBatch, RowWriter, Stored};
use crate::schema::{MODE_INT, MODE_REAL};
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
    /// This plus `other`, as Python adds them, or [`Error::Overflow`] past the 64-bit integer range.
    pub fn checked_add(self, other: Number) -> Result<Number> {
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
    /// expire time and version, or `None` on a miss. For an inline value `decode` runs while the
    /// connection is held, straight from SQLite's row buffer; it may run more than once. An inline
    /// value the locked fallback (D-016) finds is copied out under the write lock and decoded once
    /// it's released, with no connection held. `decode` may call back into the cache (a pickle
    /// hook): that nested operation runs at gate depth > 0, where nothing waits on this thread, and
    /// inside a held transaction it reuses the transaction's connection. A value
    /// file is read with no connection held (see `spill`). For a value file whose mode `refuse`
    /// rejects, `decode` gets `ValueRef::Null` instead of the file, which is never read: the caller
    /// would only turn it away (a pickle on a `pickle=False` handle, an unknown mode).
    pub fn get_with<'k, T>(
        &self,
        timeout: Duration,
        key: impl Into<Key<'k>>,
        now: f64,
        refuse: impl Fn(i64) -> bool,
        mut decode: impl FnMut(i64, ValueRef<'_>) -> Result<T>,
    ) -> Result<Option<Hit<T>>> {
        let key = key.into();
        let (mut row, taken) =
            self.read_with_deadline(timeout, |conn| look_up(conn, key, now, timeout, &mut decode))?;
        // One budget for every read and the fallback's write. The first read's deadline if its slow
        // path took one, else `now + timeout` at the first missing file, since the fast path has no
        // wait to charge. A hit never reads the clock.
        let mut budget = taken;
        let left = |budget: &mut Option<Instant>| {
            let deadline = *budget.get_or_insert_with(|| crate::lockwait::deadline_after(Instant::now(), timeout));
            deadline.saturating_duration_since(Instant::now())
        };
        let whole_timeout = |error| match error {
            Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
            other => other,
        };
        let mut tries = 0;
        loop {
            // The last re-read is examined too: it may already have the answer.
            let file = match row {
                Found::Miss => return Ok(None),
                Found::Inline(hit) => return Ok(Some(hit)),
                Found::File(file) if refuse(file.mode) => {
                    return Ok(Some(file.hit(decode(file.mode, ValueRef::Null)?)));
                }
                Found::File(file) => file,
            };
            if tries == spill::OPTIMISTIC_ATTEMPTS {
                break;
            }
            tries += 1;
            #[cfg(test)]
            run_hook(&BEFORE_OPTIMISTIC_READ, self);
            if let Some(bytes) = spill::read(self.directory(), &file.relative, file.size)? {
                return Ok(Some(file.hit(decode(file.mode, ValueRef::Blob(&bytes))?)));
            }
            // Missing or short: overwritten since the row was read, or truly gone.
            row = self
                .read(left(&mut budget), |conn| look_up(conn, key, now, timeout, &mut decode))
                .map_err(whole_timeout)?;
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
        // row under the write lock, opening its file or copying an inline value, and read or decode
        // with the lock released. Skipped in a child that can't write this database (D-020): it
        // reports a miss instead. Inside this thread's own held transaction nothing else can replace
        // the file, so it isn't reached there; if it were, in a doomed one, the write would give
        // `TransactionAborted`.
        if self.check_writable().is_err() {
            return Ok(None);
        }
        #[cfg(test)]
        run_hook(&BEFORE_FALLBACK, self);
        let locked = self
            .write(left(&mut budget), |_, conn| {
                let mut copy = |mode: i64, value: ValueRef<'_>| Ok((mode, owned(value)));
                Ok(match look_up(conn, key, now, timeout, &mut copy)? {
                    Found::Miss => Locked::Miss,
                    Found::Inline(hit) => Locked::Inline(hit),
                    // Replaced by a value of a refused mode while this reader retried: never read it.
                    Found::File(file) if refuse(file.mode) => Locked::Refused(file),
                    Found::File(file) => match spill::open(self.directory(), &file.relative)? {
                        Some((handle, path)) => Locked::Opened(handle, path, file),
                        None => Locked::Lost(file.version),
                    },
                })
            })
            .map_err(whole_timeout)?;
        let (handle, path, file) = match locked {
            Locked::Miss => return Ok(None),
            Locked::Inline(Hit { value: (mode, value), expire_time, version }) => {
                let value = decode(mode, (&value).into())?;
                return Ok(Some(Hit { value, expire_time, version }));
            }
            Locked::Refused(file) => return Ok(Some(file.hit(decode(file.mode, ValueRef::Null)?))),
            Locked::Lost(version) => {
                // Gone though nothing could replace it: a true miss (D-013), as in the loop.
                let _ = self.delete_version_nowait(version);
                return Ok(None);
            }
            Locked::Opened(handle, path, file) => (handle, path, file),
        };
        if let Some(bytes) = spill::read_open(&handle, &path, file.size)? {
            return Ok(Some(file.hit(decode(file.mode, ValueRef::Blob(&bytes))?)));
        }
        // Short: written before a crash and never completed (no fsync). A true miss.
        let _ = self.delete_version_nowait(file.version);
        Ok(None)
    }

    /// Deletes exactly `version` (nothing newer, D-013) and its value file, if that needs no waiting
    /// at all: not for the write lock, a connection slot, or another process's lock. Skipped while
    /// this thread holds a write transaction on any cache or is inside any cache operation (gate
    /// depth > 0: the gate is process-wide), in a child that can't write this file, and while a fork
    /// is pending. An error is reported as a skip.
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

    /// Deletes exactly `version` (nothing newer) and its value file, waiting for the write lock up to
    /// `timeout`. Returns whether this call removed it, so of any number of racing callers exactly
    /// one gets `true`: the compare-and-delete runs inside `BEGIN IMMEDIATE`, and `store_seq` never
    /// repeats. Inside a held transaction the row goes in the transaction and its value file when
    /// the transaction commits, as for any other write.
    pub fn delete_version(&self, timeout: Duration, version: Version) -> Result<bool> {
        self.write_files(timeout, LockPriority::Normal, |writer, _| {
            let removed = writer.delete_version(version.id, version.store_seq)?;
            Ok(Changed::removing(removed.is_some(), removed))
        })
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
    pub fn contains<'k>(&self, timeout: Duration, key: impl Into<Key<'k>>, now: f64) -> Result<bool> {
        let key = key.into();
        self.read(timeout, |conn| {
            conn.prepare_cached(
                "SELECT 1 FROM cache WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)",
            )
            .and_then(|mut stmt| stmt.exists((key.key_type(), key, now)))
            .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    /// Stores `value` under `key`, then culls. Always returns `Ok(true)` on success, like diskcache.
    pub fn set<'k>(
        &self,
        timeout: Duration,
        key: impl Into<Key<'k>>,
        value: Value<'_>,
        expire: Option<f64>,
        now: f64,
    ) -> Result<bool> {
        let key = key.into();
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
    pub fn add<'k>(
        &self,
        timeout: Duration,
        key: impl Into<Key<'k>>,
        value: Value<'_>,
        expire: Option<f64>,
        now: f64,
    ) -> Result<bool> {
        let key = key.into();
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
    pub fn delete<'k>(&self, timeout: Duration, key: impl Into<Key<'k>>, now: f64) -> Result<bool> {
        let key = key.into();
        self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            if live_row(tx, key, now, timeout)?.is_none() {
                return Ok(Changed::removing(false, []));
            }
            let removed = writer.delete_key(key)?;
            Ok(Changed::removing(removed.is_some(), removed))
        })
    }

    /// Removes the live value of `key` and returns it, still encoded, or [`Pop::Missing`] if it was
    /// missing or expired. The caller decodes it after the write has committed: decoding a pickle
    /// can run Python (`__setstate__`) that uses this cache, and inside the transaction a write from
    /// it would wait out the whole timeout behind its own thread (roadmap Phase 2.5, item 7).
    ///
    /// The value is gone before anyone has decoded it, so `Cache.pop` reads and decodes with
    /// [`Store::get_with`] and removes that version with [`Store::delete_version`], and comes here
    /// only after losing that race [`spill::OPTIMISTIC_ATTEMPTS`] times: this can't starve.
    ///
    /// A live row whose mode `refuse` rejects is left in place and reported as [`Pop::Refused`].
    /// The check runs inside the transaction, before the delete, so a value the caller can't decode
    /// is never destroyed on its way to an error. `refuse` runs with the write lock and a connection
    /// held, and perhaps more than once: it must be cheap, and must not run Python or use the cache.
    pub fn pop<'k>(
        &self,
        timeout: Duration,
        key: impl Into<Key<'k>>,
        now: f64,
        refuse: impl Fn(i64) -> bool,
    ) -> Result<Pop> {
        let key = key.into();
        self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            let found = {
                let mut stmt = tx
                    .prepare_cached(
                        "SELECT mode, value, filename, size FROM cache
                         WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)",
                    )
                    .map_err(|e| Error::from_sqlite(e, timeout))?;
                let mut rows = stmt.query((key.key_type(), key, now)).map_err(|e| Error::from_sqlite(e, timeout))?;
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
                            let value = owned(row.get_ref(1).map_err(|e| Error::from_sqlite(e, timeout))?);
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
    pub fn touch<'k>(&self, timeout: Duration, key: impl Into<Key<'k>>, expire: Option<f64>, now: f64) -> Result<bool> {
        let key = key.into();
        self.write(timeout, |writer, _| writer.set_expire_time(key, now, expire_time(now, expire)))
    }

    /// Adds `delta` to the number stored under `key` and returns the result.
    ///
    /// - A live native number keeps its expiry; the update counts as a store (new `store_seq`).
    /// - A missing or expired key starts from `default` and gets **no expiry** (diskcache's pinned
    ///   behavior), or fails with [`Error::KeyMissing`] when `default` is `None`.
    /// - An in-place update of a live counter runs no expired-row sweep or cull (a deliberate change,
    ///   matching diskcache's in-place `UPDATE`); a miss or expired key takes the full write path.
    /// - Any other stored value fails with [`Error::NotNumeric`]; a 64-bit overflow with
    ///   [`Error::Overflow`]. Both leave the value unchanged.
    pub fn incr<'k>(
        &self,
        timeout: Duration,
        key: impl Into<Key<'k>>,
        delta: Number,
        default: Option<Number>,
        now: f64,
    ) -> Result<Number> {
        let key = key.into();
        self.write_files(timeout, LockPriority::Normal, |writer, tx| {
            // The fast path: a live number is updated in place, with no prelude, totals or cull.
            if let Some(row) = incr_row(tx, key, timeout)?
                && row.expire_time.is_none_or(|at| at > now)
            {
                let next = number_from(row.mode, row.int, row.real)?.checked_add(delta)?;
                let value = next.value();
                writer.update_number_in_place(row.id, value.mode(), value.stored(), now)?;
                return Ok(Changed::unchanged(next));
            }
            let before = prelude(tx, key, now, timeout)?;
            let (current, expire) = match &before.live {
                Some(row) => (number_of(row)?, row.expire_time),
                None => (default.ok_or(Error::KeyMissing)?, None),
            };
            let next = current.checked_add(delta)?;
            let (delta_size, mut removed) = put_value(writer, key, next.value(), None, now, expire, before.old())?;
            removed.extend(cull(writer, tx, &before, delta_size, now, timeout)?);
            Ok(Changed::removing(next, removed))
        })
    }
}

/// The stored row for `key` (live or not) as `incr` needs it, or `None` if there is none.
const INCR_SQL: &str = "SELECT id, mode, value, expire_time FROM cache WHERE key_type = ?1 AND key = ?2";

fn incr_row(tx: &Connection, key: Key<'_>, timeout: Duration) -> Result<Option<IncrRow>> {
    tx.prepare_cached(INCR_SQL)
        .and_then(|mut stmt| {
            stmt.query_row((key.key_type(), key), |r| {
                let (int, real) = match r.get_ref(2)? {
                    ValueRef::Integer(n) => (Some(n), None),
                    ValueRef::Real(x) => (None, Some(x)),
                    _ => (None, None),
                };
                Ok(IncrRow { id: r.get(0)?, mode: r.get(1)?, int, real, expire_time: r.get(3)? })
            })
            .optional()
        })
        .map_err(|e| Error::from_sqlite(e, timeout))
}

/// Stores `value` (pointing the row at `pending`'s value file if it was spilled), given the stored
/// version `old` read in this transaction. Returns the change in `totals.size` and the replaced
/// version.
fn put_value(
    writer: &RowWriter<'_>,
    key: Key<'_>,
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
    key: Key<'_>,
    now: f64,
    timeout: Duration,
    decode: &mut impl FnMut(i64, ValueRef<'_>) -> Result<T>,
) -> Result<Found<T>> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    let mut stmt = conn.prepare_cached(LOOKUP_SQL).map_err(sqlite)?;
    let mut rows = stmt.query((key.key_type(), key, now)).map_err(sqlite)?;
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

/// What the locked fallback of [`Store::get_with`] found under the write lock.
enum Locked {
    Miss,
    /// An inline value, copied out to decode after the lock is released.
    Inline(Hit<(i64, SqlValue)>),
    /// A value file of a refused mode, never opened.
    Refused(FileRow),
    /// A value file opened under the lock, to read after it.
    Opened(File, PathBuf, FileRow),
    /// A value file missing though nothing could replace it (D-013).
    Lost(Version),
}

/// An owned copy of a stored value, byte for byte, as a read borrows it: TEXT goes in as a BLOB
/// (the codec reads both as raw bytes), so no UTF-8 check can fail here.
fn owned(value: ValueRef<'_>) -> SqlValue {
    match value {
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => SqlValue::Blob(bytes.to_vec()),
        ValueRef::Integer(n) => SqlValue::Integer(n),
        ValueRef::Real(x) => SqlValue::Real(x),
        ValueRef::Null => SqlValue::Null,
    }
}

#[cfg(test)]
type TestHook = std::cell::RefCell<Option<Box<dyn FnMut(&Store)>>>;

#[cfg(test)]
thread_local! {
    /// Tests only: runs in `get_with` just before each optimistic read of a value file.
    static BEFORE_OPTIMISTIC_READ: TestHook = const { std::cell::RefCell::new(None) };
    /// Tests only: runs in `get_with` just before the locked fallback takes the write lock.
    static BEFORE_FALLBACK: TestHook = const { std::cell::RefCell::new(None) };
}

/// Runs a test hook taken out of its slot, so it may call back into the store, then puts it back.
#[cfg(test)]
fn run_hook(hook: &'static std::thread::LocalKey<TestHook>, store: &Store) {
    if let Some(mut run) = hook.with(|slot| slot.borrow_mut().take()) {
        run(store);
        hook.with(|slot| {
            let mut slot = slot.borrow_mut();
            if slot.is_none() {
                *slot = Some(run);
            }
        });
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

fn prelude(conn: &Connection, key: Key<'_>, now: f64, timeout: Duration) -> Result<Prelude> {
    conn.prepare_cached(PRELUDE_SQL)
        .and_then(|mut stmt| {
            stmt.query_row((key.key_type(), key, now), |r| {
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
/// The limit is judged by what `totals.size` holds now, without reading it again: the prelude's
/// figure from before this write, plus the write's own size change, less the value files the sweep
/// just removed. Counting those still would evict live rows the sweep had made room for.
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
    let swept =
        if before.any_expired { writer.delete_expired(now, before.cull_limit)? } else { RemovedBatch::default() };
    let budget = before.cull_limit - i64::try_from(swept.rows.len()).unwrap_or(i64::MAX);
    let size = before.size + size_delta - swept.freed;
    #[cfg(debug_assertions)]
    {
        let stored: i64 = tx
            .prepare_cached("SELECT size FROM totals WHERE only = 1")
            .and_then(|mut stmt| stmt.query_row([], |r| r.get(0)))
            .map_err(|e| Error::from_sqlite(e, timeout))?;
        debug_assert_eq!(size, stored, "the reckoned size and totals.size disagree");
    }
    let mut removed = swept.rows;
    if budget > 0 && over_limit(tx, size, before.size_limit, timeout)? {
        // Its freed bytes aren't needed: nothing judges the limit after this.
        removed.extend(writer.delete_oldest(budget)?.rows);
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

/// The row `incr` found, live or not (the caller checks `expire_time`).
struct IncrRow {
    id: i64,
    mode: i64,
    int: Option<i64>,
    real: Option<f64>,
    expire_time: Option<f64>,
}

fn live_row(tx: &Connection, key: Key<'_>, now: f64, timeout: Duration) -> Result<Option<LiveRow>> {
    tx.prepare_cached(
        "SELECT mode, value, expire_time FROM cache
         WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?3)",
    )
    .and_then(|mut stmt| {
        stmt.query_row((key.key_type(), key, now), |r| {
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
    number_from(row.mode, row.int, row.real)
}

fn number_from(mode: i64, int: Option<i64>, real: Option<f64>) -> Result<Number> {
    match (mode, int, real) {
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

    /// Installs a test hook (`BEFORE_OPTIMISTIC_READ` or `BEFORE_FALLBACK`) until dropped.
    struct Hook(&'static std::thread::LocalKey<TestHook>);
    impl Hook {
        fn install(slot: &'static std::thread::LocalKey<TestHook>, hook: impl FnMut(&Store) + 'static) -> Self {
            let replaced = slot.with(|h| h.borrow_mut().replace(Box::new(hook)));
            assert!(replaced.is_none(), "a hook is already installed in this slot");
            Self(slot)
        }
    }
    impl Drop for Hook {
        fn drop(&mut self) {
            self.0.with(|h| h.borrow_mut().take());
        }
    }

    /// A mode the D-016 tests' `refuse` rejects, as a `pickle=False` handle rejects pickles.
    const REFUSED: i64 = crate::schema::MODE_PICKLE;

    fn set_big(s: &Store, mode: i64, byte: u8) {
        let big = vec![byte; spill::THRESHOLD];
        s.set(T, "k", Value::Bytes { mode, data: &big }, None, NOW).unwrap();
    }

    fn unlink_value_files(dir: &Path) {
        for file in value_files(dir) {
            std::fs::remove_file(file).unwrap();
        }
    }

    /// What a hook does on its last call.
    #[derive(Debug, Clone, Copy)]
    enum Last {
        /// Another spilled overwrite.
        Spilled,
        /// An overwrite small enough to be inline.
        Inline,
        Delete,
        /// A spilled overwrite of a refused mode, whose file is then unlinked: a reader that opened
        /// it would miss.
        Refused,
        /// The current value file unlinked, with no new version.
        Unlink,
        /// The current value file cut short, as a write a crash interrupted leaves it.
        Truncate,
    }

    fn apply(s: &Store, dir: &Path, last: Last) {
        match last {
            Last::Spilled => set_big(s, MODE_BYTES, 0xff),
            Last::Inline => s.set(T, "k", text("small"), None, NOW).map(drop).unwrap(),
            Last::Delete => assert!(s.delete(T, "k", NOW).unwrap()),
            Last::Refused => {
                set_big(s, REFUSED, 0xee);
                unlink_value_files(dir);
            }
            Last::Unlink => unlink_value_files(dir),
            Last::Truncate => {
                for file in value_files(dir) {
                    std::fs::OpenOptions::new().write(true).open(file).unwrap().set_len(1).unwrap();
                }
            }
        }
    }

    /// What a D-016 read returned, and each `decode` call's mode and whether it saw `Null`.
    struct Read {
        got: Result<Option<Vec<u8>>>,
        decodes: Vec<(i64, bool)>,
    }

    /// A `get` of a spilled value that loses every optimistic read to an overwrite. `last` happens
    /// on the eighth (and last) `BEFORE_OPTIMISTIC_READ` call, so the loop's last re-read sees it,
    /// or with `at_fallback` just before the locked fallback takes the write lock.
    fn lose_every_read(s: &Arc<Store>, dir: &Path, last: Last, at_fallback: bool, timeout: Duration) -> Read {
        set_big(s, MODE_BYTES, 0);
        let calls = std::rc::Rc::new(std::cell::Cell::new(0_u8));
        let counted = calls.clone();
        let dir = dir.to_path_buf();
        let fallback_dir = dir.clone();
        let _optimistic = Hook::install(&BEFORE_OPTIMISTIC_READ, move |s| {
            counted.set(counted.get() + 1);
            if !at_fallback && usize::from(counted.get()) == spill::OPTIMISTIC_ATTEMPTS {
                apply(s, &dir, last);
            } else {
                set_big(s, MODE_BYTES, counted.get());
            }
        });
        let _fallback = at_fallback.then(|| Hook::install(&BEFORE_FALLBACK, move |s| apply(s, &fallback_dir, last)));
        let mut decodes = Vec::new();
        let got = s
            .get_with(
                timeout,
                "k",
                NOW,
                |mode| mode == REFUSED,
                |mode, v| {
                    decodes.push((mode, matches!(v, ValueRef::Null)));
                    Ok(match v {
                        ValueRef::Blob(b) | ValueRef::Text(b) => b.to_vec(),
                        _ => Vec::new(),
                    })
                },
            )
            .map(|hit| hit.map(|h| h.value));
        assert_eq!(usize::from(calls.get()), spill::OPTIMISTIC_ATTEMPTS, "every optimistic read lost");
        Read { got, decodes }
    }

    #[test]
    fn a_reader_that_keeps_losing_to_overwrites_reads_the_latest_value_under_the_lock() {
        for at_fallback in [false, true] {
            let tmp = TempDir::new("ops-d016-spilled");
            let s = store(tmp.path(), Requested::default());
            let read = lose_every_read(&s, tmp.path(), Last::Spilled, at_fallback, T);
            assert_eq!(read.got.unwrap(), Some(vec![0xff; spill::THRESHOLD]), "at_fallback={at_fallback}");
            assert_eq!(read.decodes, [(MODE_BYTES, false)]);
        }
    }

    #[test]
    fn a_reader_that_keeps_losing_to_overwrites_never_misses_an_inline_value() {
        for at_fallback in [false, true] {
            let tmp = TempDir::new("ops-d016-inline");
            let s = store(tmp.path(), Requested::default());
            let read = lose_every_read(&s, tmp.path(), Last::Inline, at_fallback, T);
            assert_eq!(
                read.got.unwrap(),
                Some(b"small".to_vec()),
                "at_fallback={at_fallback}: the key was always there"
            );
            assert_eq!(read.decodes, [(MODE_STR, false)], "at_fallback={at_fallback}: decoded once");
        }
    }

    #[test]
    fn a_reader_that_keeps_losing_to_overwrites_sees_a_delete_as_a_miss() {
        for at_fallback in [false, true] {
            let tmp = TempDir::new("ops-d016-delete");
            let s = store(tmp.path(), Requested::default());
            let read = lose_every_read(&s, tmp.path(), Last::Delete, at_fallback, T);
            assert_eq!(read.got.unwrap(), None, "at_fallback={at_fallback}");
            assert_eq!(read.decodes, [], "at_fallback={at_fallback}");
        }
    }

    #[test]
    fn a_reader_that_keeps_losing_to_overwrites_never_opens_a_refused_file() {
        for at_fallback in [false, true] {
            let tmp = TempDir::new("ops-d016-refused");
            let s = store(tmp.path(), Requested::default());
            let read = lose_every_read(&s, tmp.path(), Last::Refused, at_fallback, T);
            // The file is gone, so a reader that opened it would have missed.
            assert_eq!(read.got.unwrap(), Some(Vec::new()), "at_fallback={at_fallback}");
            assert_eq!(read.decodes, [(REFUSED, true)], "at_fallback={at_fallback}: decode saw Null");
        }
    }

    #[test]
    fn a_value_file_missing_or_short_under_the_lock_is_a_miss_and_its_row_goes() {
        for last in [Last::Unlink, Last::Truncate] {
            // No fork can be pending, so the fallback's own cleanup can't skip: the row is gone at
            // once, not by a later read's.
            let _forks = crate::testutil::one_global_fork_at_a_time();
            let tmp = TempDir::new("ops-d016-lost");
            let s = store(tmp.path(), Requested::default());
            let read = lose_every_read(&s, tmp.path(), last, true, T);
            assert_eq!(read.got.unwrap(), None, "{last:?}");
            assert_eq!(read.decodes, [], "{last:?}");
            assert_eq!(len(&s), 0, "{last:?}: the fallback removed the row (D-013)");
        }
    }

    #[test]
    fn the_fallback_waits_for_the_write_lock_within_the_whole_calls_timeout() {
        let tmp = TempDir::new("ops-d016-timeout");
        let s = store(tmp.path(), Requested::default());
        let timeout = Duration::from_millis(400);
        // A connection of its own, not a held transaction, so nothing here holds up another test's
        // fork for the length of this wait.
        let holder = std::rc::Rc::new(crate::open::connect(tmp.path(), T).unwrap());
        let locker = holder.clone();
        let _lock = Hook::install(&BEFORE_FALLBACK, move |_| locker.execute_batch("BEGIN IMMEDIATE").unwrap());
        let started = std::time::Instant::now();
        let read = lose_every_read(&s, tmp.path(), Last::Spilled, false, timeout);
        match read.got {
            Err(Error::Timeout { seconds }) => {
                assert!((seconds - timeout.as_secs_f64()).abs() < f64::EPSILON, "{seconds}");
            }
            other => panic!("expected a timeout, got {other:?}"),
        }
        assert!(started.elapsed() >= timeout, "waited out the budget");
        assert!(started.elapsed() < 4 * timeout, "one budget for the whole call");
        holder.execute_batch("ROLLBACK").unwrap();
    }

    /// `delete_version_nowait`, past another test's pending fork, behind which it skips.
    fn delete_nowait(s: &Store, version: Version) -> VersionDelete {
        for _ in 0..5_000 {
            match s.delete_version_nowait(version) {
                VersionDelete::Skipped => std::thread::sleep(Duration::from_millis(1)),
                done => return done,
            }
        }
        VersionDelete::Skipped
    }

    fn store(dir: &Path, requested: Requested) -> Arc<Store> {
        Store::open(&OpenOptions {
            directory: dir.to_path_buf(),
            timeout: T,
            requested,
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
            role: crate::open::Role::Plain,
            default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
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

    #[test]
    fn pop_returns_every_kind_of_stored_value_and_removes_it() {
        let tmp = TempDir::new("ops-pop-kinds");
        let s = store(tmp.path(), Requested::default());
        let big = vec![b'x'; spill::THRESHOLD];
        s.set(T, "file", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
        s.set(T, "int", Value::Int(-7), None, NOW).unwrap();
        s.set(T, "real", Value::Real(2.5), None, NOW).unwrap();
        for (key, mode, value) in [
            ("file", MODE_BYTES, SqlValue::Blob(big.clone())),
            ("int", MODE_INT, SqlValue::Integer(-7)),
            ("real", MODE_REAL, SqlValue::Real(2.5)),
        ] {
            assert_eq!(s.pop(T, key, NOW, |_| false).unwrap(), Pop::Removed(Popped { mode, value }), "{key}");
        }
        assert_eq!(len(&s), 0);
        assert!(value_files(tmp.path()).is_empty(), "the popped value's file went with it");
        assert_eq!(s.pop(T, "never", NOW, |_| false).unwrap(), Pop::Missing);
    }

    #[test]
    fn pop_of_a_spilled_value_whose_file_is_gone_is_a_miss_and_its_row_goes() {
        let tmp = TempDir::new("ops-pop-lost-file");
        let s = store(tmp.path(), Requested::default());
        let big = vec![b'x'; spill::THRESHOLD];
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
        unlink_value_files(tmp.path());
        assert_eq!(s.pop(T, "k", NOW, |_| false).unwrap(), Pop::Missing);
        assert_eq!(len(&s), 0, "the row of the lost file was removed (D-013)");
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

        assert_eq!(delete_nowait(&s, first), VersionDelete::Gone, "an older version deletes nothing");
        assert_eq!(get(&s, "k", NOW).unwrap().0, b"b");
        assert_eq!(delete_nowait(&s, second), VersionDelete::Removed);
        assert!(get(&s, "k", NOW).is_none());
        assert_eq!(delete_nowait(&s, second), VersionDelete::Gone, "already gone");
        assert_eq!(len(&s), 0);
    }

    #[test]
    fn deleting_a_version_removes_only_that_version_and_says_who_won() {
        let tmp = TempDir::new("ops-version-blocking");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("a"), None, NOW).unwrap();
        let first = version(&s, "k").unwrap();
        s.set(T, "k", text("b"), None, NOW).unwrap();
        assert!(!s.delete_version(T, first).unwrap(), "an older version deletes nothing");
        assert_eq!(get(&s, "k", NOW).unwrap().0, b"b");

        let second = version(&s, "k").unwrap();
        assert!(s.delete_version(T, second).unwrap());
        assert!(!s.delete_version(T, second).unwrap(), "only the first caller removes it");
        assert_eq!(len(&s), 0);
    }

    #[test]
    fn deleting_a_version_waits_for_the_write_lock_up_to_its_timeout() {
        let tmp = TempDir::new("ops-version-wait");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        let v = version(&s, "k").unwrap();

        // A connection of its own, not a held transaction, so nothing here holds up another test's
        // fork for the length of this wait.
        let holder = crate::open::connect(tmp.path(), T).unwrap();
        holder.execute_batch("BEGIN IMMEDIATE").unwrap();
        let short = Duration::from_millis(50);
        let started = std::time::Instant::now();
        assert!(matches!(s.delete_version(short, v), Err(Error::Timeout { .. })));
        assert!(started.elapsed() >= short);
        holder.execute_batch("ROLLBACK").unwrap();
        assert!(s.delete_version(T, v).unwrap(), "and it succeeds once the lock is free");
    }

    #[test]
    fn deleting_a_spilled_version_in_a_held_transaction_keeps_its_file_until_the_commit() {
        for commit in [false, true] {
            let tmp = TempDir::new("ops-version-held");
            let s = store(tmp.path(), Requested::default());
            let big = vec![b'x'; spill::THRESHOLD];
            s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
            let v = version(&s, "k").unwrap();

            let held = s.begin_hold(T).unwrap();
            assert!(s.delete_version(T, v).unwrap());
            assert_eq!(value_files(tmp.path()).len(), 1, "the file stays until the transaction ends");
            held.finish(commit).unwrap();

            assert_eq!(value_files(tmp.path()).len(), usize::from(!commit));
            assert_eq!(get(&s, "k", NOW).is_some(), !commit);
        }
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
        assert_eq!(delete_nowait(&s, version(&s, "k").unwrap()), VersionDelete::Removed);
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
        // Its cleanup skips while another test's fork is pending; a later read retries.
        for _ in 0..5_000 {
            if len(&s) == 0 {
                break;
            }
            std::thread::sleep(Duration::from_millis(1));
            assert!(get(&s, "k", NOW).is_none());
        }
        assert_eq!(len(&s), 0, "the row of the lost file was removed (D-013)");
    }

    #[test]
    fn deleting_a_version_never_waits_for_the_write_lock() {
        let tmp = TempDir::new("ops-version-busy");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        let v = version(&s, "k").unwrap();

        // Another thread holds the write lock. Safe only because `write_nowait` returns at
        // `write_lock.try_lock()` before it enters the gate: entered first, it would wait behind
        // another test's pending fork, which waits for this hold, and that could hang the suite.
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
        assert_eq!(delete_nowait(&s, v), VersionDelete::Removed, "and works once nothing is held");
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

    fn seq_of(s: &Store, key: &str) -> i64 {
        s.read(T, |c| {
            c.query_row("SELECT store_seq FROM cache WHERE key = ?1", [key], |r| r.get(0))
                .map_err(|e| Error::from_sqlite(e, T))
        })
        .unwrap()
    }

    #[test]
    fn incr_counts_as_a_store() {
        let tmp = TempDir::new("ops-incr-seq");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        let before = seq_of(&s, "c");
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        assert!(seq_of(&s, "c") > before);
    }

    #[test]
    fn in_place_incr_versions_strictly_increase_and_never_repeat_after_a_delete() {
        let tmp = TempDir::new("ops-incr-inplace-seq");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        s.set(T, "d", Value::Int(0), None, NOW).unwrap();
        let mut last = seq_of(&s, "d");
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        assert!(seq_of(&s, "c") > last, "newer than every other row");
        last = seq_of(&s, "c");
        // Deleting the newest row raises the floor; the next incr must not reuse any number.
        assert!(s.delete(T, "c", NOW).unwrap());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        let before = seq_of(&s, "c");
        assert!(before > last);
        assert!(s.delete(T, "d", NOW).unwrap());
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        assert!(seq_of(&s, "c") > before, "a delete of an older row can't make the version repeat");
        let top = seq_of(&s, "c");
        assert!(s.delete(T, "c", NOW).unwrap());
        s.set(T, "e", Value::Int(0), None, NOW).unwrap();
        s.incr(T, "e", Number::Int(1), None, NOW).unwrap();
        assert!(seq_of(&s, "e") > top, "never a number the deleted row held");
    }

    #[test]
    fn an_incr_made_during_a_clear_survives_it() {
        let tmp = TempDir::new("ops-incr-inplace-clear");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "old", Value::Int(0), None, NOW).unwrap();
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        // Delete a newest row first, so the snapshot comes from the floor (`next_seq`), not MAX.
        s.set(T, "newest", Value::Int(0), None, NOW).unwrap();
        assert!(s.delete(T, "newest", NOW).unwrap());
        let snapshot = s.seq_snapshot(T).unwrap();
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        // One chunk of clear(), as it runs after taking `snapshot`. A real `clear()` takes its own,
        // later snapshot, so it can't be interleaved with an incr without a hook.
        let removed: i64 = s
            .write_files(T, LockPriority::Normal, |_, conn| {
                let n = conn
                    .execute("DELETE FROM cache WHERE store_seq < ?1", [snapshot])
                    .map_err(|e| Error::from_sqlite(e, T))?;
                Ok(Changed::unchanged(i64::try_from(n).unwrap()))
            })
            .unwrap();
        assert_eq!(removed, 1, "only the row that wasn't incremented");
        assert_eq!(get(&s, "c", NOW).unwrap().0, b"1");
    }

    #[test]
    fn in_place_incr_after_the_newest_row_is_deleted_takes_a_fresh_version() {
        let tmp = TempDir::new("ops-incr-inplace-floor");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        s.set(T, "newest", Value::Int(0), None, NOW).unwrap();
        let gone = seq_of(&s, "newest");
        assert!(s.delete(T, "newest", NOW).unwrap());
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        assert!(seq_of(&s, "c") > gone, "the deleted row's number is never handed out again");
    }

    #[test]
    fn a_rolled_back_in_place_incr_restores_the_value_and_the_version() {
        let tmp = TempDir::new("ops-incr-inplace-rollback");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(5), None, NOW).unwrap();
        let (seq, v) = (seq_of(&s, "c"), version(&s, "c"));
        let held = s.begin_hold(T).unwrap();
        assert_eq!(s.incr(T, "c", Number::Int(1), None, NOW).unwrap(), Number::Int(6));
        assert!(seq_of(&s, "c") > seq);
        held.finish(false).unwrap();
        assert_eq!(get(&s, "c", NOW).unwrap().0, b"5");
        assert_eq!(seq_of(&s, "c"), seq);
        assert_eq!(version(&s, "c"), v);
    }

    #[test]
    fn a_stale_version_cannot_delete_an_incremented_value() {
        let tmp = TempDir::new("ops-incr-inplace-stale");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        let stale = version(&s, "c").unwrap();
        s.incr(T, "c", Number::Int(1), None, NOW).unwrap();
        assert_eq!(delete_nowait(&s, stale), VersionDelete::Gone);
        assert!(!s.delete_version(T, stale).unwrap());
        assert_eq!(get(&s, "c", NOW).unwrap().0, b"1");
        let fresh = version(&s, "c").unwrap();
        assert_ne!(fresh, stale);
        assert_eq!(delete_nowait(&s, fresh), VersionDelete::Removed);
    }

    #[test]
    fn in_place_incr_changes_the_mode_for_int_plus_real_and_keeps_the_expiry() {
        let tmp = TempDir::new("ops-incr-inplace-mode");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(1), Some(60.0), NOW).unwrap();
        let mode = |s: &Store| -> i64 {
            s.read(T, |c| {
                c.query_row("SELECT mode FROM cache WHERE key = 'c'", [], |r| r.get(0))
                    .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap()
        };
        assert_eq!(mode(&s), MODE_INT);
        assert_eq!(s.incr(T, "c", Number::Real(0.5), None, NOW + 1.0).unwrap(), Number::Real(1.5));
        assert_eq!(mode(&s), MODE_REAL);
        assert_eq!(get(&s, "c", NOW + 1.0).unwrap().1, Some(NOW + 60.0), "expiry kept");
        assert_eq!(s.incr(T, "c", Number::Int(1), None, NOW + 2.0).unwrap(), Number::Real(2.5));
        assert_eq!(mode(&s), MODE_REAL);
    }

    #[test]
    fn in_place_incr_runs_no_sweep_and_no_cull_and_leaves_totals_alone() {
        let tmp = TempDir::new("ops-incr-inplace-nosweep");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "c", Value::Int(0), None, NOW).unwrap();
        for i in 0..5 {
            s.set(T, &format!("e{i}"), text("v"), Some(1.0), NOW).unwrap();
        }
        let totals = |s: &Store| -> (i64, i64, i64) {
            s.read(T, |c| {
                c.query_row("SELECT count, size, next_seq FROM totals WHERE only = 1", [], |r| {
                    Ok((r.get(0)?, r.get(1)?, r.get(2)?))
                })
                .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap()
        };
        let before = totals(&s);
        // The five rows are long expired; a slow-path incr would sweep them.
        s.incr(T, "c", Number::Int(1), None, NOW + 100.0).unwrap();
        assert_eq!(len(&s), 6, "no sweep");
        assert_eq!(totals(&s), before, "totals not written");
        // A miss still takes the full path, which sweeps.
        s.incr(T, "new", Number::Int(1), Some(Number::Int(0)), NOW + 100.0).unwrap();
        assert_eq!(len(&s), 2);
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

    /// Every key, least recently stored first.
    fn keys(s: &Store) -> Vec<String> {
        s.read(T, |c| {
            let mut stmt =
                c.prepare("SELECT key FROM cache ORDER BY store_seq").map_err(|e| Error::from_sqlite(e, T))?;
            stmt.query_map([], |r| r.get(0)).and_then(Iterator::collect).map_err(|e| Error::from_sqlite(e, T))
        })
        .unwrap()
    }

    /// Five live spilled values, then nine that expire after a second: a megabyte's worth, so a
    /// reckoning that still counts the nine once they're swept is over the limit.
    fn five_live_and_nine_expiring(s: &Store) {
        let blob = vec![0u8; 70_000];
        let v = Value::Bytes { mode: MODE_BYTES, data: &blob };
        for i in 0..5 {
            s.set(T, &format!("k{i}"), v, None, NOW + f64::from(i)).unwrap();
        }
        for i in 0..9 {
            s.set(T, &format!("e{i}"), v, Some(1.0), NOW + 10.0 + f64::from(i) * 0.001).unwrap();
        }
        assert_eq!(len(s), 14, "nothing was culled while filling");
        let volume = s.volume(T).unwrap();
        assert!(volume + 70_000 >= 1 << 20, "with the nine still counted the cache is over its limit ({volume})");
    }

    #[test]
    fn a_sweep_of_spilled_values_that_makes_room_evicts_nothing() {
        type Write = fn(&Store, Value<'_>);
        let blob = vec![0u8; 70_000];
        let v = Value::Bytes { mode: MODE_BYTES, data: &blob };
        // The writes that cull and add a value's bytes: a set and an add. (An incr adds none: below.)
        let writes: [(&str, Write); 2] = [
            ("set", |s, v| s.set(T, "new", v, None, NOW + 100.0).map(drop).unwrap()),
            ("add", |s, v| assert!(s.add(T, "new", v, None, NOW + 100.0).unwrap())),
        ];
        for (name, write) in writes {
            let tmp = TempDir::new("ops-cull-sweep-room");
            let s = store(tmp.path(), Requested { size_limit: Some(1 << 20), cull_limit: Some(10) });
            five_live_and_nine_expiring(&s);
            write(&s, v);
            assert_eq!(keys(&s), ["k0", "k1", "k2", "k3", "k4", "new"], "{name}: every live row survives");
        }
    }

    #[test]
    fn an_incr_whose_sweep_makes_room_evicts_nothing() {
        // An incr of a missing key culls too, but adds no bytes, so the cache has to be over its
        // limit before the write: filled with culling off, then joined again with it on (joining a
        // live store updates its limits).
        let tmp = TempDir::new("ops-cull-sweep-incr");
        let s = store(tmp.path(), Requested { size_limit: Some(1 << 20), cull_limit: Some(0) });
        let (live, expiring) = (vec![0u8; 70_000], vec![0u8; 80_000]);
        for i in 0..5 {
            s.set(T, &format!("k{i}"), Value::Bytes { mode: MODE_BYTES, data: &live }, None, NOW + f64::from(i))
                .unwrap();
        }
        // Nine, not ten: ten would use the whole budget and leave none to over-evict with.
        for i in 0..9 {
            let at = NOW + 10.0 + f64::from(i) * 0.001;
            s.set(T, &format!("e{i}"), Value::Bytes { mode: MODE_BYTES, data: &expiring }, Some(1.0), at).unwrap();
        }
        assert_eq!(len(&s), 14);
        assert!(s.volume(T).unwrap() >= 1 << 20, "over the limit before the incr");
        let s = store(tmp.path(), Requested { size_limit: None, cull_limit: Some(10) });
        let one = s.incr(T, "new", Number::Int(1), Some(Number::Int(0)), NOW + 100.0).unwrap();
        assert_eq!(one, Number::Int(1));
        assert_eq!(keys(&s), ["k0", "k1", "k2", "k3", "k4", "new"], "every live row survives");
    }

    /// Guards the other direction: this passes on the code before the fix, and fails if the swept
    /// bytes are taken off twice (nothing evicted) or the sweep's rows aren't charged to the budget.
    #[test]
    fn a_sweep_of_spilled_values_that_is_not_enough_evicts_with_the_rest_of_the_budget() {
        let tmp = TempDir::new("ops-cull-sweep-short");
        let s = store(tmp.path(), Requested { size_limit: Some(1 << 20), cull_limit: Some(10) });
        let blob = vec![0u8; 70_000];
        let v = Value::Bytes { mode: MODE_BYTES, data: &blob };
        for i in 0..12 {
            s.set(T, &format!("k{i:02}"), v, None, NOW + f64::from(i)).unwrap();
        }
        for i in 0..2 {
            s.set(T, &format!("e{i}"), v, Some(1.0), NOW + 20.0 + f64::from(i) * 0.001).unwrap();
        }
        assert_eq!(len(&s), 14, "nothing was culled while filling");
        let big = vec![1u8; 210_000];
        s.set(T, "big", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW + 100.0).unwrap();
        // Two swept, so eight of the budget of ten are left for the least recently stored.
        assert_eq!(keys(&s), ["k08", "k09", "k10", "k11", "big"]);
    }

    #[test]
    fn a_sweep_inside_a_rolled_back_transaction_puts_everything_back() {
        let tmp = TempDir::new("ops-cull-sweep-held");
        let s = store(tmp.path(), Requested { size_limit: Some(1 << 20), cull_limit: Some(10) });
        five_live_and_nine_expiring(&s);
        let (before, files, volume) = (keys(&s), value_files(tmp.path()), s.volume(T).unwrap());
        let blob = vec![0u8; 70_000];
        let held = s.begin_hold(T).unwrap();
        s.set(T, "new", Value::Bytes { mode: MODE_BYTES, data: &blob }, None, NOW + 100.0).unwrap();
        assert_eq!(keys(&s), ["k0", "k1", "k2", "k3", "k4", "new"]);
        held.finish(false).unwrap();
        assert_eq!(keys(&s), before, "the swept rows are back");
        assert_eq!(value_files(tmp.path()), files, "and so are their files, with the new one gone");
        assert_eq!(s.volume(T).unwrap(), volume, "and totals.size with them");
    }

    #[test]
    fn the_write_time_sweep_removes_rows_expired_before_now_not_at_now() {
        // As in diskcache: a row expiring exactly now is a miss, but only a later sweep removes it.
        let tmp = TempDir::new("ops-sweep-boundary");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "earlier", text("v"), Some(1.0), NOW - 2.0).unwrap(); // expires at NOW - 1
        s.set(T, "exactly", text("v"), Some(1.0), NOW - 1.0).unwrap(); // expires at NOW
        s.set(T, "new", text("v"), None, NOW).unwrap();
        assert_eq!(keys(&s), ["exactly", "new"]);
        assert!(get(&s, "exactly", NOW).is_none(), "but it reads as a miss");
        s.set(T, "later", text("v"), None, NOW + 0.5).unwrap();
        assert_eq!(keys(&s), ["new", "later"]);
    }

    #[test]
    fn deleting_a_version_from_inside_a_decode_is_skipped() {
        // A decode runs inside the read, at gate depth 1: the delete must not try, lock or no lock.
        let tmp = TempDir::new("ops-delete-in-decode");
        let s = store(tmp.path(), Requested::default());
        s.set(T, "k", text("v"), None, NOW).unwrap();
        let v = version(&s, "k").unwrap();
        let inside = s.get_with(T, "k", NOW, |_| false, |_, _| Ok(s.delete_version_nowait(v))).unwrap().unwrap();
        assert!(matches!(inside.value, VersionDelete::Skipped));
        assert_eq!(get(&s, "k", NOW).unwrap().0, b"v", "the row is still there");
        assert!(matches!(delete_nowait(&s, v), VersionDelete::Removed), "and goes from outside");
    }

    #[test]
    fn a_reader_that_keeps_losing_to_overwrites_in_an_unwritable_child_misses() {
        // D-020: in a child forked inside a transaction on this file the locked fallback can't take
        // the write lock, so after its optimistic reads it reports a miss instead.
        let tmp = TempDir::new("ops-d016-poisoned");
        let s = store(tmp.path(), Requested::default());
        set_big(&s, MODE_BYTES, 0);
        let calls = std::rc::Rc::new(std::cell::Cell::new(0_u8));
        let poisoned = std::rc::Rc::new(std::cell::RefCell::new(None));
        let (counted, poison) = (calls.clone(), poisoned.clone());
        let optimistic = Hook::install(&BEFORE_OPTIMISTIC_READ, move |s| {
            counted.set(counted.get() + 1);
            set_big(s, MODE_BYTES, counted.get());
            if usize::from(counted.get()) == spill::OPTIMISTIC_ATTEMPTS {
                *poison.borrow_mut() = Some(s.poison_for_test());
            }
        });
        let fallback = Hook::install(&BEFORE_FALLBACK, |_| panic!("an unwritable child must not take the lock"));
        let got = get(&s, "k", NOW);
        drop((optimistic, fallback));
        assert_eq!(usize::from(calls.get()), spill::OPTIMISTIC_ATTEMPTS, "every optimistic read lost");
        assert!(got.is_none(), "a miss, not a read under the lock");
        assert!(matches!(s.check_writable(), Err(Error::Unwritable)));
        drop(poisoned.borrow_mut().take());
        let last = u8::try_from(spill::OPTIMISTIC_ATTEMPTS).unwrap();
        assert_eq!(get(&s, "k", NOW).unwrap().0, vec![last; spill::THRESHOLD], "writable again, it reads");
    }

    /// Makes the next `n` commits on this thread fail busy; returns how many were left unconsumed.
    fn commits_fail_busy(n: u32, write: impl FnOnce()) -> u32 {
        crate::store::FAIL_NEXT_COMMITS_BUSY.with(|left| left.set(n));
        write();
        crate::store::FAIL_NEXT_COMMITS_BUSY.with(|left| left.replace(0))
    }

    /// `totals` agrees with the rows it counts.
    fn assert_totals_exact(s: &Store, what: &str) {
        let (totals, rows): ((i64, i64), (i64, i64)) = s
            .read(T, |c| {
                let totals =
                    c.query_row("SELECT count, size FROM totals WHERE only = 1", [], |r| Ok((r.get(0)?, r.get(1)?)));
                let rows = c.query_row("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache", [], |r| {
                    Ok((r.get(0)?, r.get(1)?))
                });
                totals.and_then(|t| rows.map(|r| (t, r))).map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap();
        assert_eq!(totals, rows, "{what}: totals (count, size) must match the rows");
    }

    #[test]
    fn a_write_whose_commit_comes_back_busy_is_retried_from_the_start() {
        let tmp = TempDir::new("ops-commit-busy");
        let s = store(tmp.path(), Requested::default());
        let runs = std::cell::Cell::new(0);
        let left = commits_fail_busy(1, || {
            s.write(T, |_, _| {
                runs.set(runs.get() + 1);
                Ok(())
            })
            .unwrap();
        });
        assert_eq!((left, runs.get()), (0, 2), "the busy COMMIT was rolled back and the closure ran again");

        // The full write path: an incr of a missing key, with its prelude, totals and cull.
        let left = commits_fail_busy(2, || {
            assert_eq!(s.incr(T, "n", Number::Int(7), Some(Number::Int(0)), NOW).unwrap(), Number::Int(7));
        });
        assert_eq!(left, 0);
        assert_eq!(get(&s, "n", NOW).unwrap().0, b"7", "the rolled-back attempts left no trace");
        assert_totals_exact(&s, "incr of a missing key");

        // The in-place path: moved by exactly one delta.
        let left = commits_fail_busy(1, || {
            assert_eq!(s.incr(T, "n", Number::Int(3), None, NOW).unwrap(), Number::Int(10));
        });
        assert_eq!(left, 0);
        assert_eq!(get(&s, "n", NOW).unwrap().0, b"10");
        assert_totals_exact(&s, "in-place incr");

        let left = commits_fail_busy(1, || assert!(s.set(T, "k", text("v"), None, NOW).unwrap()));
        assert_eq!(left, 0);
        assert_eq!(get(&s, "k", NOW).unwrap().0, b"v");
        assert_totals_exact(&s, "inline set");
    }

    #[test]
    fn a_spilled_write_whose_commit_comes_back_busy_keeps_exactly_its_own_file() {
        // The retry renames the value file again (commit_name's retried arm) and settles it once.
        let tmp = TempDir::new("ops-commit-busy-spill");
        let s = store(tmp.path(), Requested::default());
        let (first, second) = (vec![1u8; 70_000], vec![2u8; 80_000]);
        let left = commits_fail_busy(1, || {
            assert!(s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &first }, None, NOW).unwrap());
        });
        assert_eq!(left, 0);
        assert_eq!(get(&s, "k", NOW).unwrap().0, first);
        assert_eq!(value_files(tmp.path()).len(), 1, "one value file, not one per attempt");
        assert_totals_exact(&s, "spilled set");

        // An overwrite, busy twice: it ends with the new file alone, the replaced one removed.
        let left = commits_fail_busy(2, || {
            assert!(s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: &second }, None, NOW).unwrap());
        });
        assert_eq!(left, 0);
        assert_eq!(get(&s, "k", NOW).unwrap().0, second);
        assert_eq!(value_files(tmp.path()).len(), 1, "the replaced file is gone and the new one kept");
        assert_totals_exact(&s, "spilled overwrite");

        // An add takes the same path: settle_with keeps the file only because the retry stored it.
        let left = commits_fail_busy(1, || {
            assert!(s.add(T, "a", Value::Bytes { mode: MODE_BYTES, data: &first }, None, NOW).unwrap());
        });
        assert_eq!(left, 0);
        assert_eq!(get(&s, "a", NOW).unwrap().0, first);
        assert_eq!(value_files(tmp.path()).len(), 2);
        assert_totals_exact(&s, "spilled add");
    }

    #[test]
    fn a_write_whose_commit_stays_busy_times_out_and_leaves_nothing() {
        let tmp = TempDir::new("ops-commit-busy-timeout");
        let s = store(tmp.path(), Requested::default());
        let blob = vec![3u8; 70_000];
        crate::store::FAIL_NEXT_COMMITS_BUSY.with(|left| left.set(u32::MAX));
        let result = s.set(Duration::from_millis(50), "k", Value::Bytes { mode: MODE_BYTES, data: &blob }, None, NOW);
        crate::store::FAIL_NEXT_COMMITS_BUSY.with(|left| left.set(0));
        assert!(matches!(result, Err(Error::Timeout { seconds }) if (seconds - 0.05).abs() < 1e-9), "{result:?}");
        assert!(get(&s, "k", NOW).is_none());
        assert!(value_files(tmp.path()).is_empty(), "the pending value file is discarded");
        assert_totals_exact(&s, "timed-out set");
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
            role: crate::open::Role::Plain,
            default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
        })
        .unwrap();
        for i in 0..300 {
            let key = format!("s{}", i % 100);
            let now = 1.0 + f64::from(i);
            store.set(timeout, &key, Value::Bytes { mode: 1, data: b"v" }, Some(1_000.0), now).unwrap();
            store.get_with(timeout, &key, now, |_| false, |_, _| Ok(())).unwrap();
            store.incr(timeout, "n", Number::Int(1), Some(Number::Int(0)), now).unwrap();
        }
        let reprepares = store
            .read(timeout, |conn| {
                let count = |sql: &str| -> Result<i32> {
                    let stmt = conn.prepare_cached(sql).map_err(|e| Error::from_sqlite(e, timeout))?;
                    Ok(stmt.get_status(StatementStatus::RePrepare))
                };
                Ok((count(PRELUDE_SQL)?, count(LOOKUP_SQL)?, count(INCR_SQL)?, count(UPDATE_NUMBER_SQL)?))
            })
            .unwrap();
        assert_eq!(reprepares, (0, 0, 0, 0), "(prelude, lookup, incr, update) were recompiled");
    }
}
