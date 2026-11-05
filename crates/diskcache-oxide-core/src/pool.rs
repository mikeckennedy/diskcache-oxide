//! A small pool of SQLite connections that survives `fork()`.
//!
//! Every pooled connection is tagged with the [process generation](crate::gate::generation) it was
//! opened in. A forked child sees an older generation and drops those connections with
//! `mem::forget`, never closing them: the parent still owns the underlying file descriptors and
//! SQLite state, and closing them in the child would release POSIX locks the parent relies on. Each
//! fork therefore leaks the inherited connections' fds in the child, by design.
//!
//! The before-fork hook closes every idle connection first (`close_idle_all`), so normally the
//! child inherits none, apart from the forking thread's own leases and held transactions, and each
//! store's pin (a connection that never runs a statement, so it holds no lock and no shared memory;
//! the child forgets it too). That matters beyond the leak: SQLite keeps one lock record per database file
//! per process, and a new connection that finds the record saying the process holds SHARED takes no
//! kernel lock of its own. A child that inherited such a record would run with no locks at all, and
//! once the parent closed, another process's last close could delete the WAL under it.
//!
//! # Locking
//!
//! The pool's mutex is only ever taken by a thread that holds the fork [gate] in shared
//! mode (every method takes a [`SharedGuard`] as proof), or by the forking thread while it holds the
//! gate exclusively. So at the moment of a fork no other thread can be inside it, and the child never
//! inherits it locked. Checkout never waits for a connection: an empty pool opens a new one.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, TryLockError, Weak};

use rusqlite::Connection;

use crate::Result;
use crate::gate::{self, GATE, SharedGuard};

/// A connection checked out of a [`Pool`], tagged with the process generation it was opened in.
#[derive(Debug)]
pub struct PooledConn {
    conn: Connection,
    generation: u64,
    /// The store settings last applied to it; zeros until the store applies them.
    applied: Applied,
}

/// Per-connection settings a store applies at lease time (all connection settings; none writes).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Applied {
    /// `PRAGMA cache_size`, in pages.
    pub cache_pages: u64,
    /// The `wal_size_limit` that `journal_size_limit` and `wal_autocheckpoint` were derived from.
    pub wal_size_limit: u64,
}

impl PooledConn {
    #[must_use]
    pub fn conn(&self) -> &Connection {
        &self.conn
    }

    /// The store settings last applied.
    #[must_use]
    pub const fn applied(&self) -> Applied {
        self.applied
    }

    pub const fn set_applied(&mut self, applied: Applied) {
        self.applied = applied;
    }

    /// True if the connection was inherited across `fork()` and must not be used.
    #[must_use]
    pub fn is_stale(&self) -> bool {
        self.generation != gate::generation()
    }

    /// Drops the connection without closing it (for a connection inherited across `fork()`).
    pub fn forget(self) {
        std::mem::forget(self);
    }
}

#[derive(Debug)]
struct PoolState {
    idle: Vec<PooledConn>,
    /// The generation `idle` and `checked_out` belong to.
    generation: u64,
    checked_out: usize,
}

#[derive(Debug)]
struct PoolShared {
    state: Mutex<PoolState>,
}

impl PoolShared {
    fn lock(&self) -> MutexGuard<'_, PoolState> {
        // A panic while the lock was held leaves the Vec itself intact.
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Every live pool, so the before-fork hook can quiesce them all. Locked under the gate, like the
/// pools themselves.
static REGISTRY: Mutex<Vec<Weak<PoolShared>>> = Mutex::new(Vec::new());

fn registry() -> MutexGuard<'static, Vec<Weak<PoolShared>>> {
    REGISTRY.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Checks that no live pool is mid-update. Called by the before-fork hook after it has taken the
/// gate exclusively.
///
/// Only a thread holding a [`SharedGuard`] takes a pool's mutex, and only inside a pool method that
/// borrows the guard. Once `begin_fork` returns, the only levels left are the forking thread's, and
/// that thread is in the before-fork hook, not inside a pool method. So every mutex is free, and
/// `try_lock` failing means the invariant is broken: debug builds panic, and release builds fall
/// back to waiting for the mutex so the child still can't inherit it locked.
pub(crate) fn quiesce_all() {
    let pools: Vec<Arc<PoolShared>> = registry().iter().filter_map(Weak::upgrade).collect();
    for pool in &pools {
        let busy = matches!(pool.state.try_lock(), Err(TryLockError::WouldBlock));
        debug_assert!(!busy, "a pool's mutex was locked at fork time");
        if busy {
            drop(pool.lock());
        }
    }
    // A pool whose owner dropped meanwhile was already emptied under the gate, so dropping the last
    // reference here makes no SQLite call.
}

/// Closes every live pool's idle connections. Called by the before-fork hook after
/// [`quiesce_all`], with the gate held exclusively, so no other thread is using a pool: once every
/// connection on a database file is closed, SQLite frees its lock record for the file, and the
/// child doesn't inherit it (see the module docs). Only the forking thread's own leases and held
/// transactions stay open across the fork, plus the transactions of parked forkers and parked ends,
/// which also cross it, quiescent and poisoned.
pub(crate) fn close_idle_all() {
    let pools: Vec<Arc<PoolShared>> = registry().iter().filter_map(Weak::upgrade).collect();
    let current = gate::generation();
    for pool in &pools {
        let idle = {
            let mut state = pool.lock();
            reset_if_stale(&mut state, current);
            std::mem::take(&mut state.idle)
        };
        for pooled in &idle {
            // These may be the process's last connections holding a lock on the file. If no other
            // process has it open, closing the last one would make SQLite checkpoint and delete
            // the WAL here, with every other thread waiting behind the gate: the WAL just stays,
            // for a later close or the next autocheckpoint.
            let _ = pooled.conn.set_db_config(rusqlite::config::DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true);
        }
        drop(idle);
    }
}

#[derive(Debug)]
pub struct Pool {
    shared: Arc<PoolShared>,
    /// Idle connections kept; raised (never lowered) when a handle asks for more connections.
    max_idle: AtomicUsize,
}

impl Pool {
    #[must_use]
    pub fn new(_gate: &SharedGuard<'_>, max_idle: usize) -> Self {
        let shared = Arc::new(PoolShared {
            state: Mutex::new(PoolState { idle: Vec::new(), generation: gate::generation(), checked_out: 0 }),
        });
        let mut pools = registry();
        pools.retain(|weak| weak.strong_count() > 0);
        pools.push(Arc::downgrade(&shared));
        drop(pools);
        Self { shared, max_idle: AtomicUsize::new(max_idle) }
    }

    /// An idle connection of the current generation, or a new one from `connect`.
    pub fn checkout(
        &self,
        _gate: &SharedGuard<'_>,
        connect: impl FnOnce() -> Result<Connection>,
    ) -> Result<PooledConn> {
        let current = gate::generation();
        let popped = {
            let mut state = self.shared.lock();
            reset_if_stale(&mut state, current);
            state.checked_out += 1;
            state.idle.pop()
        };
        if let Some(pooled) = popped {
            return Ok(pooled);
        }
        match connect() {
            Ok(conn) => Ok(PooledConn { conn, generation: current, applied: Applied::default() }),
            Err(error) => {
                let mut state = self.shared.lock();
                if state.generation == current {
                    state.checked_out = state.checked_out.saturating_sub(1);
                }
                Err(error)
            }
        }
    }

    /// Returns a connection. A stale one is forgotten; one over the idle limit is closed.
    pub fn checkin(&self, _gate: &SharedGuard<'_>, pooled: PooledConn) {
        let current = gate::generation();
        let surplus = {
            let mut state = self.shared.lock();
            reset_if_stale(&mut state, current);
            if pooled.generation != current {
                pooled.forget();
                return;
            }
            state.checked_out = state.checked_out.saturating_sub(1);
            if state.idle.len() < self.max_idle.load(Ordering::Relaxed) {
                state.idle.push(pooled);
                None
            } else {
                Some(pooled)
            }
        };
        // Closed outside the pool's mutex, still under the caller's gate level.
        drop(surplus);
    }

    /// Takes back a checked-out connection without pooling it: closes it (or forgets it, if it was
    /// inherited across `fork()`), and stops counting it as checked out.
    pub fn discard(&self, _gate: &SharedGuard<'_>, pooled: PooledConn) {
        let current = gate::generation();
        {
            let mut state = self.shared.lock();
            reset_if_stale(&mut state, current);
            if pooled.generation != current {
                pooled.forget();
                return;
            }
            state.checked_out = state.checked_out.saturating_sub(1);
        }
        // Closed outside the pool's mutex, still under the caller's gate level.
        drop(pooled);
    }

    /// Keeps at least `max_idle` idle connections from now on.
    pub fn raise_max_idle(&self, max_idle: usize) {
        self.max_idle.fetch_max(max_idle, Ordering::Relaxed);
    }

    /// Closes every idle connection (forgetting any inherited across `fork()`).
    pub fn clear(&self, _gate: &SharedGuard<'_>) {
        let current = gate::generation();
        let idle = {
            let mut state = self.shared.lock();
            reset_if_stale(&mut state, current);
            std::mem::take(&mut state.idle)
        };
        drop(idle);
    }

    /// How many connections are checked out in the current generation.
    #[must_use]
    pub fn checked_out(&self, _gate: &SharedGuard<'_>) -> usize {
        let mut state = self.shared.lock();
        reset_if_stale(&mut state, gate::generation());
        state.checked_out
    }

    /// How many connections are idle in the current generation.
    #[must_use]
    pub fn idle(&self, _gate: &SharedGuard<'_>) -> usize {
        let mut state = self.shared.lock();
        reset_if_stale(&mut state, gate::generation());
        state.idle.len()
    }
}

impl Drop for Pool {
    fn drop(&mut self) {
        // Behind a pending fork, nothing is closed outside the gate. The forgotten reference keeps the
        // registry's `Weak` alive, so that fork's `close_idle_all` closes the idle connections with
        // the gate held exclusively.
        let Some(gate) = GATE.try_enter_shared() else {
            std::mem::forget(Arc::clone(&self.shared));
            return;
        };
        self.clear(&gate);
        let mut pools = registry();
        pools.retain(|weak| weak.strong_count() > 0 && !std::ptr::eq(weak.as_ptr(), Arc::as_ptr(&self.shared)));
    }
}

/// After a fork, forgets the parent's idle connections and its checked-out count.
fn reset_if_stale(state: &mut PoolState, current: u64) {
    if state.generation != current {
        for pooled in state.idle.drain(..) {
            pooled.forget();
        }
        state.checked_out = 0;
        state.generation = current;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Generations are only bumped by a real fork, which cargo test never does, so the stale paths
    // are exercised through reset_if_stale and hand-tagged connections.

    fn memory() -> Result<Connection> {
        Connection::open_in_memory().map_err(|e| crate::Error::from_sqlite(e, std::time::Duration::ZERO))
    }

    #[test]
    fn checkout_reuses_idle_connections() {
        let gate = GATE.enter_shared_blocking();
        let pool = Pool::new(&gate, 2);
        let a = pool.checkout(&gate, memory).unwrap();
        let b = pool.checkout(&gate, memory).unwrap();
        let c = pool.checkout(&gate, memory).unwrap();
        assert_eq!(pool.checked_out(&gate), 3);
        pool.checkin(&gate, a);
        pool.checkin(&gate, b);
        pool.checkin(&gate, c);
        assert_eq!((pool.checked_out(&gate), pool.idle(&gate)), (0, 2), "the surplus connection is closed");
        let _again = pool.checkout(&gate, || panic!("should reuse an idle connection")).unwrap();
    }

    #[test]
    fn a_raised_idle_limit_keeps_more_connections() {
        let gate = GATE.enter_shared_blocking();
        let pool = Pool::new(&gate, 1);
        pool.raise_max_idle(3);
        pool.raise_max_idle(2); // Never lowered.
        let conns: Vec<_> = (0..4).map(|_| pool.checkout(&gate, memory).unwrap()).collect();
        for pooled in conns {
            pool.checkin(&gate, pooled);
        }
        assert_eq!(pool.idle(&gate), 3);
    }

    #[test]
    fn a_stale_connection_coming_back_is_forgotten_not_counted() {
        let gate = GATE.enter_shared_blocking();
        let pool = Pool::new(&gate, 2);
        let pooled = pool.checkout(&gate, memory).unwrap();
        let stale = PooledConn {
            conn: pooled.conn,
            generation: gate::generation().wrapping_add(1),
            applied: Applied::default(),
        };
        assert!(stale.is_stale());
        pool.checkin(&gate, stale);
        assert_eq!(pool.idle(&gate), 0);
    }

    #[test]
    fn a_stale_pool_forgets_its_idle_connections_and_count() {
        let conn = Connection::open_in_memory().unwrap();
        let mut state = PoolState {
            idle: vec![PooledConn { conn, generation: 7, applied: Applied::default() }],
            generation: 7,
            checked_out: 3,
        };
        reset_if_stale(&mut state, 8);
        assert_eq!((state.idle.len(), state.checked_out, state.generation), (0, 0, 8));
    }

    #[test]
    fn dropped_pools_leave_the_registry() {
        let gate = GATE.enter_shared_blocking();
        let pool = Pool::new(&gate, 1);
        let weak = Arc::downgrade(&pool.shared);
        // Dropped under the level: behind another test's pending fork, it would stay in the registry.
        drop(pool);
        drop(gate);
        assert!(registry().iter().all(|w| !w.ptr_eq(&weak)));
    }
}
