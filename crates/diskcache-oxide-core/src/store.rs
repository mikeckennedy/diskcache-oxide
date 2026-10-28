//! `Store`: the process-wide state for one database file, shared by every cache handle on it.
//!
//! A handle (the Python `Cache` object) is a thin reference to a `Store`, so extra handles on the
//! same directory are cheap and share one connection pool, one in-process write lock and one set of
//! cached settings (04-design.md section 7; 05-phase-2-core-cache.md, "Concurrency rules").
//!
//! # Locking
//!
//! Every operation follows one order: the **write lock** (writes only), then the **gate** in shared
//! mode, then a **pool connection**. No blocking wait happens while the gate is held. A write waits
//! for the write lock, and any operation waits for a connection slot, *before* entering the gate,
//! with the interpreter released. At gate depth > 0 (a nested operation inside a held
//! transaction), both are `try_*` only: the write lock falls back to SQLite's busy path, and the
//! pool opens a connection over the cap.
//!
//! # Registry
//!
//! Stores are found by the database file's `(st_dev, st_ino)`. Each `Store` keeps one fd open on
//! its database file for its whole life, so that inode can't be reused by a file recreated in the
//! same process. The registry holds `Weak` references; dead entries are pruned when a store is
//! inserted, never in `Drop`. The registry mutex is only taken under the gate, so a child never
//! inherits it locked. Across `fork()`, the `Store` is reset in place: its pool, write lock and slot
//! counter all recognize the new [process generation](crate::gate::generation).

use std::fs::File;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::time::{Duration, Instant};

use rusqlite::{Connection, Transaction, TransactionBehavior};

use crate::gate::{self, GATE, HeldShare, SharedGuard};
use crate::genlock::{Slots, WriteLock};
use crate::open::{self, Limits, Requested};
use crate::poison::HoldGuard;
use crate::pool::{Pool, PooledConn};
use crate::rows::RowWriter;
use crate::{Error, Result, lockwait};

/// Default `max_connections` (04-design.md section 2).
pub const DEFAULT_MAX_CONNECTIONS: u64 = 8;

/// Default page-cache size of each connection, in pages (`PRAGMA cache_size`). With
/// [`DEFAULT_MAX_CONNECTIONS`] this bounds a store's page caches at `8 × pages × page_size`.
pub const DEFAULT_CACHE_PAGES: u64 = 256;

use crate::poison::FileId;

static REGISTRY: Mutex<Vec<Weak<Store>>> = Mutex::new(Vec::new());

/// The registry. Only called with the gate held shared (the `_gate` argument is the proof).
fn registry(_gate: &SharedGuard<'_>) -> MutexGuard<'static, Vec<Weak<Store>>> {
    REGISTRY.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Checks that the registry is free at fork time. Called by the before-fork hook after it took the
/// gate exclusively; see `pool::quiesce_all` for the reasoning.
pub(crate) fn quiesce_registry() {
    let busy = matches!(REGISTRY.try_lock(), Err(std::sync::TryLockError::WouldBlock));
    debug_assert!(!busy, "the store registry was locked at fork time");
    if busy {
        drop(REGISTRY.lock());
    }
}

/// How a handle wants its store opened.
#[derive(Debug, Clone)]
pub struct OpenOptions {
    pub directory: PathBuf,
    pub timeout: Duration,
    pub requested: Requested,
    pub max_connections: u64,
    /// Page-cache size of each connection, in pages (`sqlite_cache_size`).
    pub cache_pages: u64,
}

/// One database file's shared state. See the module docs.
#[derive(Debug)]
pub struct Store {
    directory: PathBuf,
    file_id: FileId,
    /// Held open so the inode stays reserved while any handle is open. Released when the last
    /// handle closes (D-018: nothing left open); the store is then `retired` from the registry, so a
    /// later open of the directory gets a fresh store even if the inode number has been reused.
    pin: Mutex<Option<File>>,
    retired: AtomicBool,
    pool: Pool,
    write_lock: WriteLock,
    slots: Slots,
    /// The largest `max_connections` any handle on this store asked for.
    max_connections: AtomicU64,
    /// The largest `cache_pages` any handle on this store asked for. Each leased connection is
    /// brought up to it.
    cache_pages: AtomicU64,
    /// Open handles. The pool drains when the last one closes.
    handles: AtomicUsize,
    /// Cached copies of the persisted limits, for the attribute getters only. Write paths read the
    /// current values inside their transaction.
    size_limit: AtomicI64,
    cull_limit: AtomicI64,
    /// A write transaction held across calls (`_hold_write_transaction()` now, `transact()` in
    /// Phase 6b): its owner thread's operations run on the pinned connection. Locked only under
    /// the gate. `pinned_active` keeps the common no-hold path to one relaxed load.
    pinned: Mutex<Option<Pinned>>,
    pinned_active: AtomicBool,
}

/// The connection of a held write transaction, already inside `BEGIN IMMEDIATE`.
#[derive(Debug)]
struct Pinned {
    owner: std::thread::ThreadId,
    generation: u64,
    conn: PooledConn,
}

impl Store {
    /// Opens (or joins) the store for `options.directory` and registers one more handle on it.
    pub fn open(options: &OpenOptions) -> Result<Arc<Self>> {
        if options.max_connections == 0 {
            return Err(Error::InvalidSetting("max_connections must be at least 1".into()));
        }
        if options.cache_pages == 0 {
            return Err(Error::InvalidSetting("sqlite_cache_size must be at least 1 page".into()));
        }
        options.requested.validate()?;
        open::ensure_directory(&options.directory)?;
        let db_path = open::db_path(&options.directory);

        // Fast path: another handle in this process already has the store.
        if let Some(store) = Self::lookup(&db_path) {
            store.join(options)?;
            return Ok(store);
        }

        // Open outside the registry lock (the open sequence may wait on other processes), then
        // insert if absent; if another thread won the race, use its store and drop this connection.
        let (conn, limits) = lockwait::retry(options.timeout, |_| {
            let _gate = GATE.enter_shared();
            let mut conn = open::connect(&options.directory, options.timeout)?;
            let limits = open::prepare(&mut conn, options.requested, options.timeout)?;
            Ok((conn, limits))
        })?;
        let pin = File::open(&db_path).map_err(|source| Error::Io { path: db_path.clone(), source })?;
        let file_id = file_id(&pin, &db_path)?;

        let gate = GATE.enter_shared();
        let mut stores = registry(&gate);
        if let Some(existing) =
            stores.iter().filter_map(Weak::upgrade).find(|s| s.file_id == file_id && !s.is_retired())
        {
            drop(stores);
            drop(conn);
            drop(gate);
            existing.join(options)?;
            return Ok(existing);
        }
        let store = Arc::new(Self {
            directory: options.directory.clone(),
            file_id,
            pin: Mutex::new(Some(pin)),
            retired: AtomicBool::new(false),
            pool: Pool::new(&gate, usize::try_from(options.max_connections).unwrap_or(usize::MAX)),
            write_lock: WriteLock::new(),
            slots: Slots::new(),
            max_connections: AtomicU64::new(options.max_connections),
            cache_pages: AtomicU64::new(options.cache_pages),
            handles: AtomicUsize::new(1),
            size_limit: AtomicI64::new(limits.size_limit),
            cull_limit: AtomicI64::new(limits.cull_limit),
            pinned: Mutex::new(None),
            pinned_active: AtomicBool::new(false),
        });
        stores.retain(|weak| weak.strong_count() > 0);
        stores.push(Arc::downgrade(&store));
        drop(stores);
        // Seed the pool with the connection the open sequence used.
        let seeded = store.pool.checkout(&gate, || Ok(conn))?;
        store.pool.checkin(&gate, seeded);
        Ok(store)
    }

    fn lookup(db_path: &Path) -> Option<Arc<Self>> {
        let metadata = std::fs::metadata(db_path).ok()?;
        let id = (metadata.dev(), metadata.ino());
        let gate = GATE.enter_shared();
        registry(&gate).iter().filter_map(Weak::upgrade).find(|s| s.file_id == id && !s.is_retired())
    }

    /// Registers another handle: raises the connection cap if asked, and applies explicitly
    /// requested settings, writing only if they differ from what's stored.
    fn join(&self, options: &OpenOptions) -> Result<()> {
        self.max_connections.fetch_max(options.max_connections, Ordering::AcqRel);
        self.cache_pages.fetch_max(options.cache_pages, Ordering::AcqRel);
        self.handles.fetch_add(1, Ordering::AcqRel);
        if options.requested == Requested::default() {
            return Ok(());
        }
        let result = self.write(options.timeout, |_writer, tx| {
            let stored = open::read_limits(tx, options.timeout)?;
            let wanted = Limits {
                size_limit: options.requested.size_limit.unwrap_or(stored.size_limit),
                cull_limit: options.requested.cull_limit.unwrap_or(stored.cull_limit),
            };
            if wanted != stored {
                tx.execute(
                    "UPDATE totals SET size_limit = ?1, cull_limit = ?2 WHERE only = 1",
                    (wanted.size_limit, wanted.cull_limit),
                )
                .map_err(|e| Error::from_sqlite(e, options.timeout))?;
            }
            Ok(wanted)
        });
        match result {
            Ok(limits) => {
                self.cache_limits(limits);
                Ok(())
            }
            Err(error) => {
                self.handles.fetch_sub(1, Ordering::AcqRel);
                Err(error)
            }
        }
    }

    /// Registers one more handle (a closed handle that's used again rejoins its store).
    pub fn add_handle(&self) {
        self.handles.fetch_add(1, Ordering::AcqRel);
    }

    /// Unregisters a handle. When the last one closes, every pooled connection is closed (D-018).
    /// The store itself stays usable: a later operation reopens connections.
    pub fn release_handle(&self) {
        if self.handles.fetch_sub(1, Ordering::AcqRel) == 1 {
            let gate = GATE.enter_shared();
            self.pool.clear(&gate);
            drop(gate);
            self.retired.store(true, Ordering::Release);
            self.pin.lock().unwrap_or_else(PoisonError::into_inner).take();
        }
    }

    fn is_retired(&self) -> bool {
        self.retired.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn directory(&self) -> &Path {
        &self.directory
    }

    /// The persisted limits as last seen by this process (for attribute getters).
    #[must_use]
    pub fn cached_limits(&self) -> Limits {
        Limits {
            size_limit: self.size_limit.load(Ordering::Acquire),
            cull_limit: self.cull_limit.load(Ordering::Acquire),
        }
    }

    pub(crate) fn cache_limits(&self, limits: Limits) {
        self.size_limit.store(limits.size_limit, Ordering::Release);
        self.cull_limit.store(limits.cull_limit, Ordering::Release);
    }

    /// Runs a read. `read` may run several times (each attempt starts from scratch), so it must
    /// have no side effects outside the database.
    pub fn read<T>(&self, timeout: Duration, mut read: impl FnMut(&Connection) -> Result<T>) -> Result<T> {
        if let Some(result) = self.on_pinned(|conn| read(conn)) {
            return result;
        }
        // Fast path: one attempt with no busy wait, no deadline and no clock read. WAL readers almost
        // never meet a lock; if this one does (or the pool is full), fall through to the full loop.
        if GATE.own_depth() == 0
            && let Some(_permit) = self.slots.try_acquire(self.max_connections.load(Ordering::Acquire))
        {
            let gate = GATE.enter_shared();
            let lease = self.lease(&gate, timeout)?;
            match read(lease.conn()) {
                Err(Error::Timeout { .. }) => {}
                done => return done,
            }
        }
        lockwait::retry(timeout, |deadline| {
            let _permit = self.reserve_slot(deadline)?;
            let gate = GATE.enter_shared();
            let lease = self.lease(&gate, timeout)?;
            read(lease.conn())
        })
    }

    /// Runs `write` inside one `BEGIN IMMEDIATE` transaction and commits it. Like [`Store::read`],
    /// `write` may run several times and must have no side effects outside the transaction; work
    /// such as writing a value file happens before calling this.
    pub fn write<T>(
        &self,
        timeout: Duration,
        write: impl FnMut(&RowWriter<'_>, &Connection) -> Result<T>,
    ) -> Result<T> {
        self.write_as(timeout, false, write)
    }

    /// [`Store::write`] for a maintenance loop that just stepped aside for waiting writers between
    /// chunks: it takes the write lock with priority (see [`WriteLock::lock_until_with_priority`]).
    pub(crate) fn write_after_yield<T>(
        &self,
        timeout: Duration,
        write: impl FnMut(&RowWriter<'_>, &Connection) -> Result<T>,
    ) -> Result<T> {
        self.write_as(timeout, true, write)
    }

    fn write_as<T>(
        &self,
        timeout: Duration,
        priority: bool,
        mut write: impl FnMut(&RowWriter<'_>, &Connection) -> Result<T>,
    ) -> Result<T> {
        let start = Instant::now();
        let deadline = lockwait::deadline_after(start, timeout);
        // 0. In a child forked inside a transaction on this file, writes can never succeed (D-020):
        // fail before waiting on anything.
        crate::poison::check_writable(self.file_id)?;
        // A thread holding a write transaction writes inside it, on its pinned connection.
        if let Some(result) = self.on_pinned(|conn| write(&RowWriter::new(conn, timeout), conn)) {
            return result;
        }
        // 1. The write lock, before the gate. At depth > 0 never wait for it (see the module docs).
        let _token = if GATE.own_depth() == 0 {
            let token = if priority {
                self.write_lock.lock_until_with_priority(deadline)
            } else {
                self.write_lock.lock_until(deadline)
            };
            Some(token.ok_or(Error::Timeout { seconds: timeout.as_secs_f64() })?)
        } else {
            self.write_lock.try_lock()
        };
        // 2-3. Slot, gate and connection per attempt; SQLite's lock is then only contended by
        // other processes (and by a nested operation that couldn't take the write lock).
        lockwait::retry(deadline.saturating_duration_since(Instant::now()), |attempt_deadline| {
            let _permit = self.reserve_slot(attempt_deadline)?;
            let gate = GATE.enter_shared();
            let lease = self.lease(&gate, timeout)?;
            let tx = WriteTx::begin(lease.conn(), timeout)?;
            let value = write(&RowWriter::new(lease.conn(), timeout), lease.conn())?;
            tx.commit()?;
            Ok(value)
        })
        .map_err(|error| match error {
            Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
            other => other,
        })
    }

    /// Fails with [`Error::Unwritable`] in a child forked inside a transaction on this file (D-020).
    pub fn check_writable(&self) -> Result<()> {
        crate::poison::check_writable(self.file_id)
    }

    /// Runs `write` in a write transaction only if that needs no waiting at all: the write lock is
    /// free, and SQLite's lock is free too (no busy wait). Returns `None` if it didn't run, or ran
    /// into a lock. For opportunistic cleanup that must never block a read (D-013).
    pub fn write_nowait<T>(&self, write: impl FnOnce(&RowWriter<'_>) -> Result<T>) -> Option<Result<T>> {
        if crate::poison::check_writable(self.file_id).is_err() || GATE.own_depth() > 0 {
            return None;
        }
        let _token = self.write_lock.try_lock()?;
        let _permit = self.slots.try_acquire(self.max_connections.load(Ordering::Acquire))?;
        let gate = GATE.enter_shared();
        let lease = self.lease(&gate, Duration::ZERO).ok()?;
        // No AttachedWindow is open, so the busy handler gives up at once.
        let tx = Transaction::new_unchecked(lease.conn(), TransactionBehavior::Immediate).ok()?;
        let result = write(&RowWriter::new(&tx, Duration::ZERO));
        match result {
            Ok(value) => Some(tx.commit().map(|()| value).map_err(|e| Error::from_sqlite(e, Duration::ZERO))),
            Err(Error::Timeout { .. }) => None,
            Err(error) => Some(Err(error)),
        }
    }

    /// Runs `f` on the calling thread's held transaction, if it has one on this store. A hold
    /// inherited across `fork()` is poisoned: it reports [`Error::ForkPoisoned`] and never runs `f`.
    fn on_pinned<T>(&self, f: impl FnOnce(&Connection) -> Result<T>) -> Option<Result<T>> {
        if !self.pinned_active.load(Ordering::Relaxed) {
            return None;
        }
        let gate = GATE.enter_shared();
        let pinned = self.pinned.lock().unwrap_or_else(PoisonError::into_inner);
        let held = pinned.as_ref().filter(|p| p.owner == std::thread::current().id())?;
        if held.generation != gate::generation() {
            return Some(Err(Error::ForkPoisoned));
        }
        let result = f(held.conn.conn());
        drop(pinned);
        drop(gate);
        Some(result)
    }

    /// Starts a write transaction that stays open across calls until [`HeldTransaction::finish`]
    /// (or drop, which rolls back). The calling thread's reads and writes on this store run inside
    /// it. It holds the write lock, a gate level and a pooled connection, and registers the file
    /// for D-020 poisoning: a child forked meanwhile can never write it.
    pub fn begin_hold(self: &Arc<Self>, timeout: Duration) -> Result<HeldTransaction> {
        crate::poison::check_writable(self.file_id)?;
        {
            let gate = GATE.enter_shared();
            let pinned = self.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            if pinned
                .as_ref()
                .is_some_and(|p| p.owner == std::thread::current().id() && p.generation == gate::generation())
            {
                return Err(Error::NestedTransaction);
            }
            drop(pinned);
            drop(gate);
        }
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let token = self.write_lock.lock_until(deadline).ok_or(Error::Timeout { seconds: timeout.as_secs_f64() })?;
        let (conn, hold, share) = lockwait::retry(deadline.saturating_duration_since(Instant::now()), |_| {
            let gate = GATE.enter_shared();
            let lease = self.lease(&gate, timeout)?;
            lease.conn().execute_batch("BEGIN IMMEDIATE").map_err(|e| Error::from_sqlite(e, timeout))?;
            let conn = lease.into_inner();
            let hold = crate::poison::begin_hold(&gate, self.file_id);
            Ok((conn, hold, gate.into_held()))
        })
        .map_err(|error| match error {
            Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
            other => other,
        })?;
        {
            let gate = GATE.enter_shared();
            let mut pinned = self.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            *pinned = Some(Pinned { owner: std::thread::current().id(), generation: gate::generation(), conn });
            self.pinned_active.store(true, Ordering::Release);
            drop(pinned);
            drop(gate);
        }
        Ok(HeldTransaction {
            store: Arc::clone(self),
            write_word: Some(token.into_raw()),
            hold: Some(hold),
            share: Some(share),
            timeout,
        })
    }

    /// Threads waiting for the write lock. Maintenance yields between chunks while this is non-zero.
    #[must_use]
    pub fn write_waiters(&self) -> u64 {
        self.write_lock.waiters()
    }

    fn reserve_slot(&self, deadline: Instant) -> Result<Option<crate::genlock::SlotPermit<'_>>> {
        if GATE.own_depth() > 0 {
            return Ok(None);
        }
        let cap = self.max_connections.load(Ordering::Acquire);
        self.slots.acquire_until(cap, deadline).map(Some).ok_or(Error::Timeout { seconds: 0.0 })
    }

    fn lease<'s, 'g>(&'s self, gate: &'g SharedGuard<'g>, timeout: Duration) -> Result<Lease<'s, 'g>> {
        let mut pooled = self.pool.checkout(gate, || open::connect(&self.directory, timeout))?;
        // New connections, and older ones after a handle asked for more, get the store's size.
        let pages = self.cache_pages.load(Ordering::Relaxed);
        if pooled.cache_pages() != pages {
            let set = pooled.conn().execute_batch(&format!("PRAGMA cache_size = {pages}"));
            if let Err(error) = set {
                self.pool.checkin(gate, pooled);
                return Err(Error::from_sqlite(error, timeout));
            }
            pooled.set_cache_pages(pages);
        }
        Ok(Lease { store: self, gate, pooled: Some(pooled) })
    }
}

/// `BEGIN IMMEDIATE` … `COMMIT` with cached statements (rusqlite's `Transaction` re-parses both on
/// every write). Rolls back if dropped without committing.
struct WriteTx<'c> {
    conn: &'c Connection,
    timeout: Duration,
    open: bool,
}

impl<'c> WriteTx<'c> {
    fn begin(conn: &'c Connection, timeout: Duration) -> Result<Self> {
        run_cached(conn, "BEGIN IMMEDIATE", timeout)?;
        Ok(Self { conn, timeout, open: true })
    }

    fn commit(mut self) -> Result<()> {
        self.open = false;
        let result = run_cached(self.conn, "COMMIT", self.timeout);
        if result.is_err() {
            // A failed COMMIT (e.g. busy) leaves the transaction open: end it before the retry.
            let _ = run_cached(self.conn, "ROLLBACK", self.timeout);
        }
        result
    }
}

impl Drop for WriteTx<'_> {
    fn drop(&mut self) {
        if self.open {
            let _ = run_cached(self.conn, "ROLLBACK", self.timeout);
        }
    }
}

fn run_cached(conn: &Connection, sql: &str, timeout: Duration) -> Result<()> {
    conn.prepare_cached(sql)
        .and_then(|mut stmt| stmt.execute([]))
        .map(|_| ())
        .map_err(|e| Error::from_sqlite(e, timeout))
}

/// A pooled connection that checks itself back in when dropped, under the gate level it was taken
/// with. With a connection cap, a missed check-in would lose capacity for good.
struct Lease<'s, 'g> {
    store: &'s Store,
    gate: &'g SharedGuard<'g>,
    pooled: Option<PooledConn>,
}

impl Lease<'_, '_> {
    /// Takes the connection out, so it outlives the lease (a held transaction keeps it).
    fn into_inner(mut self) -> PooledConn {
        self.pooled.take().unwrap_or_else(|| unreachable!("lease used after drop"))
    }

    fn conn(&self) -> &Connection {
        // Always Some until drop.
        self.pooled.as_ref().map_or_else(|| unreachable!("lease used after drop"), PooledConn::conn)
    }
}

impl Drop for Lease<'_, '_> {
    fn drop(&mut self) {
        if let Some(pooled) = self.pooled.take() {
            self.store.pool.checkin(self.gate, pooled);
        }
    }
}

/// A write transaction held across calls. See [`Store::begin_hold`]. `Send`: it may be finished or
/// dropped on another thread (the GC may drop the Python object holding it anywhere).
#[derive(Debug)]
#[must_use = "dropping a held transaction rolls it back"]
pub struct HeldTransaction {
    store: Arc<Store>,
    write_word: Option<u64>,
    hold: Option<HoldGuard<'static>>,
    share: Option<HeldShare<'static>>,
    timeout: Duration,
}

impl HeldTransaction {
    /// Commits (`commit = true`) or rolls back, and releases everything. In a child forked while
    /// the transaction was held it does neither and reports [`Error::ForkPoisoned`]: the parent
    /// still owns the transaction, and the child forgets its copy of the connection.
    pub fn finish(mut self, commit: bool) -> Result<()> {
        self.end(commit)
    }

    fn end(&mut self, commit: bool) -> Result<()> {
        let Some(write_word) = self.write_word.take() else { return Ok(()) };
        let gate = GATE.enter_shared();
        let pinned = {
            let mut slot = self.store.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            self.store.pinned_active.store(false, Ordering::Release);
            slot.take()
        };
        let result = match pinned {
            Some(p) if p.generation == gate::generation() => {
                let sql = if commit { "COMMIT" } else { "ROLLBACK" };
                let outcome = p.conn.conn().execute_batch(sql).map_err(|e| Error::from_sqlite(e, self.timeout));
                if outcome.is_err() {
                    // Leave no half-open transaction on a pooled connection.
                    let _ = p.conn.conn().execute_batch("ROLLBACK");
                }
                self.store.pool.checkin(&gate, p.conn);
                outcome
            }
            Some(inherited) => {
                inherited.conn.forget();
                Err(Error::ForkPoisoned)
            }
            None => Ok(()),
        };
        drop(gate);
        self.store.write_lock.release_raw(write_word);
        self.hold.take();
        self.share.take();
        result
    }
}

impl Drop for HeldTransaction {
    fn drop(&mut self) {
        let _ = self.end(false);
    }
}

fn file_id(file: &File, path: &Path) -> Result<FileId> {
    let metadata = file.metadata().map_err(|source| Error::Io { path: path.to_path_buf(), source })?;
    Ok((metadata.dev(), metadata.ino()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rows::{NewRow, Stored};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);

    fn options(dir: &Path) -> OpenOptions {
        OpenOptions {
            directory: dir.to_path_buf(),
            timeout: T,
            requested: Requested::default(),
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
        }
    }

    fn put(store: &Store, key: &str, value: &[u8]) {
        store
            .write(T, |w, _| {
                w.upsert(&NewRow {
                    key,
                    mode: 2,
                    value: Stored::Blob(value),
                    size: i64::try_from(value.len()).unwrap(),
                    store_time: 1.0,
                    expire_time: None,
                })
            })
            .unwrap();
    }

    fn count(store: &Store) -> i64 {
        store
            .read(T, |c| {
                c.query_row("SELECT count FROM totals", [], |r| r.get(0)).map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap()
    }

    #[test]
    fn handles_on_one_directory_share_a_store() {
        let tmp = TempDir::new("store-share");
        let a = Store::open(&options(tmp.path())).unwrap();
        let b = Store::open(&options(tmp.path())).unwrap();
        assert!(Arc::ptr_eq(&a, &b));
        put(&a, "k", b"v");
        assert_eq!(count(&b), 1);
    }

    #[test]
    fn a_second_handle_applies_explicit_settings_only() {
        let tmp = TempDir::new("store-settings");
        let a = Store::open(&OpenOptions {
            requested: Requested { size_limit: Some(5 << 20), cull_limit: None },
            ..options(tmp.path())
        })
        .unwrap();
        assert_eq!(a.cached_limits().size_limit, 5 << 20);
        let _b = Store::open(&options(tmp.path())).unwrap();
        assert_eq!(a.cached_limits().size_limit, 5 << 20, "an unspecified setting keeps the stored value");
        let _c = Store::open(&OpenOptions {
            requested: Requested { size_limit: Some(7 << 20), cull_limit: None },
            ..options(tmp.path())
        })
        .unwrap();
        assert_eq!(a.cached_limits().size_limit, 7 << 20);
    }

    #[test]
    fn many_threads_writing_through_one_store_lose_nothing() {
        let tmp = TempDir::new("store-threads");
        let store = Store::open(&options(tmp.path())).unwrap();
        std::thread::scope(|s| {
            for t in 0..8 {
                let store = &store;
                s.spawn(move || {
                    for i in 0..100 {
                        put(store, &format!("t{t}-{i}"), b"x");
                    }
                });
            }
        });
        assert_eq!(count(&store), 800);
    }

    #[test]
    fn the_connection_cap_holds_under_load() {
        let tmp = TempDir::new("store-cap");
        let store = Store::open(&OpenOptions { max_connections: 2, ..options(tmp.path()) }).unwrap();
        let peak = AtomicUsize::new(0);
        let live = AtomicUsize::new(0);
        std::thread::scope(|s| {
            for _ in 0..8 {
                s.spawn(|| {
                    for _ in 0..50 {
                        store
                            .read(T, |c| {
                                let now = live.fetch_add(1, Ordering::AcqRel) + 1;
                                peak.fetch_max(now, Ordering::AcqRel);
                                let n: i64 = c
                                    .query_row("SELECT count FROM totals", [], |r| r.get(0))
                                    .map_err(|e| Error::from_sqlite(e, T))?;
                                live.fetch_sub(1, Ordering::AcqRel);
                                Ok(n)
                            })
                            .unwrap();
                    }
                });
            }
        });
        assert!(peak.load(Ordering::Acquire) <= 2, "peak {} over the cap", peak.load(Ordering::Acquire));
    }

    #[test]
    fn closing_the_last_handle_drains_the_pool() {
        let tmp = TempDir::new("store-close");
        let a = Store::open(&options(tmp.path())).unwrap();
        let b = Store::open(&options(tmp.path())).unwrap();
        put(&a, "k", b"v");
        a.release_handle();
        let gate = GATE.enter_shared();
        assert!(b.pool.idle(&gate) > 0, "one handle is still open");
        drop(gate);
        b.release_handle();
        let gate = GATE.enter_shared();
        assert_eq!(b.pool.idle(&gate), 0);
        drop(gate);
        // Still usable afterwards: a later operation reopens a connection.
        assert_eq!(count(&b), 1);
    }

    #[test]
    fn a_recreated_directory_gets_a_new_store() {
        let tmp = TempDir::new("store-recreate");
        let dir = tmp.path().join("cache");
        let first = Store::open(&options(&dir)).unwrap();
        put(&first, "k", b"v");
        let first_id = first.file_id;
        drop(first);
        std::fs::remove_dir_all(&dir).unwrap();
        let second = Store::open(&options(&dir)).unwrap();
        assert_ne!(second.file_id, first_id);
        assert_eq!(count(&second), 0);
    }

    #[test]
    fn a_held_transaction_runs_the_holders_ops_and_blocks_other_writers_until_commit() {
        let tmp = TempDir::new("store-hold");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "inside", b"1"); // The holder's own write runs inside the transaction.
        assert_eq!(count(&store), 1, "the holder sees its own uncommitted write");
        let other_done = std::sync::atomic::AtomicBool::new(false);
        std::thread::scope(|s| {
            s.spawn(|| {
                put(&store, "outside", b"2");
                other_done.store(true, Ordering::Release);
            });
            std::thread::sleep(Duration::from_millis(100));
            assert!(!other_done.load(Ordering::Acquire), "another thread's write must wait for the hold");
            held.finish(true).unwrap();
        });
        assert!(other_done.load(Ordering::Acquire));
        assert_eq!(count(&store), 2);
    }

    #[test]
    fn dropping_a_held_transaction_rolls_back() {
        let tmp = TempDir::new("store-hold-drop");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "gone", b"1");
        drop(held);
        assert_eq!(count(&store), 0);
        put(&store, "after", b"1"); // The write lock and connection were released.
        assert_eq!(count(&store), 1);
    }

    #[test]
    fn holding_twice_on_one_thread_is_refused() {
        let tmp = TempDir::new("store-hold-nested");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        assert!(matches!(store.begin_hold(T), Err(Error::NestedTransaction)));
        held.finish(true).unwrap();
    }

    #[test]
    fn a_held_transaction_can_finish_on_another_thread() {
        let tmp = TempDir::new("store-hold-send");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "k", b"v");
        std::thread::scope(|s| {
            s.spawn(move || held.finish(true).unwrap());
        });
        put(&store, "k2", b"v"); // Not stuck behind a leaked write lock or gate level.
        assert_eq!(count(&store), 2);
    }

    #[test]
    fn connections_get_the_largest_cache_size_any_handle_asked_for() {
        let tmp = TempDir::new("store-cache-size");
        let pages = |store: &Store| {
            store
                .read(T, |c| {
                    c.query_row("PRAGMA cache_size", [], |r| r.get::<_, i64>(0)).map_err(|e| Error::from_sqlite(e, T))
                })
                .unwrap()
        };
        let a = Store::open(&OpenOptions { cache_pages: 100, ..options(tmp.path()) }).unwrap();
        assert_eq!(pages(&a), 100, "the open sequence's own connection gets the size too");
        let _smaller = Store::open(&OpenOptions { cache_pages: 50, ..options(tmp.path()) }).unwrap();
        assert_eq!(pages(&a), 100, "a smaller request never shrinks the store's budget");
        let _larger = Store::open(&OpenOptions { cache_pages: 300, ..options(tmp.path()) }).unwrap();
        assert_eq!(pages(&a), 300, "an idle connection is brought up to a larger request");
        assert!(matches!(
            Store::open(&OpenOptions { cache_pages: 0, ..options(&tmp.path().join("zero")) }),
            Err(Error::InvalidSetting(_))
        ));
    }

    #[test]
    fn a_write_inside_the_gate_never_waits_for_the_write_lock() {
        let tmp = TempDir::new("store-depth");
        let store = Store::open(&options(tmp.path())).unwrap();
        let quick = Duration::from_millis(50);
        let write = |key: &str, timeout: Duration| {
            store.write(timeout, |writer, _| {
                writer.upsert(&NewRow {
                    key,
                    mode: 2,
                    value: Stored::Blob(b"v"),
                    size: 1,
                    store_time: 1.0,
                    expire_time: None,
                })
            })
        };
        // Another holder of the in-process write lock (standing in for a thread that took it and is
        // about to enter the gate). At depth 0 a write queues behind it and times out.
        let token = store.write_lock.try_lock().unwrap();
        assert!(matches!(write("outside", quick), Err(Error::Timeout { .. })));
        // At depth > 0 (a nested operation) it must not wait for the lock: that holder may be
        // blocked entering the gate behind a pending fork that is waiting for this very level.
        let gate = GATE.enter_shared();
        let start = Instant::now();
        write("nested", T).unwrap();
        assert!(start.elapsed() < Duration::from_secs(1), "a nested write waited for the write lock");
        drop(gate);
        drop(token);
        assert_eq!(count(&store), 1);
    }

    #[test]
    fn a_write_inside_the_gate_never_waits_for_a_starving_writer() {
        // A starving waiter makes try_lock refuse even when the lock is free. A nested write must
        // still go through promptly (without the token), never wait.
        let tmp = TempDir::new("store-depth-starving");
        let store = Store::open(&options(tmp.path())).unwrap();
        crate::genlock::tests_support::mark_starving(&store.write_lock);
        let gate = GATE.enter_shared();
        let start = Instant::now();
        put(&store, "nested", b"v");
        assert!(start.elapsed() < Duration::from_secs(1), "a nested write waited behind a starving writer");
        drop(gate);
        assert_eq!(count(&store), 1);
    }

    #[test]
    fn bad_settings_are_rejected_before_touching_disk() {
        let tmp = TempDir::new("store-bad");
        let dir = tmp.path().join("never");
        assert!(matches!(
            Store::open(&OpenOptions { max_connections: 0, ..options(&dir) }),
            Err(Error::InvalidSetting(_))
        ));
        assert!(!dir.exists());
    }
}
