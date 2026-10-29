//! The D-020 poison registry: which databases this process can never write, keyed by inode.
//!
//! A thread that holds a write transaction across calls (a *hold*) records the database's
//! [`FileId`] here for as long as the transaction is open. When the process forks, the child copies
//! every live hold into its poisoned set ([`poison_live_holds`], run by the after-fork hook in the
//! child). SQLite's per-process lock table, inherited by the child, still records those write
//! locks, and releasing them in the child would corrupt the parent's transactions, so the child can
//! never write to those databases. Write paths call [`check_writable`] and fail at once with
//! [`Error::Unwritable`] instead of waiting out their timeout.
//!
//! The check is by inode, not by store, because SQLite's lock table is per inode: a fresh store on
//! the same file in the child is poisoned too. A grandchild inherits the poisoned set and adds the
//! holds live when it was forked.
//!
//! # Across `fork()`
//!
//! Like the gate, the registry is atomics only, so a child never inherits it locked:
//!
//! - holds are added under the gate (the [`SharedGuard`] argument is the proof), by a thread whose
//!   level the fork waits for, so a child never sees a half-written entry;
//! - a hold is removed with one atomic store, from any thread;
//! - the poisoned set is only added to in the child's after-fork hook, while the forking thread is
//!   the only one, and is only ever added to.

use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering};

use crate::gate::SharedGuard;
use crate::{Error, Result};

/// A database file's identity: `(st_dev, st_ino)`.
pub type FileId = (u64, u64);

/// The process-wide registry every store uses.
static REGISTRY: Registry = Registry::new();

/// Records that the calling thread holds a write transaction on `id` across calls, until the
/// returned guard is dropped.
///
/// Call it with the gate held (`gate` is the proof), before the level is turned into a
/// [`HeldShare`](crate::gate::HeldShare). Drop the guard after the transaction ends, but before
/// that share is dropped, so a fork never sees the transaction open and not registered.
pub fn begin_hold(gate: &SharedGuard<'_>, id: FileId) -> HoldGuard<'static> {
    REGISTRY.begin_hold(gate, id)
}

/// Fails with [`Error::Unwritable`] if this process was forked while some thread held a write
/// transaction on `id` (or descends from one that was). One relaxed load in a process that wasn't.
#[inline]
pub fn check_writable(id: FileId) -> Result<()> {
    REGISTRY.check_writable(id)
}

/// The after-fork hook in the child: poisons every database a hold was live on at the fork. Call
/// it only while the calling thread is the only one in the process.
pub(crate) fn poison_live_holds() {
    REGISTRY.poison_live_holds();
}

/// A live hold, see [`begin_hold`]. It may be dropped on any thread.
#[derive(Debug)]
#[must_use = "the hold is unregistered as soon as the guard is dropped"]
pub struct HoldGuard<'r> {
    slot: &'r Slot,
}

impl Drop for HoldGuard<'_> {
    fn drop(&mut self) {
        self.slot.state.store(FREE, Ordering::Release);
    }
}

/// The registry, as a type so tests can run on their own instance.
#[derive(Debug)]
pub(crate) struct Registry {
    live: IdList,
    poisoned: IdList,
    /// Set once `poisoned` has an entry. Only ever set, in a child before it has other threads.
    any_poisoned: AtomicBool,
}

impl Registry {
    pub(crate) const fn new() -> Self {
        Self { live: IdList::new(), poisoned: IdList::new(), any_poisoned: AtomicBool::new(false) }
    }

    pub(crate) fn begin_hold(&self, _gate: &SharedGuard<'_>, id: FileId) -> HoldGuard<'_> {
        HoldGuard { slot: self.live.insert(id) }
    }

    #[inline]
    pub(crate) fn check_writable(&self, id: FileId) -> Result<()> {
        // Relaxed: the flag is only set in a child before it starts other threads, and spawning a
        // thread synchronizes with it.
        if self.any_poisoned.load(Ordering::Relaxed) && self.poisoned.contains(id) {
            return Err(Error::Unwritable);
        }
        Ok(())
    }

    pub(crate) fn poison_live_holds(&self) {
        let mut added = false;
        self.live.for_each(|id| {
            if !self.poisoned.contains(id) {
                let _ = self.poisoned.insert(id);
            }
            added = true;
        });
        if added {
            self.any_poisoned.store(true, Ordering::Release);
        }
    }

    #[cfg(test)]
    fn live_ids(&self) -> Vec<FileId> {
        let mut ids = Vec::new();
        self.live.for_each(|id| ids.push(id));
        ids
    }
}

const FREE: u8 = 0;
const WRITING: u8 = 1;
const LIVE: u8 = 2;

#[derive(Debug)]
struct Slot {
    state: AtomicU8,
    dev: AtomicU64,
    ino: AtomicU64,
}

impl Slot {
    const fn new() -> Self {
        Self { state: AtomicU8::new(FREE), dev: AtomicU64::new(0), ino: AtomicU64::new(0) }
    }

    fn id(&self) -> FileId {
        (self.dev.load(Ordering::Relaxed), self.ino.load(Ordering::Relaxed))
    }
}

const CHUNK: usize = 32;

/// A lock-free multiset of ids: a chain of fixed chunks that grows and is never freed. One per
/// process holds as many slots as the most holds ever live at once, plus at most a chunk.
#[derive(Debug)]
struct IdList {
    slots: [Slot; CHUNK],
    next: OnceLock<Box<IdList>>,
}

impl IdList {
    const fn new() -> Self {
        Self { slots: [const { Slot::new() }; CHUNK], next: OnceLock::new() }
    }

    fn insert(&self, id: FileId) -> &Slot {
        let mut list = self;
        loop {
            for slot in &list.slots {
                if slot.state.compare_exchange(FREE, WRITING, Ordering::Acquire, Ordering::Relaxed).is_ok() {
                    slot.dev.store(id.0, Ordering::Relaxed);
                    slot.ino.store(id.1, Ordering::Relaxed);
                    slot.state.store(LIVE, Ordering::Release);
                    return slot;
                }
            }
            list = list.next.get_or_init(|| Box::new(Self::new()));
        }
    }

    /// Calls `f` with every live id. Exact only while nothing inserts concurrently (the child's
    /// after-fork hook, or a set only added to there).
    fn for_each(&self, mut f: impl FnMut(FileId)) {
        let mut list = Some(self);
        while let Some(current) = list {
            for slot in &current.slots {
                if slot.state.load(Ordering::Acquire) == LIVE {
                    f(slot.id());
                }
            }
            list = current.next.get().map(Box::as_ref);
        }
    }

    fn contains(&self, id: FileId) -> bool {
        let mut found = false;
        self.for_each(|live| found |= live == id);
        found
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gate::{Gate, ThreadState};

    crate::sync::thread_local! { static LOCAL: ThreadState = const { ThreadState::new() }; }
    static G: Gate = Gate::new(&LOCAL);

    const A: FileId = (1, 10);
    const B: FileId = (1, 11);
    const C: FileId = (2, 10);

    #[test]
    fn holds_are_listed_until_dropped() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let a = registry.begin_hold(&gate, A);
        let a_again = registry.begin_hold(&gate, A);
        let b = registry.begin_hold(&gate, B);
        let mut live = registry.live_ids();
        live.sort_unstable();
        assert_eq!(live, vec![A, A, B]);
        drop(a);
        assert_eq!(registry.live_ids().iter().filter(|&&id| id == A).count(), 1, "a multiset: one A is left");
        drop((a_again, b));
        assert_eq!(registry.live_ids(), Vec::<FileId>::new());
    }

    #[test]
    fn a_hold_dropped_on_another_thread_is_unlisted() {
        let registry: &'static Registry = Box::leak(Box::new(Registry::new()));
        let gate = G.try_enter_shared().unwrap();
        let hold = registry.begin_hold(&gate, A);
        std::thread::spawn(move || drop(hold)).join().unwrap();
        assert_eq!(registry.live_ids(), Vec::<FileId>::new());
    }

    #[test]
    fn the_list_grows_past_a_chunk_and_reuses_slots() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let holds: Vec<_> = (0..(CHUNK as u64 * 2 + 5)).map(|i| registry.begin_hold(&gate, (9, i))).collect();
        assert_eq!(registry.live_ids().len(), CHUNK * 2 + 5);
        drop(holds);
        assert_eq!(registry.live_ids(), Vec::<FileId>::new());
        let again: Vec<_> = (0..(CHUNK as u64 * 2 + 5)).map(|i| registry.begin_hold(&gate, (9, i))).collect();
        assert_eq!(registry.live_ids().len(), CHUNK * 2 + 5);
        assert!(registry.live.next.get().and_then(|n| n.next.get()).and_then(|n| n.next.get()).is_none());
        drop(again);
    }

    #[test]
    fn check_writable_passes_until_a_fork_poisons_live_holds() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let hold = registry.begin_hold(&gate, A);
        assert!(registry.check_writable(A).is_ok(), "the parent itself is never poisoned");

        // The child's after-fork hook, without forking.
        registry.poison_live_holds();
        assert!(matches!(registry.check_writable(A), Err(Error::Unwritable)));
        assert!(registry.check_writable(B).is_ok(), "other files stay writable");
        assert!(registry.check_writable(C).is_ok(), "same inode, other device: another file");

        // Ending the inherited hold in the child doesn't make the file writable again.
        drop(hold);
        assert!(matches!(registry.check_writable(A), Err(Error::Unwritable)));
    }

    #[test]
    fn a_fork_with_no_live_holds_poisons_nothing() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        drop(registry.begin_hold(&gate, A));
        registry.poison_live_holds();
        assert!(!registry.any_poisoned.load(Ordering::Relaxed));
        assert!(registry.check_writable(A).is_ok());
    }

    #[test]
    fn a_grandchild_inherits_the_poisoned_set_and_adds_new_holds() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let a = registry.begin_hold(&gate, A);
        registry.poison_live_holds(); // child
        drop(a);
        let b = registry.begin_hold(&gate, B);
        registry.poison_live_holds(); // grandchild, forked while the child held B
        assert!(matches!(registry.check_writable(A), Err(Error::Unwritable)));
        assert!(matches!(registry.check_writable(B), Err(Error::Unwritable)));
        // Repeated forks don't duplicate entries.
        registry.poison_live_holds();
        let mut poisoned = Vec::new();
        registry.poisoned.for_each(|id| poisoned.push(id));
        poisoned.sort_unstable();
        assert_eq!(poisoned, vec![A, B]);
        drop(b);
    }
}
