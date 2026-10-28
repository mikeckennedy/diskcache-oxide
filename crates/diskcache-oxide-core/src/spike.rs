//! Phase 1, Spikes 2 and 5 only: the smallest store that can answer the latency gate (`get`, `set`)
//! and the fork tests (`incr`, a held write transaction), built on the hot path the real cache will
//! use: the fork gate, the generation-checked pool, cached statements and a native clock read. It
//! is replaced in Phase 2; the gate and the pool are kept.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::thread::ThreadId;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::types::{ToSqlOutput, ValueRef};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};

use crate::gate::{GATE, HeldShare, SharedGuard};
use crate::pool::{Pool, PooledConn};
use crate::{Error, Result};

/// Value modes stored in the `mode` column.
pub const MODE_STR: i64 = 1;
pub const MODE_BYTES: i64 = 2;
pub const MODE_INT: i64 = 3;

/// How many idle connections a store keeps for reuse.
const MAX_IDLE: usize = 8;

const DB_FILENAME: &str = "diskcache-oxide.sqlite";

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS cache (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    mode INTEGER NOT NULL,
    value BLOB NOT NULL,
    store_time REAL NOT NULL,
    expire_time REAL,
    store_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cache_store_seq ON cache (store_seq);
";

const GET_SQL: &str = "SELECT mode, value FROM cache WHERE key = ?1 AND (expire_time IS NULL OR expire_time > ?2)";

const SET_SQL: &str = "
INSERT INTO cache (key, mode, value, store_time, expire_time, store_seq)
VALUES (?1, ?2, ?3, ?4, ?5, (SELECT COALESCE(MAX(store_seq), 0) + 1 FROM cache))
ON CONFLICT (key) DO UPDATE SET
    mode = excluded.mode,
    value = excluded.value,
    store_time = excluded.store_time,
    expire_time = excluded.expire_time,
    store_seq = excluded.store_seq
";

/// `incr` keeps the entry's expiry, like diskcache.
const INCR_UPDATE_SQL: &str = "UPDATE cache SET value = ?2, store_time = ?3 WHERE key = ?1";

/// A stored value, borrowed from the caller (`set`) or from SQLite's row buffer (`get`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoredValue<'a> {
    /// UTF-8 text.
    Str(&'a [u8]),
    Bytes(&'a [u8]),
    Int(i64),
}

impl<'a> StoredValue<'a> {
    fn mode(self) -> i64 {
        match self {
            Self::Str(_) => MODE_STR,
            Self::Bytes(_) => MODE_BYTES,
            Self::Int(_) => MODE_INT,
        }
    }

    fn to_sql(self) -> ToSqlOutput<'a> {
        match self {
            Self::Str(b) | Self::Bytes(b) => ToSqlOutput::Borrowed(ValueRef::Blob(b)),
            Self::Int(i) => ToSqlOutput::from(i),
        }
    }
}

/// What `incr` found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Incr {
    /// The new value, which was stored.
    Value(i64),
    /// No live entry, and no default.
    Missing,
    /// The entry isn't an integer.
    NotAnInteger,
    /// The result doesn't fit in an `i64`.
    Overflow,
}

/// A write transaction a thread holds open across calls (the `_hold_write_transaction` test hook).
#[derive(Debug)]
struct Hold {
    thread: ThreadId,
    /// Taken out while one of the holding thread's operations runs on it.
    conn: Option<PooledConn>,
    /// The holding thread's gate level, kept for as long as the transaction is open.
    share: HeldShare<'static>,
}

#[derive(Debug)]
pub struct SpikeStore {
    db_path: PathBuf,
    timeout: Duration,
    pool: Pool,
    /// Locked only under the gate, like the pool.
    holds: Mutex<Vec<Hold>>,
    /// `holds.len()`, read without the lock on the hot path.
    active_holds: AtomicUsize,
}

impl SpikeStore {
    pub fn open(directory: &Path, timeout: Duration) -> Result<Self> {
        std::fs::create_dir_all(directory).map_err(|source| Error::Io { path: directory.to_path_buf(), source })?;
        let gate = GATE.enter_shared();
        let store = Self {
            db_path: directory.join(DB_FILENAME),
            timeout,
            pool: Pool::new(&gate, MAX_IDLE),
            holds: Mutex::new(Vec::new()),
            active_holds: AtomicUsize::new(0),
        };
        let pooled = store.pool.checkout(&gate, || store.connect())?;
        let created = pooled.conn().execute_batch(SCHEMA).map_err(|e| Error::from_sqlite(e, timeout));
        store.pool.checkin(&gate, pooled);
        created?;
        drop(gate);
        Ok(store)
    }

    /// Calls `f` with the stored value, or returns `None` on a miss or an expired entry.
    ///
    /// `f` runs while a pooled connection and the fork gate are held, so it must not call back into
    /// a store, nor run arbitrary user code.
    pub fn get_with<T>(&self, key: &str, f: impl FnOnce(StoredValue<'_>) -> T) -> Result<Option<T>> {
        let now = now();
        self.with_conn(|conn, _| {
            let mut stmt = conn.prepare_cached(GET_SQL)?;
            let mut rows = stmt.query(params![key, now])?;
            let Some(row) = rows.next()? else { return Ok(None) };
            let mode: i64 = row.get(0)?;
            let value = match (mode, row.get_ref(1)?) {
                (MODE_INT, ValueRef::Integer(i)) => StoredValue::Int(i),
                (MODE_STR, ValueRef::Blob(b) | ValueRef::Text(b)) => StoredValue::Str(b),
                (_, ValueRef::Blob(b) | ValueRef::Text(b)) => StoredValue::Bytes(b),
                _ => StoredValue::Bytes(&[]),
            };
            Ok(Some(f(value)))
        })
    }

    pub fn set(&self, key: &str, value: StoredValue<'_>, expire: Option<f64>) -> Result<()> {
        let now = now();
        let expire_time = expire.map(|seconds| now + seconds);
        self.with_conn(|conn, _| {
            conn.prepare_cached(SET_SQL)?.execute(params![key, value.mode(), value.to_sql(), now, expire_time])?;
            Ok(())
        })
    }

    /// Adds `delta` to the integer stored at `key` atomically, starting from `default` if there is no
    /// live entry. Joins the calling thread's held transaction, if any.
    pub fn incr(&self, key: &str, delta: i64, default: Option<i64>) -> Result<Incr> {
        let now = now();
        self.with_write_txn(|conn| {
            let current = conn
                .prepare_cached(GET_SQL)?
                .query_row(params![key, now], |row| {
                    let mode: i64 = row.get(0)?;
                    Ok(match row.get_ref(1)? {
                        ValueRef::Integer(i) if mode == MODE_INT => Some(i),
                        _ => None,
                    })
                })
                .optional()?;
            let (base, exists) = match (current, default) {
                (Some(Some(value)), _) => (value, true),
                (Some(None), _) => return Ok(Incr::NotAnInteger),
                (None, Some(default)) => (default, false),
                (None, None) => return Ok(Incr::Missing),
            };
            let Some(value) = base.checked_add(delta) else { return Ok(Incr::Overflow) };
            if exists {
                conn.prepare_cached(INCR_UPDATE_SQL)?.execute(params![key, value, now])?;
            } else {
                let stored = StoredValue::Int(value);
                conn.prepare_cached(SET_SQL)?.execute(params![
                    key,
                    stored.mode(),
                    stored.to_sql(),
                    now,
                    None::<f64>
                ])?;
            }
            Ok(Incr::Value(value))
        })
    }

    /// Starts a write transaction (`BEGIN IMMEDIATE`) that the calling thread holds until
    /// [`end_hold`](Self::end_hold). The thread's operations on this store join it, and it holds
    /// the fork gate throughout, so a fork from another thread waits for it to end.
    ///
    /// If the holding thread itself forks, the transaction is poisoned in the child: its operations
    /// there, and `end_hold`, fail with [`Error::ForkPoisoned`], and nothing commits.
    pub fn begin_hold(&self) -> Result<()> {
        let gate = GATE.enter_shared();
        let thread = std::thread::current().id();
        if self.lock_holds().iter().any(|hold| hold.thread == thread) {
            return Err(Error::NestedTransaction);
        }
        let pooled = self.pool.checkout(&gate, || self.connect())?;
        if let Err(error) = pooled.conn().execute_batch("BEGIN IMMEDIATE") {
            self.pool.checkin(&gate, pooled);
            return Err(self.sqlite_err(error));
        }
        let mut holds = self.lock_holds();
        holds.push(Hold { thread, conn: Some(pooled), share: gate.into_held() });
        self.active_holds.store(holds.len(), Ordering::Release);
        Ok(())
    }

    /// Ends the calling thread's held transaction: commits it, or rolls it back if `commit` is false.
    pub fn end_hold(&self, commit: bool) -> Result<()> {
        let gate = GATE.enter_shared();
        let hold = {
            let mut holds = self.lock_holds();
            let thread = std::thread::current().id();
            let index = holds.iter().position(|hold| hold.thread == thread).ok_or(Error::NoTransaction)?;
            let hold = holds.swap_remove(index);
            self.active_holds.store(holds.len(), Ordering::Release);
            hold
        };
        let Hold { conn, share, .. } = hold;
        let result = match conn {
            // An operation of this thread is still using it, which can't happen from Python.
            None => Err(Error::NestedTransaction),
            Some(pooled) if pooled.is_stale() => {
                // Inherited across fork: the parent's transaction is the parent's to finish.
                pooled.forget();
                Err(Error::ForkPoisoned)
            }
            Some(pooled) => {
                let result = pooled.conn().execute_batch(if commit { "COMMIT" } else { "ROLLBACK" });
                if result.is_err() && !pooled.conn().is_autocommit() {
                    let _ = pooled.conn().execute_batch("ROLLBACK");
                }
                if pooled.conn().is_autocommit() {
                    self.pool.checkin(&gate, pooled);
                } else {
                    drop(pooled);
                }
                result.map_err(|e| self.sqlite_err(e))
            }
        };
        drop(gate);
        drop(share);
        result
    }

    /// How many entries are stored, expired or not (like diskcache's `len`).
    pub fn count(&self) -> Result<i64> {
        self.with_conn(|conn, _| conn.prepare_cached("SELECT COUNT(*) FROM cache")?.query_row([], |row| row.get(0)))
    }

    /// Closes every idle connection.
    pub fn close(&self) {
        let gate = GATE.enter_shared();
        self.pool.clear(&gate);
    }

    /// Runs `f` on the calling thread's held connection if it holds a transaction (`true`), or on a
    /// pooled one (`false`). The gate is held throughout; it is taken before the connection, and
    /// checkout never waits, so nothing blocks while the gate is held.
    fn with_conn<T>(&self, f: impl FnOnce(&Connection, bool) -> rusqlite::Result<T>) -> Result<T> {
        let gate = GATE.enter_shared();
        if let Some(pooled) = self.take_held(&gate)? {
            let result = f(pooled.conn(), true).map_err(|e| self.sqlite_err(e));
            self.return_held(pooled);
            return result;
        }
        let pooled = self.pool.checkout(&gate, || self.connect())?;
        let result = f(pooled.conn(), false).map_err(|e| self.sqlite_err(e));
        self.pool.checkin(&gate, pooled);
        result
    }

    /// Runs `f` inside a write transaction: the held one, or a fresh `BEGIN IMMEDIATE` that commits
    /// if `f` succeeds and rolls back otherwise.
    fn with_write_txn<T>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Result<T> {
        self.with_conn(|conn, held| {
            if held {
                return f(conn);
            }
            let tx = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
            let value = f(&tx)?;
            tx.commit()?;
            Ok(value)
        })
    }

    /// The calling thread's held connection, if it holds a transaction on this store.
    fn take_held(&self, _gate: &SharedGuard<'_>) -> Result<Option<PooledConn>> {
        if self.active_holds.load(Ordering::Acquire) == 0 {
            return Ok(None);
        }
        let thread = std::thread::current().id();
        let mut holds = self.lock_holds();
        let Some(hold) = holds.iter_mut().find(|hold| hold.thread == thread) else { return Ok(None) };
        match &hold.conn {
            Some(pooled) if pooled.is_stale() => Err(Error::ForkPoisoned),
            Some(_) => Ok(hold.conn.take()),
            None => Err(Error::NestedTransaction),
        }
    }

    fn return_held(&self, pooled: PooledConn) {
        let thread = std::thread::current().id();
        if let Some(hold) = self.lock_holds().iter_mut().find(|hold| hold.thread == thread) {
            hold.conn = Some(pooled);
        }
    }

    fn sqlite_err(&self, error: rusqlite::Error) -> Error {
        Error::from_sqlite(error, self.timeout)
    }

    fn lock_holds(&self) -> MutexGuard<'_, Vec<Hold>> {
        self.holds.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Opens a connection. Called under the gate, by the pool.
    fn connect(&self) -> Result<Connection> {
        let mut conn = Connection::open(&self.db_path).map_err(|e| self.sqlite_err(e))?;
        conn.busy_handler(Some(busy_waiter(self.timeout))).map_err(|e| self.sqlite_err(e))?;
        let mode: String =
            conn.query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0)).map_err(|e| self.sqlite_err(e))?;
        if !mode.eq_ignore_ascii_case("wal") {
            return Err(Error::NotWal { mode });
        }
        conn.execute_batch("PRAGMA synchronous = NORMAL").map_err(|e| self.sqlite_err(e))?;
        Ok(conn)
    }
}

impl Drop for SpikeStore {
    fn drop(&mut self) {
        // Transactions still held are rolled back by closing their connection (or forgotten if
        // inherited across fork). The pool empties itself when it drops.
        let gate = GATE.enter_shared();
        let holds = std::mem::take(&mut *self.lock_holds());
        for Hold { conn, share, .. } in holds {
            match conn {
                Some(pooled) if pooled.is_stale() => pooled.forget(),
                other => drop(other),
            }
            drop(share);
        }
        drop(gate);
    }
}

/// SQLite's busy handler: retries until `timeout` has passed since the first `SQLITE_BUSY`.
///
/// SQLite's built-in `busy_timeout` backs off to 100 ms between attempts, about 20 attempts a
/// second, and a lock is only ever won by polling. Against a writer that holds the lock most of the
/// time (a hot writer thread in another process) every attempt can miss, and the waiter times out
/// although the lock was free thousands of times. Found by the fork tests: a forked child's first
/// write timed out in about 1-4% of forks under a parent's write hammer, with the barrier working.
/// This polls at most 200 µs apart instead.
fn busy_waiter(timeout: Duration) -> impl FnMut(i32) -> bool + Send + 'static {
    const MIN_SLEEP: Duration = Duration::from_micros(10);
    const MAX_SLEEP: Duration = Duration::from_micros(200);
    let mut started = Instant::now();
    move |count| {
        if count == 0 {
            started = Instant::now();
        }
        let Some(left) = timeout.checked_sub(started.elapsed()).filter(|left| !left.is_zero()) else {
            return false;
        };
        let backoff = MIN_SLEEP.saturating_mul(1 << count.clamp(0, 5).unsigned_abs()).min(MAX_SLEEP);
        std::thread::sleep(backoff.min(left));
        true
    }
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("dco-spike-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    fn owned(value: StoredValue<'_>) -> (i64, Vec<u8>) {
        match value {
            StoredValue::Str(b) => (MODE_STR, b.to_vec()),
            StoredValue::Bytes(b) => (MODE_BYTES, b.to_vec()),
            StoredValue::Int(i) => (MODE_INT, i.to_le_bytes().to_vec()),
        }
    }

    #[test]
    fn set_then_get_round_trips() {
        let dir = temp_dir("roundtrip");
        let store = SpikeStore::open(&dir, Duration::from_secs(5)).unwrap();
        store.set("k", StoredValue::Str(b"value"), None).unwrap();
        assert_eq!(store.get_with("k", owned).unwrap(), Some((MODE_STR, b"value".to_vec())));
        store.set("n", StoredValue::Int(-3), None).unwrap();
        assert_eq!(store.get_with("n", owned).unwrap(), Some(owned(StoredValue::Int(-3))));
        assert_eq!(store.get_with("missing", |_| ()).unwrap(), None);
        drop(store);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn overwrite_bumps_store_seq() {
        let dir = temp_dir("seq");
        let store = SpikeStore::open(&dir, Duration::from_secs(5)).unwrap();
        store.set("a", StoredValue::Bytes(b"1"), None).unwrap();
        store.set("a", StoredValue::Bytes(b"2"), None).unwrap();
        let seq: i64 = store
            .with_conn(|c, _| c.query_row("SELECT store_seq FROM cache WHERE key = 'a'", [], |r| r.get(0)))
            .unwrap();
        assert_eq!(seq, 2);
        drop(store);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn expired_entries_miss() {
        let dir = temp_dir("expire");
        let store = SpikeStore::open(&dir, Duration::from_secs(5)).unwrap();
        store.set("k", StoredValue::Str(b"v"), Some(-1.0)).unwrap();
        assert_eq!(store.get_with("k", |_| ()).unwrap(), None);
        drop(store);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn incr_counts_from_the_default() {
        let dir = temp_dir("incr");
        let store = SpikeStore::open(&dir, Duration::from_secs(5)).unwrap();
        assert_eq!(store.incr("c", 0, Some(0)).unwrap(), Incr::Value(0));
        assert_eq!(store.incr("c", 5, Some(0)).unwrap(), Incr::Value(5));
        assert_eq!(store.incr("missing", 1, None).unwrap(), Incr::Missing);
        store.set("s", StoredValue::Str(b"x"), None).unwrap();
        assert_eq!(store.incr("s", 1, Some(0)).unwrap(), Incr::NotAnInteger);
        store.set("big", StoredValue::Int(i64::MAX), None).unwrap();
        assert_eq!(store.incr("big", 1, Some(0)).unwrap(), Incr::Overflow);
        drop(store);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn concurrent_incr_is_exact() {
        let dir = temp_dir("incr-threads");
        let store = Arc::new(SpikeStore::open(&dir, Duration::from_secs(30)).unwrap());
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let store = Arc::clone(&store);
                std::thread::spawn(move || {
                    for _ in 0..100 {
                        store.incr("c", 1, Some(0)).unwrap();
                    }
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
        assert_eq!(store.get_with("c", owned).unwrap(), Some(owned(StoredValue::Int(400))));
        drop(store);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_held_transaction_is_joined_and_commits_on_end() {
        let dir = temp_dir("hold");
        let store = SpikeStore::open(&dir, Duration::from_secs(5)).unwrap();
        let other = SpikeStore::open(&dir, Duration::from_millis(50)).unwrap();
        store.begin_hold().unwrap();
        assert!(matches!(store.begin_hold(), Err(Error::NestedTransaction)));
        store.set("k", StoredValue::Int(1), None).unwrap();
        assert_eq!(store.incr("k", 1, Some(0)).unwrap(), Incr::Value(2));
        // Another writer is locked out, and readers don't see the uncommitted write.
        assert!(matches!(other.set("x", StoredValue::Int(0), None), Err(Error::Timeout { .. })));
        assert_eq!(other.get_with("k", owned).unwrap(), None);
        assert_eq!(GATE.own_depth(), 1, "the hold keeps one gate level");
        store.end_hold(true).unwrap();
        assert_eq!(GATE.own_depth(), 0);
        assert_eq!(other.get_with("k", owned).unwrap(), Some(owned(StoredValue::Int(2))));
        assert!(matches!(store.end_hold(true), Err(Error::NoTransaction)));
        drop((store, other));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_rolled_back_hold_writes_nothing() {
        let dir = temp_dir("rollback");
        let store = SpikeStore::open(&dir, Duration::from_secs(5)).unwrap();
        store.begin_hold().unwrap();
        store.set("k", StoredValue::Int(1), None).unwrap();
        store.end_hold(false).unwrap();
        assert_eq!(store.get_with("k", owned).unwrap(), None);
        drop(store);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
