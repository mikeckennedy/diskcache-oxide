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
//! with the interpreter released. The gate wait itself is bounded by the same deadline, so a stuck
//! fork costs an operation its `timeout`, not a hang. At gate depth > 0 (a nested operation inside a held
//! transaction), both are `try_*` only: the write lock falls back to SQLite's busy path, and the
//! pool opens a connection over the cap. That busy path's backoff keeps the caller's gate levels, so
//! a pending fork waits for it, up to the write's `timeout` (see the gate's rules).
//!
//! # Registry
//!
//! Stores are found by the database file's `(st_dev, st_ino)`. Each `Store` keeps one fd open on
//! its database file while it has handles (through an idle SQLite connection, see `open_pin`), so
//! that inode can't be reused by a file recreated in the same process. When its last handle closes
//! it is *retired*: it releases that fd, and is only handed out again (revived) to a handle on the
//! same directory and the same file. Handles are registered and the last release is decided under
//! the registry mutex, so an open never joins a store that's being retired, and a file never has
//! two live stores. The registry holds `Weak` references; dead entries are pruned when a store is
//! inserted, never in `Drop`. The registry mutex is only taken under the gate, so a child never
//! inherits it locked. Across `fork()`, the `Store` is reset in place: its pool, write lock and
//! slot counter all recognize the new [process generation](crate::gate::generation).

use std::cell::{Cell, RefCell};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicIsize, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, TryLockError, Weak};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

use rusqlite::{Connection, OpenFlags, Transaction, TransactionBehavior};

use crate::gate::{self, GATE, HeldShare, SharedGuard};
use crate::genlock::{Slots, WriteLock};
use crate::open::{self, Limits, Requested};
use crate::poison::HoldGuard;
use crate::pool::{Pool, PooledConn};
use crate::rows::{Removed, RowWriter};
use crate::{Error, Result, lockwait};

/// Default `max_connections` (04-design.md section 2).
pub const DEFAULT_MAX_CONNECTIONS: u64 = 8;

/// Default page-cache size of each connection, in pages (`PRAGMA cache_size`). With
/// [`DEFAULT_MAX_CONNECTIONS`] this bounds a store's page caches at `8 × pages × page_size`.
pub const DEFAULT_CACHE_PAGES: u64 = 256;

/// Default `wal_size_limit` in bytes (see [`crate::wal`]).
pub const DEFAULT_WAL_SIZE_LIMIT: u64 = crate::wal::DEFAULT_SIZE_LIMIT;

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

/// The stores with a release deferred behind a fork, one entry per release, so a store stays in the
/// registry for its drain after its last other `Arc` has gone. Taken with no gate level, since a
/// deferrer has none, so a fork can land while it's locked and leave a child with it locked for good.
/// So it's only ever taken through [`deferred_owners`], which tells that apart from a lock that's
/// just busy.
static DEFERRED_OWNERS: Mutex<Vec<Arc<Store>>> = Mutex::new(Vec::new());

/// Who holds [`DEFERRED_OWNERS`]: 0 when nobody does, else the process id of the process whose
/// thread took it, or [`OWNERS_LOST`]. It's the real lock: the mutex is only taken while holding it,
/// and let go before it is, so the mutex is never locked while this is 0 or our own id with nobody
/// in this process inside. A child inherits the parent's id here, which is how it knows the holder
/// didn't survive the fork.
static DEFERRED_OWNERS_HOLDER: AtomicU32 = AtomicU32::new(0);

/// [`DEFERRED_OWNERS_HOLDER`] in a child that inherited it held: by a thread that's gone, for good.
/// Set by [`forget_inherited_owners`], so a later process that reuses the parent's id can't mistake
/// the dead holder for one of its own threads.
const OWNERS_LOST: u32 = u32::MAX;

/// How many times [`deferred_owners`] yields to a holder in this process before it sleeps between
/// tries. Anyone in there is only pushing or taking.
const DEFERRED_OWNERS_SPINS: u32 = 64;

/// How long [`deferred_owners`] waits for a holder in this process before giving up. Only a holder
/// descheduled for this long, or a process id reused by a child forked without our hooks, gets here.
const DEFERRED_OWNERS_PATIENCE: Duration = Duration::from_secs(10);

/// [`DEFERRED_OWNERS`] taken, which lets go of the mutex and then of [`DEFERRED_OWNERS_HOLDER`].
struct DeferredOwners(Option<MutexGuard<'static, Vec<Arc<Store>>>>);

impl std::ops::Deref for DeferredOwners {
    type Target = Vec<Arc<Store>>;
    fn deref(&self) -> &Self::Target {
        self.0.as_deref().expect("held until dropped")
    }
}

impl std::ops::DerefMut for DeferredOwners {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.0.as_deref_mut().expect("held until dropped")
    }
}

impl Drop for DeferredOwners {
    fn drop(&mut self) {
        drop(self.0.take());
        DEFERRED_OWNERS_HOLDER.store(0, Ordering::Release);
    }
}

/// [`DEFERRED_OWNERS`], waiting for a holder in this process, which only pushes or takes. `None` if
/// a fork left it held by a thread that's gone (this is a child), or, as a last resort, after
/// [`DEFERRED_OWNERS_PATIENCE`]. Nothing here waits on a holder that can't come back.
fn deferred_owners() -> Option<DeferredOwners> {
    let me = std::process::id();
    let started = Instant::now();
    let mut spins = 0;
    loop {
        match DEFERRED_OWNERS_HOLDER.compare_exchange(0, me, Ordering::AcqRel, Ordering::Acquire) {
            Ok(_) => break,
            Err(holder) if holder == me && started.elapsed() < DEFERRED_OWNERS_PATIENCE => {
                if spins < DEFERRED_OWNERS_SPINS {
                    spins += 1;
                    std::thread::yield_now();
                } else {
                    std::thread::sleep(Duration::from_micros(100));
                }
            }
            // Another process's id (a child whose hook hasn't run yet), lost, or out of patience.
            Err(_) => return None,
        }
    }
    // Holding the holder now, so this lets go of it on every path out.
    let mut owners = DeferredOwners(None);
    owners.0 = Some(match DEFERRED_OWNERS.try_lock() {
        Ok(guard) => guard,
        Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
        // Can't happen while everything takes it through here.
        Err(TryLockError::WouldBlock) => return None,
    });
    Some(owners)
}

/// The child side of the fork hooks: a holder of [`DEFERRED_OWNERS`] that isn't this process is a
/// thread the fork left behind, so the mutex stays locked for good here. Mark it lost.
pub(crate) fn forget_inherited_owners() {
    let me = std::process::id();
    let _ = DEFERRED_OWNERS_HOLDER.try_update(Ordering::AcqRel, Ordering::Acquire, |holder| {
        (holder != 0 && holder != me && holder != OWNERS_LOST).then_some(OWNERS_LOST)
    });
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
    /// The WAL file's size limit in bytes (`wal_size_limit`, see [`crate::wal`]).
    pub wal_size_limit: u64,
    /// Run forced checkpoints after writes once the WAL is over the limit (`wal_checkpoint="auto"`).
    /// Off by default: checkpoints run on [`Store::checkpoint`] and, without waiting, on close.
    pub auto_checkpoint: bool,
}

/// One database file's shared state. See the module docs.
#[derive(Debug)]
pub struct Store {
    directory: PathBuf,
    /// `directory` resolved (symlinks, `..`, relative paths), for matching a retired store to a
    /// later open of the same directory however it's spelled. Falls back to `directory`.
    canonical: PathBuf,
    file_id: FileId,
    /// Held open so the inode stays reserved while any handle is open (see [`open_pin`]). Released
    /// when the last handle closes (D-018: nothing left open); the store is then `retired`, and the
    /// registry only revives it for the same directory, since the inode number may since have been
    /// reused.
    pin: Mutex<Option<FilePin>>,
    retired: AtomicBool,
    pool: Pool,
    write_lock: WriteLock,
    slots: Slots,
    /// The largest `max_connections` any handle on this store asked for.
    max_connections: AtomicU64,
    /// The largest `cache_pages` any handle on this store asked for. Each leased connection is
    /// brought up to it.
    cache_pages: AtomicU64,
    /// The largest `wal_size_limit` any handle on this store asked for (see [`crate::wal`]).
    wal_size_limit: AtomicU64,
    wal: crate::wal::WalState,
    /// Whether any handle asked for `wal_checkpoint="auto"`.
    auto_checkpoint: AtomicBool,
    /// The `-wal` file, computed once: the WAL check runs after every write.
    wal_path: PathBuf,
    /// Open handles. The pool drains when the last one closes. Signed: see [`Store::count_handle`].
    handles: AtomicIsize,
    /// Handles closed behind a stuck fork, still counted in `handles`, for the drain to release (see
    /// [`Store::release_handle`]).
    deferred_releases: AtomicUsize,
    /// Cached copies of the persisted limits, for the attribute getters only. Write paths read the
    /// current values inside their transaction.
    size_limit: AtomicI64,
    cull_limit: AtomicI64,
    /// A write transaction held across calls (`_hold_write_transaction()` now, `transact()` in
    /// Phase 6b): its owner thread's operations run on the pinned connection. Locked only under
    /// the gate, and only for a moment: an operation borrows the connection (see
    /// [`Store::on_pinned`]) rather than keeping the mutex. `pinned_active` keeps the common
    /// no-hold path to one relaxed load.
    pinned: Mutex<PinnedSlot>,
    pinned_active: AtomicBool,
}

/// The connection of a held write transaction, already inside `BEGIN IMMEDIATE`.
#[derive(Debug)]
struct Pinned {
    owner: ThreadId,
    generation: u64,
    conn: PooledConn,
    /// Value files its writes replaced or created, settled when it ends (see [`Store::write_files`]).
    files: RefCell<HeldFiles>,
    /// It can't continue (see [`Error::TransactionAborted`]): later writes fail at once, and ending
    /// it rolls back.
    doomed: Cell<bool>,
}

/// A held transaction's value files, relative to the cache directory. The ones it replaced or
/// deleted are unlinked once it commits; the ones it created, once it rolls back.
#[derive(Debug, Default)]
struct HeldFiles {
    removed: Vec<String>,
    created: Vec<String>,
}

impl HeldFiles {
    /// How much is recorded, for [`HeldFiles::rewind`].
    fn mark(&self) -> (usize, usize) {
        (self.removed.len(), self.created.len())
    }

    /// Forgets what was recorded since `mark`, after a `ROLLBACK TO` undid those writes (nested
    /// operations' writes inside a failed one): their replaced rows are back, so those files stay,
    /// and returns the files they created, which no row refers to any more.
    fn rewind(&mut self, (removed, created): (usize, usize)) -> Vec<String> {
        self.removed.truncate(removed);
        self.created.split_off(created)
    }
}

/// What a write returns through [`Store::write_files`]: its result, and the value files (relative
/// to the cache directory) it replaced or deleted, and pointed a row at.
#[derive(Debug)]
pub(crate) struct Changed<T> {
    value: T,
    removed: Vec<String>,
    created: Option<String>,
}

impl<T> Changed<T> {
    /// A write that touched no value file.
    pub fn unchanged(value: T) -> Self {
        Self { value, removed: Vec::new(), created: None }
    }

    /// A write that replaced or deleted the rows in `removed`.
    pub fn removing(value: T, removed: impl IntoIterator<Item = Removed>) -> Self {
        Self { value, removed: removed.into_iter().filter_map(|r| r.filename).collect(), created: None }
    }

    /// And pointed a row at the value file `created`.
    pub fn creating(self, created: Option<String>) -> Self {
        Self { created, ..self }
    }
}

/// How a write takes the write lock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LockPriority {
    Normal,
    /// A maintenance loop that just stepped aside for waiting writers between chunks takes it back
    /// with priority (see [`WriteLock::lock_until_with_priority`]).
    AfterYield,
}

/// Where a held transaction's connection is.
#[derive(Debug)]
enum PinnedSlot {
    Empty,
    /// In the store, between operations.
    Here(Pinned),
    /// Lent to an operation running on its owner thread ([`Store::on_pinned`]). `abandoned` is set
    /// when the transaction was dropped inside one of its own operations (a GC finalizer, say),
    /// where it can't get the connection back: the operation rolls it back when it returns it.
    Lent {
        owner: ThreadId,
        generation: u64,
        abandoned: Option<Abandoned>,
    },
}

impl PinnedSlot {
    /// Whether `thread` holds a transaction here that this process can use.
    fn held_by(&self, thread: ThreadId) -> bool {
        match self {
            Self::Empty => false,
            Self::Here(p) => p.owner == thread && p.generation == gate::generation(),
            Self::Lent { owner, generation, .. } => *owner == thread && *generation == gate::generation(),
        }
    }
}

/// What a held transaction dropped inside one of its own operations still holds, released once
/// that operation has rolled it back.
#[derive(Debug)]
struct Abandoned {
    write_word: u64,
    hold: Option<HoldGuard<'static>>,
    share: Option<HeldShare<'static>>,
}

/// How many value files [`Store::unlink`] deletes before it releases the interpreter to do it.
const UNLINK_DETACH_AT: usize = 8;

/// How long releasing a handle (`close()`, `del`) waits behind a pending fork before it defers. A
/// flat budget of its own: `min(timeout, 1 s)` would defer on ordinary forks with small timeouts, and
/// sharing the checkpoint's budget could leave the release nothing. An ordinary fork ends well inside
/// it, so `close()` waits it out and D-018 holds when it returns.
const RELEASE_BUDGET: Duration = Duration::from_secs(1);

/// Enters the gate until `deadline`. The timeout's number is filled in by whoever knows the budget
/// (`lockwait::retry`, `write_unpinned`'s mapping, `Store::open`, `Cache.pop`).
fn enter_until(deadline: Instant) -> Result<SharedGuard<'static>> {
    GATE.enter_shared_until(deadline).ok_or(Error::Timeout { seconds: 0.0 })
}

/// How often ending a held transaction re-checks for its connection while an operation on its
/// owner thread has it.
const LENT_POLL: Duration = Duration::from_micros(50);

thread_local! {
    /// Held transactions' connections lent to operations running on this thread, by store
    /// address. An operation nested inside one (a pickle hook that calls the cache) uses the same
    /// connection, so it runs inside the transaction and never waits for it.
    static LENT: RefCell<Vec<(usize, Rc<Pinned>)>> = const { RefCell::new(Vec::new()) };
}

/// A held transaction's connection lent to an operation on its owner thread. Dropping it hands the
/// connection back to the store, or rolls the transaction back if it was abandoned meanwhile.
struct Lend<'s, 'g> {
    store: &'s Store,
    gate: &'g SharedGuard<'g>,
    pinned: Option<Rc<Pinned>>,
}

impl<'s, 'g> Lend<'s, 'g> {
    fn new(store: &'s Store, gate: &'g SharedGuard<'g>, pinned: Pinned) -> Self {
        let pinned = Rc::new(pinned);
        LENT.with(|lent| lent.borrow_mut().push((store.key(), Rc::clone(&pinned))));
        Self { store, gate, pinned: Some(pinned) }
    }

    fn pinned(&self) -> &Pinned {
        // Always Some until drop.
        self.pinned.as_deref().unwrap_or_else(|| unreachable!("lend used after drop"))
    }
}

impl Drop for Lend<'_, '_> {
    fn drop(&mut self) {
        let key = self.store.key();
        LENT.with(|lent| {
            let mut lent = lent.borrow_mut();
            if let Some(at) = lent.iter().rposition(|(k, _)| *k == key) {
                lent.remove(at);
            }
        });
        // Nested operations' clones are dropped before they return, so this is the last one.
        let Some(Ok(pinned)) = self.pinned.take().map(Rc::try_unwrap) else {
            unreachable!("a nested operation kept a held transaction's connection");
        };
        let mut slot = self.store.pinned.lock().unwrap_or_else(PoisonError::into_inner);
        match std::mem::replace(&mut *slot, PinnedSlot::Empty) {
            PinnedSlot::Lent { abandoned: Some(abandoned), .. } => {
                self.store.pinned_active.store(false, Ordering::Release);
                drop(slot);
                let (_, garbage) = close_pinned(self.store, self.gate, pinned, false, Duration::ZERO);
                // In `HeldTransaction::end`'s order: the write lock, the D-020 hold, then the gate level.
                let Abandoned { write_word, hold, share } = abandoned;
                self.store.write_lock.release_raw(write_word);
                drop(hold);
                drop(share);
                // This operation's own gate level is still held, but not the write lock.
                self.store.unlink(garbage);
            }
            _ => *slot = PinnedSlot::Here(pinned),
        }
    }
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
        if options.wal_size_limit < crate::wal::MIN_SIZE_LIMIT {
            return Err(Error::InvalidSetting(format!(
                "wal_size_limit must be at least {} bytes",
                crate::wal::MIN_SIZE_LIMIT
            )));
        }
        options.requested.validate()?;
        open::ensure_directory(&options.directory)?;
        // One deadline for the lookup, the open sequence and a joining handle's write.
        let deadline = lockwait::deadline_after(Instant::now(), options.timeout);
        Self::open_until(options, deadline).map_err(|error| match error {
            Error::Timeout { .. } => Error::Timeout { seconds: options.timeout.as_secs_f64() },
            other => other,
        })
    }

    fn open_until(options: &OpenOptions, deadline: Instant) -> Result<Arc<Self>> {
        let db_path = open::db_path(&options.directory);
        let left = || deadline.saturating_duration_since(Instant::now());

        // Fast path: another handle in this process already has the store.
        if let Some(store) = Self::lookup(&db_path, deadline)? {
            store.join(options, left())?;
            return Ok(store);
        }

        // Before the retry, so the gate level below isn't kept across it.
        let canonical = std::fs::canonicalize(&options.directory).unwrap_or_else(|_| options.directory.clone());

        // Open outside the registry lock (the open sequence may wait on other processes), then
        // insert if absent; if another thread won the race, use its store and drop this connection.
        // The pin is opened in the same gate level, so a failure closes `conn` under the gate too.
        // That level also covers the insert below: it holds `conn` and the pin, which must close
        // under the gate, so it mustn't have to wait to get back in.
        let (gate, conn, limits, pin, file_id) = lockwait::retry(left(), |attempt_deadline| {
            let gate = enter_until(attempt_deadline)?;
            let mut conn = open::connect(&options.directory, options.timeout)?;
            let limits = open::prepare(&mut conn, options.requested, options.timeout)?;
            let (pin, file_id) = open_pin(&gate, &db_path)?;
            Ok((gate, conn, limits, pin, file_id))
        })?;

        let mut stores = registry(&gate);
        // A live store for this file, or a retired one that a closed handle still holds: join it
        // (reviving a retired one), so a file never has two stores, and two write locks, at once.
        if let Some(existing) = Self::find(&stores, file_id, &canonical) {
            let (revived, unused) = existing.claim(&stores, Some(pin));
            if revived {
                existing.cache_limits(limits);
            }
            drop(stores);
            drop(unused);
            drop(conn);
            drop(gate);
            existing.join(options, left())?;
            return Ok(existing);
        }
        let store = Arc::new(Self {
            directory: options.directory.clone(),
            canonical,
            file_id,
            pin: Mutex::new(Some(pin)),
            retired: AtomicBool::new(false),
            pool: Pool::new(&gate, usize::try_from(options.max_connections).unwrap_or(usize::MAX)),
            write_lock: WriteLock::new(),
            slots: Slots::new(),
            max_connections: AtomicU64::new(options.max_connections),
            cache_pages: AtomicU64::new(options.cache_pages),
            wal_size_limit: AtomicU64::new(options.wal_size_limit),
            wal: crate::wal::WalState::new(),
            auto_checkpoint: AtomicBool::new(options.auto_checkpoint),
            wal_path: db_path.with_extension("sqlite-wal"),
            handles: AtomicIsize::new(1),
            deferred_releases: AtomicUsize::new(0),
            size_limit: AtomicI64::new(limits.size_limit),
            cull_limit: AtomicI64::new(limits.cull_limit),
            pinned: Mutex::new(PinnedSlot::Empty),
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

    /// A live store for `db_path`, with one more handle registered on it. A retired one is left to
    /// the slow path, which has the pinned file to revive it with. Times out with no handle claimed.
    fn lookup(db_path: &Path, deadline: Instant) -> Result<Option<Arc<Self>>> {
        let Ok(metadata) = std::fs::metadata(db_path) else { return Ok(None) };
        let id = (metadata.dev(), metadata.ino());
        let gate = enter_until(deadline)?;
        let stores = registry(&gate);
        let Some(store) = stores.iter().filter_map(Weak::upgrade).find(|s| s.file_id == id && !s.is_retired()) else {
            return Ok(None);
        };
        let (_, unused) = store.claim(&stores, None);
        drop(stores);
        drop(unused);
        Ok(Some(store))
    }

    /// The store for `file_id` in `stores`: a live one, else a retired one for the same directory
    /// (`canonical`, resolved). A retired store no longer pins its inode, so a file elsewhere may
    /// have reused the number; only the same directory makes it the same cache.
    fn find(stores: &[Weak<Self>], file_id: FileId, canonical: &Path) -> Option<Arc<Self>> {
        let mut retired = None;
        for store in stores.iter().filter_map(Weak::upgrade).filter(|s| s.file_id == file_id) {
            if !store.is_retired() {
                return Some(store);
            }
            // Prefer one still in use by path (handles > 0), so it isn't left serving the file
            // beside another.
            let in_use = |s: &Arc<Self>| s.handles.load(Ordering::Acquire) > 0;
            if store.canonical == canonical && !retired.as_ref().is_some_and(in_use) {
                retired = Some(store);
            }
        }
        retired
    }

    /// Registers one more handle (`delta` = 1) or releases one (`delta` = -1), and decides from the
    /// resulting count whether the store is live. Called with the registry locked (`stores` is the
    /// proof), so registering and the last release never interleave.
    ///
    /// The count is signed and the decision depends only on where it lands, so the order of a
    /// handle's own add and release doesn't matter: a Python handle reused on one thread while it's
    /// closed on another may release before it adds (the count dips to -1), and the store still
    /// ends up retired with no handles. A store that goes from no handles to some is revived with
    /// `pin` (its database file, freshly opened), unless another live store already serves that
    /// file (the same directory reached by another path while this one was retired): then it stays
    /// retired and keeps working by path (without pooling: each operation reconnects). With other
    /// handles live, a release that lands first can retire the store for a moment (pin released,
    /// pool cleared) until the matching add revives it. Returns whether it was revived, and a pin for
    /// the caller to close once the registry is released (it's never closed under the registry, which
    /// never waits on I/O): the store's own if it retired, or `pin` if it wasn't needed.
    #[must_use = "the pin it returns must be closed after the registry is released"]
    fn count_handle(
        &self,
        stores: &MutexGuard<'_, Vec<Weak<Self>>>,
        delta: isize,
        pin: Option<FilePin>,
    ) -> (bool, Option<FilePin>) {
        let after = self.handles.fetch_add(delta, Ordering::AcqRel) + delta;
        let mut slot = self.pin.lock().unwrap_or_else(PoisonError::into_inner);
        if after <= 0 {
            self.retired.store(true, Ordering::Release);
            return (false, slot.take());
        }
        if !self.is_retired() {
            return (false, pin);
        }
        let other_live = stores
            .iter()
            .filter_map(Weak::upgrade)
            .any(|s| !std::ptr::eq(Arc::as_ptr(&s), self) && s.file_id == self.file_id && !s.is_retired());
        if other_live {
            return (false, pin);
        }
        let Some(pin) = pin else {
            return (false, None);
        };
        *slot = Some(pin);
        self.retired.store(false, Ordering::Release);
        (true, None)
    }

    /// Registers one more handle; see [`Store::count_handle`], whose result it returns.
    #[must_use = "the pin it returns must be closed after the registry is released"]
    fn claim(&self, stores: &MutexGuard<'_, Vec<Weak<Self>>>, pin: Option<FilePin>) -> (bool, Option<FilePin>) {
        self.count_handle(stores, 1, pin)
    }

    /// Applies a joining handle's settings (its handle is already registered by [`Store::claim`]):
    /// raises the connection cap if asked, and applies explicitly requested settings, writing only
    /// if they differ from what's stored, within `timeout`. On failure the handle is released again,
    /// with no wait: the open has already used its whole budget.
    fn join(self: &Arc<Self>, options: &OpenOptions, timeout: Duration) -> Result<()> {
        self.max_connections.fetch_max(options.max_connections, Ordering::AcqRel);
        self.pool.raise_max_idle(usize::try_from(options.max_connections).unwrap_or(usize::MAX));
        self.cache_pages.fetch_max(options.cache_pages, Ordering::AcqRel);
        self.wal_size_limit.fetch_max(options.wal_size_limit, Ordering::AcqRel);
        if options.auto_checkpoint {
            self.auto_checkpoint.store(true, Ordering::Release);
        }
        if options.requested == Requested::default() {
            return Ok(());
        }
        let result = self.write(timeout, |_writer, tx| {
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
                match GATE.try_enter_shared() {
                    Some(gate) => self.release_under(&gate),
                    None => self.defer_release(),
                }
                Err(error)
            }
        }
    }

    /// Registers one more handle if `claim` says this caller should (a closed handle that's used
    /// again rejoins its store: `claim` flips its closed flag). It enters the gate first, so a
    /// timeout leaves the handle closed and nothing counted. A retired store is revived if its
    /// database file is still the one it was opened on; otherwise it stays retired and keeps working
    /// by path, and a new open of the directory gets a new store.
    pub fn add_handle_until(&self, deadline: Instant, claim: impl FnOnce() -> bool) -> Result<()> {
        let gate = enter_until(deadline)?;
        if !claim() {
            return Ok(());
        }
        // Opened before taking the registry, which never waits on I/O.
        let db_path = open::db_path(&self.directory);
        let pin = open_pin(&gate, &db_path).ok().filter(|(_, id)| *id == self.file_id).map(|(pin, _)| pin);
        let stores = registry(&gate);
        let (_, unused) = self.claim(&stores, pin);
        drop(stores);
        drop(unused);
        Ok(())
    }

    /// Unregisters a handle. When the last one closes, every pooled connection is closed (D-018),
    /// and the store is retired: it stops pinning its file, and the registry only hands it out
    /// again to a handle on the same directory and file. The store itself stays usable: a later
    /// operation reopens connections.
    ///
    /// Never fails, and returns within about a second whatever a fork does. A fork that outlasts
    /// it leaves the release for the drain at its end.
    pub fn release_handle(self: &Arc<Self>) {
        self.release_handle_within(RELEASE_BUDGET);
    }

    fn release_handle_within(self: &Arc<Self>, budget: Duration) {
        match GATE.enter_shared_until(lockwait::deadline_after(Instant::now(), budget)) {
            Some(gate) => self.release_under(&gate),
            None => self.defer_release(),
        }
    }

    /// The release itself, on a level the caller holds.
    fn release_under(&self, gate: &SharedGuard<'_>) {
        // Decided under the registry, like `claim`: an open that finds this store either registers
        // its handle first (and this isn't the last), or finds it retired.
        let stores = registry(gate);
        let (_, unpinned) = self.count_handle(&stores, -1, None);
        let retired = self.is_retired();
        drop(stores);
        drop(unpinned);
        if retired {
            self.pool.clear(gate);
        }
    }

    /// Leaves one handle's release for the drain at the end of the pending fork. Until then the
    /// store just looks like it has one more handle; the count is signed and decided by where it
    /// lands, so a reopen meanwhile balances. An entry in [`DEFERRED_OWNERS`] keeps the store in the
    /// registry even if this was its last other `Arc`, so the drain can close its pin and connections
    /// under the gate, and the drain then drops the entry. A later open of the directory revives the
    /// store if something else still owns it, as with any retired store.
    ///
    /// Counted before the entry goes in: the drain takes the entries before it reads the counts, so it
    /// never drops an entry for a release it won't make. A drain between the two makes the release
    /// while the caller's `Arc` keeps the store alive, and the flag, set after both, gets the entry
    /// dropped by this thread's try or by the pending fork's drain.
    fn defer_release(self: &Arc<Self>) {
        // The drain is process-wide, so it can close another store's last connection on a large WAL:
        // with the interpreter released, wherever this release was running.
        GATE.defer(
            || {
                self.deferred_releases.fetch_add(1, Ordering::SeqCst);
                match deferred_owners() {
                    Some(mut owners) => owners.push(Arc::clone(self)),
                    // Only in a child forked while a thread was in there (or a holder stuck past
                    // DEFERRED_OWNERS_PATIENCE): the store outlives its drain, which is the safe
                    // direction, as it still releases. The leak is accepted there.
                    None => std::mem::forget(Arc::clone(self)),
                }
            },
            || gate::run_blocking(&drain_deferred_releases),
        );
    }

    /// Open handles, deferred releases included (for tests).
    #[must_use]
    pub fn handles(&self) -> isize {
        self.handles.load(Ordering::Acquire)
    }

    /// Returns a leased connection: to the pool, or closed if the store is retired (a lease or a
    /// held transaction that outlived the last handle), so nothing stays open after the last
    /// close (D-018).
    fn give_back(&self, gate: &SharedGuard<'_>, pooled: PooledConn) {
        if self.is_retired() {
            self.pool.discard(gate, pooled);
            return;
        }
        // Checked again after the checkin: a release that retired the store in between cleared the
        // pool either before this checkin (then this clears it) or after (then it already did).
        self.pool.checkin(gate, pooled);
        if self.is_retired() {
            self.pool.clear(gate);
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
    #[inline]
    pub fn read<T>(&self, timeout: Duration, read: impl FnMut(&Connection) -> Result<T>) -> Result<T> {
        self.read_with_deadline(timeout, read).map(|(value, _)| value)
    }

    /// [`Store::read`], also returning the deadline its slow path took, or `None` if it took no
    /// deadline, so a caller with more reads to do can charge them to the same budget. No clock read
    /// on the fast path.
    #[inline]
    pub(crate) fn read_with_deadline<T>(
        &self,
        timeout: Duration,
        mut read: impl FnMut(&Connection) -> Result<T>,
    ) -> Result<(T, Option<Instant>)> {
        if let Some(result) = self.on_pinned(|pinned| read(pinned.conn.conn())) {
            return result.map(|value| (value, None));
        }
        // Fast path: one attempt with no busy wait, no deadline and no clock read. WAL readers almost
        // never meet a lock; if this one does (or the pool is full), fall through to the full loop.
        // That includes the lease: a new connection reads the schema when its settings are applied.
        if GATE.own_depth() == 0
            && let Some(_permit) = self.slots.try_acquire(self.max_connections.load(Ordering::Acquire))
            && let Some(gate) = GATE.try_enter_shared()
        {
            match self.lease(&gate, timeout).and_then(|lease| read(lease.conn())) {
                Err(Error::Timeout { .. }) => {}
                done => return done.map(|value| (value, None)),
            }
        }
        let mut taken = None;
        let value = lockwait::retry(timeout, |deadline| {
            taken = Some(deadline);
            let _permit = self.reserve_slot(deadline)?;
            let gate = enter_until(deadline)?;
            let lease = self.lease(&gate, timeout)?;
            read(lease.conn())
        })?;
        Ok((value, taken))
    }

    /// Runs `write` inside one `BEGIN IMMEDIATE` transaction and commits it. Like [`Store::read`],
    /// `write` may run several times and must have no side effects outside the transaction; work
    /// such as writing a value file happens before calling this. A write that replaces, deletes or
    /// creates value files goes through `Store::write_files` instead.
    pub fn write<T>(
        &self,
        timeout: Duration,
        mut write: impl FnMut(&RowWriter<'_>, &Connection) -> Result<T>,
    ) -> Result<T> {
        self.write_files(timeout, LockPriority::Normal, |writer, conn| write(writer, conn).map(Changed::unchanged))
    }

    /// [`Store::write`] for a write that replaces, deletes or creates value files: `write`
    /// returns them in a [`Changed`]. Committed on its own, the replaced files are unlinked at once.
    /// Inside a held transaction, nothing is final until it ends: they are recorded on it, while
    /// the operation still has its connection, and settled when it commits or rolls back.
    pub(crate) fn write_files<T>(
        &self,
        timeout: Duration,
        priority: LockPriority,
        mut write: impl FnMut(&RowWriter<'_>, &Connection) -> Result<Changed<T>>,
    ) -> Result<T> {
        // 0. In a child forked inside a transaction on this file, writes can never succeed (D-020):
        // fail before waiting on anything.
        crate::poison::check_writable(self.file_id)?;
        // A thread holding a write transaction writes inside it, on its pinned connection, in a
        // savepoint: an operation that fails partway (a cull error after the row was stored) must
        // leave nothing behind in a transaction that may still commit.
        if let Some(result) = self.on_pinned(|pinned| {
            let conn = pinned.conn.conn();
            // No transaction left (SQLite rolled it back after an error): a SAVEPOINT here would
            // start one of its own and this write would commit by itself.
            if pinned.doomed.get() || conn.is_autocommit() {
                pinned.doomed.set(true);
                return Err(Error::TransactionAborted);
            }
            let mark = pinned.files.borrow().mark();
            let savepoint = OpSavepoint::begin(conn, timeout, &pinned.doomed)?;
            let written =
                write(&RowWriter::new(conn, timeout), conn).and_then(|changed| savepoint.release().map(|()| changed));
            match written {
                Ok(changed) => {
                    let mut files = pinned.files.borrow_mut();
                    files.removed.extend(changed.removed);
                    files.created.extend(changed.created);
                    Ok(changed.value)
                }
                Err(error) => {
                    // The savepoint was undone (or the transaction doomed) when it dropped. Undone,
                    // the files of operations nested in this one are rewound with their rows; doomed,
                    // the end rolls everything back and settles every file the transaction created.
                    if !pinned.doomed.get() {
                        let orphans = pinned.files.borrow_mut().rewind(mark);
                        self.unlink(orphans);
                    }
                    Err(error)
                }
            }
        }) {
            return result;
        }
        let changed = self.write_unpinned(timeout, priority, write)?;
        self.unlink(changed.removed);
        Ok(changed.value)
    }

    /// Unlinks value files, by their path relative to the cache directory. One already gone is fine.
    /// Many (the end of a held transaction, a `set` that culls) run with the interpreter released. A
    /// few stay attached: each unlink takes microseconds, while on a GIL build getting the
    /// interpreter back can wait out the switch interval behind busy threads (a one-file `delete`
    /// measured 27 ms detached against 4 spinning threads).
    pub(crate) fn unlink(&self, files: impl IntoIterator<Item = String>) {
        let paths: Vec<PathBuf> = files.into_iter().map(|relative| self.directory.join(relative)).collect();
        let work = || {
            for path in &paths {
                let _ = std::fs::remove_file(path);
            }
        };
        if paths.len() < UNLINK_DETACH_AT {
            work();
        } else {
            gate::run_blocking(&work);
        }
    }

    /// The write path outside a held transaction, after step 0 of `Store::write_files`.
    fn write_unpinned<T>(
        &self,
        timeout: Duration,
        priority: LockPriority,
        mut write: impl FnMut(&RowWriter<'_>, &Connection) -> Result<T>,
    ) -> Result<T> {
        let start = Instant::now();
        let deadline = lockwait::deadline_after(start, timeout);
        // 1. The write lock, before the gate. At depth > 0 never wait for it (see the module docs).
        let top_level = GATE.own_depth() == 0;
        let token = if top_level {
            let token = if priority == LockPriority::AfterYield {
                self.write_lock.lock_until_with_priority(deadline)
            } else {
                self.write_lock.lock_until(deadline)
            };
            Some(token.ok_or(Error::Timeout { seconds: timeout.as_secs_f64() })?)
        } else {
            self.write_lock.try_lock_nested()
        };
        // 2-3. Slot, gate and connection per attempt; SQLite's lock is then only contended by
        // other processes (and by a nested operation that couldn't take the write lock).
        let result = lockwait::retry(deadline.saturating_duration_since(Instant::now()), |attempt_deadline| {
            let _permit = self.reserve_slot(attempt_deadline)?;
            let gate = enter_until(attempt_deadline)?;
            let lease = self.lease(&gate, timeout)?;
            let tx = WriteTx::begin(lease.conn(), timeout)?;
            let value = write(&RowWriter::new(lease.conn(), timeout), lease.conn())?;
            tx.commit()?;
            Ok(value)
        })
        .map_err(|error| match error {
            Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
            other => other,
        });
        // 4. After the retry loop (a checkpoint problem must never replay a committed write), still
        // holding the write lock so our own writers queue rather than meet SQLite's busy handler.
        // Never for a nested write (depth > 0): it may get a token from try_lock_nested, but an outer
        // snapshot on this thread would only make the checkpoint report busy.
        if result.is_ok() && top_level && self.auto_checkpoint.load(Ordering::Relaxed) {
            self.maybe_checkpoint(deadline);
        }
        drop(token);
        result
    }

    /// The WAL check after a committed write; see [`crate::wal`]. Never fails, waits for readers at
    /// most [`crate::wal::TRUNCATE_TRIES`] attached windows and no later than the write's `deadline`
    /// after the first, and only records what happened.
    fn maybe_checkpoint(&self, deadline: Instant) {
        use crate::wal::Decision;
        let limit = self.wal_size_limit.load(Ordering::Relaxed);
        let decision = self.wal.after_commit(limit, || self.wal_bytes());
        if decision == Decision::Checkpoint {
            let outcome = self.checkpoint_once(deadline);
            self.wal.record_outcome(&outcome);
        }
    }

    /// The `-wal` file's size in bytes (0 if it doesn't exist).
    fn wal_bytes(&self) -> u64 {
        std::fs::metadata(&self.wal_path).map_or(0, |m| m.len())
    }

    /// Checkpoints the WAL and truncates it to zero (D-024): retries busy rounds, waiting with the
    /// interpreter released, until one succeeds or `timeout` runs out. Returns `Ok(true)` if the WAL
    /// was reset, `Ok(false)` if readers or writers in other processes, or a pending fork, kept it
    /// busy the whole time,
    /// and a real error (a full disk, an I/O error) at once. `timeout` bounds the waiting, not a
    /// round already running: copying a large backlog and syncing it, in PASSIVE or in any TRUNCATE
    /// try, can take longer, and the first try always gets one fresh 2 ms window. For
    /// `Cache.checkpoint()`: an app calls it when a pause suits it (at shutdown, in a maintenance
    /// job, after a large `clear()`). Inside the calling thread's own held transaction, or a nested
    /// operation, it returns `Ok(false)` without trying (its own open transaction keeps the WAL busy).
    pub fn checkpoint(&self, timeout: Duration) -> Result<bool> {
        use crate::wal::Outcome;
        crate::poison::check_writable(self.file_id)?;
        let start = Instant::now();
        let deadline = lockwait::deadline_after(start, timeout);
        // Inside this thread's own held transaction (held levels count in own_depth) or a nested
        // operation, TRUNCATE can't succeed, and the PASSIVE copy could stall for seconds while
        // holding the gate: don't try. Another thread's held transaction is fine: the loop below
        // waits for it (detached) on the write lock.
        if GATE.own_depth() > 0 {
            return Ok(false);
        }
        let outcome = loop {
            // Holding the in-process write lock keeps this process's writers queued (detached)
            // instead of meeting SQLite's busy handler while the checkpoint resets the WAL.
            let token = self.write_lock.lock_until(deadline);
            let outcome = match token {
                Some(_) => self.checkpoint_once(deadline),
                // Writers queued behind a fork hold the lock: that's the fork, not WAL contention.
                None if GATE.stats().pending => Outcome::ForkPending,
                None => Outcome::Busy,
            };
            drop(token);
            let retry = matches!(outcome, Outcome::Busy | Outcome::NoSlot | Outcome::ForkPending);
            if !retry || Instant::now() >= deadline {
                break outcome;
            }
            lockwait::backoff(start.elapsed());
        };
        // One attempt in the counters, however many rounds it took.
        self.wal.record_outcome(&outcome);
        match outcome {
            Outcome::Done => Ok(true),
            Outcome::Busy | Outcome::NoSlot | Outcome::ForkPending => Ok(false),
            Outcome::Failed(error) => Err(error),
        }
    }

    /// For `Cache.close(checkpoint=True)`: if the WAL is past its trim size (`journal_size_limit`,
    /// half of `wal_size_limit`), checkpoints it, waiting at most `budget` for other processes (a
    /// round already copying can run past it). A small WAL, the usual case, costs one `stat`. Never
    /// fails: close must succeed whatever the WAL's state.
    ///
    /// Only for the process's last handle on this store: others leave it to the last one, so a
    /// handle closed while the cache is in use never holds up this process's writers. And not
    /// again for [`crate::wal::RETRY_AFTER_BUSY`] after an attempt that didn't succeed, so a pattern of
    /// `with Cache(...)` per request doesn't pay the budget on every exit while readers in other
    /// processes keep the WAL busy.
    pub fn checkpoint_on_close(&self, budget: Duration) {
        if self.checkpoint_due_on_close() {
            let _ = self.checkpoint(budget);
        }
    }

    /// Whether [`Store::checkpoint_on_close`] would checkpoint: one `stat` at most.
    fn checkpoint_due_on_close(&self) -> bool {
        !self.wal.in_backoff() && self.closing_on_a_large_wal()
    }

    /// Whether closing this handle (with or without [`Store::checkpoint_on_close`]) may have a WAL
    /// to copy: it's the last handle, and the WAL is over its trim size. Then the checkpoint, or
    /// SQLite's own one when [`Store::release_handle`] closes the last connection on the file in
    /// this process, can take a while. One `stat` at most. Lets a caller release the interpreter
    /// only when there's something to wait for.
    #[must_use]
    pub fn closing_on_a_large_wal(&self) -> bool {
        let trim = crate::wal::journal_size_limit(self.wal_size_limit.load(Ordering::Relaxed));
        self.handles.load(Ordering::Acquire) == 1 && self.wal_bytes() > trim
    }

    /// One round of a forced checkpoint: PASSIVE, then TRUNCATE, repeated while it reports busy, each
    /// try in a fresh 2 ms window ([`crate::wal::truncate_tries`]). Records how long it took; the
    /// caller records the outcome. The first window opens after PASSIVE, whatever the time: a window
    /// that PASSIVE's copy had already used up would let TRUNCATE lose to a reader microseconds into
    /// its transaction, after all the copying was done. Later windows end by `deadline`, but `deadline`
    /// bounds only the waiting: a try's copy and syncs can run past it, as [`Store::checkpoint`] says.
    /// Behind a pending fork it doesn't wait, and no later try starts once a fork is pending, because
    /// [`Store::maybe_checkpoint`] holds the write lock here. So a fork that arrives mid-round waits
    /// for the try under way, and one that arrives sooner for the rest of PASSIVE and the first try.
    fn checkpoint_once(&self, deadline: Instant) -> crate::wal::Outcome {
        use crate::wal::Outcome;
        let classify = |error: Error| match error {
            Error::Timeout { .. } => Outcome::Busy,
            other => Outcome::Failed(other),
        };
        let Some(_permit) = self.slots.try_acquire(self.max_connections.load(Ordering::Acquire)) else {
            return Outcome::NoSlot;
        };
        let Some(gate) = GATE.try_enter_shared() else {
            return Outcome::ForkPending;
        };
        let mut lease = match self.lease(&gate, Duration::ZERO) {
            Ok(lease) => lease,
            Err(error) => return classify(error),
        };
        // Two steps. PASSIVE first, with no busy window: it copies whatever frames it can into the
        // database without waiting for anyone. Then TRUNCATE: it waits for readers on older snapshots
        // (ours take microseconds), copies the rest, syncs and truncates the file. A single TRUNCATE
        // spent its whole window copying a large backlog and then reported busy before it could wait
        // for readers (seen on Linux with readers in other processes).
        //
        // PASSIVE can't be relied on to do that copy. It copies only up to the oldest snapshot a reader
        // still uses (SQLite's `mxSafeFrame`), and right after a commit, with readers in other processes,
        // that's often no further than the last checkpoint already copied (`nBackfill`): then it copies
        // nothing. Then the first TRUNCATE does the copy and spends its window on it, and one reader in
        // flight makes it report busy, so sustained writes beside readers in other processes failed
        // most forced checkpoints (plan 09, step 2b). Hence the repeat: SQLite keeps the copy a busy
        // try made, so a later try has little or nothing left to copy and its whole window goes to the
        // readers, and
        // SQLite has released every lock between tries. Busy comes back as a row (busy = 1), not an
        // error; a real error ends the round at once.
        //
        // All of it runs with the interpreter released (through the gate's blocker), although this thread
        // holds the gate: copying a large backlog under a loaded disk took up to 1.4 s in the soak
        // (what was seen, not a bound), and on a GIL build every other thread of the process would have
        // frozen with it. This is the same exception spill-file reads make: work with no waiting on
        // another lock or on the interpreter, so a pending fork waits for it and can't deadlock.
        // The blocker's closure must be `Sync` and a `Connection` isn't, so the closure borrows the
        // lease's connection through a mutex (`&mut PooledConn` is `Send`).
        let mut timings = None;
        {
            let cell = std::sync::Mutex::new((lease.pooled_mut(), &mut timings));
            gate::run_blocking(&|| {
                let Ok(mut guard) = cell.lock() else { return };
                let (pooled, out) = &mut *guard;
                let conn = pooled.conn();
                let start = Instant::now();
                let _ = conn.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |r| r.get::<_, i64>(0));
                let passive = start.elapsed();
                let row = crate::wal::truncate_tries(
                    deadline,
                    || GATE.stats().pending,
                    |ends| {
                        // `open` caps the window at ATTACHED_WINDOW from now.
                        let _window = lockwait::AttachedWindow::open(ends);
                        conn.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get::<_, i64>(0))
                    },
                );
                **out = Some((row, passive, start.elapsed().saturating_sub(passive)));
            });
        }
        // The lease checks the connection back in on every path, panics included.
        drop(lease);
        // Defensive: unset only if the blocker returned without running the closure.
        let Some((row, passive, truncate)) = timings else {
            return Outcome::Failed(Error::Io {
                path: self.wal_path.clone(),
                source: std::io::Error::other("the blocker returned without running the checkpoint"),
            });
        };
        self.wal.record_pause(passive, truncate);
        match row {
            Ok(0) => Outcome::Done,
            // It may have stopped trying for the fork: that says nothing about the WAL, so no backoff.
            Ok(_) if GATE.stats().pending => Outcome::ForkPending,
            Ok(_) => Outcome::Busy,
            Err(error) => classify(Error::from_sqlite(error, Duration::ZERO)),
        }
    }

    /// What forced WAL checkpoints have done on this store (for tests and debugging).
    #[must_use]
    pub fn wal_stats(&self) -> crate::wal::WalStats {
        self.wal.stats()
    }

    /// Fails with [`Error::Unwritable`] in a child forked inside a transaction on this file (D-020).
    pub fn check_writable(&self) -> Result<()> {
        crate::poison::check_writable(self.file_id)
    }

    /// Runs `write` in a write transaction only if that needs no waiting at all: the write lock is
    /// free, no fork is pending, and SQLite's lock is free too (no busy wait). Returns `None` if it
    /// didn't run, or ran into a lock. For opportunistic cleanup that must never block a read (D-013).
    pub fn write_nowait<T>(&self, write: impl FnOnce(&RowWriter<'_>) -> Result<T>) -> Option<Result<T>> {
        if crate::poison::check_writable(self.file_id).is_err() || GATE.own_depth() > 0 {
            return None;
        }
        let _token = self.write_lock.try_lock()?;
        let _permit = self.slots.try_acquire(self.max_connections.load(Ordering::Acquire))?;
        let gate = GATE.try_enter_shared()?;
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
    ///
    /// The connection is lent to `f` (moved out of `pinned`), not used under the mutex: `f` may run
    /// Python (a pickle hook) that calls the cache again, and that nested operation must find the
    /// same connection rather than wait for a mutex its own thread holds.
    fn on_pinned<T>(&self, f: impl FnOnce(&Pinned) -> Result<T>) -> Option<Result<T>> {
        if !self.pinned_active.load(Ordering::Relaxed) {
            return None;
        }
        // Refused only behind a pending fork, and only to a thread with no level: an owner has a held
        // level, and a thread inside an operation has depth > 0. So it isn't pinned, and the caller's
        // normal path applies its deadline.
        let gate = GATE.try_enter_shared()?;
        let key = self.key();
        if let Some(pinned) =
            LENT.with(|lent| lent.borrow().iter().rev().find(|(k, _)| *k == key).map(|(_, p)| Rc::clone(p)))
        {
            // Nested inside an operation already running on it.
            if pinned.generation != gate::generation() {
                return Some(Err(Error::ForkPoisoned));
            }
            return Some(f(&pinned));
        }
        let me = std::thread::current().id();
        let pinned = {
            let mut slot = self.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            let PinnedSlot::Here(held) = &*slot else { return None };
            if held.owner != me {
                return None;
            }
            if held.generation != gate::generation() {
                return Some(Err(Error::ForkPoisoned));
            }
            let lent = PinnedSlot::Lent { owner: me, generation: held.generation, abandoned: None };
            let PinnedSlot::Here(held) = std::mem::replace(&mut *slot, lent) else { unreachable!() };
            held
        };
        let lend = Lend::new(self, &gate, pinned);
        let result = f(lend.pinned());
        drop(lend);
        drop(gate);
        Some(result)
    }

    /// This store's identity within the process, for [`LENT`].
    fn key(&self) -> usize {
        std::ptr::from_ref(self).addr()
    }

    /// Whether the calling thread holds a write transaction on this store that this process can use,
    /// inside one of its operations or between them. A transaction inherited across `fork()` doesn't
    /// count. For a failure's path, not a call's: it takes the slot's mutex.
    #[cold]
    #[must_use]
    pub fn held_by_this_thread(&self) -> bool {
        if !self.pinned_active.load(Ordering::Relaxed) {
            return false;
        }
        // Refused only to a thread with no level, which proves it holds nothing: see `on_pinned`.
        let Some(gate) = GATE.try_enter_shared() else { return false };
        let held = self.pinned.lock().unwrap_or_else(PoisonError::into_inner).held_by(std::thread::current().id());
        drop(gate);
        held
    }

    /// Starts a write transaction that stays open across calls until [`HeldTransaction::finish`]
    /// (or drop, which rolls back). The calling thread's reads and writes on this store run inside
    /// it. It holds the write lock, a gate level and a pooled connection, and registers the file
    /// for D-020 poisoning: a child forked meanwhile can never write it.
    pub fn begin_hold(self: &Arc<Self>, timeout: Duration) -> Result<HeldTransaction> {
        crate::poison::check_writable(self.file_id)?;
        // Refused only to a thread with no level, which proves it isn't nested: see `on_pinned`.
        if let Some(gate) = GATE.try_enter_shared() {
            let pinned = self.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            if pinned.held_by(std::thread::current().id()) {
                return Err(Error::NestedTransaction);
            }
            drop(pinned);
            drop(gate);
        }
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        // At gate depth > 0 (a hold started from inside another operation, or while holding a
        // transaction on another cache) never wait for the write lock: rule 1 of the module docs.
        // Take it if it's free, or fail at once with an error that says why.
        let token = if GATE.own_depth() == 0 {
            self.write_lock.lock_until(deadline).ok_or(Error::Timeout { seconds: timeout.as_secs_f64() })?
        } else {
            self.write_lock.try_lock_nested().ok_or(Error::WriteLockBusyInTransaction)?
        };
        let (conn, hold, share) =
            lockwait::retry(deadline.saturating_duration_since(Instant::now()), |attempt_deadline| {
                let gate = enter_until(attempt_deadline)?;
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
            // Never refused: this thread holds the level from `into_held`.
            let Some(gate) = GATE.try_enter_shared() else {
                unreachable!("a thread holding a share was refused the gate");
            };
            let mut pinned = self.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            *pinned = PinnedSlot::Here(Pinned {
                owner: std::thread::current().id(),
                generation: gate::generation(),
                conn,
                files: RefCell::default(),
                doomed: Cell::new(false),
            });
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
        // New connections, and older ones after a handle asked for more, get the store's settings.
        // All three are connection settings that write nothing (D-019). `format!` is safe here:
        // they're integers we computed, and PRAGMA values can't be bound as parameters.
        let applied = crate::pool::Applied {
            cache_pages: self.cache_pages.load(Ordering::Relaxed),
            wal_size_limit: self.wal_size_limit.load(Ordering::Relaxed),
        };
        if pooled.applied() != applied {
            let crate::pool::Applied { cache_pages: pages, wal_size_limit: wal_limit } = applied;
            let set = pooled.conn().execute_batch(&format!(
                "PRAGMA cache_size = {pages}; PRAGMA journal_size_limit = {}; PRAGMA wal_autocheckpoint = {};",
                crate::wal::journal_size_limit(wal_limit),
                crate::wal::autocheckpoint_pages(wal_limit),
            ));
            if let Err(error) = set {
                self.give_back(gate, pooled);
                return Err(Error::from_sqlite(error, timeout));
            }
            pooled.set_applied(applied);
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

/// One operation's savepoint inside a held transaction (`SAVEPOINT op`), so an operation that fails
/// partway, or panics, leaves nothing behind in a transaction that may still commit. Released on
/// success; otherwise undone on drop with `ROLLBACK TO` and `RELEASE`. If that fails, or SQLite
/// already rolled the whole transaction back (a full disk, an I/O error), the transaction is marked
/// doomed. Savepoints of nested operations share the name: SQLite matches the innermost one.
struct OpSavepoint<'c> {
    conn: &'c Connection,
    timeout: Duration,
    doomed: &'c Cell<bool>,
    open: bool,
}

impl<'c> OpSavepoint<'c> {
    fn begin(conn: &'c Connection, timeout: Duration, doomed: &'c Cell<bool>) -> Result<Self> {
        run_cached(conn, "SAVEPOINT op", timeout)?;
        Ok(Self { conn, timeout, doomed, open: true })
    }

    /// Keeps the operation's writes in the transaction. On an error the savepoint is still open,
    /// and dropping it undoes them.
    fn release(mut self) -> Result<()> {
        run_cached(self.conn, "RELEASE op", self.timeout)?;
        self.open = false;
        Ok(())
    }
}

impl Drop for OpSavepoint<'_> {
    fn drop(&mut self) {
        if self.open {
            let undone = !self.conn.is_autocommit()
                && run_cached(self.conn, "ROLLBACK TO op", self.timeout).is_ok()
                && run_cached(self.conn, "RELEASE op", self.timeout).is_ok();
            if !undone {
                self.doomed.set(true);
            }
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

    fn pooled_mut(&mut self) -> &mut PooledConn {
        self.pooled.as_mut().unwrap_or_else(|| unreachable!("lease used after drop"))
    }

    fn conn(&self) -> &Connection {
        // Always Some until drop.
        self.pooled.as_ref().map_or_else(|| unreachable!("lease used after drop"), PooledConn::conn)
    }
}

impl Drop for Lease<'_, '_> {
    fn drop(&mut self) {
        if let Some(pooled) = self.pooled.take() {
            self.store.give_back(self.gate, pooled);
        }
    }
}

/// A write transaction held across calls. See [`Store::begin_hold`]. `Send`: it may be finished or
/// dropped on another thread (the GC may drop the Python object holding it anywhere). While the
/// owner thread is inside an operation on it, that waits for the operation, with the interpreter
/// released.
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

    /// Ends the transaction. Inside one of its own operations (the connection is lent to this
    /// thread) it can't, and reports [`Error::EndInsideOperation`] with nothing released.
    fn end(&mut self, commit: bool) -> Result<()> {
        let Some(write_word) = self.write_word else { return Ok(()) };
        let me = std::thread::current().id();
        // The levels this thread holds outside this transaction (it may be inside an operation of
        // its own when the GC finalizes the transaction here).
        let outer = GATE.own_depth();
        let mut inherited = false;
        let (gate, pinned) = loop {
            // On the transaction's own level: from another thread, a plain entry would wait for a
            // pending fork, which waits for this transaction. `share` is `Some` while `write_word` is.
            let gate = self.share.as_ref().unwrap_or_else(|| unreachable!("a live hold has a share")).enter();
            let mut slot = self.store.pinned.lock().unwrap_or_else(PoisonError::into_inner);
            match &mut *slot {
                PinnedSlot::Lent { generation, .. } if *generation != gate::generation() => {
                    // Lent in the parent to an operation on a thread the child doesn't have: never
                    // handed back. Left as it is, like any connection inherited across fork.
                    inherited = true;
                    drop(slot);
                    break (gate, None);
                }
                PinnedSlot::Lent { owner, .. } if *owner == me => return Err(Error::EndInsideOperation),
                PinnedSlot::Lent { abandoned, .. } if !commit || outer > 0 => {
                    // The owner thread is inside an operation on it. A rollback needn't wait for
                    // that operation: it's left to it, as a drop inside it is. A commit from a thread
                    // holding levels of its own can't wait either: a fork from inside that operation
                    // would wait for those levels while this thread waits for the operation. Then
                    // it's rolled back the same way, and the commit reports why.
                    *abandoned = Some(Abandoned { write_word, hold: self.hold.take(), share: self.share.take() });
                    self.write_word = None;
                    drop(slot);
                    drop(gate);
                    return if commit { Err(Error::EndInsideOperation) } else { Ok(()) };
                }
                PinnedSlot::Lent { .. } => {
                    // A commit from a thread with no other levels: wait for the operation to hand the
                    // connection back, with the interpreter released, since that operation may be
                    // running Python (a pickle hook) and need it. The operation is at gate depth > 0,
                    // where every gate wait is bounded, and a fork from it doesn't wait for this
                    // thread, which holds no level while it sleeps. This wait has no deadline, though:
                    // a hook that depends on this thread never hands the connection back.
                    drop(slot);
                    drop(gate);
                    gate::run_blocking(&|| std::thread::sleep(LENT_POLL));
                }
                _ => {
                    self.store.pinned_active.store(false, Ordering::Release);
                    let pinned = match std::mem::replace(&mut *slot, PinnedSlot::Empty) {
                        PinnedSlot::Here(p) => Some(p),
                        _ => None,
                    };
                    drop(slot);
                    break (gate, pinned);
                }
            }
        };
        self.write_word = None;
        let (result, garbage) = match pinned {
            Some(p) => close_pinned(&self.store, &gate, p, commit, self.timeout),
            None if inherited => (Err(Error::ForkPoisoned), Vec::new()),
            None => (Ok(()), Vec::new()),
        };
        drop(gate);
        self.store.write_lock.release_raw(write_word);
        self.hold.take();
        self.share.take();
        self.store.unlink(garbage);
        result
    }

    /// Leaves the rollback to the operation the connection is lent to (see [`PinnedSlot::Lent`]),
    /// from inside that operation. Operations still running in it keep writing into a transaction
    /// that will roll back.
    fn abandon(&mut self) {
        let Some(write_word) = self.write_word.take() else { return };
        let gate = self.share.as_ref().unwrap_or_else(|| unreachable!("a live hold has a share")).enter();
        let mut slot = self.store.pinned.lock().unwrap_or_else(PoisonError::into_inner);
        if let PinnedSlot::Lent { abandoned, .. } = &mut *slot {
            *abandoned = Some(Abandoned { write_word, hold: self.hold.take(), share: self.share.take() });
            return;
        }
        // Only called inside one of its operations, so the slot is lent; never leak the lock if not.
        debug_assert!(false, "abandoned a held transaction that isn't lent");
        drop(slot);
        drop(gate);
        self.store.write_lock.release_raw(write_word);
        self.hold.take();
        self.share.take();
    }
}

impl Drop for HeldTransaction {
    fn drop(&mut self) {
        if let Err(Error::EndInsideOperation) = self.end(false) {
            self.abandon();
        }
    }
}

/// Commits or rolls back a held transaction's connection and returns it to the pool, with the value
/// files that are now garbage, for the caller to unlink once it has released the write lock (a held
/// `clear()` can leave many; no row refers to them any more, and names are never reused). `end`
/// unlinks them with none of the transaction's own gate levels held (the caller may hold levels of
/// its own); an abandoned transaction's operation, with its level still held. The gate allows both
/// for unlinks. One inherited across `fork()` is forgotten instead, files and all: the parent still owns
/// that transaction.
fn close_pinned(
    store: &Store,
    gate: &SharedGuard<'_>,
    pinned: Pinned,
    commit: bool,
    timeout: Duration,
) -> (Result<()>, Vec<String>) {
    if pinned.generation != gate::generation() {
        pinned.conn.forget();
        return (Err(Error::ForkPoisoned), Vec::new());
    }
    let conn = pinned.conn.conn();
    // Doomed by a write, or ended by SQLite itself during a read (an I/O error, out of memory),
    // which no write saw.
    let outcome = if pinned.doomed.get() || conn.is_autocommit() {
        // Rolled back whatever was asked; a commit that can't happen is an error.
        if !conn.is_autocommit() {
            let _ = conn.execute_batch("ROLLBACK");
        }
        if commit { Err(Error::TransactionAborted) } else { Ok(()) }
    } else {
        let sql = if commit { "COMMIT" } else { "ROLLBACK" };
        let outcome = conn.execute_batch(sql).map_err(|e| Error::from_sqlite(e, timeout));
        if outcome.is_err() && !conn.is_autocommit() {
            // Leave no half-open transaction on a pooled connection.
            let _ = conn.execute_batch("ROLLBACK");
        }
        outcome
    };
    let committed = commit && outcome.is_ok();
    store.give_back(gate, pinned.conn);
    // Only now is it final which version of each row stands. A failed COMMIT didn't commit (SQLite
    // reports an error only then), and the ROLLBACK above ended it.
    let files = pinned.files.into_inner();
    let garbage = if committed { files.removed } else { files.created };
    (outcome, garbage)
}

/// Opens a store's pin on its database file, and the file's id. The pin is a SQLite connection that
/// never runs a statement, not a plain `File`: closing any descriptor on a file drops every POSIX
/// lock the process holds on it, SQLite's included. SQLite's own descriptors share its per-file
/// lock record, so it defers closing one while another connection holds a lock; a `File` it doesn't
/// know about would drop them. The id is read before and after the open, so it's the file the pin
/// holds. Opened under the gate (`_gate` is the proof), like every connection; a read-only open
/// that runs no statement takes no lock and never waits.
fn open_pin(_gate: &SharedGuard<'_>, db_path: &Path) -> Result<(FilePin, FileId)> {
    let id = || {
        std::fs::metadata(db_path)
            .map(|m| (m.dev(), m.ino()))
            .map_err(|source| Error::Io { path: db_path.to_path_buf(), source })
    };
    let before = id()?;
    let pin = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|e| Error::from_sqlite(e, Duration::ZERO))?;
    let after = id()?;
    if before != after {
        let replaced = std::io::Error::other("the database file was replaced while it was being opened");
        return Err(Error::Io { path: db_path.to_path_buf(), source: replaced });
    }
    Ok((FilePin { conn: Some(pin), generation: gate::generation() }, after))
}

/// A store's pin (see [`open_pin`]). Closed under the gate wherever it's dropped (a store dropped by
/// the GC without a close, a pin that turned out to be for another file), since closing a
/// connection takes SQLite's process-wide locks, which a child forked meanwhile could inherit held.
/// One inherited across `fork()` keeps pinning the file in the child but is forgotten, never
/// closed, like every inherited connection.
#[derive(Debug)]
struct FilePin {
    conn: Option<Connection>,
    generation: u64,
}

impl Drop for FilePin {
    fn drop(&mut self) {
        let Some(conn) = self.conn.take() else { return };
        if self.generation != gate::generation() {
            std::mem::forget(conn);
            return;
        }
        // A pin holds no lock and runs no statement, so never closing it is the safe direction. Only
        // a backstop: `release_under` takes the pin under a level, and a deferred release keeps the
        // store alive until its drain does.
        match GATE.try_enter_shared() {
            Some(gate) => {
                drop(conn);
                drop(gate);
            }
            None => std::mem::forget(conn),
        }
    }
}

/// Releases the handles deferred behind a fork that has now ended, if the gate can be entered (see
/// [`Gate::drain_deferred`](crate::gate::Gate::drain_deferred)). Run from the after-fork hooks.
/// Closing a last connection can run SQLite's own checkpoint, so call it with the interpreter
/// released. In a child, the connections it inherited are forgotten rather than closed.
pub fn drain_deferred_releases() {
    GATE.drain_deferred(|gate| {
        // Before the counts (see `Store::defer_release`), and kept past the releases. A last `Arc`
        // goes at the end of this closure, under the level and outside both mutexes.
        let _owners = deferred_owners().map(|mut owners| std::mem::take(&mut *owners)).unwrap_or_default();
        // Collected first, so an `Arc` dropped here can never close a pin under the registry mutex.
        let mut deferred: Vec<Arc<Store>> = registry(gate).iter().filter_map(Weak::upgrade).collect();
        deferred.retain(|store| store.deferred_releases.load(Ordering::SeqCst) > 0);
        for store in &deferred {
            for _ in 0..store.deferred_releases.swap(0, Ordering::SeqCst) {
                store.release_under(gate);
            }
        }
    });
}

#[cfg(test)]
mod fork_timeout;

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
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
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

    fn add_handle(store: &Store) {
        store.add_handle_until(Instant::now() + T, || true).unwrap();
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
        let peak = std::sync::atomic::AtomicUsize::new(0);
        let live = std::sync::atomic::AtomicUsize::new(0);
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
        // Another test's fork would close the idle connection this looks for.
        let _forks = crate::testutil::one_global_fork_at_a_time();
        let tmp = TempDir::new("store-close");
        let a = Store::open(&options(tmp.path())).unwrap();
        let b = Store::open(&options(tmp.path())).unwrap();
        put(&a, "k", b"v");
        a.release_handle();
        let gate = GATE.enter_shared_blocking();
        assert!(b.pool.idle(&gate) > 0, "one handle is still open");
        drop(gate);
        b.release_handle();
        let gate = GATE.enter_shared_blocking();
        assert_eq!(b.pool.idle(&gate), 0);
        drop(gate);
        // Still usable afterwards: a later operation reopens a connection.
        assert_eq!(count(&b), 1);
    }

    #[test]
    fn a_closed_store_still_held_is_revived_instead_of_duplicated() {
        // A closed Python handle keeps its Arc<Store>. An open of the same directory after the last
        // close must revive that store, not create a second one with its own write lock.
        let tmp = TempDir::new("store-revive");
        let first = Store::open(&options(tmp.path())).unwrap();
        put(&first, "k", b"v");
        first.release_handle();
        assert!(first.is_retired());
        let second = Store::open(&options(tmp.path())).unwrap();
        assert!(Arc::ptr_eq(&first, &second), "a second store for the same file");
        assert!(!first.is_retired());
        // And the closed handle rejoining finds the same store too, and keeps it live.
        second.release_handle();
        add_handle(&first);
        assert!(!first.is_retired(), "a rejoining handle revives its store");
        let third = Store::open(&options(tmp.path())).unwrap();
        assert!(Arc::ptr_eq(&first, &third));
        assert_eq!(first.handles.load(Ordering::Acquire), 2);
        third.release_handle();
        first.release_handle();
    }

    #[test]
    fn a_pin_claim_doesnt_keep_is_handed_back_to_close_outside_the_registry() {
        let tmp = TempDir::new("store-pin-back");
        let store = Store::open(&options(tmp.path())).unwrap();
        let db_path = open::db_path(tmp.path());
        let gate = GATE.enter_shared_blocking();
        // Live: the store keeps its own pin and hands the new one back.
        let (pin, _) = open_pin(&gate, &db_path).unwrap();
        let stores = registry(&gate);
        let (revived, unused) = store.claim(&stores, Some(pin));
        drop(stores);
        assert!(!revived && unused.is_some());
        // A release that lands first (the count dips to 0) retires the store and hands back its pin.
        let stores = registry(&gate);
        let (_, own) = store.count_handle(&stores, -2, None);
        drop(stores);
        assert!(store.is_retired() && own.is_some());
        // Retired, a claim with a pin keeps it: nothing comes back.
        let (pin, _) = open_pin(&gate, &db_path).unwrap();
        let stores = registry(&gate);
        let (revived, unused) = store.claim(&stores, Some(pin));
        drop(stores);
        assert!(revived && unused.is_none());
        drop(gate);
        store.release_handle();
    }

    #[test]
    fn a_pin_for_a_file_another_live_store_serves_is_handed_back_too() {
        // The third case: retired, but another store (the same file through a hard link) is live,
        // so the claim can't revive it and must hand the fresh pin back rather than close it.
        let tmp = TempDir::new("store-pin-other-live");
        let (d1, d2) = (tmp.path().join("one"), tmp.path().join("two"));
        let first = Store::open(&options(&d1)).unwrap();
        first.release_handle();
        std::fs::create_dir(&d2).unwrap();
        std::fs::hard_link(open::db_path(&d1), open::db_path(&d2)).unwrap();
        let second = Store::open(&options(&d2)).unwrap();
        let gate = GATE.enter_shared_blocking();
        let (pin, _) = open_pin(&gate, &open::db_path(&d1)).unwrap();
        let stores = registry(&gate);
        let (revived, unused) = first.claim(&stores, Some(pin));
        drop(stores);
        assert!(!revived && unused.is_some());
        assert!(first.is_retired());
        drop(unused);
        drop(gate);
        first.release_handle();
        second.release_handle();
    }

    #[test]
    fn racing_opens_and_closes_never_make_two_stores_for_one_file() {
        let tmp = TempDir::new("store-race");
        let keeper = Store::open(&options(tmp.path())).unwrap();
        keeper.release_handle(); // Retired, but held: every open below must get this store back.
        std::thread::scope(|s| {
            for _ in 0..8 {
                s.spawn(|| {
                    for _ in 0..200 {
                        let store = Store::open(&options(tmp.path())).unwrap();
                        assert!(Arc::ptr_eq(&store, &keeper), "an open got a second store for the file");
                        store.release_handle();
                    }
                });
            }
        });
        assert_eq!(keeper.handles.load(Ordering::Acquire), 0);
        assert!(keeper.is_retired());
        assert!(keeper.pin.lock().unwrap().is_none());
        let gate = GATE.enter_shared_blocking();
        assert_eq!(keeper.pool.idle(&gate), 0);
    }

    #[test]
    fn a_retired_store_is_only_found_for_its_own_directory() {
        let tmp = TempDir::new("store-retired-other");
        let first = Store::open(&options(tmp.path())).unwrap();
        first.release_handle();
        let stores = [Arc::downgrade(&first)];
        let elsewhere = tmp.path().join("elsewhere");
        assert!(Store::find(&stores, first.file_id, &elsewhere).is_none(), "an inode number reused elsewhere");
        assert!(Store::find(&stores, first.file_id, &first.canonical).is_some());
    }

    #[test]
    fn the_same_directory_by_another_path_revives_the_same_store() {
        let tmp = TempDir::new("store-alias");
        let first = Store::open(&options(tmp.path())).unwrap();
        first.release_handle();
        let alias = tmp.path().join("sub").join("..");
        std::fs::create_dir(tmp.path().join("sub")).unwrap();
        let second = Store::open(&options(&alias)).unwrap();
        assert!(Arc::ptr_eq(&first, &second), "a second store for the same directory under another path");
        second.release_handle();
    }

    #[test]
    fn a_reused_handle_never_revives_its_store_beside_another_live_one() {
        // A hard link gives the same file two directories, so `canonical` can't fold them together:
        // a closed handle on the first must not come back live next to the second's store.
        let tmp = TempDir::new("store-hardlink");
        let (d1, d2) = (tmp.path().join("one"), tmp.path().join("two"));
        let first = Store::open(&options(&d1)).unwrap();
        first.release_handle();
        std::fs::create_dir(&d2).unwrap();
        std::fs::hard_link(open::db_path(&d1), open::db_path(&d2)).unwrap();
        let second = Store::open(&options(&d2)).unwrap();
        assert!(!Arc::ptr_eq(&first, &second));
        add_handle(&first);
        assert!(first.is_retired(), "two live stores for one file");
        assert!(first.pin.lock().unwrap().is_none());
        assert!(!second.is_retired());
        first.release_handle();
        second.release_handle();
    }

    #[test]
    fn a_release_that_lands_before_its_add_still_leaves_the_store_retired() {
        // A Python handle reused on one thread while another closes it: the release can come first.
        let tmp = TempDir::new("store-order");
        let store = Store::open(&options(tmp.path())).unwrap();
        store.release_handle(); // Closed: retired, no handles.
        store.release_handle(); // The racing close's release lands first: -1.
        add_handle(&store); // Then the reuse's add: back to 0.
        assert_eq!(store.handles.load(Ordering::Acquire), 0);
        assert!(store.is_retired(), "revived with no handles");
        assert!(store.pin.lock().unwrap().is_none(), "the file stays pinned with no handles");
        add_handle(&store);
        assert!(!store.is_retired());
        store.release_handle();
    }

    #[test]
    fn a_lease_returned_after_the_last_close_is_closed_not_pooled() {
        let tmp = TempDir::new("store-late-lease");
        let store = Store::open(&options(tmp.path())).unwrap();
        let gate = GATE.enter_shared_blocking();
        let lease = store.lease(&gate, T).unwrap();
        store.release_handle();
        drop(lease);
        assert_eq!(store.pool.idle(&gate), 0, "a connection pooled on a store with no handles (D-018)");
        assert_eq!(store.pool.checked_out(&gate), 0, "the closed connection is still counted as out");
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
    fn a_held_transaction_finishes_on_another_thread_while_a_fork_waits_for_it() {
        let _forks = crate::testutil::one_global_fork_at_a_time();
        // The fork waits for the transaction's level; the thread ending it must not wait for the
        // fork. It enters on the transaction's own level instead (review 07, architect).
        let tmp = TempDir::new("store-hold-fork-finish");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "k", b"v");
        let forked = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let fork_thread = {
            let forked = Arc::clone(&forked);
            std::thread::spawn(move || {
                gate::begin_fork(); // Waits until the transaction ends.
                forked.store(true, Ordering::Release);
                gate::end_fork_parent();
            })
        };
        std::thread::sleep(Duration::from_millis(100)); // The fork is now pending.
        assert!(!forked.load(Ordering::Acquire), "the fork must wait for the held transaction");
        let (done, finished) = std::sync::mpsc::channel();
        std::thread::spawn(move || done.send(held.finish(true)).unwrap());
        // On failure the threads stay stuck with the gate claimed, and later tests hang behind it.
        let result =
            finished.recv_timeout(Duration::from_secs(5)).expect("finish waited for the fork that waits for it");
        result.unwrap();
        fork_thread.join().unwrap();
        assert!(forked.load(Ordering::Acquire));
        assert_eq!(count(&store), 1, "committed");
    }

    #[test]
    fn a_held_transaction_finished_elsewhere_waits_out_its_owners_fork() {
        let _forks = crate::testutil::one_global_fork_at_a_time();
        // That fork counts the transaction's level as the owner's own, so it doesn't wait for it,
        // and the finish must not run its COMMIT while the fork is exclusive.
        let tmp = TempDir::new("store-hold-owner-forks");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "k", b"v");
        gate::begin_fork(); // Returns at once: the only level is this thread's own.
        let (done, finished) = std::sync::mpsc::channel();
        std::thread::spawn(move || done.send(held.finish(true)).unwrap());
        assert!(
            finished.recv_timeout(Duration::from_millis(200)).is_err(),
            "the finish ran while its owner's fork was exclusive"
        );
        gate::end_fork_parent();
        finished.recv_timeout(Duration::from_secs(5)).expect("the finish never ran after the fork").unwrap();
        assert_eq!(count(&store), 1);
    }

    /// Ends `held` on another thread while its owner (this thread) is inside an operation on it,
    /// and returns how long ending took and what it returned. `level`: the ender holds a gate level
    /// of its own (it's inside an operation when the GC finalizes the transaction).
    fn end_mid_operation(
        store: &Arc<Store>,
        held: HeldTransaction,
        commit: bool,
        level: bool,
    ) -> (Duration, Result<()>) {
        let (inside, go) = std::sync::mpsc::channel();
        let (done, ended) = std::sync::mpsc::channel();
        let end_thread = std::thread::spawn(move || {
            // Before the operation starts, and on the transaction's own level: tests run in one
            // process, and behind another test's pending fork a plain entry has no level to give.
            let _level = level.then(|| held.share.as_ref().unwrap().enter());
            go.recv().unwrap();
            let started = Instant::now();
            let result = held.finish(commit);
            done.send((started.elapsed(), result)).unwrap();
        });
        let outcome = store
            .read(T, |_| {
                inside.send(()).unwrap();
                // The ender must not wait for this operation: it reports while the operation runs.
                Ok(ended.recv_timeout(Duration::from_secs(5)).expect("ending waited for the operation"))
            })
            .unwrap();
        end_thread.join().unwrap();
        outcome
    }

    #[test]
    fn a_rollback_from_another_thread_mid_operation_is_left_to_the_operation() {
        let tmp = TempDir::new("store-hold-abandon-elsewhere");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "k", b"v");
        let (took, result) = end_mid_operation(&store, held, false, false);
        result.unwrap();
        assert!(took < Duration::from_secs(1), "the rollback waited {took:?}");
        assert_eq!(count(&store), 0, "rolled back when the operation returned");
        put(&store, "after", b"v"); // The write lock was released.
        assert_eq!(count(&store), 1);
    }

    #[test]
    fn a_commit_from_another_thread_holding_a_level_mid_operation_is_refused() {
        // Waiting there could deadlock with a fork from inside the operation.
        let tmp = TempDir::new("store-hold-commit-elsewhere");
        let store = Store::open(&options(tmp.path())).unwrap();
        let held = store.begin_hold(T).unwrap();
        put(&store, "k", b"v");
        let (took, result) = end_mid_operation(&store, held, true, true);
        assert!(matches!(result, Err(Error::EndInsideOperation)), "{result:?}");
        assert!(took < Duration::from_secs(1), "the commit waited {took:?}");
        assert_eq!(count(&store), 0, "rolled back when the operation returned");
        put(&store, "after", b"v");
        assert_eq!(count(&store), 1);
    }

    #[test]
    fn a_held_transaction_sqlite_ended_during_a_read_ends_cleanly() {
        // SQLite can end a transaction on its own during a read (an I/O error), which no write
        // marks as doomed. Ending it must still report that, not a raw "no transaction" error.
        let tmp = TempDir::new("store-hold-ended-in-read");
        let store = Store::open(&options(tmp.path())).unwrap();
        for commit in [false, true] {
            let held = store.begin_hold(T).unwrap();
            put(&store, "k", b"v");
            store.read(T, |conn| conn.execute_batch("ROLLBACK").map_err(|e| Error::from_sqlite(e, T))).unwrap();
            let result = held.finish(commit);
            if commit {
                assert!(matches!(result, Err(Error::TransactionAborted)), "{result:?}");
            } else {
                result.unwrap();
            }
            assert_eq!(count(&store), 0);
        }
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

    /// Runs `body` on its own thread and fails the test if it hasn't finished within 5 s, so a
    /// deadlock fails instead of hanging the suite.
    fn within_5s(body: impl FnOnce() + Send + 'static) {
        let (done, finished) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            body();
            let _ = done.send(());
        });
        assert!(finished.recv_timeout(Duration::from_secs(5)).is_ok(), "deadlocked (or panicked)");
    }

    #[test]
    fn an_operation_nested_inside_a_held_transactions_own_operation_runs_inside_it() {
        // As a pickle hook that calls the cache does, from inside a get on the transaction's thread.
        within_5s(|| {
            let tmp = TempDir::new("store-hold-nested");
            let store = Store::open(&options(tmp.path())).unwrap();
            let held = store.begin_hold(T).unwrap();
            let seen = store
                .read(T, |_| {
                    put(&store, "k", b"v");
                    Ok(count(&store))
                })
                .unwrap();
            assert_eq!(seen, 1, "the nested write and read ran inside the transaction");
            held.finish(true).unwrap();
            assert_eq!(count(&store), 1);
        });
    }

    #[test]
    fn ending_a_held_transaction_inside_its_own_operation_rolls_it_back_after() {
        within_5s(|| {
            let tmp = TempDir::new("store-hold-end-inside");
            let store = Store::open(&options(tmp.path())).unwrap();
            put(&store, "before", b"v");
            let held = Mutex::new(Some(store.begin_hold(T).unwrap()));
            put(&store, "in-hold", b"v");
            let ended = store
                .read(T, |_| {
                    let held = held.lock().unwrap().take().expect("read runs once on the held connection");
                    Ok(held.finish(true))
                })
                .unwrap();
            assert!(matches!(ended, Err(Error::EndInsideOperation)));
            assert_eq!(GATE.own_depth(), 0, "the transaction's gate level was released");
            assert_eq!(count(&store), 1, "rolled back once the read returned");
            put(&store, "after", b"v"); // Not stuck behind a leaked write lock.
            store.begin_hold(T).unwrap().finish(true).unwrap();
            assert_eq!(count(&store), 2);
        });
    }

    #[test]
    fn a_hold_inside_the_gate_never_waits_for_the_write_lock() {
        let tmp = TempDir::new("store-hold-depth");
        let store = Store::open(&options(tmp.path())).unwrap();
        let token = store.write_lock.try_lock().unwrap(); // As if another thread were writing.
        let start = Instant::now();
        std::thread::scope(|s| {
            s.spawn(|| {
                let _gate = GATE.enter_shared_blocking(); // Depth > 0, as inside another operation.
                assert!(matches!(store.begin_hold(Duration::from_secs(5)), Err(Error::WriteLockBusyInTransaction)));
            });
        });
        assert!(start.elapsed() < Duration::from_secs(1), "begin_hold waited for the write lock at depth > 0");
        drop(token);
        let held = {
            let _gate = GATE.enter_shared_blocking();
            store.begin_hold(T).expect("a free write lock is taken at depth > 0")
        };
        held.finish(true).unwrap();
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
        let gate = GATE.enter_shared_blocking();
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
        // still go through promptly, never wait: it takes the free lock anyway (try_lock_nested).
        let tmp = TempDir::new("store-depth-starving");
        let store = Store::open(&options(tmp.path())).unwrap();
        crate::genlock::tests_support::mark_starving(&store.write_lock);
        let gate = GATE.enter_shared_blocking();
        let start = Instant::now();
        put(&store, "nested", b"v");
        assert!(start.elapsed() < Duration::from_secs(1), "a nested write waited behind a starving writer");
        drop(gate);
        assert_eq!(count(&store), 1);
    }

    #[test]
    fn nested_and_nowait_writes_never_check_the_wal() {
        let tmp = TempDir::new("store-wal-nested");
        let store = Store::open(&OpenOptions {
            wal_size_limit: crate::wal::MIN_SIZE_LIMIT,
            auto_checkpoint: true,
            ..options(tmp.path())
        })
        .unwrap();
        let writes = crate::wal::CHECK_EVERY_COMMITS * 2;
        {
            let _gate = GATE.enter_shared_blocking(); // A nested write: depth > 0.
            for i in 0..writes {
                put(&store, &format!("n{i}"), b"v");
            }
        }
        for i in 0..writes {
            let row = NewRow {
                key: "nowait",
                mode: 2,
                value: Stored::Blob(b"v"),
                size: 1,
                store_time: f64::from(u32::try_from(i).unwrap()),
                expire_time: None,
            };
            // It skips while another test's fork is pending.
            let wrote = (0..5_000).any(|_| {
                let wrote = store.write_nowait(|w| w.upsert(&row)).is_some();
                if !wrote {
                    std::thread::sleep(Duration::from_millis(1));
                }
                wrote
            });
            assert!(wrote, "write_nowait kept skipping");
        }
        assert_eq!(store.wal_stats().checks, 0, "a nested or write_nowait write checked the WAL");
        put(&store, "top", b"v");
        assert_eq!(store.wal_stats().checks, 1, "a top-level write checks");
    }

    #[test]
    fn a_larger_wal_limit_from_a_second_handle_is_applied_to_pooled_connections() {
        let tmp = TempDir::new("store-wal-join");
        let pragmas = |store: &Store| {
            store
                .read(T, |c| {
                    let j: i64 = c
                        .query_row("PRAGMA journal_size_limit", [], |r| r.get(0))
                        .map_err(|e| Error::from_sqlite(e, T))?;
                    let a: i64 = c
                        .query_row("PRAGMA wal_autocheckpoint", [], |r| r.get(0))
                        .map_err(|e| Error::from_sqlite(e, T))?;
                    Ok((j, a))
                })
                .unwrap()
        };
        let small = crate::wal::MIN_SIZE_LIMIT;
        let a = Store::open(&OpenOptions { wal_size_limit: small, ..options(tmp.path()) }).unwrap();
        let expect = |limit: u64| {
            (
                i64::try_from(crate::wal::journal_size_limit(limit)).unwrap(),
                i64::try_from(crate::wal::autocheckpoint_pages(limit)).unwrap(),
            )
        };
        assert_eq!(pragmas(&a), expect(small));
        let large = 16 * small;
        let _b = Store::open(&OpenOptions { wal_size_limit: large, ..options(tmp.path()) }).unwrap();
        assert_eq!(pragmas(&a), expect(large), "an idle pooled connection is brought up to the larger limit");
        assert!(matches!(
            Store::open(&OpenOptions { wal_size_limit: small - 1, ..options(&tmp.path().join("small")) }),
            Err(Error::InvalidSetting(_))
        ));
    }

    #[test]
    fn by_default_writes_never_check_the_wal_and_checkpoint_resets_it() {
        let tmp = TempDir::new("store-wal-manual");
        let store =
            Store::open(&OpenOptions { wal_size_limit: crate::wal::MIN_SIZE_LIMIT, ..options(tmp.path()) }).unwrap();
        for i in 0..crate::wal::CHECK_EVERY_COMMITS * 2 {
            put(&store, &format!("k{i}"), &[7u8; 4096]);
        }
        assert_eq!(store.wal_stats().checks, 0, "manual mode: no WAL checks on the write path");
        assert!(store.wal_bytes() > 0);
        assert!(store.checkpoint(T).unwrap(), "an idle store checkpoints at once");
        assert_eq!(store.wal_bytes(), 0, "TRUNCATE leaves an empty WAL");
        assert_eq!(store.wal_stats().successes, 1);
    }

    #[test]
    fn a_busy_truncate_is_tried_again_in_fresh_windows_until_the_deadline_stops_it() {
        // Another test's fork would end the round early: the gate is process-wide.
        let _forks = crate::testutil::one_global_fork_at_a_time();
        // A peer's open snapshot outlasts every window, so each try waits out its whole window.
        let tmp = TempDir::new("store-wal-tries");
        let store = Store::open(&options(tmp.path())).unwrap();
        put(&store, "k0", b"v");
        let peer = Connection::open(open::db_path(tmp.path())).unwrap();
        peer.execute_batch("BEGIN; SELECT count(*) FROM Cache;").unwrap();
        put(&store, "k1", b"v");

        let start = Instant::now();
        let outcome = store.checkpoint_once(start + T);
        let elapsed = start.elapsed();
        assert!(matches!(outcome, crate::wal::Outcome::Busy), "{outcome:?}");
        let all_windows = lockwait::ATTACHED_WINDOW * crate::wal::TRUNCATE_TRIES;
        assert!(elapsed >= all_windows, "{elapsed:?}: it should have waited out {all_windows:?}");

        // Past its deadline a round still gets its one window, and only that one. No upper bound on
        // the time here: a descheduled test would fail it. The count is in wal.rs's tests.
        let outcome = store.checkpoint_once(Instant::now());
        assert!(matches!(outcome, crate::wal::Outcome::Busy), "{outcome:?}");

        peer.execute_batch("ROLLBACK").unwrap();
        assert!(matches!(store.checkpoint_once(Instant::now() + T), crate::wal::Outcome::Done));
        assert_eq!(store.wal_bytes(), 0);
    }

    #[test]
    fn a_read_on_a_new_connection_waits_out_a_lock_instead_of_failing() {
        // A new connection reads the schema when the store applies its settings, so it can meet a
        // lock that an established connection would read past (here a peer in exclusive locking
        // mode; in practice another process's last-close checkpoint). The read must wait for it.
        let tmp = TempDir::new("store-fresh-read");
        let store = Store::open(&options(tmp.path())).unwrap();
        put(&store, "k", b"v");
        store.pool.clear(&GATE.enter_shared_blocking());
        let peer = Connection::open(open::db_path(tmp.path())).unwrap();
        peer.execute_batch("PRAGMA locking_mode = EXCLUSIVE; SELECT count(*) FROM Cache;").unwrap();
        let release = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(200));
            drop(peer);
        });
        let start = Instant::now();
        assert_eq!(count(&store), 1);
        assert!(start.elapsed() >= Duration::from_millis(150), "the peer held its lock for 200 ms");
        release.join().unwrap();
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
