//! The D-020 poison registry: which databases this process can never write, or never use at all,
//! keyed by inode.
//!
//! A thread that holds a write transaction across calls (a *hold*) records the database's
//! [`FileId`] here for as long as the transaction is open. When the process forks, the child copies
//! every live hold into its poisoned set (`poison_live_holds`, run by the after-fork hook in the
//! child). SQLite's per-process lock table, inherited by the child, still records those write
//! locks, and releasing them in the child would corrupt the parent's transactions, so the child can
//! never write to those databases. Write paths call [`check_writable`] and fail at once with
//! [`Error::Unwritable`] instead of waiting out their timeout.
//!
//! The check is by inode, not by store, because SQLite's lock table is per inode: a fresh store on
//! the same file in the child is poisoned too. A grandchild inherits the poisoned set and adds the
//! holds live when it was forked.
//!
//! # Unusable
//!
//! A second, stronger level (plan 32): a database this process inherited an *open connection* to
//! can't be used here at all, reads included. SQLite's lock record for the file, inherited with the
//! connection, says the process holds locks the kernel never gave the child, so a new connection
//! takes none: once the parent closes, its reads go stale and its writes are lost, with no error.
//! The child's after-fork hook marks every such file (from the pool scan, and every live hold),
//! and [`check_usable`] fails with [`Error::Unusable`] wherever a connection
//! would be opened or leased. Every unusable file is unwritable too, and [`check_writable`] reports
//! the stronger error.
//!
//! # Across `fork()`
//!
//! Like the gate, the registry is atomics only, so a child never inherits it locked:
//!
//! - holds are added under the gate (the [`SharedGuard`] argument is the proof), by a thread whose
//!   level the fork waits for, so a child never sees a half-written entry;
//! - a hold is removed with one atomic store, from any thread;
//! - the poisoned set is only added to in the child's after-fork hook, while the forking thread is
//!   the only one, and is only ever added to (tests aside: `poison_for_test`'s entry goes when its
//!   guard drops).

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
/// that share is dropped, so a fork never sees the transaction open and not registered. A parked end
/// drops the share first and keeps the hold, which only widens the window a child is poisoned in.
pub fn begin_hold(gate: &SharedGuard<'_>, id: FileId) -> HoldGuard<'static> {
    REGISTRY.begin_hold(gate, id)
}

/// Fails if this process can never write `id`: with [`Error::Unusable`] if it can't use the file at
/// all (see [`check_usable`]), else with [`Error::Unwritable`] if it was forked while some thread
/// held a write transaction on it (or descends from one that was). One relaxed load in a process
/// that was never poisoned.
#[inline]
pub fn check_writable(id: FileId) -> Result<()> {
    REGISTRY.check_writable(id)
}

/// Fails with [`Error::Unusable`] if this process was forked while a connection to `id` was open
/// (or descends from one that was): see the module docs. One relaxed load in a process that wasn't.
///
/// Only a check made under a gate level is final: in a child, the first gate entry is what runs
/// the after-fork scan when another library's `after_in_child` hook gets there before ours.
#[inline]
pub fn check_usable(id: FileId) -> Result<()> {
    REGISTRY.check_usable(id)
}

/// The after-fork hook in the child: `id` had a connection open at the fork. Call it only while
/// the calling thread is the only one in the process.
pub(crate) fn mark_unusable(id: FileId) {
    REGISTRY.mark_unusable(id);
}

/// The after-fork hook in the child, when it can't tell which files had a connection open (the pool
/// registry was inherited locked): no database is usable in this process.
pub(crate) fn mark_all_unusable() {
    REGISTRY.mark_all_unusable();
}

/// Whether a file was marked unusable since the last call, for the hook's one log line.
pub fn take_newly_unusable() -> bool {
    REGISTRY.newly_unusable.swap(false, Ordering::AcqRel)
}

/// Whether no database is usable in this process: the after-fork scan couldn't tell which files
/// had a connection open.
#[must_use]
pub fn all_unusable() -> bool {
    REGISTRY.all_unusable.load(Ordering::Acquire)
}

/// The after-fork hook in the child: poisons every database a hold was live on at the fork. Call
/// it only while the calling thread is the only one in the process.
pub(crate) fn poison_live_holds() {
    REGISTRY.poison_live_holds();
}

/// Tests only: makes `id` unwritable, as in a child forked inside a hold on it, until the returned
/// guard is dropped (a later test's database may reuse the inode).
#[cfg(test)]
pub(crate) fn poison_for_test(id: FileId) -> HoldGuard<'static> {
    let slot = REGISTRY.poisoned.insert(id);
    REGISTRY.any_poisoned.store(true, Ordering::Release);
    HoldGuard { slot }
}

/// Tests only: makes `id` unusable, as in a child that inherited an open connection to it, until the
/// returned guard is dropped.
#[cfg(test)]
pub(crate) fn unusable_for_test(id: FileId) -> HoldGuard<'static> {
    let slot = REGISTRY.unusable.insert(id);
    REGISTRY.any_poisoned.store(true, Ordering::Release);
    HoldGuard { slot }
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
    /// The files an open connection was inherited to. Every live hold's file is in both sets.
    unusable: IdList,
    /// Set once `poisoned` or `unusable` has an entry, or `all_unusable` is set. Only ever set, in a
    /// child before it has other threads.
    any_poisoned: AtomicBool,
    /// Every file is unusable. Only ever set, like `any_poisoned`, and before it.
    all_unusable: AtomicBool,
    /// Something was marked unusable and the hook hasn't logged it yet.
    newly_unusable: AtomicBool,
}

impl Registry {
    pub(crate) const fn new() -> Self {
        Self {
            live: IdList::new(),
            poisoned: IdList::new(),
            unusable: IdList::new(),
            any_poisoned: AtomicBool::new(false),
            all_unusable: AtomicBool::new(false),
            newly_unusable: AtomicBool::new(false),
        }
    }

    pub(crate) fn begin_hold(&self, _gate: &SharedGuard<'_>, id: FileId) -> HoldGuard<'_> {
        HoldGuard { slot: self.live.insert(id) }
    }

    #[inline]
    pub(crate) fn check_writable(&self, id: FileId) -> Result<()> {
        // Relaxed: the flag is only set in a child before it starts other threads, and spawning a
        // thread synchronizes with it.
        if self.any_poisoned.load(Ordering::Relaxed) {
            self.check_usable_cold(id)?;
            if self.poisoned.contains(id) {
                return Err(Error::Unwritable);
            }
        }
        Ok(())
    }

    #[inline]
    pub(crate) fn check_usable(&self, id: FileId) -> Result<()> {
        // Relaxed, as in `check_writable`.
        if self.any_poisoned.load(Ordering::Relaxed) {
            return self.check_usable_cold(id);
        }
        Ok(())
    }

    #[cold]
    fn check_usable_cold(&self, id: FileId) -> Result<()> {
        if self.all_unusable.load(Ordering::Relaxed) || self.unusable.contains(id) {
            return Err(Error::Unusable);
        }
        Ok(())
    }

    pub(crate) fn mark_unusable(&self, id: FileId) {
        if !self.unusable.contains(id) {
            let _ = self.unusable.insert(id);
            self.newly_unusable.store(true, Ordering::Release);
        }
        self.any_poisoned.store(true, Ordering::Release);
    }

    pub(crate) fn mark_all_unusable(&self) {
        if !self.all_unusable.swap(true, Ordering::AcqRel) {
            self.newly_unusable.store(true, Ordering::Release);
        }
        self.any_poisoned.store(true, Ordering::Release);
    }

    pub(crate) fn poison_live_holds(&self) {
        let mut added = false;
        self.live.for_each(|id| {
            if !self.poisoned.contains(id) {
                let _ = self.poisoned.insert(id);
            }
            // A hold keeps its connection open, so the file is unusable too. Marked here as well as
            // by the pool scan, so it never depends on a mutex the child may have inherited locked.
            self.mark_unusable(id);
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
        // A scoped thread rather than a leaked `'static` registry, so Miri's leak check stays on.
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let hold = registry.begin_hold(&gate, A);
        std::thread::scope(|s| {
            s.spawn(move || drop(hold));
        });
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
        assert!(
            matches!(registry.check_writable(A), Err(Error::Unusable)),
            "a hold's file is unusable, the stronger error"
        );
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
        assert!(registry.check_writable(B).is_ok(), "other files stay writable");
        assert!(registry.check_usable(B).is_ok());
        assert!(registry.check_writable(C).is_ok(), "same inode, other device: another file");

        // Ending the inherited hold in the child doesn't make the file writable again.
        drop(hold);
        assert!(matches!(registry.check_writable(A), Err(Error::Unusable)));
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
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
        assert!(matches!(registry.check_writable(A), Err(Error::Unusable)));
        assert!(matches!(registry.check_writable(B), Err(Error::Unusable)));
        // Repeated forks don't duplicate entries.
        registry.poison_live_holds();
        let mut poisoned = Vec::new();
        registry.poisoned.for_each(|id| poisoned.push(id));
        poisoned.sort_unstable();
        assert_eq!(poisoned, vec![A, B]);
        let mut unusable = Vec::new();
        registry.unusable.for_each(|id| unusable.push(id));
        unusable.sort_unstable();
        assert_eq!(unusable, vec![A, B], "nor in the unusable set");
        drop(b);
    }

    #[test]
    fn a_file_marked_unusable_fails_both_checks_and_others_pass() {
        let registry = Registry::new();
        assert!(registry.check_usable(A).is_ok());
        registry.mark_unusable(A);
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
        assert!(matches!(registry.check_writable(A), Err(Error::Unusable)), "unusable is unwritable too");
        assert!(registry.check_usable(B).is_ok());
        assert!(registry.check_writable(B).is_ok());
        assert!(registry.check_usable(C).is_ok(), "same inode, other device: another file");
        assert!(registry.newly_unusable.swap(false, Ordering::AcqRel), "flagged once for the log line");
        registry.mark_unusable(A);
        assert!(!registry.newly_unusable.load(Ordering::Acquire), "marking it again is not news");
    }

    #[test]
    fn an_unwritable_file_that_is_not_unusable_still_reports_unwritable() {
        let registry = Registry::new();
        let _ = registry.poisoned.insert(A);
        registry.any_poisoned.store(true, Ordering::Release);
        assert!(matches!(registry.check_writable(A), Err(Error::Unwritable)));
        assert!(registry.check_usable(A).is_ok());
    }

    #[test]
    fn all_unusable_fails_every_file() {
        let registry = Registry::new();
        registry.mark_all_unusable();
        for id in [A, B, C] {
            assert!(matches!(registry.check_usable(id), Err(Error::Unusable)));
            assert!(matches!(registry.check_writable(id), Err(Error::Unusable)));
        }
    }
}
