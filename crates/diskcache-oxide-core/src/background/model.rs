//! Model-checked interleavings of a background maintenance turn against the last close of its store
//! (plan 28, decision 13; `cfg(shuttle)` only).
//!
//! The store's registry and pool are `std` mutexes, which shuttle can't schedule, so this rebuilds the
//! protocol on shuttle's primitives around the real [`Slots`] counter the store uses for turns in
//! flight:
//!
//! - `begin_turn` is `Store::begin_background_turn`: under the registry, refused on a retired store,
//!   else a permit from the counter.
//! - `chunk` is one of `BackgroundTurn`'s calls: refused once the store has retired, else a lease (an
//!   idle connection, or a new one) and its return, which is `Store::give_back`: closed if the store
//!   has retired, else checked in and, if the store retired meanwhile, the pool cleared again.
//! - `release` is the last `close()`: retirement decided under the registry, the pool cleared, then
//!   `wait_for_background_turns` until no turn is in flight (unbounded here: the real wait's deadline
//!   is a liveness bound, not part of the safety argument).
//! - `revive` is a reopen claiming the retired store under the registry.
//!
//! The property: when a close returns, and the store wasn't revived meanwhile, no connection is open,
//! idle or leased (D-018).

use std::sync::Arc;
use std::sync::atomic::Ordering;

use shuttle::sync::Mutex;
use shuttle::sync::atomic::{AtomicBool, AtomicUsize};
use shuttle::thread;

use crate::genlock::{SlotPermit, Slots};
use crate::model::{Reach, check};

#[derive(Default)]
struct Store {
    registry: Mutex<()>,
    retired: AtomicBool,
    turns: Slots,
    /// Connections in the pool.
    idle: AtomicUsize,
    /// Connections a turn has out.
    leased: AtomicUsize,
}

impl Store {
    fn open(&self) -> usize {
        self.idle.load(Ordering::SeqCst) + self.leased.load(Ordering::SeqCst)
    }

    fn begin_turn(&self) -> Option<SlotPermit<'_>> {
        let _registry = self.registry.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.retired.load(Ordering::SeqCst) {
            return None;
        }
        self.turns.try_acquire(u64::MAX)
    }

    /// One of the turn's reads or writes. False if it was refused.
    fn chunk(&self) -> bool {
        if self.retired.load(Ordering::SeqCst) {
            return false;
        }
        // The lease: an idle connection if there is one, else a new one.
        if self.idle.try_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1)).is_err() {
            // A new connection.
        }
        self.leased.fetch_add(1, Ordering::SeqCst);
        thread::yield_now();
        // `Store::give_back` (store.rs): keep this in step with it. Removing its second retirement
        // check below makes both models fail.
        self.leased.fetch_sub(1, Ordering::SeqCst);
        if !self.retired.load(Ordering::SeqCst) {
            self.idle.fetch_add(1, Ordering::SeqCst);
            if self.retired.load(Ordering::SeqCst) {
                self.idle.store(0, Ordering::SeqCst);
            }
        }
        true
    }

    /// The last close. Returns whether it waited for a turn in flight.
    fn release(&self) -> bool {
        {
            let _registry = self.registry.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
            self.retired.store(true, Ordering::SeqCst);
        }
        self.idle.store(0, Ordering::SeqCst);
        let waited = self.turns.in_use() != 0;
        while self.turns.in_use() != 0 && self.retired.load(Ordering::SeqCst) {
            thread::yield_now();
        }
        waited
    }

    fn revive(&self) {
        let _registry = self.registry.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        self.retired.store(false, Ordering::SeqCst);
    }
}

/// A turn of two chunks.
fn turn(store: &Store) {
    if let Some(_permit) = store.begin_turn() {
        for _ in 0..2 {
            if !store.chunk() {
                break;
            }
        }
    }
}

/// Model 1: a turn against the last close. When the close returns, nothing is open.
#[test]
fn model_the_last_close_leaves_nothing_a_turn_opened() {
    static WAITED: Reach = Reach::new();
    check(|| {
        let store = Arc::new(Store::default());
        // A connection already pooled, as a live store has.
        store.idle.store(1, Ordering::SeqCst);
        let worker = {
            let store = Arc::clone(&store);
            thread::spawn(move || turn(&store))
        };
        if store.release() {
            WAITED.hit();
        }
        assert_eq!(store.open(), 0, "a connection was open after the last close returned");
        worker.join().unwrap();
        assert_eq!(store.open(), 0, "a connection was left open after the turn ended");
    });
    WAITED.assert_reached("a close that waited for a turn in flight");
}

/// Model 2: a turn against a close, a reopen and a second close. The second close leaves nothing
/// open, whatever the turn did across the reopen.
#[test]
fn model_a_reopen_between_closes_leaves_nothing_after_the_second() {
    static ACROSS: Reach = Reach::new();
    check(|| {
        let store = Arc::new(Store::default());
        store.idle.store(1, Ordering::SeqCst);
        let workers: Vec<_> = (0..2)
            .map(|_| {
                let store = Arc::clone(&store);
                thread::spawn(move || turn(&store))
            })
            .collect();
        store.release();
        store.revive();
        if store.turns.in_use() != 0 {
            // A turn that began before the first close is still running across the reopen.
            ACROSS.hit();
        }
        store.release();
        assert_eq!(store.open(), 0, "a connection was open after the second close returned");
        for worker in workers {
            worker.join().unwrap();
        }
        assert_eq!(store.open(), 0);
    });
    ACROSS.assert_reached("a turn still running across the reopen");
}
